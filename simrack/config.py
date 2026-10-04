"""Static limits and defaults for the lab front end.

What is live on this host (guests, bridges, Mist sites, subnets) comes from the
lab profile, never from here: see profile.py and lab-profile.example.toml.
"""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass, field

from .errors import LabError

# --- Sandbox boundaries. -------------------------------------------------------

#: Sandbox VMIDs live in this range only, unless the lab profile moves it.
SANDBOX_VMID_START = 320
SANDBOX_VMID_END = 399

#: Sandbox bridges are named with this prefix. Everything else is refused.
SANDBOX_BRIDGE_PREFIX = "sbx"

#: Fabric links need jumbo frames; 1500 caused fabric-wide overlay BGP flaps.
FABRIC_MTU = 9216

#: A sandbox switch's fxp0 joins the management bridge named in the lab profile.
#: The bridge itself is never modified.
MGMT_BRIDGE = "vmbr0"

#: Unused switch ports sit here with the link down, so vJunos always sees all
#: ten data ports in a stable order. The bridge carries no traffic.
PARK_BRIDGE = "sbxpark"

#: vJunos-switch data ports: ge-0/0/0 to ge-0/0/9 on net1 to net10.
SWITCH_PORTS = 10

#: Where QEMU puts each guest's serial socket (``<vmid>.serial0``).
SERIAL_DIR = "/var/run/qemu-server"

#: Sandbox LXC clients, when a recipe asks for them.
SANDBOX_LXC_START = 350
SANDBOX_LXC_END = 399

# --- Resource guardrails. ------------------------------------------------------

#: Juniper's minimum per vJunos switch. Do not go lower.
SWITCH_CORES = 4
SWITCH_MEM_MB = 5120
SWITCH_DISK_GB = 32

#: Refuse to build a sandbox that would leave less than this free.
MIN_FREE_RAM_MB = 6144

#: Used when the lab profile does not name an API.
DEFAULT_MIST_API = "https://api.mist.com/api/v1"
DEFAULT_PVE_API = "https://127.0.0.1:8006/api2/json"

#: Proxmox names a node after its host. The lab profile sets it exactly: it is
#: case sensitive, and the wrong case gets HTTP 596 from pveproxy.
DEFAULT_PVE_NODE = socket.gethostname().split(".")[0]

#: Boot sources a sandbox node may use: an installer ISO or a disk image in a
#: storage's import area. Existing guest disks (vm-NNN-disk-*) are never allowed.
IMAGE_PATTERNS = {
    "iso": r"^[A-Za-z0-9_-]+:iso/[A-Za-z0-9_.+-]+\.iso$",
    "import": r"^[A-Za-z0-9_-]+:import/[A-Za-z0-9_.+-]+\.(qcow2|img|raw|vmdk)$",
}

STATE_DIR = "/opt/simrack/state"

#: What the setup page saves in the state folder.
PROFILE_FILE = "lab-profile.toml"
TOKENS_FILE = "tokens.json"

#: Why SimRack changes nothing until a lab profile says what is live.
NO_PROFILE = "Fill in SimRack's setup page. Until it says what is live, SimRack cannot tell, so it changes nothing."


@dataclass
class Settings:
    """Runtime settings: the lab profile says what is live; the tokens say what SimRack may change."""

    profile_path: str = ""
    pve_api_base: str = DEFAULT_PVE_API
    pve_node: str = DEFAULT_PVE_NODE
    pve_token: str = ""
    mist_api_base: str = DEFAULT_MIST_API
    mist_token: str = ""
    org_id: str = ""
    state_dir: str = STATE_DIR
    bind_host: str = "127.0.0.1"
    bind_port: int = 8787
    #: Size of one vJunos switch: Juniper's minimum. Free memory, read live, decides
    #: how many fit, so a RAM upgrade needs no change here.
    switch_mem_mb: int = SWITCH_MEM_MB
    #: What is live, from the lab profile. Never touched.
    production_vmids: frozenset = frozenset()
    production_lxc: frozenset = frozenset()
    production_bridges: frozenset = frozenset()
    production_mist_sites: frozenset = frozenset()
    production_subnets: tuple = ()
    sandbox_vmid_start: int = SANDBOX_VMID_START
    sandbox_vmid_end: int = SANDBOX_VMID_END
    sandbox_lxc_start: int = SANDBOX_LXC_START
    sandbox_lxc_end: int = SANDBOX_LXC_END
    sandbox_bridge_prefix: str = SANDBOX_BRIDGE_PREFIX
    fabric_mtu: int = FABRIC_MTU
    min_free_ram_mb: int = MIN_FREE_RAM_MB
    mgmt_bridge: str = MGMT_BRIDGE
    #: None means the management port is untagged.
    mgmt_vlan: int | None = None
    mgmt_cidr: str = ""
    mgmt_pool: str = ""
    park_bridge: str = PARK_BRIDGE
    switch_ports: int = SWITCH_PORTS
    serial_dir: str = SERIAL_DIR
    #: Whether assistants may also tear down, revert, delete switches and type at a console.
    assistant_risky: bool = False
    extras: dict = field(default_factory=dict)
    #: Saved files that could not be used, as LabError dicts, for the setup page to show.
    problems: tuple = ()

    @classmethod
    def load(cls, environ=None) -> "Settings":
        """Read the lab profile and tokens the setup page saved in the state folder.
        Each token comes with the one address it may be sent to.

        The environment names only that folder and the token that guards the page.
        A bad profile is left out, so SimRack changes nothing, and named in ``problems``.
        """
        environ = os.environ if environ is None else environ
        state_dir = environ.get("SIMRACK_STATE_DIR", STATE_DIR)
        lab: dict = {}
        problems: list[dict] = []
        path = os.path.join(state_dir, PROFILE_FILE)
        if os.path.exists(path):
            from .profile import ProfileError, load_profile

            try:
                lab = load_profile(path)
                lab["profile_path"] = path
            except ProfileError as error:
                problems.append(error.as_dict())
        tokens: dict = {}
        if os.path.exists(os.path.join(state_dir, TOKENS_FILE)):
            try:
                with open(os.path.join(state_dir, TOKENS_FILE), encoding="utf-8") as handle:
                    tokens = json.load(handle)
            except (OSError, ValueError) as error:
                problems.append(LabError(f"SimRack's saved tokens cannot be read: {error}.", detail="Save both tokens again on the setup page.").as_dict())
        return cls(
            **lab,
            pve_token=tokens.get("proxmox", ""),
            pve_api_base=tokens.get("proxmox_api") or DEFAULT_PVE_API,
            mist_token=tokens.get("mist", ""),
            mist_api_base=tokens.get("mist_api") or DEFAULT_MIST_API,
            state_dir=state_dir,
            extras={"token": environ.get("SIMRACK_TOKEN", "")},
            problems=tuple(problems),
        )
