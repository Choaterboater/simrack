"""Turn a cabled sandbox into the Mist campus fabric that matches it.

Pure functions, no I/O: the service reads Mist, calls these, and writes the
results back. The cables are the source of truth. Each cable between two
fabric members becomes a topology link (border above core above distribution
above access) and an ``evpn_uplink``/``evpn_downlink`` port on each end.
"""

from __future__ import annotations

import copy
import ipaddress
import re

from .shapes import _expand, _natural

MIST_ROLES = ("access", "border", "collapsed-core", "core", "distribution", "esilag-access")
#: Lower is closer to the border. Equal tiers are never a fabric link.
TIER = {"border": 0, "core": 1, "collapsed-core": 1, "distribution": 2, "access": 3, "esilag-access": 3}
#: Mist groups these roles into pods.
POD_ROLES = ("distribution", "access", "esilag-access")
#: An ESI-LAG access switch hangs off these, and Mist names those ports itself.
ESILAG_PEERS = ("collapsed-core", "distribution")
ROUTED_AT = ("core", "distribution", "edge")
#: Which roles carry the anycast gateways, by ``routed_at``.
GATEWAY_ROLES = {"edge": ("access",), "core": ("core", "collapsed-core"), "distribution": ("distribution", "collapsed-core")}

SAFE_UNDERLAY = "10.255.224.0/20"
SAFE_ROUTER_IDS = "172.31.0.0/23"
SAFE_LOOPBACKS = "172.31.2.0/24"
#: A usual range that is taken steps down, one of its own size at a time, inside these.
PRIVATE_BLOCKS = tuple(ipaddress.ip_network(block) for block in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
DEFAULT_AS_BASE = 65001
UPLINK = "evpn_uplink"
DOWNLINK = "evpn_downlink"
LINK_KINDS = ("uplinks", "downlinks", "esilaglinks")
_POD_WORD = re.compile(r"^pod\s*(\d+)$", re.IGNORECASE)


def normalise_mac(text) -> str:
    """``02:00:00:AB:01:41`` to ``020000ab0141``."""
    return re.sub(r"[^0-9a-f]", "", str(text or "").lower())


def _clash(net, avoid: dict) -> str | None:
    return next((label for label, other in avoid.items() if net.overlaps(other)), None)


def _free_subnet(start: str, avoid: dict) -> str | None:
    """``start``, or the nearest subnet its size below it in its private block
    that overlaps nothing in ``avoid``. None when every one does."""
    first = ipaddress.ip_network(start)
    block = next((block for block in PRIVATE_BLOCKS if first.subnet_of(block)), first)
    for address in range(int(first.network_address), int(block.network_address) - 1, -first.num_addresses):
        candidate = ipaddress.ip_network((address, first.prefixlen))
        if _clash(candidate, avoid) is None:
            return str(candidate)
    return None


def _fabric_subnets(wanted: dict, avoid: dict, notes: list[str]) -> dict:
    """Each fabric range (``what``: (the shape's value, the usual range)). The
    shape's own settle first when usable; the rest step clear of ``avoid`` (label
    to network) and of every range settled before them."""
    avoid, settled, why = dict(avoid), {}, {}
    for what, (value, _) in wanted.items():
        if value in (None, ""):
            continue
        try:
            net = ipaddress.ip_network(str(value), strict=False)
        except ValueError:
            why[what] = f"The {what} subnet {value!r} is not a subnet"
            continue
        clash = _clash(net, avoid) if net.version == 4 else None
        if net.version != 4:
            why[what] = f"The {what} subnet {value} is not IPv4"
        elif clash:
            why[what] = f"The {what} subnet {value} overlaps {clash}"
        else:
            settled[what] = str(net)
            avoid[f"the {what} subnet {net}"] = net
    for what, (_, usual) in wanted.items():
        if what in settled:
            continue
        clash = _clash(ipaddress.ip_network(usual), avoid)
        reason = why.get(what) or (clash and f"The usual {what} subnet {usual} overlaps {clash}")
        picked = _free_subnet(usual, avoid)
        if picked is None:
            notes.append(f"{reason}, and no other {what} subnet that size is free, so the fabric keeps {usual}.")
            picked = usual
        elif reason:
            notes.append(f"{reason}, so the fabric uses {picked}.")
        settled[what] = picked
        avoid[f"the {what} subnet {picked}"] = ipaddress.ip_network(picked)
    return settled


def _routed_at(recipe, roles: dict, notes: list[str]) -> str:
    if "collapsed-core" in roles.values():
        return "core"
    wanted = recipe.routed_at
    if wanted in (None, ""):
        return "edge"
    if wanted not in ROUTED_AT:
        notes.append(f"routed_at {wanted!r} is not one of {', '.join(ROUTED_AT)}, so the fabric routes at the edge.")
        return "edge"
    return wanted


def _pods(nodes, pods) -> tuple[dict, dict]:
    """Pod number per node, and ``pod_names``, from the shape's pods then the node's pod name."""
    shape_names: dict[int, str] = {}
    by_name: dict[str, int] = {}
    for pod in pods or []:
        try:
            number = int(str(pod.get("id")))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= number <= 255:
            name = str(pod.get("name") or f"Pod {number}")
            shape_names[number] = name
            by_name.setdefault(name.strip().lower(), number)
    assigned: dict[str, int] = {}
    names: dict[int, str] = {}
    unknown = []
    for node in nodes:
        text = str(node.pod).strip() if node.pod not in (None, "") else ""
        number = None
        if not text:
            number = 1
        elif text.lower() in by_name:
            number = by_name[text.lower()]
        elif text.isdigit():
            number = int(text)
        elif _POD_WORD.match(text):
            number = int(_POD_WORD.match(text).group(1))
        if number is not None and 1 <= number <= 255:
            assigned[node.name] = number
            names.setdefault(number, shape_names.get(number, f"Pod {number}"))
        else:
            unknown.append((node, text))
    taken = set(assigned.values()) | set(shape_names)
    fresh: dict[str, int] = {}
    for node, text in unknown:
        key = text.lower()
        if key not in fresh:
            number = 2
            while number in taken:
                number += 1
            fresh[key] = number
            taken.add(number)
            names[number] = text[:40]
        assigned[node.name] = fresh[key]
    if not names:
        names[1] = shape_names.get(1, "Pod 1")
    pod_names = {str(number): names[number] for number in sorted(names)}
    return assigned, pod_names


def _gateways(recipe) -> dict:
    gateways = {}
    for network in recipe.networks:
        if not network.gateway:
            continue
        netmask = str(ipaddress.ip_network(network.cidr, strict=False).netmask)
        gateways[network.name] = {"type": "static", "ip": network.gateway, "netmask": netmask, "evpn_anycast": True}
    return gateways


def link_kind(role_a: str, role_b: str) -> str | None:
    """What Mist makes of a cable between two fabric roles: ``esilaglinks``,
    ``tiered`` (an uplink on the lower end, a downlink on the upper) or None."""
    pair = {role_a, role_b}
    if "esilag-access" in pair:
        other = (pair - {"esilag-access"}) or {"esilag-access"}
        return "esilaglinks" if other.pop() in ESILAG_PEERS else None
    if TIER[role_a] == TIER[role_b]:
        return None
    return "tiered"


def not_a_link(role_a: str, role_b: str) -> str:
    """Why ``link_kind`` said None, in words."""
    if "esilag-access" in (role_a, role_b):
        return "not a fabric link (an ESI-LAG access switch hangs off a collapsed core or distribution)"
    return f"not a fabric link ({role_a} to {role_b})"


def topology_body(sandbox, macs: dict, pods=None, protected=()) -> tuple[dict, dict]:
    """The detailed ``PUT /sites/{id}/evpn_topologies`` body for the sandbox's cables.

    ``macs`` maps node name to the switch's Mist MAC; a switch without one is
    not in the site yet. Returns ``(body, info)`` where ``info`` lists the
    members, what was left out and why, and any notes.
    """
    recipe = sandbox.recipe
    notes: list[str] = []
    skipped: list[str] = []
    members: list[str] = []
    role: dict[str, str] = {}
    switch_nodes = [n for n in sandbox.nodes if n.kind == "switch"]
    for node in switch_nodes:
        if node.role not in MIST_ROLES:
            skipped.append(f"{node.name}: not a Mist fabric role ({node.role})")
        elif not macs.get(node.name):
            skipped.append(f"{node.name}: not in the Mist site")
        else:
            members.append(node.name)
            role[node.name] = node.role

    links = {name: {kind: [] for kind in LINK_KINDS} for name in members}
    ports = {name: {UPLINK: [], DOWNLINK: []} for name in members}
    switch_names = {n.name for n in switch_nodes}

    def add(name, kind, peer):
        if macs[peer] not in links[name][kind]:
            links[name][kind].append(macs[peer])

    for link in sandbox.links:
        a, b = link.a_node, link.b_node
        if a not in switch_names and b not in switch_names:
            continue
        label = f"{a} {link.a_port} - {b} {link.b_port}"
        outside = [n for n in (a, b) if n not in role]
        if outside:
            verb = "is" if len(outside) == 1 else "are"
            skipped.append(f"{label}: {' and '.join(outside)} {verb} not in the fabric")
            continue
        kind = link_kind(role[a], role[b])
        if kind == "esilaglinks":
            add(a, "esilaglinks", b)
            add(b, "esilaglinks", a)
            continue
        if kind is None:
            skipped.append(f"{label}: {not_a_link(role[a], role[b])}")
            continue
        (upper, upper_port), (lower, lower_port) = sorted(
            ((a, link.a_port), (b, link.b_port)), key=lambda end: TIER[role[end[0]]]
        )
        add(upper, "downlinks", lower)
        add(lower, "uplinks", upper)
        ports[upper][DOWNLINK].append(upper_port)
        ports[lower][UPLINK].append(lower_port)

    routed_at = _routed_at(recipe, role, notes)
    pod_of, pod_names = _pods([n for n in switch_nodes if n.name in role and role[n.name] in POD_ROLES], pods)
    gateways = _gateways(recipe)
    gateway_roles = GATEWAY_ROLES[routed_at]

    switches = []
    configs = {}
    for name in members:
        entry = {"mac": macs[name], "role": role[name], **links[name]}
        if role[name] in POD_ROLES:
            entry["pod"] = pod_of[name]
        switches.append(entry)
        conf = {}
        port_config = {
            ",".join(sorted(set(used), key=_natural)): {"usage": usage} for usage, used in ports[name].items() if used
        }
        if port_config:
            conf["port_config"] = port_config
        if gateways and role[name] in gateway_roles:
            conf["other_ip_configs"] = copy.deepcopy(gateways)
            if recipe.vrf:
                conf["vrf_config"] = {"enabled": True}
        if conf:
            configs[macs[name]] = conf

    avoid = {f"the live lab {live}": ipaddress.ip_network(live) for live in protected}
    for network in recipe.networks:
        try:
            avoid[f"the {network.name} network {network.cidr}"] = ipaddress.ip_network(network.cidr, strict=False)
        except ValueError:
            continue  # the guardrail refuses it before anything is sent
    subnets = _fabric_subnets(
        {
            "underlay": (recipe.underlay_cidr, SAFE_UNDERLAY),
            "router ID": (recipe.loopback_cidr, SAFE_ROUTER_IDS),
            "loopback": (getattr(recipe, "auto_loopback_cidr", None), SAFE_LOOPBACKS),
        },
        avoid,
        notes,
    )
    options = {
        "routed_at": routed_at,
        "overlay": {"as": recipe.overlay_as},
        "underlay": {
            "as_base": recipe.underlay_as_base or DEFAULT_AS_BASE,
            "subnet": subnets["underlay"],
        },
        "auto_router_id_subnet": subnets["router ID"],
        "auto_loopback_subnet": subnets["loopback"],
    }
    body = {
        "name": sandbox.name,
        "overwrite": True,
        "switches": switches,
        "pod_names": pod_names,
        "evpn_options": options,
        "switch_configs": configs,
    }
    return body, {"members": members, "skipped": skipped, "notes": notes}


def basic_body(body: dict) -> dict:
    """The same topology with members and roles only, for a Mist that refuses the detailed form."""
    basic = {key: copy.deepcopy(body[key]) for key in ("id", "name", "overwrite", "pod_names", "evpn_options") if key in body}
    basic["switches"] = [{key: s[key] for key in ("mac", "role", "pod") if key in s} for s in body.get("switches", [])]
    return basic


def topology_pairs(topology: dict) -> set[frozenset]:
    """Every pair of MACs the topology links, in either direction."""
    pairs = set()
    for switch in (topology or {}).get("switches") or []:
        mac = normalise_mac(switch.get("mac"))
        for kind in LINK_KINDS:
            for peer in switch.get(kind) or []:
                if normalise_mac(peer) and mac:
                    pairs.add(frozenset((mac, normalise_mac(peer))))
    return pairs


def merge_switch_config(current: dict, conf: dict) -> dict:
    """``conf`` (fabric ports, gateways, VRF) laid over a device's config, keeping everything else."""
    merged = copy.deepcopy(current or {})
    new_ports = conf.get("port_config") or {}
    if new_ports:
        owned = set()
        for key in new_ports:
            owned.update(_expand(key))
        kept = {}
        for key, value in (merged.get("port_config") or {}).items():
            expanded = _expand(key)
            if not set(expanded) & owned:
                kept[key] = value
                continue
            rest = [port for port in expanded if port not in owned]
            if rest:
                kept[",".join(rest)] = value
        kept.update(copy.deepcopy(new_ports))
        merged["port_config"] = kept
    if conf.get("other_ip_configs"):
        others = dict(merged.get("other_ip_configs") or {})
        others.update(copy.deepcopy(conf["other_ip_configs"]))
        merged["other_ip_configs"] = others
    if "vrf_config" in conf:
        merged["vrf_config"] = copy.deepcopy(conf["vrf_config"])
    return merged


def site_setting(current: dict, recipe, password: str) -> dict:
    """The site setting with the recipe's networks, VRF and root password, everything else kept."""
    merged = copy.deepcopy(current or {})
    wanted = recipe.mist_setting()
    networks = merged.get("networks") if isinstance(merged.get("networks"), dict) else {}
    for name, conf in wanted["networks"].items():
        entry = dict(networks[name]) if isinstance(networks.get(name), dict) else {}
        entry.pop("vlan", None)
        entry.update(conf)
        networks[name] = entry
    merged["networks"] = networks
    if wanted.get("vrf_instances"):
        instances = merged.get("vrf_instances") if isinstance(merged.get("vrf_instances"), dict) else {}
        for vrf, conf in wanted["vrf_instances"].items():
            entry = dict(instances[vrf]) if isinstance(instances.get(vrf), dict) else {}
            have = entry.get("networks")
            names = list(have) if isinstance(have, (dict, list)) else []
            names += [n for n in conf["networks"] if n not in names]
            entry["networks"] = names
            entry.setdefault("extra_routes", {})
            instances[vrf] = entry
        merged["vrf_instances"] = instances
    mgmt = dict(merged.get("switch_mgmt") or {}) if isinstance(merged.get("switch_mgmt"), dict) else {}
    mgmt["root_password"] = password
    merged["switch_mgmt"] = mgmt
    return merged
