"""Case 22: an assistant drives SimRack through an MCP server, one yes/no per change.

``python3 -m simrack mcp`` speaks the Model Context Protocol on stdin and stdout
and calls SimRack's own web API, so the assistant's host labels every tool as a
look or a change and asks before each change. The tools on offer follow what
SimRack may do right now: look-only while it is paused or has no profile,
changes when its tokens allow them, and the risky ones (tear down, revert,
delete a switch, type at a console) only when the lab profile says
``[assistants] risky = true``.
"""

from __future__ import annotations

import dataclasses
import io
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

from simrack.api import serve
from simrack.errors import BackendError
from simrack.mcp import Server
from tests.fakes import (
    LAB_PROFILE,
    FakeProxmox,
    RedirectingPair,
    SlowProxmox,
    TempDir,
    lab_settings,
    make_manager,
    mist_may_only_read,
    proxmox_may_only_look,
)

#: The vJunos template the test lab's Proxmox host holds.
TEMPLATE = 320

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HELLO = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "test-host", "version": "0"}}


def profile_with(tmp: str, extra: str) -> str:
    """The test lab profile with ``extra`` added, written beside the state folder's own copy."""
    path = os.path.join(tmp, "variant.toml")
    with open(LAB_PROFILE, encoding="utf-8") as source, open(path, "w", encoding="utf-8") as target:
        target.write(source.read() + "\n" + extra)
    return path


class Assistant:
    """What an assistant's host does: start ``simrack mcp`` and trade JSON-RPC lines with it."""

    def __init__(self, test: unittest.TestCase, url: str, *options: str):
        env = {key: value for key, value in os.environ.items() if key != "SIMRACK_TOKEN"}
        self.process = subprocess.Popen(
            [sys.executable, "-m", "simrack", "mcp", "--url", url, *options],
            cwd=REPO,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        test.addCleanup(self.close)
        self.ids = itertools.count(1)
        self.inbox: queue.Queue = queue.Queue()
        self.early: dict = {}
        self.notifications: list = []
        self.log: list = []
        self.readers = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=lambda: self.log.extend(self.process.stderr), daemon=True),
        ]
        for reader in self.readers:
            reader.start()

    def _read(self):
        for line in self.process.stdout:
            try:
                self.inbox.put(json.loads(line))
            except ValueError:
                self.inbox.put({"not JSON": line})
        self.inbox.put(None)

    def send(self, message: dict) -> None:
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None, *, timeout: float = 10) -> dict:
        return self.reply_to(self.ask(method, params), timeout=timeout)

    def ask(self, method: str, params: dict | None = None) -> int:
        """Send a request without waiting for its reply; reply_to collects it."""
        ident = next(self.ids)
        self.send({"jsonrpc": "2.0", "id": ident, "method": method, **({} if params is None else {"params": params})})
        return ident

    def notify(self, method: str, params: dict | None = None) -> None:
        self.send({"jsonrpc": "2.0", "method": method, **({} if params is None else {"params": params})})

    def reply_to(self, ident, *, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while ident not in self.early:
            message = self._next(deadline)
            if message is None:
                raise AssertionError(f"no reply to {ident} in {timeout}s; stderr: {''.join(self.log)}")
            if "id" in message:
                self.early[message["id"]] = message
            else:
                self.notifications.append(message)
        return self.early.pop(ident)

    def notice(self, *, timeout: float) -> dict | None:
        """The next notification SimRack sends, or None if it sends none in time."""
        deadline = time.monotonic() + timeout
        while not self.notifications:
            message = self._next(deadline)
            if message is None:
                return None
            if "id" in message:
                self.early[message["id"]] = message
            else:
                self.notifications.append(message)
        return self.notifications.pop(0)

    def _next(self, deadline: float) -> dict | None:
        try:
            message = self.inbox.get(timeout=max(0.01, deadline - time.monotonic()))
        except queue.Empty:
            return None
        if message is None:
            raise AssertionError(f"simrack mcp exited ({self.process.wait()}); stderr: {''.join(self.log)}")
        if "not JSON" in message:
            raise AssertionError(f"stdout carries only protocol messages, got {message['not JSON']!r}")
        return message

    def hello(self, params: dict = HELLO) -> dict:
        reply = self.request("initialize", params)
        self.notify("notifications/initialized")
        return reply

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for reader in self.readers:
            reader.join(timeout=5)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


class McpCase(unittest.TestCase):
    """SimRack on fakes behind its own web API, for assistants to connect to."""

    #: Settings to change from the test lab's, for a whole test class.
    settings: dict = {}
    #: The Proxmox host the test lab's SimRack drives.
    proxmox_class = FakeProxmox

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.proxmox = self.proxmox_class(templates=[TEMPLATE])
        self.manager = make_manager(self.tmp, proxmox=self.proxmox, **self.settings)
        self.httpd = serve(self.manager, "127.0.0.1", 0)
        self.httpd.log = lambda message: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def connect(self, *options: str) -> Assistant:
        return Assistant(self, self.url, *options)


class TestAnAssistantConnects(McpCase):
    def test_simrack_answers_in_the_assistants_protocol_version_or_its_own_latest(self):
        for asked, answered in (("2025-11-25", "2025-11-25"), ("2024-11-05", "2024-11-05"), ("2099-01-01", "2025-11-25")):
            with self.subTest(asked):
                result = self.connect().request("initialize", {**HELLO, "protocolVersion": asked})["result"]

                self.assertEqual(result["protocolVersion"], answered)
                self.assertEqual(result["serverInfo"]["name"], "simrack")
                self.assertEqual(result["capabilities"]["tools"], {"listChanged": True})
                self.assertIn("state", result["instructions"], "the assistant is told to look at the state first")

    def test_a_ping_is_answered_and_a_notification_is_not(self):
        assistant = self.connect()
        assistant.hello()
        assistant.notify("notifications/cancelled", {"requestId": 99})

        self.assertEqual(assistant.request("ping")["result"], {})
        self.assertEqual(assistant.early, {}, "nothing answers a notification")

    def test_a_mistake_gets_a_json_rpc_error_and_the_server_carries_on(self):
        assistant = self.connect()
        assistant.hello()

        self.assertEqual(assistant.request("resources/list")["error"]["code"], -32601, "SimRack offers only tools")
        assistant.process.stdin.write("{not json\n")
        assistant.process.stdin.flush()
        self.assertEqual(assistant.reply_to(None)["error"]["code"], -32700)
        assistant.send([{"jsonrpc": "2.0", "id": 7, "method": "ping"}])
        self.assertEqual(assistant.reply_to(None)["error"]["code"], -32600, "a batch is not a request")
        self.assertEqual(assistant.request("ping")["result"], {})


#: The tools an assistant gets, by what they do. Looks change nothing.
LOOKS = {"state", "access_check", "list_sandboxes", "get_sandbox", "list_shapes", "list_recipes", "mist_health", "job_result", "check_cabling"}
LAB_CHANGES = {"build_sandbox", "build_from_shape", "power_node", "add_cable", "move_cable", "remove_cable", "save_point", "fix_cabling"}
MIST_CHANGES = {"mist_create_site", "mist_build_fabric", "adopt_switch"}
RISKY = {"tear_down", "revert_guests", "revert_mist", "delete_node", "console_command"}


def tools_of(assistant: Assistant) -> dict:
    return {tool["name"]: tool for tool in assistant.request("tools/list")["result"]["tools"]}


class TestWhatAnAssistantIsOffered(McpCase):
    def test_a_writable_simrack_offers_its_changes_marked_as_changes_and_holds_the_risky_ones_back(self):
        assistant = self.connect()
        assistant.hello()

        tools = tools_of(assistant)

        self.assertEqual(set(tools), LOOKS | LAB_CHANGES | MIST_CHANGES | {"mist_save_point"})
        for name, tool in tools.items():
            with self.subTest(name):
                self.assertEqual(tool["inputSchema"]["type"], "object")
                self.assertIs(tool["annotations"]["readOnlyHint"], name in LOOKS)
                self.assertTrue(tool["description"])

    def test_a_paused_simrack_offers_nothing_that_changes_the_lab_or_mist(self):
        self.manager.set_paused(True)
        assistant = self.connect()
        assistant.hello()

        tools = tools_of(assistant)

        self.assertEqual(set(tools), LOOKS | {"mist_save_point"}, "a Mist save point is kept on the SimRack host")

    def test_mist_changes_are_offered_only_while_the_mist_token_may_write(self):
        assistant = self.connect()
        assistant.hello()

        with mist_may_only_read(self.manager):
            tools = tools_of(assistant)

        self.assertEqual(set(tools), LOOKS | LAB_CHANGES | {"mist_save_point"})


class ProxmoxSlowToSayWhatTheTokenMayDo(FakeProxmox):
    """A host slow to say what the token may do, as a real one can be when SimRack has not asked lately."""

    #: Seconds the first answer takes.
    delay = 0.5

    def permissions(self, path):
        if not self.permission_reads:
            time.sleep(self.delay)
        return super().permissions(path)


class TestTheFirstListWaitsForSimRack(McpCase):
    proxmox_class = ProxmoxSlowToSayWhatTheTokenMayDo

    def test_the_first_list_waits_for_simrack_to_find_out_what_its_tokens_may_do(self):
        out = io.StringIO()
        server = Server(self.url, out=out)
        self.addCleanup(server.stopping.set)

        with mock.patch("simrack.mcp.STATE_TIMEOUT", self.proxmox.delay / 5):
            server.receive(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": HELLO}))
            server.receive(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))

        listed = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(listed["id"], 2)
        tools = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(tools, LOOKS | LAB_CHANGES | MIST_CHANGES | {"mist_save_point"}, "the first list is not only the looks")


class TestRiskyTools(McpCase):
    settings = {"assistant_risky": True}

    def test_the_risky_tools_come_when_the_profile_allows_them_and_are_marked_destructive(self):
        assistant = self.connect()
        assistant.hello()

        tools = tools_of(assistant)

        self.assertEqual(set(tools), LOOKS | LAB_CHANGES | MIST_CHANGES | {"mist_save_point"} | RISKY)
        for name in RISKY | {"remove_cable"}:
            self.assertIs(tools[name]["annotations"]["destructiveHint"], True, name)
        self.manager.set_paused(True)
        self.assertFalse(RISKY & set(tools_of(assistant)), "a pause stops the risky tools too")

    def test_casper_is_told_which_changes_delete_things_and_which_disrupt_a_switch(self):
        assistant = self.connect()
        assistant.hello()

        tools = tools_of(assistant)

        kinds = {name: tool.get("_meta", {}).get("casper/change-kind") for name, tool in tools.items()}
        self.assertEqual({name for name, kind in kinds.items() if kind is None}, LOOKS, "a look makes no change")
        self.assertEqual({name for name, kind in kinds.items() if kind == "delete"}, {"tear_down", "delete_node", "remove_cable", "revert_mist"})
        self.assertEqual({name for name, kind in kinds.items() if kind == "disruptive"}, {"power_node", "revert_guests", "console_command"})
        self.assertEqual(set(kinds.values()), {None, "delete", "disruptive", "config"})

    def test_a_risky_tool_is_refused_once_the_setup_page_stops_allowing_it(self):
        assistant = self.connect()
        assistant.hello()
        call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE)
        self.assertIn("tear_down", tools_of(assistant))

        self.manager.use(dataclasses.replace(self.manager.settings, assistant_risky=False))
        result = call(assistant, "tear_down", sandbox="demo")

        self.assertIs(result.get("isError"), True)
        self.assertIn("setup page", result["content"][0]["text"])
        self.assertFalse(call(assistant, "get_sandbox", sandbox="demo").get("isError"), "the sandbox is still there")


class TestTheRiskySetting(unittest.TestCase):
    def test_risky_tools_are_off_unless_the_profile_turns_them_on(self):
        with TempDir() as tmp:
            self.assertFalse(make_manager(tmp).state()["assistants"]["risky"])
        with TempDir() as tmp:
            manager = make_manager(tmp, profile=profile_with(tmp, "[assistants]\nrisky = true\n"))
            self.assertTrue(manager.state()["assistants"]["risky"])

    def test_only_true_or_false_will_do(self):
        for text in ('risky = "yes"', "risky = 1"):
            with self.subTest(text), TempDir() as tmp:
                settings = lab_settings(tmp, profile=profile_with(tmp, f"[assistants]\n{text}\n"))
                self.assertEqual(settings.profile_path, "", "a bad profile is left out, so SimRack changes nothing")
                (problem,) = settings.problems
                self.assertIn("assistants.risky", problem["error"])
                self.assertIn("true or false", problem["error"])


class TestTheTokenStaysWithSimRack(unittest.TestCase):
    def test_the_token_does_not_follow_a_redirect(self):
        with RedirectingPair() as pair:
            Server(pair.saved, token="t").http("GET", "/api/state", timeout=5)
        self.assertEqual(pair.carried, {"saved": [True], "elsewhere": [False]})


def call(assistant: Assistant, tool: str, /, **arguments) -> dict:
    return assistant.request("tools/call", {"name": tool, "arguments": arguments})["result"]


def said(result: dict):
    (content,) = result["content"]
    return json.loads(content["text"])


class TestAnAssistantUsesTheTools(McpCase):
    def test_a_look_returns_what_simrack_says(self):
        assistant = self.connect()
        assistant.hello()

        result = call(assistant, "list_recipes")

        self.assertFalse(result.get("isError"))
        self.assertIn("collapsed-core", {recipe["name"] for recipe in said(result)["recipes"]})

    def test_a_change_sends_its_arguments_to_simrack(self):
        assistant = self.connect()
        assistant.hello()

        result = call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE, notes="For the Tuesday demo")

        self.assertFalse(result.get("isError"), result)
        sandbox = said(call(assistant, "get_sandbox", sandbox="demo"))
        self.assertEqual(sandbox["recipe"]["name"], "single-switch")
        self.assertIn("For the Tuesday demo", sandbox["notes"])

    def test_when_simrack_refuses_the_assistant_is_told_why_and_can_carry_on(self):
        assistant = self.connect()
        assistant.hello()

        result = call(assistant, "build_sandbox", name="demo", recipe="single-switch")

        self.assertIs(result.get("isError"), True)
        (content,) = result["content"]
        self.assertIn("A template or an image is required.", content["text"])
        self.assertIn("Pass template_vmid", content["text"], "SimRack's advice comes too")
        self.assertEqual(assistant.request("ping")["result"], {})

    def test_asking_for_a_tool_simrack_does_not_have_is_a_protocol_error(self):
        assistant = self.connect()
        assistant.hello()

        unknown = assistant.request("tools/call", {"name": "format_disk", "arguments": {}})
        nameless = assistant.request("tools/call", {"arguments": {}})

        self.assertEqual(unknown["error"]["code"], -32602)
        self.assertIn("format_disk", unknown["error"]["message"])
        self.assertEqual(nameless["error"]["code"], -32602)

    def test_arguments_that_do_not_fit_the_tool_are_sent_back_to_be_fixed(self):
        assistant = self.connect()
        assistant.hello()

        results = {
            "sandbox": call(assistant, "get_sandbox"),
            "template_vmid": call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=str(TEMPLATE)),
            "withMistSite": call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE, withMistSite=True),
            "a_port": call(assistant, "add_cable", sandbox="demo", a_node="sbx-acc-01", a_port="eth0", b_node="sbx-acc-02", b_port="ge-0/0/1"),
        }

        for argument, result in results.items():
            with self.subTest(argument):
                self.assertIs(result.get("isError"), True)
                self.assertIn(argument, result["content"][0]["text"])
        self.assertEqual(said(call(assistant, "list_sandboxes"))["sandboxes"], [], "nothing was built")

    def test_checking_the_cabling_only_looks_and_fixing_it_puts_it_back(self):
        bridge = self.manager.create_sandbox("demo", "collapsed-core", template_vmid=TEMPLATE).links[0].bridge
        del self.proxmox.networks[bridge]
        assistant = self.connect()
        assistant.hello()

        looked = said(call(assistant, "check_cabling", sandbox="demo"))
        self.assertEqual((looked["repair"], looked["summary"]["broken"]), (False, 1))
        self.assertNotIn(bridge, self.proxmox.networks, "the check only looks, though SimRack may change the lab")

        fixed = said(call(assistant, "fix_cabling", sandbox="demo"))
        self.assertEqual((fixed["repair"], fixed["summary"]["fixed"]), (True, 1))
        self.assertIn(bridge, self.proxmox.networks)


#: How a tool hands over a change that is still running: the job SimRack keeps it as.
JOB = re.compile(r"job_result with job ([0-9a-f]{6}-[0-9]+)")


class TestALongChange(McpCase):
    proxmox_class = SlowProxmox

    def setUp(self):
        super().setUp()
        self.addCleanup(self.proxmox.let_go.set)

    def test_a_change_still_running_when_the_wait_ends_is_handed_over_as_a_job(self):
        assistant = self.connect("--wait", "0.5")
        assistant.hello()

        building = assistant.ask("tools/call", {"name": "build_sandbox", "arguments": {"name": "demo", "recipe": "single-switch", "template_vmid": TEMPLATE}})
        self.assertEqual(assistant.request("ping")["result"], {})
        self.assertNotIn(building, assistant.early, "the ping was not kept waiting behind the build")
        started = assistant.reply_to(building)["result"]

        self.assertFalse(started.get("isError"), started)
        job = JOB.search(started["content"][0]["text"]).group(1)
        self.assertTrue(self.proxmox.cloning.is_set())
        self.assertIn(f"job {job}", call(assistant, "job_result", job=job)["content"][0]["text"])

        self.proxmox.let_go.set()
        deadline = time.monotonic() + 10
        while "Still running" in (finished := call(assistant, "job_result", job=job))["content"][0]["text"]:
            self.assertLess(time.monotonic(), deadline, "the build never finished")

        self.assertFalse(finished.get("isError"), finished)
        self.assertEqual(said(finished)["name"], "demo")

    def test_simrack_keeps_the_job_so_a_new_session_can_ask_how_it_ended(self):
        first = self.connect("--wait", "0.5")
        first.hello()
        started = call(first, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE)
        job = JOB.search(started["content"][0]["text"]).group(1)
        first.close()

        self.proxmox.let_go.set()
        second = self.connect()
        second.hello()
        finished = call(second, "job_result", job=job)

        self.assertFalse(finished.get("isError"), finished)
        self.assertEqual(said(finished)["name"], "demo")

    def test_a_change_simrack_refuses_comes_back_in_simrack_s_words(self):
        assistant = self.connect()
        assistant.hello()
        self.proxmox.let_go.set()
        call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE)

        again = call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE)

        self.assertIs(again.get("isError"), True, again)
        self.assertIn("demo", again["content"][0]["text"])
        self.assertNotIn("job", again["content"][0]["text"], "a change that ends within the wait is answered, not handed over")

    def test_asking_after_a_job_simrack_never_had_says_so(self):
        assistant = self.connect()
        assistant.hello()

        result = call(assistant, "job_result", job="beef00-99")

        self.assertIs(result.get("isError"), True)
        self.assertIn("no job beef00-99", result["content"][0]["text"])
        self.assertIn("restarts", result["content"][0]["text"])

    def test_a_job_is_named_the_way_simrack_gave_it(self):
        assistant = self.connect()
        assistant.hello()

        for wrong in (99, "99", "../state"):
            with self.subTest(job=wrong):
                result = call(assistant, "job_result", job=wrong)

                self.assertIs(result.get("isError"), True)
                self.assertIn("job", result["content"][0]["text"])
                self.assertNotIn("no job", result["content"][0]["text"], "SimRack is not asked about a job it cannot have given")


class TestTheToolsFollowSimRack(McpCase):
    def test_the_assistant_hears_once_each_time_the_tools_on_offer_change(self):
        assistant = self.connect("--poll", "0.2")
        assistant.hello()
        self.assertIn("build_sandbox", tools_of(assistant))
        self.assertIsNone(assistant.notice(timeout=0.8), "nothing changed, so nothing is said")

        self.manager.set_paused(True)

        self.assertEqual(assistant.notice(timeout=5), {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        self.assertIsNone(assistant.notice(timeout=0.8), "told once, not at every look")
        self.assertNotIn("build_sandbox", tools_of(assistant))
        self.manager.set_paused(False)
        self.assertEqual(assistant.notice(timeout=5)["method"], "notifications/tools/list_changed")
        self.assertIn("build_sandbox", tools_of(assistant))


#: A Proxmox token as SimRack's setup commands make it, with a placeholder secret.
PVE_TOKEN = "simrack@pve!simrack=00000000-0000-0000-0000-000000000000"
#: What access_check says of this server's own pin: changes on, or pinned off by --read-only.
UNPINNED = {"flag": "--read-only", "state": "on"}
PINNED = {"flag": "--read-only", "state": "off"}
EXAMPLE_ORG = {"kind": "org", "id": "org-example", "name": "Example Org"}


class TestTheHostAsksWhatTheTokensMayDo(McpCase):
    """access_check answers in Casper's access-check v2: what each token may do, whatever SimRack allows right now."""

    settings = {"pve_token": PVE_TOKEN}

    def check(self, *options: str) -> dict:
        assistant = self.connect(*options)
        assistant.hello()
        answer = said(call(assistant, "access_check"))
        self.assertEqual(answer["contract"], "casper/access-check v2")
        return {product["product"]: product for product in answer["products"]}

    def test_it_is_a_look_that_needs_nothing_so_a_host_may_call_it_on_its_own(self):
        assistant = self.connect()
        assistant.hello()

        tool = tools_of(assistant)["access_check"]

        self.assertIs(tool["annotations"]["readOnlyHint"], True)
        self.assertNotIn("destructiveHint", tool["annotations"])
        self.assertEqual(tool["inputSchema"]["required"], [])
        self.assertNotIn("_meta", tool, "no change kind: it changes nothing")

    def test_it_names_who_each_token_is_and_where_it_may_change_things(self):
        products = self.check()

        self.assertEqual(
            products["proxmox"],
            {"product": "proxmox", "access": "read-write", "identity": "simrack@pve", "server_gate": UNPINNED},
            "no role: Proxmox does not say which role gave the token its privileges",
        )
        self.assertEqual(
            products["mist"],
            {
                "product": "mist",
                "access": "read-write",
                "identity": "lab@example.com",
                "role": "write",
                "can_change": [EXAMPLE_ORG],
                "server_gate": UNPINNED,
            },
        )

    def test_a_pause_is_not_the_tokens_so_it_does_not_make_them_read_only(self):
        self.manager.set_paused(True)

        products = self.check()

        self.assertEqual({name: product["access"] for name, product in products.items()}, {"proxmox": "read-write", "mist": "read-write"})

    def test_tokens_that_may_change_nothing_are_read_only(self):
        with proxmox_may_only_look(self.manager), mist_may_only_read(self.manager):
            products = self.check()

        self.assertEqual((products["proxmox"]["access"], products["proxmox"]["identity"]), ("read-only", "simrack@pve"))
        self.assertEqual((products["mist"]["access"], products["mist"]["role"]), ("read-only", "read"))
        self.assertEqual(products["mist"]["read_only"], [EXAMPLE_ORG])
        self.assertNotIn("can_change", products["mist"])
        self.manager.mist.role = None
        self.assertEqual(self.check()["mist"]["access"], "read-only", "a Mist token with no role at all")

    def test_a_token_not_added_yet_is_missing(self):
        self.manager.use(dataclasses.replace(self.manager.settings, pve_token="", mist_token=""))

        products = self.check()

        for name in ("proxmox", "mist"):
            self.assertEqual(products[name], {"product": name, "access": "unknown", "login": "missing", "server_gate": UNPINNED})

    def test_what_simrack_cannot_find_out_is_unknown_never_a_guess(self):
        self.manager.mist.fail_reads = True
        unreachable = BackendError("Cannot reach the Proxmox API.")

        with mock.patch.object(self.proxmox, "permissions", side_effect=unreachable):
            products = self.check()

        for name in ("proxmox", "mist"):
            self.assertEqual(products[name], {"product": name, "access": "unknown", "server_gate": UNPINNED})

    def test_with_no_lab_profile_proxmox_is_unknown_but_mist_still_answers(self):
        self.manager.use(dataclasses.replace(self.manager.settings, profile_path=""))

        products = self.check()

        self.assertEqual(products["proxmox"], {"product": "proxmox", "access": "unknown", "server_gate": UNPINNED})
        self.assertEqual(products["mist"]["access"], "read-write", "a Mist token reaches the same orgs with or without a profile")

    def test_mist_lists_every_place_the_token_reaches_not_only_simracks_org(self):
        self.manager.mist.privileges = [
            {"scope": "org", "org_id": "org-example", "name": "Example Org", "role": "write"},
            {"scope": "site", "org_id": "org-other", "site_id": "site-lab", "name": "Lab Site", "role": "admin"},
            {"scope": "sitegroup", "org_id": "org-other", "sitegroup_id": "group-branches", "name": "Branches", "role": "read"},
        ]

        mist = self.check()["mist"]

        self.assertEqual(mist["access"], "read-write")
        self.assertEqual(mist["can_change"], [EXAMPLE_ORG, {"kind": "site", "id": "site-lab", "name": "Lab Site"}])
        self.assertEqual(mist["read_only"], [{"kind": "sitegroup", "id": "group-branches", "name": "Branches"}])
        self.assertNotIn("role", mist, "the token holds more than one role")

    def test_a_list_that_cannot_be_given_whole_is_left_out_rather_than_cut_short(self):
        self.manager.mist.privileges = [
            {"scope": "org", "org_id": "org-example", "name": "Example Org", "role": "write"},
            {"scope": "msp", "msp_id": "msp-example", "name": "Example MSP", "role": "admin"},
            {"scope": "site", "org_id": "org-example", "site_id": "site-odd", "name": "Lab <Site>", "role": "read"},
        ]

        mist = self.check()["mist"]

        self.assertEqual(mist["access"], "read-write")
        self.assertNotIn("can_change", mist, "Casper's contract has no kind for a whole MSP")
        self.assertNotIn("read_only", mist, "a name Casper would not show")

    def test_a_mist_role_simrack_does_not_know_makes_mist_unknown(self):
        self.manager.mist.privileges = [
            {"scope": "org", "org_id": "org-example", "name": "Example Org", "role": "write"},
            {"scope": "site", "org_id": "org-example", "site_id": "site-lab", "name": "Lab Site", "role": "custom"},
        ]

        mist = self.check()["mist"]

        self.assertEqual(mist["access"], "unknown")
        self.assertNotIn("can_change", mist)
        self.assertNotIn("read_only", mist)

    def test_pinned_read_only_it_offers_only_looks_and_still_says_what_the_tokens_may_do(self):
        assistant = self.connect("--read-only")
        self.assertIn("--read-only", assistant.hello()["result"]["instructions"])

        tools = tools_of(assistant)
        refused = call(assistant, "build_sandbox", name="demo", recipe="single-switch", template_vmid=TEMPLATE)

        self.assertEqual(set(tools), LOOKS, "no Mist save point either: it writes a file on the SimRack host")
        self.assertIs(refused.get("isError"), True)
        self.assertIn("--read-only", refused["content"][0]["text"])
        self.assertEqual(said(call(assistant, "list_sandboxes"))["sandboxes"], [], "nothing was built")
        products = self.check("--read-only")
        self.assertEqual({name: product["access"] for name, product in products.items()}, {"proxmox": "read-write", "mist": "read-write"})
        self.assertEqual({name: product["server_gate"] for name, product in products.items()}, {"proxmox": PINNED, "mist": PINNED})


if __name__ == "__main__":
    unittest.main()
