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

from simrack.access import VM_PRIVILEGES
from simrack.api import serve
from simrack.config import Settings
from simrack.errors import GuardrailViolation
from simrack.mist import MistClient
from simrack.proxmox import ProxmoxClient
from simrack.service import SandboxManager
from tests.fakes import (
    PVE_ADMINISTRATOR,
    PVE_AUDITOR,
    PVE_DATASTORE_USER,
    PVE_SDN_USER,
    FakeMist,
    FakeProxmox,
    TempDir,
    lab_settings,
)
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
        for lacking in ("VM.Allocate", "VM.Snapshot", "VM.Config.Network", "Datastore.AllocateSpace", "SDN.Use"):
            self.assertIn(lacking, reason)
        for held in ("VM.Audit", "Sys.Audit", "Datastore.Audit"):
            self.assertNotIn(held, reason)
        with self.assertRaises(GuardrailViolation):
            manager.create_sandbox("look-only", "single-switch", template_vmid=320)
        self.assertEqual(proxmox.calls, [])

    def test_change_rights_count_only_on_simracks_pool(self):
        """Granted on /vms, SimRack's role reaches every live guest too."""
        role = frozenset(VM_PRIVILEGES)
        rest = {"/nodes": PVE_AUDITOR, "/storage/local": PVE_AUDITOR, "/storage/local-lvm": PVE_DATASTORE_USER, "/sdn/zones/localnetwork": PVE_SDN_USER}

        on_every_guest = self.manager(proxmox=FakeProxmox(templates=[320], privileges={"/vms": role, **rest})).state()
        self.assertFalse(on_every_guest["writes_enabled"])
        self.assertIn("on /pool/simrack", on_every_guest["read_only_reason"])

        in_the_pool = FakeProxmox(templates=[320], privileges={"/pool/simrack": role, "/vms": PVE_AUDITOR, **rest})
        self.assertTrue(self.manager(proxmox=in_the_pool).state()["writes_enabled"])

    def test_a_token_that_may_clone_no_template_may_still_change_the_lab(self):
        """Which templates it may clone is asked when a build names one."""
        proxmox = FakeProxmox(templates=[320], privileges=PVE_ADMINISTRATOR - {"VM.Clone"})
        self.assertTrue(self.manager(proxmox=proxmox).state()["writes_enabled"])

    def test_a_template_the_token_may_not_clone_is_refused_with_the_command_that_allows_it(self):
        proxmox = FakeProxmox(templates=[320], privileges=PVE_ADMINISTRATOR - {"VM.Clone"})
        manager = self.manager(proxmox=proxmox)

        with self.assertRaises(GuardrailViolation) as refused:
            manager.create_sandbox("no-clone", "single-switch", template_vmid=320)

        self.assertIn("may not clone template 320", refused.exception.message)
        self.assertIn("pveum acl modify /vms/320 --users simrack@pve --roles PVETemplateUser", refused.exception.detail)
        self.assertEqual(proxmox.calls, [], "refused before anything is made, the park bridge too")
        self.assertNotIn("no-clone", manager.sandboxes)

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

    def test_a_write_role_on_the_orgs_msp_or_an_org_group_holding_it_turns_mist_changes_on(self):
        """A user token can hold its role above the org: on the MSP, or on an org group the org is in."""
        for n, privilege in enumerate((
            {"scope": "msp", "msp_id": "msp-example", "name": "Example MSP", "role": "admin"},
            {"scope": "orggroup", "msp_id": "msp-example", "orggroup_ids": ["og-example"], "name": "Lab orgs", "role": "write"},
            {"scope": "orggroup", "msp_id": "msp-example", "orggroup_id": "og-example", "name": "Lab orgs", "role": "write"},
        )):
            with self.subTest(privilege=privilege):
                mist = FakeMist()
                mist.privileges = [privilege]
                manager = self.manager(mist=mist)

                self.assertTrue(manager.state()["mist"]["writes_enabled"])
                manager.mist_create_site(manager.create_sandbox(f"above-org-{n}", "single-switch", template_vmid=320))
                self.assertEqual([call[0] for call in mist.calls], ["create_site"])
                self.assertEqual(mist.org_reads, 1, "asked once, then remembered with the rest")

    def test_an_msp_or_org_group_role_that_does_not_hold_this_org_changes_nothing_here(self):
        for case, privilege, status in (
            ("another MSP", {"scope": "msp", "msp_id": "msp-other", "name": "Other MSP", "role": "admin"}, None),
            ("another org group", {"scope": "orggroup", "orggroup_ids": ["og-other"], "name": "Other orgs", "role": "write"}, None),
            ("an org Mist hides from the token", {"scope": "msp", "msp_id": "msp-other", "name": "Other MSP", "role": "admin"}, 403),
        ):
            with self.subTest(case):
                mist = FakeMist()
                mist.privileges, mist.org_status = [privilege], status
                state = self.manager(mist=mist).state()
                self.assertFalse(state["mist"]["writes_enabled"])
                self.assertIn("no role on this org", state["mist"]["read_only_reason"])

    def test_a_look_only_role_held_above_the_org_is_named_with_where_it_is_held(self):
        mist = FakeMist()
        mist.privileges = [
            {"scope": "msp", "msp_id": "msp-example", "name": "Example MSP", "role": "read"},
            {"scope": "orggroup", "orggroup_ids": ["og-example"], "name": "Lab orgs", "role": "helpdesk"},
        ]
        state = self.manager(mist=mist).state()

        self.assertFalse(state["mist"]["writes_enabled"])
        reason = state["mist"]["read_only_reason"]
        self.assertIn("read through its MSP", reason)
        self.assertIn("helpdesk through an org group", reason)

    def test_when_mist_cannot_say_what_holds_the_org_mist_changes_stay_off(self):
        mist = FakeMist()
        mist.privileges, mist.org_status = [{"scope": "msp", "msp_id": "msp-example", "name": "Example MSP", "role": "admin"}], 503
        state = self.manager(mist=mist).state()

        self.assertFalse(state["mist"]["writes_enabled"])
        self.assertIn("cannot tell what the Mist token may do", state["mist"]["read_only_reason"])

    def test_mist_is_asked_about_the_org_only_when_a_role_above_it_could_hold_it(self):
        for role in ("write", "read", None):
            with self.subTest(role=role):
                mist = FakeMist(role=role)
                self.manager(mist=mist).state()
                self.assertEqual(mist.org_reads, 0)

    def test_with_no_mist_org_set_mist_changes_stay_off_whatever_the_token_may_do(self):
        mist = FakeMist(role="admin")
        settings = lab_settings(self.tmp, pve_token="fake", mist_token="fake-token", org_id="")
        manager = SandboxManager(settings, proxmox=FakeProxmox(templates=[320]), mist=mist)

        state = manager.state()
        self.assertTrue(state["writes_enabled"], "the lab needs no Mist org")
        self.assertFalse(state["mist"]["writes_enabled"])
        self.assertIn("no Mist org is set", state["mist"]["read_only_reason"])
        sandbox = manager.create_sandbox("no-org", "single-switch", template_vmid=320)
        with self.assertRaises(GuardrailViolation):
            manager.mist_create_site(sandbox)
        self.assertEqual(mist.calls, [])

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

    def test_a_pause_needs_true_or_false_and_anything_else_changes_nothing(self):
        self.post("/api/pause", {"paused": True})
        for body in ({}, {"paused": "false"}, {"paused": 0}, {"paused": None}, {"pause": False}):
            with self.subTest(body=body):
                status, reply = self.post("/api/pause", body)
                self.assertEqual(status, 400, reply)
                self.assertIn('{"paused": false}', reply["error"])
                self.assertIs(self.manager.access.paused(), True)

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

    def test_mist_is_asked_which_msp_and_org_groups_hold_the_org(self):
        body = json.dumps({"id": "org-1", "msp_id": "msp-1", "orggroup_ids": ["og-1"]}).encode()
        org = {}
        sent = wire(MistClient(Settings(mist_token="t", org_id="org-1")), lambda c: org.update(c.org()), body)

        self.assertEqual([(s["method"], urllib.parse.urlparse(s["url"]).path) for s in sent], [("GET", "/api/v1/orgs/org-1")])
        self.assertEqual((org["msp_id"], org["orggroup_ids"]), ("msp-1", ["og-1"]))

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


class TestTheUiAsksOnlyBeforeUndoingSomething(unittest.TestCase):
    """The UI files as shipped. What the box does when clicked is checked live in a browser."""

    # Each of these undoes, removes or stops something, or pushes config to every switch, so it asks first.
    ASKS = {"Revert guests", "Revert Mist", "Delete sandbox", "Delete shape", "Delete", "Shut down", "Power off",
            "Unplug", "Build fabric"}
    DANGER = {"Revert guests", "Revert Mist", "Delete sandbox", "Delete shape", "Delete", "Power off"}
    # Each of these saves, adds or starts something, so it just happens.
    JUST_DOES = {"Save point", "Build sandbox", "Build", "Add guest", "Plug in", "Move cable", "Start", "Send",
                 "Create Mist site"}

    def setUp(self):
        self.js, self.html = shipped("app.js"), shipped("index.html")
        self.buttons = [(tag, text.strip()) for tag, text in re.findall(r"(<button\b[^>]*>)([^<]*)", self.js + self.html)]

    def named(self, text):
        tags = [tag for tag, said in self.buttons if said == text]
        self.assertTrue(tags, f"a {text} button")
        return tags

    def test_the_box_offers_cancel_then_the_action(self):
        box = re.search(r'<dialog id="ask".*?</dialog>', self.html, re.S)
        self.assertIsNotNone(box, "index.html has the box")
        choices = re.findall(r'<button[^>]*value="(\w+)"[^>]*>([^<]*)</button>', box.group(0))
        self.assertEqual([value for value, _ in choices], ["no", "yes"])
        self.assertEqual(choices[0][1], "Cancel")
        self.assertIn("autofocus", re.search(r'<button[^>]*value="no"[^>]*>', box.group(0)).group(0), "Enter means Cancel")

    def test_a_yes_covers_one_click(self):
        for gone in ("simrack_ok", "this once", "for this session", "data-ask-always"):
            self.assertNotIn(gone, self.js + self.html)

    def test_what_undoes_removes_or_stops_something_asks(self):
        for text in self.ASKS:
            for tag in self.named(text):
                self.assertIn("data-ask=", tag, text)
        for text in self.DANGER:
            for tag in self.named(text):
                self.assertIn("data-ask-danger", tag, text)

    def test_saving_adding_or_starting_just_happens(self):
        for text in self.JUST_DOES:
            for tag in self.named(text):
                self.assertNotIn("data-ask", tag, text)
        adopting = [tag for tag, _ in self.buttons if 'data-act="adopt"' in tag]
        self.assertTrue(adopting)
        for tag in adopting:
            self.assertNotIn("data-ask", tag)

    def test_building_the_fabric_in_mist_asks(self):
        fabric = [tag for tag, _ in self.buttons if 'data-op="fabric"' in tag]
        self.assertGreaterEqual(len(fabric), 2)
        for tag in fabric:
            self.assertIn('data-ask="mist"', tag)

    def test_every_change_button_is_one_or_the_other(self):
        named_elsewhere = ('data-act="new"', 'data-act="cable-from"', 'data-act="adopt"', 'data-op="fabric"')
        changes = [(tag, text) for tag, text in self.buttons if "data-write" in tag and not any(o in tag for o in named_elsewhere)]
        self.assertGreaterEqual(len(changes), 20)
        for tag, text in changes:
            self.assertIn(text, self.ASKS | self.JUST_DOES, tag)

    def test_saved_points_list_the_newest_first(self):
        self.assertRegex(self.js, r"fillPoints\(\$\(\"\[data-pve-snaps\]\"\), s\.proxmox_snapshots\)")
        self.assertRegex(self.js, r"fillPoints\(\$\(\"\[data-mist-snaps\]\"\), s\.mist_snapshots\)")

    def test_nothing_asks_to_type_a_name_or_click_twice(self):
        for gone in ("data-confirm", "confirmed(", "Type the sandbox name", "confirm: true"):
            self.assertNotIn(gone, self.js + self.html)

    def test_no_text_sends_the_user_to_an_env_switch(self):
        for gone in ("SIMRACK_MIST_WRITES", "SIMRACK_ALLOW_WRITES", "MIST_TOKEN"):
            self.assertNotIn(gone, self.js + self.html)

    def test_the_top_bar_can_pause_changes(self):
        self.assertRegex(self.html, r'<button[^>]*id="pausebtn"[^>]*data-act="pause"')
        self.assertIn('"/api/pause"', self.js)

