"""Sandbox cable bridges, made directly with iproute2 (no shell).

The Proxmox network API stages a rewrite of /etc/network/interfaces and
reloads it, which is a production file. Sandbox bridges are throwaway, so they
are created at runtime only and never persisted; the service re-creates any
that are missing when it starts (see SandboxManager.ensure_bridges).
"""

from __future__ import annotations

import os
import re
import subprocess

from .config import IFNAME_MAX
from .errors import BackendError, GuardrailViolation

#: Bridge-level mask, same as the live fabric (0xfff8): pass LLDP and friends.
BRIDGE_GROUP_FWD_MASK = 65528
#: Per-port mask on each tap: LLDP (bit 14) + LACP (bit 2). Bit 2 is refused at bridge level.
PORT_GROUP_FWD_MASK = 16388

_NAME = re.compile(rf"[A-Za-z][A-Za-z0-9_]{{1,{IFNAME_MAX - 1}}}")
_SYS = "/sys/class/net"


def _check(name: str, prefix: str) -> str:
    """Only a bridge named with the lab profile's sandbox prefix, within Linux's limit."""
    if not prefix:
        raise GuardrailViolation(f"{name!r} is not a sandbox bridge name.", detail="SimRack has no sandbox bridge prefix, so it makes no bridges.")
    if not (name or "").startswith(prefix) or len(name) == len(prefix) or not _NAME.fullmatch(name):
        raise GuardrailViolation(
            f"{name!r} is not a sandbox bridge name.",
            detail=f"Sandbox bridges are {prefix} and then letters, digits or _, {IFNAME_MAX} characters at most.",
        )
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


def exists(name: str) -> bool:
    return os.path.isdir(f"{_SYS}/{name}/bridge")


def create(name: str, mtu: int, *, prefix: str) -> None:
    _check(name, prefix)
    if not exists(name):
        _ip("link", "add", "name", name, "mtu", str(int(mtu)), "type", "bridge", "stp_state", "0")
    _write(f"{_SYS}/{name}/bridge/group_fwd_mask", BRIDGE_GROUP_FWD_MASK)
    _ip("link", "set", "dev", name, "up")


def delete(name: str, *, prefix: str) -> None:
    _check(name, prefix)
    if exists(name):
        _ip("link", "del", "dev", name)


def tune_port(vmid: int, net_index: int, *, prefix: str) -> bool:
    """Let LACP through on a running guest's tap. False if the tap is not there,
    or is not in a sandbox bridge."""
    tap = f"{_SYS}/tap{int(vmid)}i{int(net_index)}"
    master = os.path.realpath(f"{tap}/brport/bridge")
    if not prefix or not os.path.isdir(tap) or not os.path.basename(master).startswith(prefix):
        return False
    _write(f"{tap}/brport/group_fwd_mask", PORT_GROUP_FWD_MASK)
    return True
