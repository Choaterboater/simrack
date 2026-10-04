"""The lab profile: one TOML file that says what is live on this host.

SimRack never touches what the profile protects, and with no profile it stays
read-only, because then it cannot tell what is live. A mistake in the file leaves
the whole file out, so SimRack changes nothing, and names the key: a typo in a
protected list must never quietly leave something live unprotected.
``lab-profile.example.toml`` shows every key.
"""

from __future__ import annotations

import difflib
import ipaddress
import json
import tomllib

from .config import (
    PARK_BRIDGE,
    SANDBOX_BRIDGE_PREFIX,
    SANDBOX_LXC_END,
    SANDBOX_LXC_START,
    SANDBOX_VMID_END,
    SANDBOX_VMID_START,
)
from .errors import LabError

HINT = "Fix it on SimRack's setup page and save, or import a fixed profile there. Until then SimRack changes nothing."


class ProfileError(LabError):
    """The lab profile is missing, unreadable or has a bad value."""


_REQUIRED = object()

#: section -> key -> (kind, default, Settings field). _REQUIRED marks a key the file must set.
SCHEMA: dict[str, dict[str, tuple]] = {
    "proxmox": {
        "node": ("text", _REQUIRED, "pve_node"),
    },
    "mist": {
        "org_id": ("text", "", "org_id"),
    },
    "management": {
        "bridge": ("text", _REQUIRED, "mgmt_bridge"),
        "vlan": ("vlan", None, "mgmt_vlan"),
        "cidr": ("subnet", _REQUIRED, "mgmt_cidr"),
        "pool": ("pool", _REQUIRED, "mgmt_pool"),
    },
    "protected": {
        "vmids": ("numbers", (), "production_vmids"),
        "lxc": ("numbers", (), "production_lxc"),
        "bridges": ("texts", (), "production_bridges"),
        "mist_sites": ("texts", (), "production_mist_sites"),
        "subnets": ("subnets", (), "production_subnets"),
    },
    "sandbox": {
        "vmids": ("range", (SANDBOX_VMID_START, SANDBOX_VMID_END), ("sandbox_vmid_start", "sandbox_vmid_end")),
        "lxc": ("range", (SANDBOX_LXC_START, SANDBOX_LXC_END), ("sandbox_lxc_start", "sandbox_lxc_end")),
        "bridge_prefix": ("text", SANDBOX_BRIDGE_PREFIX, "sandbox_bridge_prefix"),
        "park_bridge": ("text", PARK_BRIDGE, "park_bridge"),
    },
    "assistants": {
        "risky": ("bool", False, "assistant_risky"),
    },
}

#: Keys SimRack no longer reads, and where each setting belongs now. Saving the
#: setup page, or importing a file there, leaves them out.
RETIRED = {
    "proxmox.hookscript": "Proxmox lets only root@pam set a hookscript, so it goes on the vJunos template "
    "once (qm set <template> --hookscript ...) and every clone copies it.",
    "proxmox.api": "The Proxmox address is now set next to the Proxmox token on the setup page, so the token "
    "is only ever sent where you chose.",
    "mist.api": "The Mist region is now set next to the Mist token on the setup page, so the token "
    "is only ever sent where you chose.",
}

#: The file must have these, even if a protected list is empty: saying so is the point.
REQUIRED_SECTIONS = ("proxmox", "management", "protected")


def load_profile(path: str) -> dict:
    """Read a lab profile and return it as ``Settings`` keyword arguments."""
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except OSError as error:
        raise ProfileError(f"Cannot read lab profile {path}: {error.strerror or error}.", detail=HINT) from error
    except tomllib.TOMLDecodeError as error:
        raise ProfileError(f"Lab profile {path} is not valid TOML: {error}.", detail=HINT) from error
    return profile_values(raw, path)


def profile_values(raw: dict, path: str = "", hint: str = HINT) -> dict:
    """Check a lab profile's sections and keys; return them as ``Settings`` keyword arguments."""

    def fail(problem: str) -> ProfileError:
        return ProfileError(f"Lab profile {path}: {problem}" if path else problem, detail=hint)

    for section in raw:
        if section not in SCHEMA:
            raise fail(f"{section} is not a known section{_did_you_mean(section, SCHEMA)}.")
        if not isinstance(raw[section], dict):
            raise fail(f"{section} must be a [{section}] table.")
    for section in REQUIRED_SECTIONS:
        if section not in raw:
            raise fail(f"[{section}] is required{' (it may hold empty lists)' if section == 'protected' else ''}: {section} section missing.")

    values: dict = {}
    for section, keys in SCHEMA.items():
        given = raw.get(section, {})
        for key in given:
            if f"{section}.{key}" in RETIRED:
                raise fail(f"{section}.{key} is no longer a setting. {RETIRED[f'{section}.{key}']} Saving the setup page drops this key.")
            if key not in keys:
                raise fail(f"{section}.{key} is not a known setting{_did_you_mean(key, keys)}.")
        for key, (kind, default, field) in keys.items():
            name = f"{section}.{key}"
            if key in given:
                value = _check(kind, given[key], name, fail)
            elif default is _REQUIRED:
                raise fail(f"{name} is required.")
            else:
                value = _check(kind, default, name, fail) if kind in ("numbers", "texts", "subnets") else default
            if isinstance(field, tuple):
                values.update(zip(field, value))
            else:
                values[field] = value

    _check_pool_inside(values["mgmt_pool"], values["mgmt_cidr"], fail)
    return values


def without_retired(raw: dict) -> tuple[dict, list[dict]]:
    """A lab profile without the keys SimRack no longer reads, and each key left out with why."""
    kept: dict = {}
    left_out: list[dict] = []
    for section, keys in raw.items():
        if not isinstance(keys, dict):
            kept[section] = keys
            continue
        kept[section] = {}
        for key, value in keys.items():
            if f"{section}.{key}" in RETIRED:
                left_out.append({"key": f"{section}.{key}", "why": RETIRED[f"{section}.{key}"]})
            else:
                kept[section][key] = value
    return kept, left_out


def dump_profile(raw: dict) -> str:
    """A lab profile as TOML, its sections and keys in the order SCHEMA lists them."""
    lines = ["# SimRack's lab profile, saved from its setup page.", ""]
    for section, keys in SCHEMA.items():
        if isinstance(raw.get(section), dict):
            lines.append(f"[{section}]")
            lines += [f"{key} = {_toml(raw[section][key])}" for key in keys if key in raw[section]]
            lines.append("")
    return "\n".join(lines)


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    raise ProfileError(f"{value!r} cannot be written to a lab profile.")


def _did_you_mean(word: str, choices) -> str:
    close = difflib.get_close_matches(word, list(choices), n=1)
    return f" (did you mean {close[0]}?)" if close else ""


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check(kind: str, value, name: str, fail):
    if kind == "text":
        if not isinstance(value, str) or not value.strip():
            raise fail(f"{name} must be non-empty text.")
        return value.strip()
    if kind == "vlan":
        if not _whole(value) or not 1 <= value <= 4094:
            raise fail(f"{name} must be a VLAN number from 1 to 4094, or left out for an untagged port.")
        return value
    if kind == "numbers":
        if not isinstance(value, (list, tuple)) or not all(_whole(item) for item in value):
            raise fail(f"{name} must be a list of whole numbers, like [200, 201].")
        return frozenset(value)
    if kind == "texts":
        if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) and item.strip() for item in value):
            raise fail(f'{name} must be a list of names, like ["vmbr0"].')
        return frozenset(item.strip() for item in value)
    if kind == "subnet":
        return _subnet(value, name, fail)
    if kind == "subnets":
        if not isinstance(value, (list, tuple)):
            raise fail(f'{name} must be a list of subnets, like ["10.10.10.0/24"].')
        return tuple(_subnet(item, name, fail) for item in value)
    if kind == "range":
        if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(_whole(item) for item in value):
            raise fail(f"{name} must be [first, last], like [320, 399].")
        if value[0] > value[1]:
            raise fail(f"{name}: the first number must not be above the last.")
        return (value[0], value[1])
    if kind == "pool":
        return _pool(value, name, fail)
    if kind == "bool":
        if not isinstance(value, bool):
            raise fail(f"{name} must be true or false.")
        return value
    raise AssertionError(f"unknown kind {kind}")


def _subnet(value, name: str, fail) -> str:
    try:
        network = ipaddress.ip_network(str(value), strict=False)
    except ValueError:
        raise fail(f"{name}: {value!r} is not a subnet, like 10.10.10.0/24.") from None
    if not isinstance(value, str) or network.version != 4:
        raise fail(f"{name}: {value!r} is not an IPv4 subnet.")
    return str(network)


def _pool(value, name: str, fail) -> str:
    try:
        first, last = (ipaddress.IPv4Address(part.strip()) for part in str(value).split("-"))
    except ValueError:
        raise fail(f"{name} must be first-last addresses, like 192.0.2.200-192.0.2.249.") from None
    if first > last:
        raise fail(f"{name}: the first address must not be after the last.")
    return f"{first}-{last}"


def _check_pool_inside(pool: str, cidr: str, fail) -> None:
    network = ipaddress.ip_network(cidr)
    if any(ipaddress.IPv4Address(part) not in network for part in pool.split("-")):
        raise fail(f"management.pool {pool} is not inside management.cidr {cidr}.")
