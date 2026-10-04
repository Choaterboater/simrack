"""Case 20: the tokens decide what SimRack may change, and the operator can pause it.

No environment switch turns writes on. Proxmox says what its token may do
(GET /access/permissions), Mist says what its token may do (GET /self), and a
lab profile must say what is live. Pausing stops every change until resumed.
"""

from __future__ import annotations

import json
import os
import re
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from simrack.api import serve
from simrack.config import Settings
from simrack.errors import GuardrailViolation
from simrack.mist import MistClient
from simrack.proxmox import ProxmoxClient
from simrack.service import SandboxManager
from tests.fakes import PVE_ADMINISTRATOR, PVE_AUDITOR, FakeMist, FakeProxmox, TempDir, lab_settings
from tests.test_case19_real_gear import wire


class TestTheTokenDecides(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def manager(self, proxmox=None, mist=None) -> SandboxManager:
        settings = lab_settings(self.tmp, pve_token="fake", mist_token="fake-token")
        return SandboxManager(settings, proxmox=proxmox or FakeProxmox(templates=[320]), mist=mist or FakeMist())

    def test_a_token_that_may_change_the_lab_turns_changes_on(self):
        state = self.manager().state()

        self.assertTrue(state["writes_enabled"])
        self.assertEqual(state["read_only_reason"], "")
        self.assertTrue(state["mist"]["writes_enabled"])

    def test_pausing_stops_every_change_until_resumed_even_after_a_restart(self):
        proxmox = FakeProxmox(templates=[320])
        self.manager(proxmox=proxmox).set_paused(True)

        restarted = self.manager(proxmox=proxmox)
        state = restarted.state()
        self.assertFalse(state["writes_enabled"])
        self.assertTrue(state["paused"])
        self.assertIn("paused", state["read_only_reason"])
        self.assertFalse(state["mist"]["writes_enabled"])
        with self.assertRaises(GuardrailViolation) as refused:
            restarted.create_sandbox("pause-test", "single-switch", template_vmid=320)
        self.assertIn("paused", refused.exception.message)
        self.assertEqual(proxmox.calls, [])

        restarted.set_paused(False)
        self.assertTrue(restarted.state()["writes_enabled"])
        restarted.create_sandbox("pause-test", "single-switch", template_vmid=320)
        self.assertNotEqual(proxmox.calls, [])

    def test_a_proxmox_token_that_may_only_look_names_what_it_lacks_and_changes_nothing(self):
        proxmox = FakeProxmox(templates=[320], privileges=PVE_AUDITOR)
        manager = self.manager(proxmox=proxmox)

        state = manager.state()
        self.assertFalse(state["writes_enabled"])
        reason = state["read_only_reason"]
        for lacking in ("VM.Allocate", "VM.Clone", "VM.Config.Network", "Datastore.AllocateSpace", "SDN.Use"):
            self.assertIn(lacking, reason)
        for held in ("VM.Audit", "Sys.Audit", "Datastore.Audit"):
            self.assertNotIn(held, reason)
        with self.assertRaises(GuardrailViolation):
            manager.create_sandbox("look-only", "single-switch", template_vmid=320)
        self.assertEqual(proxmox.calls, [])

    def test_a_mist_token_that_may_only_look_turns_mist_changes_off_but_not_the_lab(self):
        mist = FakeMist(role="read")
        manager = self.manager(mist=mist)

        state = manager.state()
        self.assertTrue(state["writes_enabled"])
        self.assertFalse(state["mist"]["writes_enabled"])
        self.assertIn("read", state["mist"]["read_only_reason"])
        sandbox = manager.create_sandbox("mist-look", "single-switch", template_vmid=320)
        with self.assertRaises(GuardrailViolation):
            manager.mist_create_site(sandbox)
        self.assertEqual(mist.calls, [])

    def test_a_mist_token_that_may_change_another_org_or_lists_no_role_changes_nothing_here(self):
        for mist in (FakeMist(role="admin", org_id="org-other"), FakeMist(role=None)):
            with self.subTest(org=mist.org_id, role=mist.role):
                state = self.manager(mist=mist).state()
                self.assertFalse(state["mist"]["writes_enabled"])
                self.assertIn("no role on this org", state["mist"]["read_only_reason"])

    def test_the_status_view_does_not_ask_proxmox_and_mist_again_on_every_poll(self):
        proxmox, mist = FakeProxmox(templates=[320]), FakeMist()
        manager = self.manager(proxmox=proxmox, mist=mist)

        manager.state()
        asked = (proxmox.permission_reads, mist.self_reads)
        manager.state()

        self.assertGreater(min(asked), 0)
        self.assertEqual((proxmox.permission_reads, mist.self_reads), asked)


    def test_a_token_that_may_not_change_the_lab_puts_back_no_bridges_at_start(self):
        proxmox = FakeProxmox(templates=[320])
        self.manager(proxmox=proxmox).create_sandbox("rebooted", "single-switch", template_vmid=320)
        proxmox.networks.pop("sbxpark")
        proxmox.privileges = PVE_AUDITOR
        writes = len(proxmox.calls)

        self.assertEqual(self.manager(proxmox=proxmox).ensure_bridges(), [])
        self.assertEqual(proxmox.calls[writes:], [])

        proxmox.privileges = PVE_ADMINISTRATOR
        self.assertEqual(self.manager(proxmox=proxmox).ensure_bridges(), ["sbxpark"])


class SlowProxmox(FakeProxmox):
    """A clone that waits to be let go, so a change can be caught mid-way."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cloning, self.let_go = threading.Event(), threading.Event()

    def clone_vm(self, *args, **kwargs):
        self.cloning.set()
        self.let_go.wait(10)
        return super().clone_vm(*args, **kwargs)


class TestOverHttp(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.proxmox = SlowProxmox(templates=[320])
        self.proxmox.let_go.set()
        settings = lab_settings(self.tmp, pve_token="fake", mist_token="fake-token")
        self.manager = SandboxManager(settings, proxmox=self.proxmox, mist=FakeMist())
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def post(self, path, payload, timeout=10):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_address[1]}{path}",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_pause_and_resume(self):
        self.assertEqual(self.post("/api/pause", {"paused": True}), (200, {"paused": True}))
        status, body = self.post("/api/sandboxes", {"name": "while-paused", "recipe": "single-switch", "template_vmid": 320})
        self.assertEqual(status, 409, body)
        self.assertIn("paused", body["error"])
        self.assertEqual(self.post("/api/pause", {"paused": False}), (200, {"paused": False}))

    def test_a_pause_gets_through_while_a_change_is_running(self):
        self.proxmox.let_go.clear()
        build = threading.Thread(
            target=self.post, args=("/api/sandboxes", {"name": "slow-one", "recipe": "single-switch", "template_vmid": 320})
        )
        build.start()
        self.assertTrue(self.proxmox.cloning.wait(5))
        try:
            answer = self.post("/api/pause", {"paused": True}, timeout=3)
        finally:
            self.proxmox.let_go.set()
            build.join(10)
        self.assertEqual(answer, (200, {"paused": True}))

    def test_a_pause_stops_a_running_build_and_it_removes_what_it_made(self):
        self.proxmox.let_go.clear()
        answer = {}
        build = threading.Thread(
            target=lambda: answer.update(
                reply=self.post("/api/sandboxes", {"name": "halted", "recipe": "collapsed-core", "template_vmid": 320})
            )
        )
        build.start()
        self.assertTrue(self.proxmox.cloning.wait(5))
        self.post("/api/pause", {"paused": True}, timeout=3)
        self.proxmox.let_go.set()
        build.join(10)

        status, body = answer["reply"]
        self.assertEqual(status, 409, body)
        self.assertIn("paused", body["error"])
        self.assertEqual(sorted(self.proxmox.vms), [320], "the build removed the guest it had made")
        self.assertNotIn("halted", self.manager.sandboxes)

    def test_teardown_needs_no_typed_confirmation(self):
        self.manager.create_sandbox("gone-soon", "single-switch", template_vmid=320)
        status, body = self.post("/api/sandboxes/gone-soon/teardown", {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["complete"])


class TestAskingWhatATokenMayDo(unittest.TestCase):
    def test_proxmox_is_asked_for_the_tokens_privileges_on_one_path(self):
        path = "/sdn/zones/localnetwork/sbxpark"
        body = json.dumps({"data": {path: {"SDN.Use": 1}}}).encode()
        held = {}
        sent = wire(ProxmoxClient(Settings(pve_token="t")), lambda c: held.update(c.permissions(path)), body)

        (request,) = sent
        url = urllib.parse.urlparse(request["url"])
        self.assertEqual((request["method"], url.path), ("GET", "/api2/json/access/permissions"))
        self.assertEqual(urllib.parse.parse_qs(url.query), {"path": [path]})
        self.assertEqual(held, {"SDN.Use": 1})

    def test_mist_is_asked_who_the_token_is(self):
        body = json.dumps({"privileges": [{"scope": "org", "org_id": "org-1", "role": "write"}]}).encode()
        me = {}
        sent = wire(MistClient(Settings(mist_token="t")), lambda c: me.update(c.whoami()), body)

        self.assertEqual([(s["method"], urllib.parse.urlparse(s["url"]).path) for s in sent], [("GET", "/api/v1/self")])
        self.assertEqual(me["privileges"][0]["role"], "write")

    def test_a_mist_client_with_no_write_gate_sends_no_change(self):
        client = MistClient(Settings(mist_token="t", org_id="org-1"))
        with mock.patch("urllib.request.urlopen") as urlopen, self.assertRaises(GuardrailViolation):
            client.delete_site("s1")
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "simrack", "static")


def shipped(name: str) -> str:
    with open(os.path.join(STATIC, name), encoding="utf-8") as handle:
        return handle.read()


class TestTheUiAsksBeforeEachChange(unittest.TestCase):
    """The UI files as shipped. What the box does when clicked is checked live in a browser."""

    def setUp(self):
        self.js, self.html = shipped("app.js"), shipped("index.html")
        self.buttons = re.findall(r"<button\b[^>]*>", self.js + self.html)

    def test_the_box_offers_no_then_this_once_then_this_session(self):
        box = re.search(r'<dialog id="ask".*?</dialog>', self.html, re.S)
        self.assertIsNotNone(box, "index.html has the yes/no box")
        choices = re.findall(r'<button[^>]*value="(\w+)"[^>]*>([^<]+)</button>', box.group(0))
        self.assertEqual(choices, [("no", "1 No"), ("once", "2 Yes, this once"), ("session", "3 Yes for this session")])
        self.assertIn("autofocus", re.search(r'<button[^>]*value="no"[^>]*>', box.group(0)).group(0), "Enter means No")

    def test_every_change_button_asks(self):
        opens_a_form = ('data-act="new"', 'data-act="cable-from"')
        changes = [b for b in self.buttons if "data-write" in b and not any(o in b for o in opens_a_form)]
        self.assertGreaterEqual(len(changes), 20)
        for button in changes:
            self.assertIn("data-ask=", button)

    def test_changes_that_destroy_something_ask_every_time(self):
        destroying = [b for b in self.buttons if re.search(r'data-ask="(teardown|revert|delete)"', b)]
        self.assertGreaterEqual(len(destroying), 5)
        for button in destroying:
            self.assertIn("data-ask-always", button)

    def test_nothing_asks_to_type_a_name_or_click_twice(self):
        for gone in ("data-confirm", "confirmed(", "Type the sandbox name", "confirm: true"):
            self.assertNotIn(gone, self.js + self.html)

    def test_no_text_sends_the_user_to_an_env_switch(self):
        for gone in ("SIMRACK_MIST_WRITES", "SIMRACK_ALLOW_WRITES", "MIST_TOKEN"):
            self.assertNotIn(gone, self.js + self.html)

    def test_the_top_bar_can_pause_changes(self):
        self.assertRegex(self.html, r'<button[^>]*id="pausebtn"[^>]*data-act="pause"')
        self.assertIn('"/api/pause"', self.js)

