"""Fabric shapes: the design of a live fabric, read from Mist, minus its identity.

A shape records which switches a fabric has, their roles and pods, and which
port is cabled to which. It never carries addresses, MACs or config, so it can
later be built as a sandbox without colliding with the live lab.

Mist's EVPN topology lists each switch's neighbours (``uplinks``,
``downlinks``, ``esilaglinks``) by MAC, but not the ports. The ports come from
LLDP when the port stats are supplied, and are otherwise guessed from each
switch's ``evpn_*`` port config.
"""

from __future__ import annotations

import copy
import datetime as _dt
import re

from .errors import LabError
from .models import check_name

#: Sandbox name prefix per Mist role.
PREFIX = {
    "border": "bl",
    "core": "core",
    "collapsed-core": "core",
    "distribution": "dist",
    "access": "acc",
    "esilag-access": "acc",
}
ROLE_ORDER = ["border", "core", "collapsed-core", "distribution", "access", "esilag-access"]
LINK_KINDS = ("uplinks", "downlinks", "esilaglinks")
MIRROR = {"uplinks": "downlinks", "downlinks": "uplinks", "esilaglinks": "esilaglinks"}
USAGE_WORD = {"uplinks": "uplink", "downlinks": "downlink", "esilaglinks": "esilag"}
#: vJunos-switch data ports are ge-0/0/0 to ge-0/0/9.
SANDBOX_PORT = re.compile(r"^ge-0/0/[0-9]$")
SANDBOX_PORTS = 10
MAX_SWITCHES = 64
_PORT_SPEC = re.compile(r"^([a-z]+-\d+/\d+/)(\d+)(?:-(\d+))?$")

EXPECTED = (
    "Import the topology from GET /api/v1/sites/<site>/evpn_topologies/<id>. "
    "Optionally add the switches (…/stats/devices?type=switch) for names and the port stats "
    "(…/stats/ports/search) for the exact cabling."
)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _natural(text: str) -> tuple:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", str(text)))


def _expand(spec: str) -> list[str]:
    """``ge-0/0/1,ge-0/0/0`` or ``ge-0/0/0-3`` to a sorted list of ports."""
    ports = []
    for part in str(spec).replace(" ", "").split(","):
        found = _PORT_SPEC.match(part)
        if not found:
            continue
        stem, first, last = found.group(1), int(found.group(2)), found.group(3)
        for number in range(first, (int(last) if last else first) + 1):
            ports.append(f"{stem}{number}")
    return sorted(set(ports), key=_natural)


def _usage(conf) -> str:
    return str((conf or {}).get("usage") or "") if isinstance(conf, dict) else ""


def _slug(text: str) -> str:
    return re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def _shape_name(text: str) -> str:
    slug = _slug(text)[:32].strip("-")
    for candidate in (slug, ("mist-" + slug)[:32].strip("-")):
        try:
            return check_name(candidate)
        except ValueError:
            continue
    return "mist-fabric"


# -- reading what the operator pasted -------------------------------------------


class _Found:
    def __init__(self) -> None:
        self.topologies: list[dict] = []
        self.devices: list[dict] = []
        self.ports: list[dict] = []
        self.name: str | None = None


def _take_list(items: list, found: _Found) -> None:
    sample = next((item for item in items if isinstance(item, dict)), None)
    if sample is None:
        return
    if "port_id" in sample:
        found.ports.extend(item for item in items if isinstance(item, dict))
    elif "switches" in sample or "evpn_options" in sample or ("id" in sample and "name" in sample and "mac" not in sample):
        found.topologies.extend(item for item in items if isinstance(item, dict))
    elif "mac" in sample:
        found.devices.extend(item for item in items if isinstance(item, dict))


def _take(doc, found: _Found, depth: int = 0) -> None:
    if depth > 4:
        return
    if isinstance(doc, list):
        _take_list(doc, found)
        return
    if not isinstance(doc, dict):
        return
    if "switches" in doc or "evpn_options" in doc:
        found.topologies.append(doc)
        return
    if depth == 0 and isinstance(doc.get("name"), str) and any(k in doc for k in ("topology", "topologies", "documents")):
        found.name = doc["name"]
    for key in ("topology", "topologies", "devices", "ports", "results", "documents"):
        value = doc.get(key)
        if key == "documents" and isinstance(value, list):
            for item in value:
                _take(item, found, depth + 1)
        elif key == "topology" and isinstance(value, dict):
            found.topologies.append(value)
        elif key == "devices" and isinstance(value, list):
            found.devices.extend(item for item in value if isinstance(item, dict))
        elif key == "ports" and isinstance(value, (list, dict)):
            rows = value.get("results", []) if isinstance(value, dict) else value
            found.ports.extend(item for item in rows if isinstance(item, dict))
        elif value is not None:
            _take(value, found, depth + 1)


def _pick_topology(found: _Found) -> dict:
    full, seen = [], set()
    for topology in found.topologies:
        if not (isinstance(topology.get("switches"), list) and topology["switches"]):
            continue
        key = (topology.get("id"), topology.get("name")) if topology.get("id") or topology.get("name") else id(topology)
        if key not in seen:
            seen.add(key)
            full.append(topology)
    if len(full) > 1:
        names = ", ".join(str(t.get("name") or t.get("id") or "unnamed") for t in full)
        raise LabError(f"That holds {len(full)} topologies: {names}.", detail="Import one topology at a time.")
    if full:
        return full[0]
    listed = [t for t in found.topologies if t.get("id")]
    if listed:
        where = "/sites/{}/".format(listed[0]["site_id"]) if listed[0].get("site_id") else "/sites/<site>/"
        pick = "; ".join(f"{t.get('name') or 'unnamed'}: GET /api/v1{where}evpn_topologies/{t['id']}" for t in listed[:5])
        raise LabError("That is Mist's topology list, which leaves the switches out.", detail=f"Fetch the topology by id instead. {pick}")
    if found.topologies:
        name = found.topologies[0].get("name") or "That topology"
        raise LabError(f"{name} has no switches.", detail="Assign switches to the topology in Mist first.")
    raise LabError("No Mist EVPN topology found in that.", detail=EXPECTED)


# -- building the shape ------------------------------------------------------------


def shape_from_mist(doc, now: str | None = None) -> dict:
    """Turn Mist's EVPN topology (plus, optionally, its switches and port
    stats) into a shape. Raises LabError for anything that is not one."""
    found = _Found()
    _take(doc, found)
    topology = _pick_topology(found)
    switches = [s for s in topology["switches"] if isinstance(s, dict) and s.get("mac")]
    if not switches:
        raise LabError(f"{topology.get('name') or 'That topology'} has no switches.", detail=EXPECTED)
    if len(switches) > MAX_SWITCHES:
        raise LabError(f"{len(switches)} switches is more than a shape can hold ({MAX_SWITCHES}).")
    name = check_name(found.name, "shape name") if found.name is not None else _shape_name(topology.get("name") or "fabric")

    names = {}
    for device in found.devices:
        label = device.get("name") or device.get("hostname")
        if device.get("mac") and isinstance(label, str) and label.strip():
            names[str(device["mac"]).lower()] = label.strip()[:64]
    macs = [str(s["mac"]).lower() for s in switches]
    switch = dict(zip(macs, switches))
    label = {mac: names.get(mac) or (switch[mac].get("name") if isinstance(switch[mac].get("name"), str) else None) or mac for mac in macs}
    role = {mac: str(switch[mac].get("role") or "none") for mac in macs}
    mac_of = {label[m].lower(): m for m in macs if label[m] != m}
    named = bool(mac_of)
    notes: list[str] = []

    # Sandbox names: one counter per prefix, in live-name order.
    sandbox = {}
    rank = {r: i for i, r in enumerate(ROLE_ORDER)}
    for prefix in dict.fromkeys(PREFIX.get(r, "sw") for r in role.values()):
        group = sorted((m for m in macs if PREFIX.get(role[m], "sw") == prefix), key=lambda m: (rank.get(role[m], 99), _natural(label[m])))
        for number, mac in enumerate(group, 1):
            sandbox[mac] = f"sbx-{prefix}-{number:02d}"
    order = lambda m: (rank.get(role[m], 99), sandbox[m])  # noqa: E731

    # Neighbours, symmetric: if A lists B as an uplink, B has A as a downlink.
    neighbours = {m: {kind: [] for kind in LINK_KINDS} for m in macs}
    seen_pairs = {m: set() for m in macs}
    for mac in macs:
        for kind in LINK_KINDS:
            for other in switch[mac].get(kind) or []:
                other = str(other).lower()
                if other in switch and other != mac and other not in seen_pairs[mac]:
                    seen_pairs[mac].add(other)
                    neighbours[mac][kind].append(other)
    for mac in macs:
        for kind in LINK_KINDS:
            for other in list(neighbours[mac][kind]):
                if mac not in seen_pairs[other]:
                    seen_pairs[other].add(mac)
                    neighbours[other][MIRROR[kind]].append(mac)
    pairs = sorted({tuple(sorted((a, b), key=order)) for a in macs for b in seen_pairs[a]}, key=lambda p: (order(p[0]), order(p[1])))

    # Guessed ports: each kind's neighbours, in order, onto that kind's evpn ports.
    port_config = {}
    for mac in macs:
        config = switch[mac].get("config")
        found_pc = config.get("port_config") if isinstance(config, dict) else None
        port_config[mac] = found_pc if isinstance(found_pc, dict) else {}
    guess = {}
    for mac in macs:
        assigned, taken = {}, set()
        for spec, conf in port_config[mac].items():
            taken.update(_expand(spec))
        for kind in LINK_KINDS:
            ports = sorted(
                {p for spec, conf in port_config[mac].items() if _usage(conf).startswith("evpn") and USAGE_WORD[kind] in _usage(conf) for p in _expand(spec)},
                key=_natural,
            )
            for other, port in zip(sorted(neighbours[mac][kind], key=order), ports):
                assigned.setdefault(other, port)
        number = 0
        for kind in LINK_KINDS:
            for other in sorted(neighbours[mac][kind], key=order):
                if other in assigned:
                    continue
                while f"ge-0/0/{number}" in taken or f"ge-0/0/{number}" in assigned.values():
                    number += 1
                assigned[other] = f"ge-0/0/{number}"
        for other, port in assigned.items():
            guess[(mac, other)] = port

    # LLDP: ports on each switch that see a fabric neighbour by name.
    lldp: dict[tuple, list] = {}
    for row in found.ports:
        local, seen_name = str(row.get("mac") or "").lower(), str(row.get("neighbor_system_name") or "").lower()
        other, port = mac_of.get(seen_name), row.get("port_id")
        usage = str(row.get("port_usage") or "")
        if local in switch and other in seen_pairs.get(local, ()) and isinstance(port, str) and (not usage or usage.startswith("evpn")):
            lldp.setdefault((local, other), []).append(port)
    for key in lldp:
        lldp[key] = sorted(set(lldp[key]), key=_natural)
    if found.ports and not named:
        notes.append("Port stats need the switch names to match LLDP neighbours; add the devices file for the exact cabling.")

    links = []
    for a, b in pairs:
        pa, pb = lldp.get((a, b), []), lldp.get((b, a), [])
        if pa and pb:
            for x, y in zip(pa, pb):
                links.append({"a": a, "a_port": x, "a_seen": True, "b": b, "b_port": y, "b_seen": True})
            if len(pa) != len(pb):
                notes.append(f"{sandbox[a]} and {sandbox[b]} disagree on how many cables join them; kept {min(len(pa), len(pb))}.")
        else:
            links.append({"a": a, "a_port": pa[0] if pa else guess[(a, b)], "a_seen": bool(pa),
                          "b": b, "b_port": pb[0] if pb else guess[(b, a)], "b_seen": bool(pb)})

    # A guessed end must not land on a port that LLDP proved is in use, or on another guess.
    used = {m: set() for m in macs}
    for link in links:
        for end in ("a", "b"):
            if link[end + "_seen"]:
                used[link[end]].add(link[end + "_port"])
    for link in links:
        for end, far in (("a", "b"), ("b", "a")):
            if link[end + "_seen"]:
                continue
            mac, port = link[end], link[end + "_port"]
            if port in used[mac]:
                number = 0
                while f"ge-0/0/{number}" in used[mac]:
                    number += 1
                notes.append(f"{sandbox[mac]} {port} was taken; the cable to {sandbox[link[far]]} uses ge-0/0/{number}.")
                port = link[end + "_port"] = f"ge-0/0/{number}"
            used[mac].add(port)

    # vJunos only has ge-0/0/0-9: renumber a switch whose live ports differ.
    remap = {}
    for mac in sorted(macs, key=order):
        live = sorted(used[mac], key=_natural)
        if all(SANDBOX_PORT.match(p) for p in live):
            continue
        remap[mac] = {p: f"ge-0/0/{i}" for i, p in enumerate(live)}
        notes.append(f"{sandbox[mac]} live ports {', '.join(live)} become ge-0/0/0–{len(live) - 1} in the sandbox.")
        if len(live) > SANDBOX_PORTS:
            notes.append(f"{sandbox[mac]} needs {len(live)} fabric ports; vJunos has {SANDBOX_PORTS}.")
    out_links = []
    for link in links:
        item = {"a_node": sandbox[link["a"]], "a_port": link["a_port"], "b_node": sandbox[link["b"]], "b_port": link["b_port"],
                "via": "lldp" if link["a_seen"] and link["b_seen"] else "guess"}
        for end in ("a", "b"):
            if link[end] in remap:
                item[end + "_live_port"] = item[end + "_port"]
                item[end + "_port"] = remap[link[end]][item[end + "_port"]]
        out_links.append(item)

    via = {link["via"] for link in out_links}
    ports_source = "lldp" if via == {"lldp"} else "guessed" if "lldp" not in via else "mixed"
    if ports_source == "guessed" and not (found.ports and not named):
        notes.append("Ports are a best guess from Mist's port config. Add the port stats for the exact cabling.")
    elif ports_source == "mixed":
        missing = sum(1 for link in out_links if link["via"] != "lldp")
        notes.append(f"LLDP did not see {missing} of {len(out_links)} cables; their ports are a best guess from the port config.")

    for mac in sorted(macs, key=order):
        outside = [
            f"{spec} ({_usage(conf)})"
            for spec, conf in sorted(port_config[mac].items(), key=lambda kv: _natural(kv[0]))
            if _usage(conf) and not _usage(conf).startswith("evpn")
        ]
        if outside:
            notes.append(f"{sandbox[mac]} {', '.join(outside)}: outside the fabric, left unplugged.")
        if role[mac] not in PREFIX:
            notes.append(f"{sandbox[mac]} has role {role[mac]!r}; it is drawn as a host.")

    pod_names = topology.get("pod_names") if isinstance(topology.get("pod_names"), dict) else {}
    pods = {}
    for mac in macs:
        pod = switch[mac].get("pod")
        if pod is not None and pod != "":
            pods[str(pod)] = str(pod_names.get(str(pod)) or f"Pod {pod}")[:40]
    nodes = []
    for mac in sorted(macs, key=order):
        pod = switch[mac].get("pod")
        ports = sorted({link["a_port"] for link in out_links if link["a_node"] == sandbox[mac]} | {link["b_port"] for link in out_links if link["b_node"] == sandbox[mac]}, key=_natural)
        nodes.append({"name": sandbox[mac], "role": role[mac], "pod": pods.get(str(pod)) if pod not in (None, "") else None, "from": label[mac], "ports": ports})

    options = topology.get("evpn_options") if isinstance(topology.get("evpn_options"), dict) else {}
    evpn = {
        "routed_at": options.get("routed_at"),
        "overlay_as": (options.get("overlay") or {}).get("as"),
        "underlay_as_base": (options.get("underlay") or {}).get("as_base"),
    }
    roles = set(role.values())
    routed = evpn["routed_at"]
    kind = (
        "EVPN multihoming" if roles & {"collapsed-core", "esilag-access"}
        else "IP Clos" if routed == "edge"
        else "Core-distribution (ERB)" if routed == "distribution"
        else "Core-distribution (CRB)" if routed == "core"
        else "EVPN fabric"
    )
    modified = topology.get("modified_time")
    source = {
        "kind": "mist",
        "topology": topology.get("name"),
        "topology_id": topology.get("id"),
        "site_id": topology.get("site_id"),
        "modified": _dt.datetime.fromtimestamp(modified, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(modified, (int, float)) else None,
        "imported_at": now or _now(),
    }
    return {
        "name": name,
        "kind": kind,
        "source": {k: v for k, v in source.items() if v is not None},
        "evpn": {k: v for k, v in evpn.items() if v is not None},
        "pods": [{"id": key, "name": value} for key, value in sorted(pods.items(), key=lambda kv: _natural(kv[0]))],
        "nodes": nodes,
        "links": out_links,
        "ports_source": ports_source,
        "notes": notes,
    }


_GE_PORT = re.compile(r"^ge-0/0/(\d+)$")


def plan_build(shape: dict, switches=None, *, ports: int = SANDBOX_PORTS) -> dict:
    """Which switches to build from a shape and which of its cables survive.

    ``switches`` is the ticked subset (all of them when None). A cable stays only
    if both ends are built, both ports exist on a vJunos (ge-0/0/0 up to
    ``ports``-1) and neither port is already taken; anything else is returned in
    ``dropped`` with the reason, in the shape's order.
    """
    nodes = shape.get("nodes") or []
    known = [node["name"] for node in nodes]
    if switches is None:
        chosen = set(known)
    else:
        if not isinstance(switches, (list, tuple)) or not all(isinstance(s, str) for s in switches):
            raise LabError("Pick switches as a list of names.", detail='For example ["sbx-core-01", "sbx-acc-01"].')
        if not switches:
            raise LabError("Tick at least one switch.", detail="A sandbox needs one switch or more.")
        for name in switches:
            if name not in known:
                raise LabError(f"{name} is not in shape {shape.get('name')}.", detail="Shape switches: " + ", ".join(known))
        chosen = set(switches)

    last = f"ge-0/0/{ports - 1}"
    kept, dropped, used = [], [], set()
    for link in shape.get("links") or []:
        a, b = link["a_node"], link["b_node"]
        if a not in chosen and b not in chosen:
            continue
        reason = None
        if a not in chosen:
            reason = f"{a} not built"
        elif b not in chosen:
            reason = f"{b} not built"
        elif a == b:
            reason = f"{a} is cabled to itself"
        if reason is None:
            for port in (link["a_port"], link["b_port"]):
                found = _GE_PORT.match(str(port))
                if not found:
                    reason = f"{port} is not a sandbox port"
                    break
                if int(found.group(1)) >= ports:
                    reason = f"{port} is past {last}"
                    break
        if reason is None:
            for end in ((a, link["a_port"]), (b, link["b_port"])):
                if end in used:
                    reason = f"{end[0]} {end[1]} already cabled"
                    break
        if reason:
            dropped.append({**copy.deepcopy(link), "reason": reason})
            continue
        used |= {(a, link["a_port"]), (b, link["b_port"])}
        kept.append(copy.deepcopy(link))
    return {
        "nodes": [copy.deepcopy(node) for node in nodes if node["name"] in chosen],
        "links": kept,
        "dropped": dropped,
    }
