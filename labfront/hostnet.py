"""Sandbox cable bridges, made directly with iproute2 (no shell).

The Proxmox network API stages a rewrite of /etc/network/interfaces and
reloads it, which is a production file. Sandbox bridges are throwaway, so they
are created at runtime only and never persisted; the service re-creates any
that are missing when it starts (see SandboxManager.ensure_bridges).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from .errors import BackendError, GuardrailViolation

#: Bridge-level mask, same as the live fabric (0xfff8): pass LLDP and friends.
BRIDGE_GROUP_FWD_MASK = 65528
#: Per-port mask on each tap: LLDP (bit 14) + LACP (bit 2). Bit 2 is refused at bridge level.
PORT_GROUP_FWD_MASK = 16388

_NAME = re.compile(r"^sbx[A-Za-z0-9_]{1,12}$")
_SYS = "/sys/class/net"


def _check(name: str) -> str:
    if not _NAME.fullmatch(name or ""):
        raise GuardrailViolation(f"{name!r} is not a sandbox bridge name.", detail="Sandbox bridges are sbx + up to 12 letters, digits or _.")
    return name


def _ip(*argv: str) -> str:
    try:
        done = subprocess.run(["ip", *argv], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackendError("Cannot run ip(8) on the host.", detail=str(error)) from error
    if done.returncode != 0:
        raise BackendError(f"ip {' '.join(argv)} failed.", detail=(done.stderr or done.stdout).strip()[:300])
    return done.stdout


def _write(path: str, value: int) -> None:
    with open(path, "w", encoding="ascii") as handle:
        handle.write(str(value))


def list_bridges() -> list[dict]:
    out = _ip("-j", "link", "show", "type", "bridge")
    try:
        links = json.loads(out or "[]")
    except json.JSONDecodeError:
        links = []
    return [{"iface": link.get("ifname"), "type": "bridge", "mtu": link.get("mtu")} for link in links]


def exists(name: str) -> bool:
    return os.path.isdir(f"{_SYS}/{name}/bridge")


def create(name: str, mtu: int) -> None:
    _check(name)
    if not exists(name):
        _ip("link", "add", "name", name, "mtu", str(int(mtu)), "type", "bridge", "stp_state", "0")
    _write(f"{_SYS}/{name}/bridge/group_fwd_mask", BRIDGE_GROUP_FWD_MASK)
    _ip("link", "set", "dev", name, "up")


def delete(name: str) -> None:
    _check(name)
    if exists(name):
        _ip("link", "del", "dev", name)


def tune_port(vmid: int, net_index: int) -> bool:
    """Let LACP through on a running guest's tap. False if the tap is not there."""
    tap = f"{_SYS}/tap{int(vmid)}i{int(net_index)}"
    master = os.path.realpath(f"{tap}/brport/bridge")
    if not os.path.isdir(tap) or not os.path.basename(master).startswith("sbx"):
        return False
    _write(f"{tap}/brport/group_fwd_mask", PORT_GROUP_FWD_MASK)
    return True
