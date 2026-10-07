"""SimRack's MCP server: an assistant drives SimRack through SimRack's own web API.

The assistant's host starts ``python3 -m simrack mcp`` and trades JSON-RPC 2.0
messages with it, one per line on stdin and stdout. Each change is its own
tool, so the host can label it and ask before it runs. Logs go to stderr:
stdout carries only the protocol. Standard library only, like the rest of SimRack.
"""

from __future__ import annotations

import json
import os
import re
import string
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__
from .jobs import LONGEST_WAIT

#: Newest first. An assistant asking for a version not listed is answered in the newest.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602
#: How long to wait for SimRack's state when deciding which tools to offer.
STATE_TIMEOUT = 4.0
#: The same, until SimRack has answered once and there is a state to fall back on. Its first answer can be
#: slower: it asks Proxmox and Mist what its tokens may do when it has not asked lately.
FIRST_STATE_TIMEOUT = 15.0
#: How long a look, or handing SimRack a change, may take before SimRack counts as not answering.
LOOK_TIMEOUT = 60.0
#: How much longer than it was asked to wait SimRack may take to say how a job stands.
JOB_SLACK = 10.0

INSTRUCTIONS = (
    "SimRack builds throwaway sandboxes of vJunos switches on a Proxmox host and can mirror them in Mist. "
    "Call state first: it says whether SimRack may change anything right now and, if not, why. "
    "Only the changes SimRack may make right now are offered; the list changes when that does."
)
RISKY_OFF = (
    "SimRack's setup page does not let assistants tear down, revert, delete switches or type at a console. "
    "Ask the person running SimRack to allow it there, or to do this from SimRack's own page."
)
NO_MIST = "SimRack has no Mist token yet. Add one on SimRack's setup page."
PINNED = (
    "SimRack's MCP server was started with --read-only, so it makes no changes. "
    "Ask the person running the assistant to start it without --read-only."
)
PINNED_NOTE = " This server was started with --read-only: it offers looks only, whatever state says."
#: What access_check answers in, for Casper.
ACCESS_CONTRACT = "casper/access-check v2"

SANDBOX = {"type": "string", "description": "The sandbox's name, as list_sandboxes shows it."}
NODE = {"type": "string", "description": "A switch in the sandbox, like sbx-acc-01."}
PORT = {"type": "string", "pattern": "^(ge|et)-[0-9]+/[0-9]+/[0-9]+$", "description": "A switch port, like ge-0/0/1."}
LABEL = {
    "type": "string",
    "pattern": "^[A-Za-z][A-Za-z0-9_-]{0,39}$",
    "description": "A name for the save point: a letter, then up to 39 letters, digits, - or _.",
}
NEW_NAME = {
    "type": "string",
    "pattern": "^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$",
    "description": "A name for the new sandbox: 3 to 32 lowercase letters, digits or hyphens.",
}
JOB = {"type": "string", "pattern": "^[0-9a-f]{6}-[1-9][0-9]{0,8}$", "description": "The job a tool handed over, like 3f9a0c-7."}
BUILD_OPTIONS = {
    "template_vmid": {"type": "integer", "description": "The vJunos template to clone: one of state's templates. Give this or image."},
    "image": {"type": "string", "description": "A disk image or installer to boot instead: one of state's images. Give this or template_vmid."},
    "start": {"type": "boolean", "description": "Start the switches once built. Default true."},
    "with_mist_site": {"type": "boolean", "description": "Also make a Mist site for the sandbox. Default false."},
}


@dataclass(frozen=True)
class Tool:
    """One thing an assistant may ask SimRack to do, and the web API call that does it."""

    name: str
    title: str
    description: str
    method: str
    path: str
    #: What SimRack must allow right now for the tool to be offered: see ``Server.gates``.
    gate: str = "look"
    #: look changes nothing; change does; undo throws work away.
    kind: str = "look"
    properties: dict = field(default_factory=dict)
    required: tuple = ()
    #: Talks to the Mist cloud, not only the lab host.
    mist: bool = False
    #: The kind of change, for Casper's change box: config, disruptive or delete.
    change_kind: str = "config"
    #: Sent with every call, over anything the assistant passed.
    body: dict = field(default_factory=dict)

    def listing(self) -> dict:
        annotations = {"title": self.title, "readOnlyHint": self.kind == "look", "openWorldHint": self.mist}
        if self.kind != "look":
            annotations["destructiveHint"] = self.kind == "undo"
        tool = {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "required": list(self.required), "additionalProperties": False},
            "annotations": annotations,
        }
        if self.kind == "change":
            tool["_meta"] = {"casper/safety": "write"}
        if self.kind != "look":
            tool.setdefault("_meta", {})["casper/change-kind"] = self.change_kind
        return tool


def _sandbox_tool(name, title, description, method, suffix, *, extra=None, required=(), **options) -> Tool:
    return Tool(
        name,
        title,
        description,
        method,
        "/api/sandboxes/{sandbox}" + suffix,
        properties={"sandbox": SANDBOX, **(extra or {})},
        required=("sandbox", *required),
        **options,
    )


TOOLS = (
    Tool(
        "state",
        "SimRack state",
        "What SimRack may change right now and, if not, why; the host's free memory; and the recipes, shapes, "
        "templates, images and sandboxes. Call this first.",
        "GET",
        "/api/state",
    ),
    Tool(
        "access_check",
        "Check access",
        "Who SimRack's Proxmox and Mist tokens are and where each may change things, whatever SimRack allows right now. "
        "Says unknown when SimRack cannot find out.",
        "GET",
        "/api/access",
    ),
    Tool("list_sandboxes", "List sandboxes", "The sandboxes, with their switches, cables and Mist sites.", "GET", "/api/sandboxes"),
    _sandbox_tool("get_sandbox", "Show a sandbox", "One sandbox in full: switches, cables, save points and notes.", "GET", ""),
    Tool("list_shapes", "List shapes", "Fabric shapes imported from Mist, that build_from_shape can copy.", "GET", "/api/shapes"),
    Tool("list_recipes", "List recipes", "The built-in sandbox recipes that build_sandbox can use.", "GET", "/api/recipes"),
    _sandbox_tool(
        "mist_health",
        "Mist health",
        "How the sandbox's switches look in Mist: connected, adopted, and whether their config is in step.",
        "GET",
        "/mist/health",
        gate="mist-look",
        mist=True,
    ),
    Tool(
        "job_result",
        "Job result",
        "The outcome of a change that was still running when its tool answered. "
        "SimRack keeps the job, so a new session can ask too. "
        "Waits for it as long as a change tool does; call again while it is still running.",
        "",
        "",
        properties={"job": JOB},
        required=("job",),
    ),
    _sandbox_tool(
        "check_cabling",
        "Check cabling",
        "Checks every cable in the sandbox against Proxmox, and against LLDP and Mist where they can be read. "
        "Changes nothing: fix_cabling puts back what drifted.",
        "POST",
        "/fabric/check",
        body={"repair": False},
        mist=True,
    ),
    Tool(
        "build_sandbox",
        "Build a sandbox",
        "Builds a new sandbox of vJunos switches from a recipe, cabled and started. Takes minutes.",
        "POST",
        "/api/sandboxes",
        gate="lab",
        kind="change",
        properties={
            "name": NEW_NAME,
            "recipe": {"type": "string", "description": "A recipe from list_recipes. Default collapsed-core."},
            **BUILD_OPTIONS,
            "notes": {"type": "string", "description": "A note to keep with the sandbox."},
        },
        required=("name",),
        mist=True,
    ),
    Tool(
        "build_from_shape",
        "Build from a shape",
        "Builds a new sandbox shaped like an imported fabric: one vJunos per switch, cabled port for port. "
        "Cables a sandbox cannot carry are left out and listed. Takes minutes.",
        "POST",
        "/api/shapes/{shape}/build",
        gate="lab",
        kind="change",
        properties={
            "shape": {"type": "string", "description": "A shape from list_shapes."},
            "name": NEW_NAME,
            "switches": {"type": "array", "items": {"type": "string"}, "description": "Only these switches of the shape. Default all."},
            **BUILD_OPTIONS,
        },
        required=("shape", "name"),
        mist=True,
    ),
    _sandbox_tool(
        "power_node",
        "Power a switch",
        "Starts, shuts down or reboots a switch. stop is like pulling the plug: the switch gets no chance to shut down.",
        "POST",
        "/nodes/{node}/power",
        gate="lab",
        kind="change",
        change_kind="disruptive",
        extra={"node": NODE, "action": {"type": "string", "enum": ["start", "shutdown", "reboot", "stop"]}},
        required=("node", "action"),
    ),
    _sandbox_tool(
        "add_cable",
        "Add a cable",
        "Cables a port on one switch to a port on another.",
        "POST",
        "/cables",
        gate="lab",
        kind="change",
        extra={"a_node": NODE, "a_port": PORT, "b_node": NODE, "b_port": PORT},
        required=("a_node", "a_port", "b_node", "b_port"),
    ),
    _sandbox_tool(
        "move_cable",
        "Move a cable",
        "Moves one end of a cable to another port. get_sandbox lists each cable's bridge.",
        "POST",
        "/cables/{bridge}/move",
        gate="lab",
        kind="change",
        extra={
            "bridge": {"type": "string", "description": "The cable's bridge, from get_sandbox."},
            "to_node": NODE,
            "to_port": PORT,
            "from_node": {"type": "string", "description": "Which end to move, when both ends are on the same switch."},
        },
        required=("bridge", "to_node", "to_port"),
    ),
    _sandbox_tool(
        "remove_cable",
        "Remove a cable",
        "Unplugs a cable. add_cable puts it back.",
        "POST",
        "/cables/{bridge}/remove",
        gate="lab",
        kind="undo",
        change_kind="delete",
        extra={"bridge": {"type": "string", "description": "The cable's bridge, from get_sandbox."}},
        required=("bridge",),
    ),
    _sandbox_tool(
        "fix_cabling",
        "Fix cabling",
        "Checks the cabling as check_cabling does, then puts back what drifted: makes missing cable bridges again, "
        "plugs ends back where they belong and parks stray ports. It never adds NICs.",
        "POST",
        "/fabric/check",
        gate="lab",
        kind="change",
        body={"repair": True},
        mist=True,
    ),
    _sandbox_tool(
        "save_point",
        "Save a revert point",
        "Saves a Proxmox revert point of every switch in the sandbox.",
        "POST",
        "/snapshot",
        gate="lab",
        kind="change",
        extra={"label": LABEL},
        required=("label",),
    ),
    _sandbox_tool(
        "mist_save_point",
        "Save a Mist revert point",
        "Saves a copy of the sandbox's Mist site and switch configs on the SimRack host. Changes nothing in Mist.",
        "POST",
        "/mist/snapshot",
        gate="mist-look",
        kind="change",
        extra={"label": LABEL},
        required=("label",),
        mist=True,
    ),
    _sandbox_tool(
        "mist_create_site",
        "Make the Mist site",
        "Makes a Mist site for a sandbox that has none.",
        "POST",
        "/mist/site",
        gate="mist",
        kind="change",
        mist=True,
    ),
    _sandbox_tool(
        "mist_build_fabric",
        "Build the Mist fabric",
        "Builds the sandbox's fabric in Mist from its switches and cables. Saves a Mist revert point first.",
        "POST",
        "/mist/fabric",
        gate="mist",
        kind="change",
        mist=True,
    ),
    _sandbox_tool(
        "adopt_switch",
        "Adopt a switch",
        "Adopts a running switch into the sandbox's Mist site. The switch must be up.",
        "POST",
        "/nodes/{node}/adopt",
        gate="mist",
        kind="change",
        extra={"node": NODE},
        required=("node",),
        mist=True,
    ),
    _sandbox_tool(
        "tear_down",
        "Tear down a sandbox",
        "Deletes the sandbox: its switches, its cables and, unless keep_mist, its Mist site, after deleting the "
        "site's fabric topology and releasing its vJunos switches from the org. If anything is refused, the sandbox "
        "stays listed and the result says why: call again once it is fixed.",
        "POST",
        "/teardown",
        gate="risky",
        kind="undo",
        change_kind="delete",
        extra={"keep_mist": {"type": "boolean", "description": "Leave the Mist site in place. Default false."}},
        mist=True,
    ),
    _sandbox_tool(
        "revert_guests",
        "Revert the switches",
        "Puts every switch back to a saved revert point. What changed since is lost.",
        "POST",
        "/revert",
        gate="risky",
        kind="undo",
        change_kind="disruptive",
        extra={"label": LABEL},
        required=("label",),
    ),
    _sandbox_tool(
        "revert_mist",
        "Revert Mist",
        "Puts the sandbox's Mist site back to a saved Mist revert point. What changed in Mist since is lost. "
        "Mist never takes an Additional CLI line back off a switch, so cli_left_on_switch names each line "
        "the switches keep: a delete line sent through Mist, or reverting the switches, removes it.",
        "POST",
        "/mist/revert",
        gate="risky-mist",
        kind="undo",
        change_kind="delete",
        extra={"label": LABEL},
        required=("label",),
        mist=True,
    ),
    _sandbox_tool(
        "delete_node",
        "Delete a switch",
        "Deletes one switch from the sandbox, and its cables.",
        "POST",
        "/nodes/{node}/delete",
        gate="risky",
        kind="undo",
        change_kind="delete",
        extra={"node": NODE},
        required=("node",),
    ),
    _sandbox_tool(
        "console_command",
        "Type at a switch's console",
        "Types one line at a switch's serial console and returns what it printed. Anything can be typed, so anything can change.",
        "POST",
        "/nodes/{node}/console",
        gate="risky",
        kind="undo",
        change_kind="disruptive",
        extra={"node": NODE, "command": {"type": "string", "minLength": 1, "maxLength": 2000}},
        required=("node", "command"),
    ),
)


class Unreachable(Exception):
    """SimRack's web API did not answer."""


class Refused(Exception):
    """SimRack's web API answered with an error: the words are SimRack's own."""


BY_NAME = {tool.name: tool for tool in TOOLS}
JSON_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list}


def plain(text: str) -> dict:
    """A tool's answer in words."""
    return {"content": [{"type": "text", "text": text}]}


def trouble(text: str) -> dict:
    """A tool's answer when it could not do what was asked: the assistant reads why and can try again."""
    return {**plain(text), "isError": True}


def fine(said) -> dict:
    """A tool's answer when SimRack did what was asked: SimRack's own JSON."""
    return plain(json.dumps(said, separators=(",", ":")))


def complaint(problem, fallback: str) -> str:
    """SimRack's own words for why it would not, or ``fallback`` when it gave none."""
    if not isinstance(problem, dict) or not problem.get("error"):
        return fallback
    return " ".join(str(problem[part]) for part in ("error", "detail") if problem.get(part))


def misfits(tool: Tool, arguments) -> list[str]:
    """What is wrong with the arguments for the tool, for the assistant to fix; empty when they fit."""
    if not isinstance(arguments, dict):
        return ["Send the arguments as a JSON object."]
    problems = [f"{name} is required." for name in tool.required if name not in arguments]
    for name, value in arguments.items():
        rule = tool.properties.get(name)
        if rule is None:
            problems.append(f"{tool.name} takes no argument called {name}. It takes: {', '.join(tool.properties) or 'nothing'}.")
        else:
            problems.extend(_misfit(name, rule, value))
    return problems


def _misfit(name: str, rule: dict, value) -> list[str]:
    kind = rule["type"]
    if not isinstance(value, JSON_TYPES[kind]) or (kind == "integer" and isinstance(value, bool)):
        return [f"{name} must be {'an' if kind[0] in 'aeiou' else 'a'} {kind}, not {json.dumps(value)}."]
    if kind == "array":
        return [] if all(not _misfit(name, rule["items"], item) for item in value) else [f"{name} must be a list of {rule['items']['type']}s."]
    if "enum" in rule and value not in rule["enum"]:
        return [f"{name} must be one of: {', '.join(rule['enum'])}."]
    if kind == "string" and not rule.get("minLength", 0) <= len(value) <= rule.get("maxLength", len(value)):
        return [f"{name} must be {rule.get('minLength', 0)} to {rule['maxLength']} characters long."]
    # Every pattern here is anchored, so fullmatch is the schema's meaning, and it refuses the trailing newline $ lets by.
    if "pattern" in rule and not re.fullmatch(rule["pattern"], value):
        return [f"{name} does not fit. {rule.get('description', '')}".strip()]
    return []


class Server:
    def __init__(self, url: str, *, token: str = "", out=None, wait: float = 50.0, poll: float = 15.0, read_only: bool = False):
        self.url = url.rstrip("/")
        self.token = token
        #: Started with --read-only: looks only, whatever SimRack allows.
        self.read_only = read_only
        self.out = out or sys.stdout
        self.out_lock = threading.Lock()
        # No proxies: the bearer token goes to SimRack and nowhere else.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        #: SimRack's last state, kept when a later look fails so the tools on offer do not flap.
        self.state: dict = {}
        #: How long a call waits for SimRack before it hands the assistant a job instead.
        self.wait = wait
        #: Seconds between looks at SimRack, once the assistant has a list of tools that could go stale.
        self.poll = poll
        self.told = ""
        self.told_lock = threading.Lock()
        self.watcher: threading.Thread | None = None
        self.stopping = threading.Event()

    def run(self, lines) -> None:
        try:
            for line in lines:
                if line.strip():
                    self.receive(line)
        finally:
            self.stopping.set()

    def receive(self, line: bytes | str) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            return self.fail(None, PARSE_ERROR, "Parse error: each line must be one JSON-RPC message.")
        if not isinstance(message, dict):
            return self.fail(None, INVALID_REQUEST, "Invalid request: send one JSON-RPC message per line, not a batch.")
        if "id" not in message:
            return
        ident, method = message["id"], message.get("method")
        if not isinstance(method, str):
            if "result" in message or "error" in message:
                return
            return self.fail(ident, INVALID_REQUEST, "Invalid request: no method.")
        if method == "initialize":
            self.reply(ident, self.initialize(message.get("params") or {}))
        elif method == "ping":
            self.reply(ident, {})
        elif method == "tools/list":
            tools = self.offered(self.refresh())
            self.listed(tools)
            self.reply(ident, {"tools": tools})
        elif method == "tools/call":
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            tool = BY_NAME.get(name) if isinstance(name, str) else None
            if tool is None:
                return self.fail(ident, INVALID_PARAMS, f"Unknown tool: {name}. tools/list shows the tools SimRack has.")
            # Answered from its own thread, so pings and other calls are not stuck behind a long change.
            threading.Thread(target=self.answer, args=(ident, tool, params.get("arguments") or {}), daemon=True).start()
        else:
            self.fail(ident, METHOD_NOT_FOUND, f"Method not found: {method}. SimRack offers tools only.")

    def refresh(self) -> dict:
        try:
            self.state = self.http("GET", "/api/state", timeout=STATE_TIMEOUT if self.state else FIRST_STATE_TIMEOUT)
        except (Refused, Unreachable):
            pass
        return self.state

    @staticmethod
    def gates(state: dict) -> dict:
        """Which kinds of tool SimRack allows right now. Nothing known means looks only."""
        mist = state.get("mist") or {}
        lab = state.get("writes_enabled") is True
        mist_writable = lab and mist.get("writes_enabled") is True
        risky = lab and (state.get("assistants") or {}).get("risky") is True
        return {
            "look": True,
            "mist-look": mist.get("configured") is True,
            "lab": lab,
            "mist": mist_writable,
            "risky": risky,
            "risky-mist": risky and mist_writable,
        }

    def offered(self, state: dict) -> list:
        gates = self.gates(state)
        return [tool.listing() for tool in TOOLS if gates[tool.gate] and not (self.read_only and tool.kind != "look")]

    def listed(self, tools: list) -> None:
        """Note the tools the assistant was just given, and from then on watch for them going stale."""
        with self.told_lock:
            self.told = json.dumps(tools, sort_keys=True)
            if self.watcher is None:
                self.watcher = threading.Thread(target=self.watch, daemon=True)
                self.watcher.start()

    def watch(self) -> None:
        """Tell the assistant once each time the tools SimRack would offer stop matching the ones it last got,
        for example when someone pauses SimRack or allows risky tools on the setup page."""
        while not self.stopping.wait(self.poll):
            now = json.dumps(self.offered(self.refresh()), sort_keys=True)
            with self.told_lock:
                changed, self.told = now != self.told, now
            if changed:
                self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    def refusal(self, tool: Tool) -> str:
        """Why SimRack would not let an assistant use the tool now, judged on fresh state; empty when it would.
        SimRack's API refuses changes itself, but not the risky tools: only this check holds those back."""
        if self.read_only and tool.kind != "look":
            return PINNED
        if tool.gate == "look":
            return ""
        try:
            state = self.http("GET", "/api/state", timeout=LOOK_TIMEOUT)
        except (Refused, Unreachable) as problem:
            return str(problem)
        gates = self.gates(state)
        if gates[tool.gate]:
            return ""
        if tool.gate == "mist-look":
            return NO_MIST
        if not gates["lab"]:
            return state.get("read_only_reason") or "SimRack may not change anything right now."
        if tool.gate.startswith("risky") and not gates["risky"]:
            return RISKY_OFF
        return (state.get("mist") or {}).get("read_only_reason") or "SimRack may not change Mist right now."

    def answer(self, ident, tool: Tool, arguments: dict) -> None:
        try:
            result = self.call(tool, arguments)
        except Exception as error:  # A fault here must not leave the assistant waiting for an answer for ever.
            traceback.print_exc(file=sys.stderr)
            result = trouble(f"SimRack's MCP server failed while running {tool.name}: {error!r}. SimRack's page shows what was done.")
        self.reply(ident, result)

    def call(self, tool: Tool, arguments: dict) -> dict:
        problems = misfits(tool, arguments)
        if problems:
            return trouble(" ".join(problems))
        if tool.name == "job_result":
            return self.follow(arguments["job"], "")
        return self.use(tool, arguments)

    def use(self, tool: Tool, arguments: dict) -> dict:
        """Have SimRack do what the tool does, once SimRack allows it. SimRack runs a change as a job."""
        refusal = self.refusal(tool)
        if refusal:
            return trouble(refusal)
        in_path = {field for _, field, _, _ in string.Formatter().parse(tool.path) if field}
        path = tool.path.format(**{field: urllib.parse.quote(str(arguments[field]), safe="") for field in in_path})
        body = {**{field: value for field, value in arguments.items() if field not in in_path}, **tool.body}
        try:
            if tool.method == "GET":
                said = self.http("GET", path, timeout=LOOK_TIMEOUT)
                return self.gated(said) if tool.name == "access_check" else fine(said)
            status, said = self.exchange("POST", path, body, timeout=LOOK_TIMEOUT, headers={"Prefer": "respond-async"})
        except (Refused, Unreachable) as problem:
            return trouble(str(problem))
        if status != 202:  # SimRack answers at once when there is nothing to wait for, like pausing.
            return fine(said)
        job = said.get("job") if isinstance(said, dict) else None
        if not isinstance(job, str):
            return trouble("SimRack took the change but did not say which job it is. Update SimRack.")
        return self.follow(job, tool.name)

    def follow(self, job: str, name: str) -> dict:
        """How the job ended, or, if it is still running when this call has waited long enough, the job to ask about."""
        deadline = time.monotonic() + self.wait
        while True:
            ask = min(max(deadline - time.monotonic(), 0.0), LONGEST_WAIT)
            try:
                said = self.http("GET", f"/api/jobs/{urllib.parse.quote(job, safe='')}?wait={ask:.3f}", timeout=ask + JOB_SLACK)
            except Refused as problem:
                return trouble(str(problem))
            except Unreachable as problem:
                return trouble(f"{problem} SimRack may still be working on it: call job_result with job {job} to ask again.")
            state = said.get("state") if isinstance(said, dict) else None
            if state == "done":
                status, result = said.get("status"), said.get("result")
                if not isinstance(status, int) or status >= 400:
                    return trouble(complaint(result, f"SimRack answered {status}."))
                return fine(result)
            if state != "running":
                return trouble(f"SimRack's answer about job {job} is not one this server can read. Update SimRack.")
            # SimRack waits at most LONGEST_WAIT per ask; only a call allowed to wait longer asks again.
            if ask < LONGEST_WAIT:
                return plain(
                    f"Still running: SimRack carries on with {name or said.get('what', 'the change')}. "
                    f"Call job_result with job {job} to wait for how it ends."
                )

    def gated(self, said: dict) -> dict:
        """SimRack's answer in Casper's contract, with whether this server lets changes through."""
        products = said.get("products") if isinstance(said, dict) else None
        if not isinstance(products, list) or not all(isinstance(product, dict) for product in products):
            return trouble("SimRack's access answer is not one Casper can read. Update SimRack.")
        gate = {"flag": "--read-only", "state": "off" if self.read_only else "on"}
        answer = {"contract": ACCESS_CONTRACT, "products": [{**product, "server_gate": gate} for product in products]}
        return fine(answer)

    def http(self, method: str, path: str, body: dict | None = None, *, timeout: float) -> dict:
        """SimRack's JSON answer. Raises Refused, in SimRack's own words, or Unreachable."""
        return self.exchange(method, path, body, timeout=timeout)[1]

    def exchange(self, method: str, path: str, body: dict | None = None, *, timeout: float, headers: dict | None = None) -> tuple:
        """SimRack's status and JSON answer, for when how SimRack took the request matters."""
        headers = {"Accept": "application/json", **(headers or {})}
        data = None
        if method == "POST":
            data = json.dumps(body or {}).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        if self.token:
            request.add_unredirected_header("Authorization", f"Bearer {self.token}")
        try:
            with self.opener.open(request, timeout=timeout) as reply:
                return reply.status, json.loads(reply.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                try:
                    problem = json.loads(error.read())
                except (OSError, ValueError):
                    problem = {}
            raise Refused(complaint(problem, f"SimRack answered {error.code} {error.reason}.")) from error
        except (OSError, ValueError) as error:
            raise Unreachable(f"SimRack is not answering at {self.url}: {getattr(error, 'reason', error)}.") from error

    def initialize(self, params: dict) -> dict:
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "simrack", "version": __version__},
            "instructions": INSTRUCTIONS + (PINNED_NOTE if self.read_only else ""),
        }

    def reply(self, ident, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": ident, "result": result})

    def fail(self, ident, code: int, message: str) -> None:
        self.send({"jsonrpc": "2.0", "id": ident, "error": {"code": code, "message": message}})

    def send(self, message: dict) -> None:
        line = json.dumps(message, separators=(",", ":"))
        with self.out_lock:
            self.out.write(line + "\n")
            self.out.flush()


def main(url: str, *, wait: float = 50.0, poll: float = 15.0, read_only: bool = False) -> int:
    server = Server(url, token=os.environ.get("SIMRACK_TOKEN", ""), wait=wait, poll=poll, read_only=read_only)
    server.run(iter(sys.stdin.buffer.readline, b""))
    return 0
