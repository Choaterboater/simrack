"""Static limits and defaults for the lab front end.

What is live on this host (guests, bridges, Mist sites, subnets) comes from the
lab profile, never from here: see profile.py and lab-profile.example.toml.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field

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

STATE_DIR = "/opt/labfront/state"

#: Why LabFront changes nothing until a lab profile says what is live.
NO_PROFILE = "Set LABFRONT_PROFILE to this lab's profile and restart. Without one LabFront cannot tell what is live, so it changes nothing."


@dataclass
class Settings:
    """Runtime settings: the lab profile says what is live, the environment holds secrets and switches."""

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
    #: Set by the operator to allow writes; the app starts read-only without it.
    allow_writes: bool = False
    mist_writes_enabled: bool = False
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
    extras: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, environ=None) -> "Settings":
        """Load the lab profile named by LABFRONT_PROFILE; raise ProfileError if it is bad."""
        environ = os.environ if environ is None else environ
        lab: dict = {}
        path = environ.get("LABFRONT_PROFILE", "")
        if path:
            from .profile import load_profile

            lab = load_profile(path)
            lab["profile_path"] = path
        # Without a profile nothing says what is live, so the write switches stay off.
        return cls(
            **lab,
            pve_token=environ.get("LABFRONT_PVE_TOKEN", ""),
            mist_token=environ.get("MIST_TOKEN", ""),
            mist_writes_enabled=bool(path) and environ.get("LABFRONT_MIST_WRITES", "0") == "1",
            allow_writes=bool(path) and environ.get("LABFRONT_ALLOW_WRITES", "0") == "1",
            state_dir=environ.get("LABFRONT_STATE_DIR", STATE_DIR),
            extras={"token": environ.get("LABFRONT_TOKEN", "")},
        )
