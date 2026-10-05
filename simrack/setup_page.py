"""SimRack's setup page: the tokens and the lab profile, saved in the state folder.

Nothing here comes from the environment. The tokens file is readable by
SimRack's own user only, and a token is never sent back: the page sees only
whether each one is set, and the Proxmox token's name.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import re
import tomllib
import urllib.parse

from .access import PROXMOX_TOKEN, token_commands
from .config import PROFILE_FILE, TOKENS_FILE, Settings
from .errors import GuardrailViolation, LabError, NotFound
from .profile import ProfileError, dump_profile, profile_values, without_retired
from .service import PORT_KINDS

FIX_ON_THE_PAGE = "Fix it on the setup page and save again. Nothing was saved."
FIX_THE_FILE = "Fix the file and import it again. Nothing was saved."

#: A Mist cloud's API, as Juniper lists them: api.mist.com, api.eu.mist.com, api.gc1.mist.com and so on.
MIST_API = re.compile(r"https://api(\.[a-z0-9-]+)?\.mist\.com/api/v1/?")


class SetupPage:
    def __init__(self, manager) -> None:
        self.manager = manager

    def page(self) -> dict:
        settings = self.manager.settings
        problems: list[dict] = list(settings.problems)
        found, bridges, org_id = self._found(problems)
        saved = self._saved()
        return {
            "done": bool(settings.profile_path),
            "profile": saved if saved is not None else self._suggested(found, bridges, org_id),
            "found": found,
            "problems": problems,
            "tokens": {
                "proxmox": {"set": bool(settings.pve_token), "id": settings.pve_token.split("=", 1)[0], "api": settings.pve_api_base},
                "mist": {"set": bool(settings.mist_token), "api": settings.mist_api_base},
            },
            "proxmox_token_commands": token_commands(settings),
        }

    def _saved(self) -> dict | None:
        """The profile saved, even one with a problem to fix; None if there is none to read."""
        try:
            with open(os.path.join(self.manager.settings.state_dir, PROFILE_FILE), "rb") as handle:
                return tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError):
            return None

    def _found(self, problems: list[dict]) -> tuple[dict, list[dict], str]:
        """What Proxmox and Mist show now, each with a label, and not what SimRack built.
        What they cannot show yet is a problem to fix first."""
        settings = self.manager.settings
        found: dict = {"vmids": [], "lxc": [], "bridges": [], "subnets": [], "mist_sites": []}
        bridges: list[dict] = []
        try:
            bridges = [bridge for bridge in self.manager.proxmox.bridges() if not bridge["iface"].startswith((settings.sandbox_bridge_prefix, settings.park_bridge))]
            found.update(self._on_the_host(bridges))
        except LabError as error:
            problems.append(error.as_dict())
        org_id = settings.org_id
        try:
            org_id = self._org()
            ours = {sandbox.mist_site_id for sandbox in self.manager.sandboxes.values()}
            sites = self.manager.mist.sites(org_id) if org_id else []
            found["mist_sites"] = sorted(({"id": site["id"], "label": site.get("name", "")} for site in sites if site["id"] not in ours), key=lambda item: item["id"])
        except LabError as error:
            problems.append(error.as_dict())
        return found, bridges, org_id

    def _suggested(self, found: dict, bridges: list[dict], org_id: str) -> dict:
        """What to set: protect everything found."""
        settings = self.manager.settings
        protected = {key: [item["id"] for item in items] for key, items in found.items()}
        return {
            "proxmox": {"node": settings.pve_node},
            "mist": {"org_id": org_id or None},
            "management": _management(bridges, settings),
            "protected": protected,
            "sandbox": _sandbox(set(protected["vmids"]) | set(protected["lxc"]), settings),
        }

    def _org(self) -> str:
        """The org already set, or else the one org the Mist token has a role on."""
        mist = self.manager.mist
        if self.manager.settings.org_id or not mist.configured():
            return self.manager.settings.org_id
        orgs = {grant["org_id"] for grant in mist.whoami().get("privileges", []) if grant.get("scope") == "org" and grant.get("org_id")}
        return orgs.pop() if len(orgs) == 1 else ""

    def _on_the_host(self, bridges: list[dict]) -> dict:
        """Everything already on the host, to keep SimRack away from it. A template
        is left out, since SimRack only clones it, and so is what SimRack built."""
        proxmox = self.manager.proxmox
        ours = {node.vmid for sandbox in self.manager.sandboxes.values() for node in sandbox.nodes}
        subnets: dict = {}
        for bridge in bridges:
            if bridge.get("cidr"):
                subnets.setdefault(ipaddress.ip_interface(bridge["cidr"]).network, []).append(bridge["iface"])

        def guests(listed) -> list[dict]:
            return sorted(({"id": int(guest["vmid"]), "label": guest.get("name", "")} for guest in listed if int(guest["vmid"]) not in ours), key=lambda item: item["id"])

        return {
            "vmids": guests(vm for vm in proxmox.list_vms() if not vm.get("template")),
            "lxc": guests(proxmox.list_lxc()),
            "bridges": sorted(({"id": bridge["iface"], "label": bridge.get("cidr", "")} for bridge in bridges), key=lambda item: item["id"]),
            "subnets": [{"id": str(net), "label": ", ".join(sorted(subnets[net]))} for net in sorted(subnets, key=lambda net: (net.version, net))],
        }

    def save(self, body: dict) -> dict:
        """Check the profile the page sends, or a file imported there, keep it, and start using it."""
        imported = isinstance(body.get("toml"), str)
        fix = FIX_THE_FILE if imported else FIX_ON_THE_PAGE
        try:
            raw = tomllib.loads(body["toml"]) if imported else body.get("profile")
        except tomllib.TOMLDecodeError as error:
            raise ProfileError(f"The imported file is not valid TOML: {error}.", detail=fix) from error
        if not isinstance(raw, dict):
            raise ProfileError("Send the lab profile as {\"profile\": {section: {key: value}}}.")
        raw = {section: {key: value for key, value in keys.items() if value is not None} if isinstance(keys, dict) else keys for section, keys in raw.items()}
        raw, left_out = without_retired(raw)
        values = profile_values(raw, hint=fix)
        partly_protected = self._keeps_every_sandbox(dataclasses.replace(self.manager.settings, **values), "import the file again" if imported else "save again")
        text = dump_profile(raw)
        profile_values(tomllib.loads(text), "as written")
        _write_private(os.path.join(self.manager.settings.state_dir, PROFILE_FILE), text)
        self.reload()
        return {**self.page(), "left_out": left_out, "partly_protected": partly_protected}

    def _keeps_every_sandbox(self, after: Settings, again: str) -> list[dict]:
        """Refuse settings that would strand a sandbox SimRack built, so teardown could no
        longer remove it: a guest outside the sandbox range, a cable bridge without the
        prefix, or switches parked on a bridge SimRack no longer uses. A part the new
        profile protects is left alone instead; it is named, not refused, since it may
        be a live guest now."""
        park_now = self.manager.settings.park_bridge
        stranded: dict[str, list[str]] = {}
        partly: list[dict] = []
        for sandbox in sorted(self.manager.sandboxes.values(), key=lambda sandbox: sandbox.name):
            problems: list[str] = []
            parts: list[str] = []
            for node in sandbox.nodes:
                if node.vmid in after.production_vmids or node.vmid in after.production_lxc:
                    parts.append(f"{node.name} (vmid {node.vmid})")
                elif not after.sandbox_vmid_start <= node.vmid <= after.sandbox_vmid_end:
                    problems.append(f"{node.name} is vmid {node.vmid}, outside {after.sandbox_vmid_start}-{after.sandbox_vmid_end}")
            for link in sandbox.links:
                if link.bridge in after.production_bridges:
                    parts.append(f"bridge {link.bridge}")
                elif not link.bridge.startswith(after.sandbox_bridge_prefix):
                    problems.append(f"bridge {link.bridge} does not start with {after.sandbox_bridge_prefix!r}")
            if sandbox.mist_site_id and sandbox.mist_site_id in after.production_mist_sites:
                parts.append(f"Mist site {sandbox.mist_site_id}")
            if after.park_bridge != park_now and any(node.kind in PORT_KINDS for node in sandbox.nodes):
                problems.append(f"its switches park unused ports on {park_now}")
            if problems:
                stranded[sandbox.name] = problems
            if parts:
                partly.append({"sandbox": sandbox.name, "parts": parts})
        if stranded:
            it = "it" if len(stranded) == 1 else "them"
            raise GuardrailViolation(
                f"This profile would strand {' and '.join(stranded)}: SimRack could no longer tear {it} down.",
                detail=" ".join(f"{name}: {'; '.join(problems)}." for name, problems in stranded.items())
                + f" Tear {it} down first, then {again}. Nothing was saved.",
            )
        return partly

    def export(self) -> dict:
        """The saved lab profile as TOML, to keep or to import on another SimRack."""
        if not self.manager.settings.profile_path:
            raise NotFound("No lab profile is saved yet.")
        with open(self.manager.settings.profile_path, encoding="utf-8") as handle:
            return {"toml": handle.read()}

    def save_tokens(self, given: dict) -> dict:
        """Save the tokens given, each with the address it may be sent to; a blank
        value keeps what is already saved."""
        settings = self.manager.settings
        tokens = {"proxmox": settings.pve_token, "proxmox_api": settings.pve_api_base, "mist": settings.mist_token, "mist_api": settings.mist_api_base}
        pasted = set()
        for name in tokens:
            value = given.get(name)
            if isinstance(value, str) and value.strip():
                tokens[name] = value.strip()
                pasted.add(name)
        _check_addresses(tokens["proxmox_api"], tokens["mist_api"])
        for name, label, now in (("proxmox", "Proxmox", settings.pve_api_base), ("mist", "Mist", settings.mist_api_base)):
            if tokens[f"{name}_api"] != now and name not in pasted:
                raise LabError(
                    f"Paste the {label} token along with its new address, {tokens[f'{name}_api']}. "
                    "SimRack never sends a saved token to a new address unless it is pasted again.",
                    detail=FIX_ON_THE_PAGE,
                )
        if tokens["proxmox"] and not PROXMOX_TOKEN.fullmatch(tokens["proxmox"]):
            raise LabError("The Proxmox token must be its ID and secret as user@realm!name=secret.", detail=FIX_ON_THE_PAGE)
        if any(character.isspace() for character in tokens["mist"]):
            raise LabError("The Mist token is the key alone, with no spaces and no word such as Token before it.", detail=FIX_ON_THE_PAGE)
        _write_private(os.path.join(settings.state_dir, TOKENS_FILE), json.dumps(tokens))
        self.reload()
        return {"tokens": self.page()["tokens"]}

    def reload(self) -> None:
        """Take what is saved now, without a restart."""
        current = self.manager.settings
        self.manager.use(Settings.load({"SIMRACK_STATE_DIR": current.state_dir, "SIMRACK_TOKEN": current.extras.get("token", "")}))


def _management(bridges: list[dict], settings) -> dict:
    """The host reaches its gateway over this bridge, so the sandboxes' management
    goes there too. The pool sits high in the subnet, .200-.249 of its last /24,
    stepping down 50 at a time past the host's own address and the gateway."""
    uplink = next((bridge for bridge in bridges if bridge.get("gateway") and bridge.get("cidr")), None)
    if uplink is None:
        return {"bridge": settings.mgmt_bridge}
    host = ipaddress.ip_interface(uplink["cidr"])
    taken = {host.ip, ipaddress.ip_address(uplink["gateway"])}
    suggestion = {"bridge": uplink["iface"], "cidr": str(host.network)}
    if host.network.num_addresses >= 256:
        last_24 = host.network.broadcast_address - 255
        for first in (last_24 + 200, last_24 + 150, last_24 + 100, last_24 + 50):
            if not any(first <= address <= first + 49 for address in taken):
                suggestion["pool"] = f"{first}-{first + 49}"
                break
    return suggestion


def _sandbox(taken: set[int], settings) -> dict:
    """The sandbox ranges, 100 higher at a time until none of the host's own guests is in them."""
    for step in range(0, 100_000, 100):
        first, last = settings.sandbox_vmid_start + step, settings.sandbox_vmid_end + step
        if not any(first <= vmid <= last for vmid in taken):
            return {"vmids": [first, last], "lxc": [settings.sandbox_lxc_start + step, settings.sandbox_lxc_end + step]}
    return {}


def _check_addresses(proxmox: str, mist: str) -> None:
    """A token goes over https only, to a plain API address, and a Mist token only to a Mist cloud."""
    parts = urllib.parse.urlsplit(proxmox)
    if parts.scheme != "https" or not parts.hostname or "@" in parts.netloc or parts.path.rstrip("/") != "/api2/json" or parts.query or parts.fragment:
        raise LabError(f"The Proxmox address must be https://<host>:<port>/api2/json with nothing else in it, not {proxmox}.", detail=FIX_ON_THE_PAGE)
    if not MIST_API.fullmatch(mist):
        raise LabError(f"The Mist address must be a Mist cloud's API, such as https://api.mist.com/api/v1 or https://api.eu.mist.com/api/v1, not {mist}.", detail=FIX_ON_THE_PAGE)


def _write_private(path: str, text: str) -> None:
    """Write a file only its owner may read, replacing any old one whole."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(text)
    os.replace(tmp, path)
