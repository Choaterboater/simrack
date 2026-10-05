"""Data model for a sandbox: the nodes, the cabling, and the Mist recipe."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from .config import IFNAME_MAX

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
_VMID = re.compile(r"^[1-9][0-9]{2,5}$")
_PORT = re.compile(r"^(ge|et)-[0-9]+/[0-9]+/[0-9]+$")
_VLAN = re.compile(r"^[0-9]{1,4}$")


def check_name(value: str, label: str = "name") -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise ValueError(f"{label} must be lowercase alphanumeric with dashes (2-32 chars), got {value!r}")
    return value


def check_vmid(value: int) -> int:
    value = int(value)
    if not _VMID.fullmatch(str(value)):
        raise ValueError(f"vmid must be a plain number, got {value!r}")
    return value


def check_port(value: str) -> str:
    if not isinstance(value, str) or not _PORT.fullmatch(value):
        raise ValueError(f"port must look like ge-0/0/2, got {value!r}")
    return value


@dataclass
class Node:
    """A sandbox guest: a vJunos switch, the vSRX, or a client container."""

    name: str
    vmid: int
    role: str = "access"  # access | core | border | vsrx | client
    kind: str = "switch"  # switch | vsrx | client | image
    template_vmid: int | None = None
    image: str | None = None
    storage: str = "local-lvm"
    running: bool = False
    #: fxp0 address, from the lab profile's management pool
    mgmt_ip: str | None = None
    #: the Mist pod a shape put this switch in, if any
    pod: str | None = None
    #: when SimRack last adopted this switch into the sandbox's Mist site
    adopted_at: str | None = None

    def __post_init__(self) -> None:
        check_name(self.name, "node name")
        check_vmid(self.vmid)


@dataclass
class Link:
    """A point-to-point cable. One Proxmox bridge, MTU 9216, per link."""

    a_node: str
    a_port: str
    b_node: str
    b_port: str
    bridge: str
    mtu: int = 9216

    def __post_init__(self) -> None:
        check_name(self.a_node, "link endpoint")
        check_name(self.b_node, "link endpoint")
        check_port(self.a_port)
        check_port(self.b_port)
        if self.a_node == self.b_node:
            raise ValueError("A cable cannot connect a node to itself")
        if self.a_port == self.b_port and self.a_node == self.b_node:
            raise ValueError("A cable cannot loop back into the same port")
        if len(self.bridge) > IFNAME_MAX or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*?[0-9]+_[0-9]+_[0-9]{1,2}", self.bridge):
            raise ValueError(
                f"bridge must be named <prefix><vmid>_<vmid>_<ports> within the {IFNAME_MAX} character "
                f"interface-name limit, got {self.bridge!r}"
            )

    def endpoints(self) -> list[tuple[str, str]]:
        return [(self.a_node, self.a_port), (self.b_node, self.b_port)]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Network:
    """A VLAN in the sandbox, mirroring the live lab's data/voice pattern."""

    name: str
    vlan: int
    cidr: str
    gateway: str

    def __post_init__(self) -> None:
        if not _VLAN.fullmatch(str(self.vlan)):
            raise ValueError(f"vlan must be numeric, got {self.vlan!r}")
        if not re.fullmatch(r"^[0-9.]+/\d+$", self.cidr):
            raise ValueError(f"cidr must look like 10.60.10.0/24, got {self.cidr!r}")


@dataclass
class Recipe:
    """A fabric blueprint for a sandbox.

    Its subnets must stay clear of the subnets the lab profile protects; the
    guardrails refuse a fabric build that would overlap one.
    """

    name: str = "ip-clos"
    description: str = ""
    site_name: str = ""
    org_id: str | None = None
    #: Fabric-wide settings applied to the sandbox site.
    networks: list[Network] = field(default_factory=list)
    vrf: str | None = None
    overlay_as: int = 65200
    #: EVPN underlay and auto-assigned router IDs and loopbacks, all clear of the live lab.
    underlay_cidr: str = "10.255.224.0/20"
    loopback_cidr: str = "172.31.0.0/23"
    auto_loopback_cidr: str = "172.31.2.0/24"
    mgmt_network: str = "management"
    #: Node roles in the blueprint, in the order the front end builds them.
    roles: list[dict] = field(default_factory=list)
    #: Border uplink to a WAN router (the vSRX), as a /31 per border.
    border_p2p: bool = True
    wan_router_as: int = 65100
    interface_filter: str | None = None
    extra_config_cmds: list[str] = field(default_factory=list)
    #: Set when the sandbox was built from an imported shape.
    shape: str | None = None
    topology_kind: str | None = None
    routed_at: str | None = None
    underlay_as_base: int | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["networks"] = [asdict(n) if not isinstance(n, dict) else n for n in self.networks]
        return data

    def mist_setting(self) -> dict:
        """The site networks and VRF in the live site's format (``PUT /sites/{id}/setting``).

        Management stays out of band on fxp0, so it is not a site network.
        """
        setting: dict = {"networks": {n.name: {"vlan_id": int(n.vlan), "subnet": n.cidr} for n in self.networks}}
        if self.vrf:
            setting["vrf_instances"] = {self.vrf: {"networks": [n.name for n in self.networks], "extra_routes": {}}}
        return setting


@dataclass
class MistSnapshot:
    label: str
    taken_at: str
    site_id: str
    site_setting: dict
    evpn_topologies: list[dict]
    devices: dict = field(default_factory=dict)
    device_cli: dict = field(default_factory=dict)
    #: where a root password was taken out; revert puts the sandbox's own back there
    root_password_removed: dict = field(default_factory=lambda: {"site_setting": False, "devices": []})


@dataclass
class Sandbox:
    name: str
    created_at: str
    recipe: Recipe
    nodes: list[Node] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    mist_site_id: str | None = None
    mist_site_name: str | None = None
    #: label -> snapshot metadata, for both Proxmox and Mist reverts
    proxmox_snapshots: dict = field(default_factory=dict)
    mist_snapshots: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: when "Build fabric in Mist" last succeeded, and the last "Check cabling" result
    fabric_built_at: str | None = None
    fabric_check: dict | None = None

    def __post_init__(self) -> None:
        check_name(self.name, "sandbox name")

    def node(self, name: str) -> Node:
        for node in self.nodes:
            if node.name == name:
                return node
        raise KeyError(name)

    def next_vmid(self, start: int, end: int) -> int:
        used = {node.vmid for node in self.nodes}
        for candidate in range(start, end + 1):
            if candidate not in used:
                return candidate
        raise ValueError(f"no free vmid in {start}-{end}")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "created_at": self.created_at,
            "recipe": self.recipe.to_dict(),
            "nodes": [asdict(n) for n in self.nodes],
            "links": [link.to_dict() for link in self.links],
            "mist_site_id": self.mist_site_id,
            "mist_site_name": self.mist_site_name,
            "proxmox_snapshots": self.proxmox_snapshots,
            "mist_snapshots": self.mist_snapshots,
            "notes": self.notes,
            "fabric_built_at": self.fabric_built_at,
            "fabric_check": self.fabric_check,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Sandbox":
        recipe = Recipe(**{k: v for k, v in data["recipe"].items() if k in Recipe.__dataclass_fields__})
        recipe.networks = [Network(**n) for n in data["recipe"].get("networks", [])]
        return cls(
            name=data["name"],
            created_at=data["created_at"],
            recipe=recipe,
            nodes=[Node(**n) for n in data.get("nodes", [])],
            links=[Link(**link_data) for link_data in data.get("links", [])],
            mist_site_id=data.get("mist_site_id"),
            mist_site_name=data.get("mist_site_name"),
            proxmox_snapshots=data.get("proxmox_snapshots", {}),
            mist_snapshots=data.get("mist_snapshots", {}),
            notes=data.get("notes", []),
            fabric_built_at=data.get("fabric_built_at"),
            fabric_check=data.get("fabric_check"),
        )
