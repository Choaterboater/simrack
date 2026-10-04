"""Whether SimRack may change the lab. The tokens decide; nothing in the environment does.

A lab profile must say what is live first. Then Proxmox says what its token may
do and Mist says what its token may do. A change is offered only when all of
them allow it. The operator can pause every change; the pause outlives a restart.

What a token may do is asked at most every CACHE_SECONDS, so the status view's
polling does not hammer Proxmox or Mist. The profile and the pause are read live.
Proxmox and Mist still check every request themselves.
"""

from __future__ import annotations

import os
import shlex
import time
from typing import Callable

from .config import NO_PROFILE, Settings
from .errors import BackendError, GuardrailViolation, LabError, NotConfigured

#: A refusal: the error to raise, its message and its detail.
Refusal = tuple[type[LabError], str, str]

CACHE_SECONDS = 30.0

PAUSED: Refusal = (GuardrailViolation, "Changes are paused.", "Resume them from the top bar when you are ready.")

#: Mist roles that may change an org: Super User and Network Admin.
MIST_WRITE_ROLES = ("admin", "write")

#: What SimRack does to a sandbox guest, as Proxmox checks it: create, clone,
#: delete, set memory, CPU, disks, CD-ROM, NICs and options, power, snapshot.
#: VM.Snapshot also allows rollback.
VM_PRIVILEGES = (
    "VM.Allocate",
    "VM.Audit",
    "VM.Clone",
    "VM.Config.CDROM",
    "VM.Config.CPU",
    "VM.Config.Disk",
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.PowerMgmt",
    "VM.Snapshot",
)


def required_privileges(settings: Settings) -> dict[str, tuple[str, ...]]:
    """The privileges SimRack's Proxmox token needs, by ACL path.

    Sandbox bridges are made on the fly, so SDN.Use must come from the zone.
    Bridges are host-only (hostnet), so the node needs no Sys.Modify.
    """
    mgmt = f"/sdn/zones/localnetwork/{settings.mgmt_bridge}"
    if settings.mgmt_vlan is not None:
        mgmt += f"/{settings.mgmt_vlan}"
    return {
        f"/vms/{settings.sandbox_vmid_start}": VM_PRIVILEGES,
        f"/nodes/{settings.pve_node}": ("Sys.Audit",),
        "/storage/local-lvm": ("Datastore.AllocateSpace",),
        "/storage/local": ("Datastore.Audit",),
        "/sdn/zones/localnetwork": ("SDN.Use",),
        f"/sdn/zones/localnetwork/{settings.park_bridge}": ("SDN.Use",),
        mgmt: ("SDN.Use",),
    }


def token_commands(settings: Settings) -> list[str]:
    """Commands, run as root on the host, for a token that may do just what SimRack checks.

    One role holds every privilege SimRack needs, granted where it checks them.
    Sandbox guests do not exist yet, so theirs go on /vms; a grant reaches the paths below it.
    """
    needed = required_privileges(settings)
    privileges = sorted({privilege for held in needed.values() for privilege in held})
    paths = {"/vms" if path.startswith("/vms/") else path for path in needed}
    grants = sorted(path for path in paths if not any(path.startswith(other + "/") for other in paths))
    return [
        shlex.join(["pveum", "role", "add", "SimRack", "--privs", " ".join(privileges)]),
        shlex.join(["pveum", "user", "add", "simrack@pve"]),
        *(shlex.join(["pveum", "acl", "modify", path, "--users", "simrack@pve", "--roles", "SimRack"]) for path in grants),
        shlex.join(["pveum", "user", "token", "add", "simrack@pve", "simrack", "--privsep", "0", "--output-format", "json"])
        + """ | python3 -c 'import json, sys; t = json.load(sys.stdin); print(t["full-tokenid"] + "=" + t["value"])'""",
    ]


class Access:
    def __init__(self, settings: Settings, proxmox, mist, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.settings = settings
        self.proxmox = proxmox
        self.mist = mist
        self._clock = clock
        self._asked: dict[str, tuple[float, Refusal | None]] = {}

    def forget(self) -> None:
        """Ask Proxmox and Mist again next time, as after a token changes."""
        self._asked.clear()

    def _remember(self, key: str, ask: Callable[[], Refusal | None]) -> Refusal | None:
        now = self._clock()
        if key in self._asked and now - self._asked[key][0] < CACHE_SECONDS:
            return self._asked[key][1]
        refusal = ask()
        self._asked[key] = (now, refusal)
        return refusal

    # -- pause ------------------------------------------------------------------

    def _pause_marker(self) -> str:
        return os.path.join(self.settings.state_dir, "paused")

    def paused(self) -> bool:
        return os.path.exists(self._pause_marker())

    def set_paused(self, paused: bool) -> None:
        marker = self._pause_marker()
        if not paused:
            if os.path.exists(marker):
                os.remove(marker)
            return
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%SZ\n", time.gmtime()))

    # -- the lab (Proxmox) ------------------------------------------------------

    def lab_refusal(self) -> Refusal | None:
        """Why SimRack may not change the lab, or None when it may."""
        if not self.settings.profile_path:
            return (GuardrailViolation, "SimRack is read-only: no lab profile is loaded.", NO_PROFILE)
        if self.paused():
            return PAUSED
        if not self.settings.pve_token:
            return (NotConfigured, "Proxmox API token is not set.", "Add one on SimRack's setup page.")
        return self._remember("proxmox", self._ask_proxmox)

    def _ask_proxmox(self) -> Refusal | None:
        lacking = {}
        try:
            for path, needed in required_privileges(self.settings).items():
                held = self.proxmox.permissions(path)
                if missing := [privilege for privilege in needed if privilege not in held]:
                    lacking[path] = missing
        except LabError as error:
            return (BackendError, "SimRack cannot tell what the Proxmox token may do.", f"{error.message} {error.detail}".strip())
        if lacking:
            return (
                GuardrailViolation,
                "SimRack is read-only: the Proxmox token may not change the lab.",
                " ".join(f"It lacks {', '.join(privileges)} on {path}." for path, privileges in lacking.items())
                + " The setup page shows how to make a token that may.",
            )
        return None

    def check_lab(self) -> None:
        raise_if(self.lab_refusal())

    # -- Mist -------------------------------------------------------------------

    def mist_refusal(self) -> Refusal | None:
        """Why SimRack may not change Mist, or None when it may."""
        if not self.settings.profile_path:
            return (GuardrailViolation, "Mist changes are off: no lab profile is loaded.", NO_PROFILE)
        if self.paused():
            return PAUSED
        if not self.mist.configured():
            return (
                NotConfigured,
                "Mist API token is not set.",
                "Add a Mist token on SimRack's setup page. Without one every Mist action is off.",
            )
        return self._remember("mist", self._ask_mist)

    def _ask_mist(self) -> Refusal | None:
        try:
            me = self.mist.whoami()
        except LabError as error:
            return (BackendError, "SimRack cannot tell what the Mist token may do.", f"{error.message} {error.detail}".strip())
        org = self.settings.org_id
        roles = [
            privilege.get("role")
            for privilege in (me.get("privileges") if isinstance(me, dict) else None) or []
            if isinstance(privilege, dict) and privilege.get("scope") == "org" and (not org or privilege.get("org_id") == org)
        ]
        if any(role in MIST_WRITE_ROLES for role in roles):
            return None
        held = f"its role on this org is {' and '.join(sorted(set(map(str, roles))))}" if roles else "it has no role on this org"
        return (
            GuardrailViolation,
            f"Mist changes are off: {held}.",
            "Mist lets the admin (Super User) and write (Network Admin) roles change an org. "
            "The setup page shows how to make a token that may.",
        )

    def check_mist(self) -> None:
        raise_if(self.mist_refusal())


def raise_if(refusal: Refusal | None) -> None:
    if refusal:
        error, message, detail = refusal
        raise error(message, detail=detail)


def describe(refusal: Refusal | None) -> str:
    """A refusal as one line for the status view; empty when allowed."""
    return " ".join(refusal[1:]) if refusal else ""
