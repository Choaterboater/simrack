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
import re
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
#: Mist roles that may only look: Observer, Helpdesk and Installer, as Casper counts them.
MIST_READ_ROLES = ("read", "helpdesk", "installer")
#: Where a user token's role can sit above the org, and how a refusal names it.
MIST_ABOVE_ORG = {"msp": "through its MSP", "orggroup": "through an org group"}

#: A Proxmox API token as the setup page takes it: user@realm!name=secret.
PROXMOX_TOKEN = re.compile(r"(?P<user>[^\s=:/]+@[A-Za-z][\w.-]*)![A-Za-z][\w.-]*=\S+")

#: What Casper's access-check v2 shows: the kinds of place, how many, and the text it will print.
SCOPE_KINDS = ("org", "site", "sitegroup")
MAX_SCOPES = 64
PLAIN = re.compile(r"[A-Za-z0-9 _.@:/+-]{1,64}")

#: The Proxmox resource pool every sandbox guest is made in. SimRack's token may
#: change only the guests in it, so it can never touch a live one.
POOL = "simrack"

#: What SimRack does to a sandbox guest, as Proxmox checks it: create (or clone
#: into the pool), delete, set memory, CPU, disks, CD-ROM, NICs and options,
#: power, snapshot. VM.Snapshot also allows rollback. Cloning also needs VM.Clone
#: on the template, granted per template.
VM_PRIVILEGES = (
    "VM.Allocate",
    "VM.Audit",
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

    Guests are changed only through SimRack's pool, so a live guest is never
    reachable; every guest is only looked at. Sandbox bridges are made on the
    fly, so SDN.Use must come from the zone. Bridges are host-only (hostnet), so
    the node needs no Sys.Modify.
    """
    mgmt = f"/sdn/zones/localnetwork/{settings.mgmt_bridge}"
    if settings.mgmt_vlan is not None:
        mgmt += f"/{settings.mgmt_vlan}"
    return {
        f"/pool/{POOL}": VM_PRIVILEGES,
        "/vms": ("VM.Audit",),
        f"/nodes/{settings.pve_node}": ("Sys.Audit",),
        "/storage/local-lvm": ("Datastore.AllocateSpace",),
        "/storage/local": ("Datastore.Audit",),
        "/sdn/zones/localnetwork": ("SDN.Use",),
        f"/sdn/zones/localnetwork/{settings.park_bridge}": ("SDN.Use",),
        mgmt: ("SDN.Use",),
    }


def token_commands(settings: Settings) -> list[str]:
    """Commands, run as root on the host, for a token that may do just what SimRack checks.

    SimRack's role goes on its pool, so the token may change only the guests made
    there. Everywhere else it gets Proxmox's smallest built-in role: a look at
    every guest, the node and the ISO store; space on local-lvm; the zone's
    bridges, since sandbox bridges are named only when a sandbox is built. Each
    template on the node may be cloned.
    """
    node = f"/nodes/{settings.pve_node}"
    grants = (
        (f"/pool/{POOL}", "SimRack"),
        ("/vms", "PVEAuditor"),
        (node, "PVEAuditor"),
        ("/storage/local", "PVEAuditor"),
        ("/storage/local-lvm", "PVEDatastoreUser"),
        ("/sdn/zones/localnetwork", "PVESDNUser"),
    )
    templates = (
        shlex.join(["pvesh", "get", f"{node}/qemu", "--output-format", "json"])
        + """ | python3 -c 'import json, sys; print(*(vm["vmid"] for vm in json.load(sys.stdin) if vm.get("template")))'"""
    )
    return [
        shlex.join(["pveum", "pool", "add", POOL]),
        shlex.join(["pveum", "role", "add", "SimRack", "--privs", " ".join(VM_PRIVILEGES)]),
        shlex.join(["pveum", "user", "add", "simrack@pve"]),
        *(shlex.join(["pveum", "acl", "modify", path, "--users", "simrack@pve", "--roles", role]) for path, role in grants),
        f'for id in $({templates}); do {clone_command("$id")}; done',
        shlex.join(["pveum", "user", "token", "add", "simrack@pve", "simrack", "--privsep", "0", "--output-format", "json"])
        + """ | python3 -c 'import json, sys; t = json.load(sys.stdin); print(t["full-tokenid"] + "=" + t["value"])'""",
    ]


def clone_command(template: int | str) -> str:
    """The command, run as root on the host, that lets SimRack's token clone a template."""
    return f"pveum acl modify /vms/{template} --users simrack@pve --roles PVETemplateUser"


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

    def check_clone(self, template_vmid: int) -> None:
        """Refuse a build from a template the token may not clone, before anything is made.

        Asked each time: templates come and go, and each is granted on its own.
        """
        if "VM.Clone" not in self.proxmox.permissions(f"/vms/{template_vmid}"):
            raise GuardrailViolation(
                f"The Proxmox token may not clone template {template_vmid}.",
                detail=f"Run this as root on the Proxmox host, then build again: {clone_command(template_vmid)}",
            )

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
        if not self.settings.org_id:
            return (
                GuardrailViolation,
                "Mist changes are off: no Mist org is set.",
                "Set the Mist org ID on SimRack's setup page; SimRack changes only that org.",
            )
        return self._remember("mist", self._ask_mist)

    def _ask_mist(self) -> Refusal | None:
        try:
            me = self.mist.whoami()
        except LabError as error:
            return cannot_tell(error)
        privileges = [
            privilege for privilege in (me.get("privileges") if isinstance(me, dict) else None) or [] if isinstance(privilege, dict)
        ]
        held = [
            (privilege.get("role"), "")
            for privilege in privileges
            if privilege.get("scope") == "org" and privilege.get("org_id") == self.settings.org_id
        ]
        above = [privilege for privilege in privileges if privilege.get("scope") in MIST_ABOVE_ORG]
        if above and not any(role in MIST_WRITE_ROLES for role, _ in held):
            try:
                org = self.mist.org(self.settings.org_id)
            except BackendError as error:
                if error.status not in (403, 404):
                    return cannot_tell(error)
                org = {}  # Mist hides an org from a token with no role on it.
            except LabError as error:
                return cannot_tell(error)
            held += [(privilege.get("role"), MIST_ABOVE_ORG[privilege["scope"]]) for privilege in above if holds(privilege, org)]
        if any(role in MIST_WRITE_ROLES for role, _ in held):
            return None
        roles = sorted({f"{role} {where}".strip() for role, where in held})
        said = f"its role on this org is {' and '.join(roles)}" if roles else "it has no role on this org"
        return (
            GuardrailViolation,
            f"Mist changes are off: {said}.",
            "Mist lets the admin (Super User) and write (Network Admin) roles change an org, held on the org, "
            "its MSP or an org group it is in. The setup page shows how to make a token that may.",
        )

    def check_mist(self) -> None:
        raise_if(self.mist_refusal())

    # -- what the tokens may do, for an assistant's host -------------------------

    def products(self) -> list[dict]:
        """What each token may do, in Casper's access-check v2: who it is and where it may change things.

        This is the tokens' own reach. A pause stops SimRack, not the tokens, so it
        does not count here. What SimRack cannot find out is unknown, never a guess.
        """
        return [self._proxmox_product(), self._mist_product()]

    def _proxmox_product(self) -> dict:
        """Read-write when the token holds every privilege SimRack needs; read-only when Proxmox says it lacks one.

        No role: Proxmox does not say which role gave the token its privileges.
        """
        product = {"product": "proxmox", "access": "unknown"}
        if not self.settings.pve_token:
            return {**product, "login": "missing"}
        if not self.settings.profile_path:
            # Only the profile names the node, bridges and storage the token needs.
            return product
        refusal = self._remember("proxmox", self._ask_proxmox)
        if refusal is not None and refusal[0] is not GuardrailViolation:
            return product
        product["access"] = "read-only" if refusal else "read-write"
        if user := proxmox_user(self.settings.pve_token):
            product["identity"] = user
        return product

    def _mist_product(self) -> dict:
        """Every place the token reaches, in any org, not only the profile's: asked afresh each time."""
        product = {"product": "mist", "access": "unknown"}
        if not self.mist.configured():
            return {**product, "login": "missing"}
        try:
            me = self.mist.whoami()
        except LabError:
            return product
        privileges = me.get("privileges") if isinstance(me, dict) else None
        if not isinstance(privileges, list):
            return product
        if plain(me.get("email")):
            product["identity"] = me["email"]
        roles = [privilege.get("role") if isinstance(privilege, dict) else None for privilege in privileges]
        if any(role not in MIST_WRITE_ROLES + MIST_READ_ROLES for role in roles):
            return product
        changes = [privilege for privilege, role in zip(privileges, roles) if role in MIST_WRITE_ROLES]
        looks = [privilege for privilege, role in zip(privileges, roles) if role in MIST_READ_ROLES]
        product["access"] = "read-write" if changes else "read-only"
        if len(set(roles)) == 1:
            product["role"] = roles[0]
        for key, chosen in (("can_change", changes), ("read_only", looks)):
            if places := scope_list(chosen):
                product[key] = places
        return product


def cannot_tell(error: LabError) -> Refusal:
    return (BackendError, "SimRack cannot tell what the Mist token may do.", f"{error.message} {error.detail}".strip())


def holds(privilege: dict, org: dict) -> bool:
    """Whether a role held on an MSP or an org group reaches the org, as GET /orgs/{id} names them."""
    if privilege.get("scope") == "msp":
        return bool(ids(privilege.get("msp_id")) & ids(org.get("msp_id")))
    if privilege.get("scope") == "orggroup":
        return bool((ids(privilege.get("orggroup_ids")) | ids(privilege.get("orggroup_id"))) & ids(org.get("orggroup_ids")))
    return False


def ids(value) -> set[str]:
    """The IDs a Mist field holds, whether it is one ID or a list of them."""
    return {item for item in (value if isinstance(value, list) else [value]) if isinstance(item, str) and item}


def plain(value) -> bool:
    """Whether Casper will show the text as it is."""
    return isinstance(value, str) and PLAIN.fullmatch(value) is not None


def proxmox_user(token: str) -> str:
    """The Proxmox user a token belongs to, never its secret; empty when the token is not one SimRack takes."""
    match = PROXMOX_TOKEN.fullmatch(token or "")
    return match["user"] if match and plain(match["user"]) else ""


def scope_list(privileges: list[dict]) -> list[dict]:
    """The places the Mist privileges reach, as Casper lists them; empty unless every one can be listed whole."""
    if len(privileges) > MAX_SCOPES:
        return []
    places = []
    for privilege in privileges:
        kind = privilege.get("scope")
        if kind not in SCOPE_KINDS:
            return []
        place = {"kind": kind, "id": privilege.get(f"{kind}_id"), "name": privilege.get("name")}
        if not (plain(place["id"]) and plain(place["name"])):
            return []
        places.append(place)
    return places


def raise_if(refusal: Refusal | None) -> None:
    if refusal:
        error, message, detail = refusal
        raise error(message, detail=detail)


def describe(refusal: Refusal | None) -> str:
    """A refusal as one line for the status view; empty when allowed."""
    return " ".join(refusal[1:]) if refusal else ""
