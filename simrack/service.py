"""SandboxManager: the business logic behind every button in the front end."""

from __future__ import annotations

import datetime as _dt
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
from typing import Callable

from . import fabric
from .access import POOL, Access, describe, raise_if
from .config import Settings
from .console import SerialConsole, mist_lines
from .console import adopt as console_adopt
from .errors import BackendError, GuardrailViolation, LabError, NotFound
from .guardrails import Guardrails
from .mist import MistClient
from .models import Link, MistSnapshot, Node, Sandbox, check_name, check_port
from .proxmox import ProxmoxClient
from .recipes import get_recipe, ip_clos_sandbox, list_recipes
from .shapes import plan_build, shape_from_mist

#: net0 is always the out-of-band management NIC, so a switch's ge-0/0/N is net N+1.
MGMT_NET_INDEX = 0
#: Guests with data ports: they get all of them, parked until cabled.
PORT_KINDS = ("switch",)
#: Root passwords: no 0/O, 1/I/l, so they can be read off a screen and typed.
_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bridge_name(prefix: str, a_vmid: int, b_vmid: int, a_port: str, b_port: str) -> str:
    """One bridge per cable, including the ports, so two cables between the same
    pair of switches get different bridges, and the name does not depend on which
    end is named first. The lab profile keeps it within Linux's 15 characters."""
    (low, pa), (high, pb) = sorted(((int(a_vmid), int(a_port.split("/")[-1])), (int(b_vmid), int(b_port.split("/")[-1]))))
    return f"{prefix}{low}_{high}_{pa}{pb}"


def _net_index(port: str) -> int:
    return int(port.split("/")[-1]) + 1


def _nic_token(value: str | None) -> str:
    """``virtio=<mac>`` for an existing NIC (keeping its MAC, whatever its model), else ``virtio``."""
    first = (value or "").split(",", 1)[0]
    if "=" in first:
        return "virtio=" + first.split("=", 1)[1]
    return "virtio"


def _snapshot_label(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", text)[:40]


def _nic_options(value: str | None) -> dict:
    """``virtio=<mac>,bridge=x,link_down=1`` as ``{"bridge": "x", "link_down": "1"}``."""
    options = {}
    for part in (value or "").split(",")[1:]:
        key, _, val = part.partition("=")
        options[key.strip()] = val.strip()
    return options


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _new_password() -> str:
    while True:
        password = "-".join("".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(4)) for _ in range(4))
        if re.search(r"[A-Z]", password) and re.search(r"[a-z]", password) and re.search(r"[0-9]", password):
            return password


#: Generated Junos lines that carry a secret, a hash or a $9$ (reversible) string.
_SECRET_CLI = re.compile(r"password|secret|authentication-key|pre-shared-key|community|\$[1569]\$", re.IGNORECASE)


def _without_root_password(config: dict) -> tuple[dict, bool]:
    """``config`` minus ``switch_mgmt.root_password``, and whether it had one."""
    mgmt = config.get("switch_mgmt")
    if not isinstance(mgmt, dict) or "root_password" not in mgmt:
        return config, False
    return {**config, "switch_mgmt": {key: value for key, value in mgmt.items() if key != "root_password"}}, True


def _with_root_password(config: dict, password: str) -> dict:
    return {**config, "switch_mgmt": {**(config.get("switch_mgmt") or {}), "root_password": password}}


def _cli_without_secrets(reply: object) -> dict:
    """Mist's generated config for a switch, minus every line that holds a secret."""
    lines = reply.get("cli") if isinstance(reply, dict) else None
    if not isinstance(lines, list):
        return {}
    return {"cli": [line for line in lines if isinstance(line, str) and not _SECRET_CLI.search(line)]}


class SandboxManager:
    def __init__(
        self,
        settings: Settings | None = None,
        proxmox: ProxmoxClient | None = None,
        mist: MistClient | None = None,
        clock: Callable[[], str] = _now,
    ) -> None:
        self.settings = settings or Settings()
        self.proxmox = proxmox or ProxmoxClient(self.settings)
        self.mist = mist or MistClient(self.settings, write_gate=lambda: self.access.mist_refusal())
        self.access = Access(self.settings, self.proxmox, self.mist)
        self.guard = Guardrails(self.settings)
        self.clock = clock
        self.state_dir = self.settings.state_dir
        self.sandboxes: dict[str, Sandbox] = {}
        self.shapes: dict[str, dict] = {}
        # Reveal runs outside the API's write lock; this keeps a password made once.
        self._secret_lock = threading.Lock()
        self._load()
        self._load_shapes()

    def use(self, settings: Settings) -> None:
        """Take new settings from the setup page without a restart."""
        self.settings = settings
        self.guard = Guardrails(settings)
        self.proxmox.use(settings)
        self.mist.use(settings)
        self.access.settings = settings
        self.access.forget()

    # -- persistence ------------------------------------------------------------

    def _path(self, name: str) -> str:
        return os.path.join(self.state_dir, "sandboxes", f"{name}.json")

    def _load(self) -> None:
        folder = os.path.join(self.state_dir, "sandboxes")
        if not os.path.isdir(folder):
            return
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, filename), encoding="utf-8") as handle:
                    sandbox = Sandbox.from_dict(json.load(handle))
            except (OSError, ValueError, KeyError) as error:
                raise LabError(f"Cannot read saved sandbox {filename}: {error}") from error
            self.sandboxes[sandbox.name] = sandbox

    def _save(self, sandbox: Sandbox) -> None:
        os.makedirs(os.path.dirname(self._path(sandbox.name)), exist_ok=True)
        tmp = self._path(sandbox.name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(sandbox.to_dict(), handle, indent=2, sort_keys=True)
        os.replace(tmp, self._path(sandbox.name))

    # -- per-sandbox root password: its own 0600 file, never in state or the API.

    def _secret_path(self, name: str) -> str:
        return os.path.join(self.state_dir, "secrets", f"{check_name(name, 'sandbox name')}.json")

    @staticmethod
    def _write_private(path: str, data: bytes) -> None:
        """Atomic, and readable only by SimRack's own user: 0600 in a 0700 folder."""
        folder = os.path.dirname(path)
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        tmp = path + ".tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as out:
            os.fchmod(out.fileno(), 0o600)
            out.write(data)
        os.replace(tmp, path)

    def _write_secret(self, name: str, password: str) -> None:
        self._write_private(self._secret_path(name), json.dumps({"user": "root", "root_password": password}).encode("utf-8"))

    def _root_password(self, sandbox: Sandbox) -> str:
        """The sandbox's root password, made the first time it is needed."""
        with self._secret_lock:
            try:
                with open(self._secret_path(sandbox.name), encoding="utf-8") as handle:
                    password = json.load(handle).get("root_password")
                if isinstance(password, str) and password:
                    return password
            except (OSError, ValueError, AttributeError):
                pass
            password = _new_password()
            self._write_secret(sandbox.name, password)
            return password

    def reveal_root_password(self, sandbox: Sandbox) -> dict:
        """Read-only on purpose: the operator needs it to log in even when SimRack may not change the lab."""
        return {"user": "root", "root_password": self._root_password(sandbox)}

    def _forget_secret(self, name: str) -> None:
        try:
            os.remove(self._secret_path(name))
        except FileNotFoundError:
            pass

    def _mist_snapshot_dir(self, name: str) -> str:
        return os.path.join(self.state_dir, "mist-snapshots", check_name(name, "sandbox name"))

    def set_paused(self, paused: bool) -> dict:
        """Stop or resume every change. Pausing changes nothing in the lab, so it is always allowed."""
        self.access.set_paused(paused)
        return {"paused": self.access.paused()}

    def get(self, name: str) -> Sandbox:
        try:
            return self.sandboxes[name]
        except KeyError as error:
            raise NotFound(f"No sandbox named {name}.", detail=f"Known sandboxes: {', '.join(sorted(self.sandboxes)) or 'none'}") from error

    def list_sandboxes(self) -> list[dict]:
        return [self.sandboxes[name].to_dict() for name in sorted(self.sandboxes)]

    # -- shapes: fabric designs imported from Mist. Local files only, so they work read-only.

    def _shape_path(self, name: str) -> str:
        return os.path.join(self.state_dir, "shapes", f"{check_name(name, 'shape name')}.json")

    def _load_shapes(self) -> None:
        folder = os.path.join(self.state_dir, "shapes")
        if not os.path.isdir(folder):
            return
        for filename in sorted(os.listdir(folder)):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(folder, filename), encoding="utf-8") as handle:
                    shape = json.load(handle)
                check_name(shape["name"], "shape name")
            except (OSError, ValueError, KeyError, TypeError):
                continue  # a damaged shape is not worth refusing to start over
            self.shapes[shape["name"]] = shape

    def import_shape(self, doc) -> dict:
        """Read a fabric's design from Mist JSON and keep it. Re-importing replaces it."""
        shape = shape_from_mist(doc, now=self.clock())
        path = self._shape_path(shape["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            json.dump(shape, handle, indent=2, sort_keys=True)
        os.replace(path + ".tmp", path)
        self.shapes[shape["name"]] = shape
        return shape

    def list_shapes(self) -> list[dict]:
        return [self.shapes[name] for name in sorted(self.shapes)]

    def get_shape(self, name: str) -> dict:
        self._shape_path(name)
        try:
            return self.shapes[name]
        except KeyError as error:
            raise NotFound(f"No shape named {name}.", detail=f"Known shapes: {', '.join(sorted(self.shapes)) or 'none'}") from error

    def delete_shape(self, name: str) -> bool:
        self.get_shape(name)
        try:
            os.remove(self._shape_path(name))
        except FileNotFoundError:
            pass
        del self.shapes[name]
        return True

    # -- read-only views --------------------------------------------------------

    def state(self) -> dict:
        """Everything the UI needs. Never mutates anything."""
        free_ram = total_ram = None
        inventory_error = ""
        try:
            memory = self.proxmox.memory_mb()
            free_ram, total_ram = memory["free"], memory["total"] or None
            vms = [
                {"vmid": v["vmid"], "name": v.get("name", ""), "status": v.get("status"), "kind": "qemu", "template": v.get("template") == 1}
                for v in self.proxmox.list_vms()
            ]
        except (BackendError, LabError) as error:
            inventory_error = error.message
            vms = []
        lab_refusal, mist_refusal = self.access.lab_refusal(), self.access.mist_refusal()
        return {
            "generated_at": self.clock(),
            "production": {
                "vmids": sorted(self.settings.production_vmids),
                "bridges": sorted(self.settings.production_bridges),
                "mist_sites": sorted(self.settings.production_mist_sites),
            },
            "limits": {
                "vmid_range": [self.settings.sandbox_vmid_start, self.settings.sandbox_vmid_end],
                "switch_mem_mb": self.settings.switch_mem_mb,
                "min_free_ram_mb": self.settings.min_free_ram_mb,
                "switch_ports": self.settings.switch_ports,
            },
            "host": {"node": self.settings.pve_node, "free_ram_mb": free_ram, "total_ram_mb": total_ram, "inventory_error": inventory_error},
            "vms": vms,
            "recipes": list_recipes(),
            "sandboxes": self.list_sandboxes(),
            "shapes": self.list_shapes(),
            "mist": {
                "configured": self.mist.configured(),
                "writes_enabled": mist_refusal is None,
                "read_only_reason": describe(mist_refusal),
                "api_base": self.settings.mist_api_base,
            },
            "templates": self._list_templates(),
            "images": self._list_images(),
            "writes_enabled": lab_refusal is None,
            "read_only_reason": describe(lab_refusal),
            "paused": self.access.paused(),
            "profile": {"loaded": bool(self.settings.profile_path), "path": self.settings.profile_path},
            "assistants": {"risky": self.settings.assistant_risky},
        }

    def _list_images(self) -> list[dict]:
        images = []
        try:
            for item in self.proxmox.list_images():
                try:
                    kind = self.guard.check_image(item.get("volid", ""))
                except GuardrailViolation:
                    continue
                images.append({"volid": item["volid"], "kind": kind, "size_gb": round((item.get("size") or 0) / 1024**3, 1)})
        except (BackendError, LabError, AttributeError):
            return []
        return images

    def _list_templates(self) -> list[dict]:
        try:
            return [
                {"vmid": vm["vmid"], "name": vm.get("name", ""), "memory_mb": vm.get("maxmem", 0) // (1024 * 1024)}
                for vm in self.proxmox.list_vms()
                if vm.get("template") == 1
            ]
        except (BackendError, LabError):
            return []

    # -- sandboxes --------------------------------------------------------------

    def create_sandbox(
        self,
        name: str,
        recipe_name: str = "collapsed-core",
        *,
        template_vmid: int | None = None,
        start: bool = True,
        with_mist_site: bool = False,
        notes: str = "",
        image: str | None = None,
    ) -> Sandbox:
        """Build a sandbox from a recipe. The live lab is never referenced."""
        self.access.check_lab()
        check_name(name, "sandbox name")
        if name in self.sandboxes:
            raise GuardrailViolation(f"Sandbox {name} already exists.", detail="Use a different name or tear it down first.")
        recipe = get_recipe(recipe_name)
        recipe.site_name = f"Sandbox {name}"

        sandbox = Sandbox(name=name, created_at=self.clock(), recipe=recipe, mist_site_name=recipe.site_name)
        if notes:
            sandbox.notes.append(notes)

        roles = list(recipe.roles)
        if not roles:
            roles = [{"name": "sbx-acc-01", "role": "access", "ports": 4}]
        self.guard.check_ram(self.proxmox.free_memory_mb(), len(roles))

        try:
            for role in roles:
                self.provision_node(
                    sandbox,
                    role["name"],
                    role=role.get("role", "access"),
                    kind=role.get("kind", "switch"),
                    template_vmid=template_vmid,
                    image=image,
                    start=start,
                    save=False,
                )
            self._cable_recipe(sandbox)
            if with_mist_site:
                self.mist_create_site(sandbox)
        except Exception:
            self._rollback_build(sandbox)
            raise
        source = f" with template vmid {template_vmid}" if template_vmid else f" from {image}" if image else ""
        sandbox.notes.append(f"Built from recipe {recipe_name}{source}")
        self.sandboxes[name] = sandbox
        self._save(sandbox)
        self._root_password(sandbox)
        return sandbox

    def build_from_shape(
        self,
        shape_name: str,
        name: str,
        *,
        switches: list[str] | None = None,
        template_vmid: int | None = None,
        image: str | None = None,
        start: bool = True,
        with_mist_site: bool = False,
    ) -> dict:
        """Build a sandbox shaped like an imported fabric: one vJunos per ticked
        switch, cabled port for port. Cables a sandbox cannot carry are left out
        and listed in ``dropped``. Everything is checked before anything is made."""
        self.access.check_lab()
        check_name(name, "sandbox name")
        if name in self.sandboxes:
            raise GuardrailViolation(f"Sandbox {name} already exists.", detail="Use a different name or tear it down first.")
        shape = self.get_shape(shape_name)
        plan = plan_build(shape, switches, ports=self.settings.switch_ports)
        if image:
            self.guard.check_image(image)
        elif not template_vmid:
            raise LabError(
                "A template or an image is required.",
                detail="Pass template_vmid (a clean vJunos template) or image (local:import/<disk>.qcow2).",
            )
        else:
            template_vmid = self.guard.check_template(template_vmid)
        self.guard.check_ram(self.proxmox.free_memory_mb(), len(plan["nodes"]))

        total = len(shape.get("nodes") or [])
        description = f"Built from shape {shape['name']} ({len(plan['nodes'])} of {total} switches)."
        evpn = shape.get("evpn") or {}
        recipe = ip_clos_sandbox()
        recipe.name = shape["name"]
        recipe.description = description
        recipe.site_name = f"Sandbox {name}"
        recipe.overlay_as = evpn.get("overlay_as", recipe.overlay_as)
        recipe.shape = shape["name"]
        recipe.topology_kind = shape.get("kind")
        recipe.routed_at = evpn.get("routed_at")
        recipe.underlay_as_base = evpn.get("underlay_as_base")
        recipe.roles = [{"name": n["name"], "role": n.get("role") or "access", "ports": 4, "pod": n.get("pod")} for n in plan["nodes"]]

        sandbox = Sandbox(name=name, created_at=self.clock(), recipe=recipe, mist_site_name=recipe.site_name)
        try:
            for item in plan["nodes"]:
                node = self.provision_node(
                    sandbox,
                    item["name"],
                    role=item.get("role") or "access",
                    template_vmid=template_vmid,
                    image=image,
                    start=start,
                    save=False,
                )
                node.pod = item.get("pod")
            for link in plan["links"]:
                self.cable(sandbox, link["a_node"], link["a_port"], link["b_node"], link["b_port"], save=False)
            if with_mist_site:
                self.mist_create_site(sandbox)
        except Exception:
            self._rollback_build(sandbox)
            raise
        sandbox.notes.append(description)
        if plan["dropped"]:
            sandbox.notes.append(
                f"Left out {len(plan['dropped'])} cable(s): "
                + "; ".join(f"{d['a_node']} {d['a_port']} - {d['b_node']} {d['b_port']} ({d['reason']})" for d in plan["dropped"])
            )
        self.sandboxes[name] = sandbox
        self._save(sandbox)
        self._root_password(sandbox)
        return {"sandbox": sandbox, "dropped": plan["dropped"]}

    def provision_node(
        self,
        sandbox: Sandbox,
        name: str,
        *,
        role: str = "access",
        kind: str = "switch",
        template_vmid: int | None = None,
        image: str | None = None,
        storage: str = "local-lvm",
        start: bool = True,
        save: bool = True,
    ) -> Node:
        """Add one guest: a clone of the vJunos template, or a boot from an image."""
        self.access.check_lab()
        check_name(name, "node name")
        template_vmid = self.guard.check_template(template_vmid)
        if any(n.name == name for n in sandbox.nodes):
            raise GuardrailViolation(f"{name} already exists in {sandbox.name}.")
        if template_vmid and not image and str(self.proxmox.get_vm(template_vmid).get("template", 0)) != "1":
            raise GuardrailViolation(
                f"vmid {template_vmid} is not a template.",
                detail="SimRack clones only a Proxmox template. Make one with qm template <vmid> "
                "from a vJunos that has never booted (ADVICE.md, step 2).",
            )
        if template_vmid and not image:
            self.access.check_clone(template_vmid)

        start_id = self.settings.sandbox_lxc_start if kind == "client" else self.settings.sandbox_vmid_start
        end_id = self.settings.sandbox_lxc_end if kind == "client" else self.settings.sandbox_vmid_end
        vmid = self._next_free_vmid(start_id, end_id)
        self.guard.check_vmid(vmid)

        if kind == "switch":
            self.guard.check_ram(self.proxmox.free_memory_mb(), 1)

        node = Node(
            name=name,
            vmid=vmid,
            role=role,
            kind=kind,
            template_vmid=template_vmid,
            image=image,
            storage=storage,
            mgmt_ip=self._next_mgmt_ip(sandbox),
        )

        memory = self.settings.switch_mem_mb if kind == "switch" else 2048
        if image:
            image_kind = self.guard.check_image(image)
        elif not template_vmid:
            raise LabError(
                "A template or an image is required.",
                detail="Pass template_vmid (a clean vJunos template) or image "
                "(local:import/<disk>.qcow2 or local:iso/<installer>.iso).",
            )
        if kind in PORT_KINDS:
            self._ensure_park()

        created = False  # the vmid is SimRack's once Proxmox accepts the create or clone
        try:
            if image:
                nics = self._switch_nics({}) if kind in PORT_KINDS else {}
                upid = self.proxmox.create_vm(
                    vmid,
                    name,
                    pool=POOL,
                    memory_mb=memory,
                    cores=4,
                    storage=storage,
                    iso=image if image_kind == "iso" else None,
                    import_from=image if image_kind == "import" else None,
                    disk_bus="virtio0" if kind == "switch" else "scsi0",
                    # vJunos-switch checks this SMBIOS product and runs a nested VM.
                    smbios_product="VM-VEX" if kind == "switch" else None,
                    cpu="host" if kind == "switch" else None,
                    extra={key: value for key, value in nics.items() if value is not None} or None,
                )
                created = True
                self.proxmox.wait_task(upid, timeout=1800)
            else:
                upid = self.proxmox.clone_vm(template_vmid, vmid, name=name, pool=POOL, full=True)
                created = True
                self.proxmox.wait_task(upid, timeout=1800)
                nics = self._switch_nics(self.proxmox.get_vm(vmid)) if kind in PORT_KINDS else {}
                self.proxmox.set_vm_config(vmid, memory=memory, cores=4, onboot=0, **nics)
            if start:
                self.proxmox.wait_task(self.proxmox.set_power(vmid, "start"), timeout=120)
        except Exception:
            if created:
                try:
                    self._destroy_vm(vmid, name)
                except LabError:
                    pass
            raise

        node.running = start
        sandbox.nodes.append(node)
        if save:
            self._save(sandbox)
        return node

    # -- data ports -------------------------------------------------------------
    # A switch always has every port: net0 is fxp0 on the management VLAN and
    # net1..N are ge-0/0/0..N-1. A port with no cable sits link-down on the park
    # bridge, so Junos sees it as unplugged rather than missing.

    def _parked(self) -> str:
        return f"bridge={self.settings.park_bridge},firewall=0,link_down=1"

    def _switch_nics(self, existing: dict) -> dict:
        mgmt = f"bridge={self.settings.mgmt_bridge}"
        if self.settings.mgmt_vlan is not None:
            mgmt += f",tag={self.settings.mgmt_vlan}"
        nics = {f"net{MGMT_NET_INDEX}": f"{_nic_token(existing.get('net0'))},{mgmt}"}
        for index in range(1, self.settings.switch_ports + 1):
            nics[f"net{index}"] = f"{_nic_token(existing.get(f'net{index}'))},{self._parked()}"
        for key in existing:
            if re.fullmatch(r"net\d+", key) and key not in nics:
                nics[key] = None
        return nics

    def _ensure_park(self) -> None:
        park = self.guard.check_bridge(self.settings.park_bridge)
        if not self.proxmox.bridge_exists(park):
            self.proxmox.create_bridge(park, mtu=self.settings.fabric_mtu)

    def _drop_park_if_unused(self) -> None:
        if any(node.kind in PORT_KINDS for sandbox in self.sandboxes.values() for node in sandbox.nodes):
            return
        try:
            if self.proxmox.bridge_exists(self.settings.park_bridge):
                self.proxmox.delete_bridge(self.guard.check_bridge(self.settings.park_bridge))
        except LabError:
            pass

    def _park(self, node: Node, index: int, config: dict | None = None) -> None:
        if config is None:
            config = self.proxmox.get_vm(node.vmid)
        self.proxmox.set_vm_config(node.vmid, **{f"net{index}": f"{_nic_token(config.get(f'net{index}'))},{self._parked()}"})

    def _check_port_in_range(self, port: str) -> None:
        last = self.settings.switch_ports - 1
        if int(port.split("/")[-1]) > last:
            raise GuardrailViolation(
                f"{port} is past ge-0/0/{last}.",
                detail=f"A sandbox switch has ge-0/0/0 to ge-0/0/{last}.",
            )

    def _next_free_vmid(self, start: int, end: int) -> int:
        """Free across every sandbox and every guest Proxmox already knows about,
        so two sandboxes can never be handed the same vmid."""
        used = {node.vmid for other in self.sandboxes.values() for node in other.nodes}
        used |= set(self.settings.production_vmids) | set(self.settings.production_lxc)
        # No guessing: a vmid picked from a partial list could already be someone's guest.
        # VMs and containers share one set of vmids, so both lists count.
        try:
            used |= {int(guest["vmid"]) for guest in (*self.proxmox.list_vms(), *self.proxmox.list_lxc())}
        except (KeyError, TypeError, ValueError) as error:
            raise BackendError(
                "Proxmox listed its guests in a shape SimRack does not know.",
                detail="SimRack picks a vmid only from the full list of guests, so it built nothing.",
            ) from error
        for candidate in range(start, end + 1):
            if candidate not in used:
                return candidate
        raise GuardrailViolation(
            f"No free vmid in {start}-{end}.",
            detail="Every sandbox slot is taken. Tear a sandbox down or widen the range.",
        )

    def _next_mgmt_ip(self, sandbox: Sandbox) -> str:
        """First free address in the lab profile's management pool across every
        sandbox, so two sandboxes never plan the same fxp0 address."""
        used = {n.mgmt_ip for other in self.sandboxes.values() for n in other.nodes if n.mgmt_ip}
        used |= {n.mgmt_ip for n in sandbox.nodes if n.mgmt_ip}
        first, last = (ipaddress.IPv4Address(end) for end in self.settings.mgmt_pool.split("-"))
        for number in range(int(first), int(last) + 1):
            address = str(ipaddress.IPv4Address(number))
            if address not in used:
                return address
        raise GuardrailViolation(
            f"The management pool {self.settings.mgmt_pool} is full.",
            detail="Every address in it is planned for a sandbox switch. Tear a sandbox down, "
            "or widen [management] pool in the lab profile.",
        )

    def _guest(self, vmid: int) -> dict | None:
        """The VM Proxmox has at this vmid, read from the full list, or None."""
        try:
            guests = {int(vm["vmid"]): vm for vm in self.proxmox.list_vms()}
        except (KeyError, TypeError, ValueError) as error:
            raise BackendError(
                "Proxmox listed its guests in a shape SimRack does not know.",
                detail="SimRack deletes a guest only after it has checked the guest's name, so it deleted nothing.",
            ) from error
        return guests.get(int(vmid))

    def _own_guest(self, vmid: int, name: str) -> dict | None:
        """The guest SimRack made at this vmid, or None once it is gone. Refuses a
        guest Proxmox knows by another name: renamed, or another guest in its place."""
        self.guard.check_vmid(vmid)
        guest = self._guest(vmid)
        if guest is not None and guest.get("name") != name:
            other = guest.get("name") or "a guest with no name"
            raise GuardrailViolation(
                f"vmid {vmid} is {other} in Proxmox now, not {name}.",
                detail="Something outside SimRack renamed it or put another guest there. SimRack deletes "
                f"only guests it made, so it left this one alone and kept {name} in the sandbox. "
                "If it should go, delete it in Proxmox, then try again.",
            )
        return guest

    def _destroy_vm(self, vmid: int, name: str) -> None:
        """Stop (PVE refuses to delete a running guest), then delete and wait."""
        if self._own_guest(vmid, name) is None:
            return
        if self.proxmox.vm_status(vmid).get("status") == "running":
            self.proxmox.wait_task(self.proxmox.set_power(vmid, "stop", timeout=60), timeout=120)
        self.proxmox.wait_task(self.proxmox.delete_vm(vmid, purge=True), timeout=300)

    def _rollback_build(self, sandbox: Sandbox) -> None:
        """A build failed part way: remove what it made so nothing is orphaned."""
        clean = True
        for node in sandbox.nodes:
            try:
                self._destroy_vm(node.vmid, node.name)
            except LabError:
                clean = False
        for link in sandbox.links:
            try:
                self.proxmox.delete_bridge(link.bridge)
            except LabError:
                pass
        if clean:
            self._drop_park_if_unused()

    def ensure_bridges(self) -> list[str]:
        """Sandbox bridges are not persistent; put back any a host reboot removed.
        The park bridge comes first: no switch can start without it.
        Puts back nothing while the lab may not change."""
        if self.access.lab_refusal() is not None:
            return []
        made = []
        wanted = []
        if any(node.kind in PORT_KINDS for sandbox in self.sandboxes.values() for node in sandbox.nodes):
            wanted.append(self.settings.park_bridge)
        wanted += [link.bridge for sandbox in self.sandboxes.values() for link in sandbox.links]
        for bridge in wanted:
            try:
                self.guard.check_bridge(bridge)
                if not self.proxmox.bridge_exists(bridge):
                    self.proxmox.create_bridge(bridge, mtu=self.settings.fabric_mtu)
                    made.append(bridge)
            except LabError:
                continue
        return made

    def delete_node(self, sandbox: Sandbox, name: str, *, purge: bool = True) -> None:
        self.access.check_lab()
        node = self.guard.check_node_is_sandbox(sandbox, name)
        if purge:
            self._own_guest(node.vmid, node.name)  # before any cable moves, so a refusal changes nothing
        for link in [cable for cable in sandbox.links if name in (cable.a_node, cable.b_node)]:
            self.remove_cable(sandbox, link.bridge, save=False)
        if purge:
            self._destroy_vm(node.vmid, node.name)
        sandbox.nodes = [n for n in sandbox.nodes if n.name != name]
        sandbox.notes.append(f"Deleted {name} (vmid {node.vmid})")
        self._save(sandbox)

    # -- power ------------------------------------------------------------------

    def set_power(self, sandbox: Sandbox, name: str, action: str, *, save: bool = True) -> dict:
        """Bring a sandbox switch up or down without touching Proxmox by hand."""
        self.access.check_lab()
        node = self.guard.check_node_is_sandbox(sandbox, name)
        running = self.proxmox.vm_status(node.vmid).get("status") == "running"
        if action == "start" and node.kind == "switch" and not running:
            self.guard.check_ram(self.proxmox.free_memory_mb(), 1)
        # A Proxmox reboot task ends once the guest is down, and Proxmox starts
        # it again later on new taps, too late to reopen LACP. So shut down, then
        # start; and, as Proxmox does, reboot only a running guest.
        steps = (("shutdown", "start") if running else ()) if action == "reboot" else (action,)
        for step in steps:
            self.proxmox.wait_task(self.proxmox.set_power(node.vmid, step), timeout=180)
        status = self.proxmox.vm_status(node.vmid)
        node.running = status.get("status") == "running"
        if node.running and action in ("start", "reboot"):
            self._reopen_lacp(sandbox, node)
        if save:
            self._save(sandbox)
        return {"node": name, "vmid": node.vmid, "action": action, "status": status.get("status", "unknown")}

    def _reopen_lacp(self, sandbox: Sandbox, node: Node) -> None:
        """A start gives the guest new taps, and a new tap drops LACP. The
        template hookscript is optional, so SimRack opens them itself."""
        for link in sandbox.links:
            for end_node, port in link.endpoints():
                if end_node == node.name:
                    self.proxmox.tune_port(node.vmid, _net_index(port))

    # -- cabling ----------------------------------------------------------------

    def _cable_recipe(self, sandbox: Sandbox) -> None:
        """Cable the built nodes the way the live lab is cabled.

        Each core gets its own access ports and each access its own core ports,
        so a 2x2 clos uses ge-0/0/2 and ge-0/0/3 on every switch, the way a
        border/core/access campus fabric is usually cabled.
        """
        MAX_PORT = self.settings.switch_ports - 1
        cores = [n for n in sandbox.nodes if n.role == "core"]
        accesses = [n for n in sandbox.nodes if n.role == "access"]
        borders = [n for n in sandbox.nodes if n.role == "border"]
        # Core ge-0/0/0-1 face the borders, ge-0/0/2-3 face the access switches.
        for core_index, core in enumerate(cores):
            for access_index, access in enumerate(accesses):
                core_port = 2 + access_index
                access_port = 2 + core_index
                if core_port > MAX_PORT or access_port > MAX_PORT:
                    sandbox.notes.append(
                        f"Skipped {core.name}<->{access.name}: would need ge-0/0/{core_port} "
                        f"and ge-0/0/{access_port}, past the last port (ge-0/0/{MAX_PORT})"
                    )
                    continue
                self.cable(sandbox, core.name, f"ge-0/0/{core_port}", access.name, f"ge-0/0/{access_port}", save=False)
        for border_index, border in enumerate(borders):
            for core_index, core in enumerate(cores):
                if border_index > 1 or core_index > MAX_PORT:
                    sandbox.notes.append(f"Skipped {border.name}<->{core.name}: no free border port on {core.name}")
                    continue
                self.cable(sandbox, border.name, f"ge-0/0/{core_index}", core.name, f"ge-0/0/{border_index}", save=False)

    def cable(
        self,
        sandbox: Sandbox,
        a_node: str,
        a_port: str,
        b_node: str,
        b_port: str,
        *,
        save: bool = True,
    ) -> Link:
        """Plug a cable in. Creates the MTU 9216 bridge and moves both NICs."""
        self.access.check_lab()
        check_port(a_port)
        check_port(b_port)
        self._check_port_in_range(a_port)
        self._check_port_in_range(b_port)
        node_a = self.guard.check_node_is_sandbox(sandbox, a_node)
        node_b = self.guard.check_node_is_sandbox(sandbox, b_node)
        if node_a.vmid == node_b.vmid:
            raise GuardrailViolation("A cable cannot connect a switch to itself.")

        for node, port in ((node_a, a_port), (node_b, b_port)):
            for existing in sandbox.links:
                if (existing.a_node, existing.a_port) == (node.name, port) or (existing.b_node, existing.b_port) == (node.name, port):
                    raise GuardrailViolation(
                        f"{node.name} {port} is already cabled to "
                        f"{existing.b_node if existing.a_node == node.name else existing.a_node}.",
                        detail="Unplug it first, or use a different port.",
                    )

        bridge = self.guard.check_bridge(_bridge_name(self.settings.sandbox_bridge_prefix, node_a.vmid, node_b.vmid, a_port, b_port))
        if any(existing.bridge == bridge for existing in sandbox.links):
            raise GuardrailViolation(f"Bridge {bridge} is already in use.", detail="Pick different ports.")
        link = Link(a_node=a_node, a_port=a_port, b_node=b_node, b_port=b_port, bridge=bridge)
        self.proxmox.create_bridge(link.bridge, mtu=self.settings.fabric_mtu)

        self._attach(sandbox, node_a, a_port, link.bridge)
        self._attach(sandbox, node_b, b_port, link.bridge)
        sandbox.links.append(link)
        sandbox.notes.append(f"Cabled {a_node} {a_port} <-> {b_node} {b_port} on {link.bridge}")
        if save:
            self._save(sandbox)
        return link

    def _attach(self, sandbox: Sandbox, node: Node, port: str, bridge: str) -> None:
        index = _net_index(port)
        if index == MGMT_NET_INDEX:
            raise GuardrailViolation(
                f"{port} would collide with the management NIC.",
                detail="ge-0/0/-1 does not exist; net0 is out-of-band management.",
            )
        config = self.proxmox.get_vm(node.vmid)
        self.proxmox.set_vm_config(node.vmid, **{f"net{index}": f"{_nic_token(config.get(f'net{index}'))},bridge={bridge},firewall=0"})
        self.proxmox.tune_port(node.vmid, index)

    def move_cable(self, sandbox: Sandbox, bridge: str, to_node: str, to_port: str, from_node: str | None = None) -> dict:
        """Unplug one end of a cable and plug it somewhere else.

        Naming a node that is already one end of the cable moves *that* end, so
        "move sbx-core-01 ge-0/0/1" pulls the core's plug out of whatever port it
        is in and puts it in ge-0/0/1. Pass ``from_node`` to move a specific end
        when the target node is not on this cable.
        """
        self.access.check_lab()
        self.guard.check_bridge(bridge)
        link = next((cable for cable in sandbox.links if cable.bridge == bridge), None)
        if link is None:
            raise NotFound(f"No cable on bridge {bridge}.", detail="Nothing to move.")
        target = self.guard.check_node_is_sandbox(sandbox, to_node)
        check_port(to_port)
        self._check_port_in_range(to_port)

        if from_node is None:
            if to_node == link.a_node:
                side = "a"
            elif to_node == link.b_node:
                side = "b"
            else:
                raise GuardrailViolation(
                    f"{to_node} is not on this cable.",
                    detail="Name a node that is an endpoint, or pass from_node to say which end to move.",
                )
        else:
            self.guard.check_node_is_sandbox(sandbox, from_node)
            if from_node == link.a_node:
                side = "a"
            elif from_node == link.b_node:
                side = "b"
            else:
                raise GuardrailViolation(f"{from_node} is not on this cable.")

        moving = sandbox.node(link.a_node if side == "a" else link.b_node)
        moving_port = link.a_port if side == "a" else link.b_port
        if to_node == moving.name and moving_port == to_port:
            raise GuardrailViolation(
                f"{moving.name} {to_port} is where the cable already is.",
                detail="Pick a different port.",
            )

        # The target port must be free on the target switch, ignoring this cable.
        for other in sandbox.links:
            if other.bridge == bridge:
                continue
            if (other.a_node, other.a_port) == (to_node, to_port) or (other.b_node, other.b_port) == (to_node, to_port):
                peer = other.b_node if other.a_node == to_node else other.a_node
                raise GuardrailViolation(
                    f"{to_node} {to_port} is already cabled to {peer}.",
                    detail="Unplug it first, or use a different port.",
                )

        staying = link.b_node if side == "a" else link.a_node
        if to_node == staying:
            raise GuardrailViolation(
                "A cable cannot connect a switch to itself.",
                detail=f"{staying} is already the other end of this cable. Pick a different guest.",
            )

        new_index = _net_index(to_port)
        old_index = _net_index(moving_port)
        if target.vmid == moving.vmid:
            # Same switch, new port: one config change, so both ports flip together.
            config = self.proxmox.get_vm(moving.vmid)
            self.proxmox.set_vm_config(
                moving.vmid,
                **{
                    f"net{new_index}": f"{_nic_token(config.get(f'net{new_index}'))},bridge={bridge},firewall=0",
                    f"net{old_index}": f"{_nic_token(config.get(f'net{old_index}'))},{self._parked()}",
                },
            )
            self.proxmox.tune_port(moving.vmid, new_index)
        else:
            # Another switch: the new one plugs in, as cable() would, then the old one is parked.
            self._attach(sandbox, target, to_port, bridge)
            self._park(moving, old_index)

        if side == "a":
            link.a_node, link.a_port = to_node, to_port
        else:
            link.b_node, link.b_port = to_node, to_port
        sandbox.notes.append(f"Moved {bridge} from {moving.name} {moving_port} to {to_node} {to_port}")
        self._save(sandbox)
        return {"link": link.to_dict(), "moved_from": f"{moving.name} {moving_port}", "moved_to": f"{to_node} {to_port}"}

    def remove_cable(self, sandbox: Sandbox, bridge: str, *, save: bool = True) -> None:
        self.access.check_lab()
        self.guard.check_bridge(bridge)
        link = next((cable for cable in sandbox.links if cable.bridge == bridge), None)
        if link is None:
            raise NotFound(f"No cable on bridge {bridge}.")
        for node_name, port in link.endpoints():
            self._park(sandbox.node(node_name), _net_index(port))
        self.proxmox.delete_bridge(bridge)
        sandbox.links = [cable for cable in sandbox.links if cable.bridge != bridge]
        sandbox.notes.append(f"Unplugged {bridge}")
        if save:
            self._save(sandbox)

    # -- proxmox snapshots ------------------------------------------------------

    def snapshot(self, sandbox: Sandbox, label: str) -> dict:
        """Proxmox-side revert point. Call before anything risky."""
        self.access.check_lab()
        label = re.sub(r"[^a-zA-Z0-9_-]", "-", label)[:40]
        taken = {}
        for node in sandbox.nodes:
            self.proxmox.wait_task(self.proxmox.create_snapshot(node.vmid, label), timeout=600)
            taken[node.name] = label
        sandbox.proxmox_snapshots[label] = {"taken_at": self.clock(), "nodes": taken}
        self._save(sandbox)
        return {"label": label, "nodes": taken, "taken_at": self.clock()}

    def revert(self, sandbox: Sandbox, label: str) -> dict:
        """The revert button: put every sandbox guest back on a known-good state."""
        self.access.check_lab()
        record = sandbox.proxmox_snapshots.get(label)
        if record is None:
            raise NotFound(
                f"No Proxmox snapshot named {label}.",
                detail=f"Known snapshots: {', '.join(sorted(sandbox.proxmox_snapshots)) or 'none'}",
            )
        nodes = [node for node in sandbox.nodes if node.name in record["nodes"]]
        # Revert starts every guest it rolls back; a stopped switch costs memory again.
        cold = [n for n in nodes if n.kind == "switch" and self.proxmox.vm_status(n.vmid).get("status") != "running"]
        if cold:
            self.guard.check_ram(self.proxmox.free_memory_mb(), len(cold))
        result = {}
        for node in nodes:
            if self.proxmox.vm_status(node.vmid).get("status") == "running":
                self.proxmox.wait_task(self.proxmox.set_power(node.vmid, "stop", timeout=60), timeout=120)
            self.proxmox.wait_task(self.proxmox.rollback_snapshot(node.vmid, label), timeout=600)
            self.proxmox.wait_task(self.proxmox.set_power(node.vmid, "start"), timeout=120)
            self._reopen_lacp(sandbox, node)
            node.running = True
            result[node.name] = "rolled back and started"
        sandbox.notes.append(f"Reverted to Proxmox snapshot {label}")
        self._save(sandbox)
        return {"label": label, "nodes": result}

    # -- mist -------------------------------------------------------------------

    def mist_create_site(self, sandbox: Sandbox) -> dict:
        """Every sandbox gets its own Mist site. That is guardrail #1."""
        self.access.check_lab()
        self.guard.check_site(sandbox.mist_site_id)
        if sandbox.mist_site_id:
            return {"site_id": sandbox.mist_site_id, "site_name": sandbox.mist_site_name, "created": False}
        self.access.check_mist()
        site = self.mist.create_site(sandbox.mist_site_name or f"{sandbox.name}-site")
        sandbox.mist_site_id = site.get("id")
        sandbox.mist_site_name = site.get("name", sandbox.mist_site_name)
        sandbox.notes.append(f"Mist site {sandbox.mist_site_name} created ({sandbox.mist_site_id})")
        self._save(sandbox)
        return {"site_id": sandbox.mist_site_id, "site_name": sandbox.mist_site_name, "created": True}

    def _mist_switches(self, site_id: str, nodes) -> tuple[dict, list[str]]:
        """Find sandbox switches in the site by name or host name, any case.
        Returns ``({node name: (device id, mac)}, the names Mist lists)``."""
        listed = self.mist.devices(site_id, "switch")
        index: dict[str, dict] = {}
        seen = []
        for device in listed:
            seen.append(str(device.get("name") or device.get("hostname") or device.get("mac") or device.get("id")))
            for key in (device.get("name"), device.get("hostname")):
                if key:
                    index.setdefault(str(key).strip().lower(), device)
        found = {}
        for node in nodes:
            device = index.get(node.name.lower())
            if device is None or not device.get("id"):
                continue
            mac = fabric.normalise_mac(device.get("mac"))
            if not mac:
                mac = fabric.normalise_mac(self.mist.device(site_id, device["id"]).get("mac"))
            if mac:
                found[node.name] = (device["id"], mac)
        return found, seen

    def _sandbox_topology(self, site_id: str, sandbox: Sandbox, notes: list[str] | None = None) -> dict | None:
        """The site's topology for this sandbox, in full: the one named after it, else the only one."""
        topologies = self.mist.evpn_topologies(site_id)
        chosen = next((t for t in topologies if t.get("name") == sandbox.name), None)
        if chosen is None and len(topologies) == 1:
            chosen = topologies[0]
        if chosen is None or not chosen.get("id"):
            if len(topologies) > 1 and notes is not None:
                notes.append(f"Mist has {len(topologies)} topologies in this site and none is named {sandbox.name}.")
            return None
        return self.mist.evpn_topology(site_id, chosen["id"])

    def _shape_pods(self, sandbox: Sandbox):
        if not sandbox.recipe.shape:
            return None
        try:
            return self.get_shape(sandbox.recipe.shape).get("pods")
        except LabError:
            return None

    def mist_build_fabric(self, sandbox: Sandbox) -> dict:
        """Make the sandbox's Mist site match its cables: networks, VRF and root password
        on the site, every switch managed by Mist, and an EVPN topology with a link and a
        fabric port for each cable. Mist is snapshotted first, so Revert undoes it."""
        self.access.check_lab()
        self.access.check_mist()
        site_id = self._require_site(sandbox)
        recipe = sandbox.recipe
        switches = [n for n in sandbox.nodes if n.kind in PORT_KINDS]
        found, seen = self._mist_switches(site_id, switches)
        missing = [n.name for n in switches if n.name not in found]
        if missing:
            raise GuardrailViolation(
                f"Mist does not have {', '.join(missing)} yet.",
                detail="Adopt them first (Join Mist, step 2), then build again. "
                f"Mist sees: {', '.join(seen) or 'none'}.",
            )

        macs = {name: mac for name, (_, mac) in found.items()}
        body, info = fabric.topology_body(
            sandbox,
            macs,
            pods=self._shape_pods(sandbox),
            protected=self.settings.production_subnets,
            switch_ports=self.settings.switch_ports,
        )
        options = body["evpn_options"]
        self.guard.check_subnets(
            {
                "underlay": options["underlay"]["subnet"],
                "router ID": options["auto_router_id_subnet"],
                "loopback": options["auto_loopback_subnet"],
                **{f"{network.name} network": network.cidr for network in recipe.networks},
            }
        )
        notes = list(info["notes"])

        wanted = f"before-fabric-{self.clock()}"
        label, extra = _snapshot_label(wanted), 2
        while label in sandbox.mist_snapshots:
            label, extra = _snapshot_label(f"{wanted}-{extra}"), extra + 1
        label = self.mist_snapshot(sandbox, label)["label"]
        undo = f"Revert to {label} to undo the partial build."
        changed: set[str] = set()
        topology_id = form = None
        try:
            setting = fabric.site_setting(self.mist.site_setting(site_id), recipe, self._root_password(sandbox))
            self.mist.put_site_setting(site_id, setting)
            for node in switches:
                device_id = found[node.name][0]
                current = self.mist.device(site_id, device_id)
                if current.get("managed") is not True or current.get("mist_configured") is not True:
                    self.mist.put_device(site_id, device_id, {**current, "managed": True, "mist_configured": True})
                    changed.add(device_id)
            if len(info["members"]) >= 2:
                topology_id, form = self._put_topology(site_id, sandbox, body, found, notes, changed)
            else:
                notes.append("Mist needs at least two switches for a fabric topology, so the site got its networks and nothing more.")
        except BackendError as error:
            raise BackendError(error.message, detail=(f"{error.detail} " if error.detail else "") + undo, status=error.status) from error

        sandbox.fabric_built_at = self.clock()
        count = len(info["members"])
        built = f"{count} switch{'' if count == 1 else 'es'} in a {form} topology" if form else "networks only, no topology"
        sandbox.notes.append(f"Built the fabric in Mist site {sandbox.mist_site_name}: {built} (undo: Revert to {label})")
        self._save(sandbox)
        try:
            check = self.fabric_check(sandbox)
        except LabError:
            check = None
        return {
            "site_id": site_id,
            "topology_id": topology_id,
            "form": form,
            "switches": info["members"],
            "skipped": info["skipped"],
            "notes": notes,
            "devices_changed": len(changed),
            "summary": {
                "networks": list(recipe.mist_setting()["networks"]),
                "vrfs": [recipe.vrf] if recipe.vrf else [],
                "root_password": "set",
            },
            "snapshot": label,
            "check": check,
        }

    def _put_topology(self, site_id: str, sandbox: Sandbox, body: dict, found: dict, notes: list[str], changed: set) -> tuple:
        """Create or update the sandbox's topology. If Mist refuses the detailed form (400),
        send members and roles only and put each switch's fabric ports on the switch."""
        current = self._sandbox_topology(site_id, sandbox)
        if current is not None:
            body["id"] = current["id"]
            members = {s["mac"] for s in body["switches"]}
            for switch in current.get("switches") or []:
                mac = fabric.normalise_mac(switch.get("mac"))
                if mac and mac not in members and switch.get("role") != "none":
                    # Role none is how Mist takes a switch out of a topology.
                    body["switches"].append({"mac": mac, "role": "none"})
                    members.add(mac)
        try:
            stored, form = self.mist.put_evpn_topology(site_id, body), "detailed"
        except BackendError as error:
            if error.status != 400:
                raise
            refused = error
            try:
                stored, form = self.mist.put_evpn_topology(site_id, fabric.basic_body(body)), "basic"
            except BackendError as error:
                raise BackendError(
                    "Mist refused the fabric topology.",
                    detail=f"It refused the detailed form ({refused.detail or refused.status}) "
                    f"and the basic one ({error.detail or error.status}).",
                    status=error.status,
                ) from error
            notes.append(
                "Mist refused the detailed topology, so it got the basic one (members and roles) "
                "and each switch got its fabric ports, gateways and VRF directly."
            )
        # Mist answers 200 to the detailed form but keeps switch_configs as plain data:
        # fabric ports, gateways and VRF only take effect on each device.
        for device_id, mac in found.values():
            conf = body["switch_configs"].get(mac)
            if not conf:
                continue
            current = self.mist.device(site_id, device_id)
            merged = fabric.merge_switch_config(current, conf)
            merged.update(managed=True, mist_configured=True)
            if merged != current:
                self.mist.put_device(site_id, device_id, merged)
                changed.add(device_id)
        return (stored or {}).get("id") or body.get("id"), form

    def mist_snapshot(self, sandbox: Sandbox, label: str) -> dict:
        """The Mist half of the revert button: site, topology and every device.

        No password is kept: a root password is noted where it was and put back
        from the sandbox's own secret on revert, and generated config lines that
        carry a secret are dropped (they are for reading, never replayed)."""
        site_id = self._require_site(sandbox)
        label = _snapshot_label(label)
        setting, setting_had_password = _without_root_password(self.mist.site_setting(site_id))
        snapshot = MistSnapshot(
            label=label,
            taken_at=self.clock(),
            site_id=site_id,
            site_setting=setting,
            # The list leaves out each topology's switches; only the topology itself has them.
            evpn_topologies=[self.mist.evpn_topology(site_id, t["id"]) for t in self.mist.evpn_topologies(site_id) if t.get("id")],
        )
        snapshot.root_password_removed["site_setting"] = setting_had_password
        for device in self.mist.devices(site_id, "switch"):
            config, had_password = _without_root_password(self.mist.device(site_id, device["id"]))
            snapshot.devices[device["id"]] = config
            if had_password:
                snapshot.root_password_removed["devices"].append(device["id"])
            try:
                snapshot.device_cli[device.get("name", device["id"])] = _cli_without_secrets(self.mist.device_cli(site_id, device["id"]))
            except BackendError:
                snapshot.device_cli[device.get("name", device["id"])] = {}
        path = os.path.join(self._mist_snapshot_dir(sandbox.name), f"{label}.json")
        body = {
            "label": label,
            "taken_at": snapshot.taken_at,
            "site_id": site_id,
            "site_setting": snapshot.site_setting,
            "evpn_topologies": snapshot.evpn_topologies,
            "devices": snapshot.devices,
            "device_cli": snapshot.device_cli,
            "root_password_removed": snapshot.root_password_removed,
        }
        self._write_private(path, json.dumps(body, indent=2, sort_keys=True).encode("utf-8"))
        sandbox.mist_snapshots[label] = {"taken_at": snapshot.taken_at, "path": path, "site_id": site_id}
        self._save(sandbox)
        return {"label": label, "taken_at": snapshot.taken_at, "site_id": site_id, "devices": len(snapshot.devices), "path": path}

    def mist_revert(self, sandbox: Sandbox, label: str) -> dict:
        """Put Mist back the way the snapshot found it."""
        self.access.check_lab()
        self.access.check_mist()
        record = sandbox.mist_snapshots.get(label)
        if record is None:
            raise NotFound(
                f"No Mist snapshot named {label}.",
                detail=f"Known snapshots: {', '.join(sorted(sandbox.mist_snapshots)) or 'none'}",
            )
        with open(record["path"], encoding="utf-8") as handle:
            data = json.load(handle)
        site_id = data["site_id"]
        self.guard.check_site(site_id)
        removed = data.get("root_password_removed") or {}
        password = self._root_password(sandbox) if removed.get("site_setting") or removed.get("devices") else ""

        # A topology the snapshot did not have goes first, before the networks it carries.
        saved_ids = {t.get("id") for t in data["evpn_topologies"]}
        live_ids, topologies_removed = set(), []
        for topology in self.mist.evpn_topologies(site_id):
            if topology.get("id") and topology["id"] not in saved_ids:
                self.mist.delete_evpn_topology(site_id, topology["id"])
                topologies_removed.append(topology.get("name") or topology["id"])
            else:
                live_ids.add(topology.get("id"))

        # PUT is a replace for these objects, so the site setting goes back whole.
        setting = _with_root_password(data["site_setting"], password) if removed.get("site_setting") else data["site_setting"]
        self.mist.put_site_setting(site_id, setting)
        for topology in data["evpn_topologies"]:
            # One deleted since the snapshot is made again: a PUT to its old id would be a 404.
            body = topology if topology.get("id") in live_ids else {k: v for k, v in topology.items() if k != "id"}
            try:
                self.mist.put_evpn_topology(site_id, body)
            except BackendError as error:
                if error.status != 400:
                    raise
                # As in the build: a Mist that refuses links in the body gets members and roles.
                self.mist.put_evpn_topology(site_id, fabric.basic_body(body))
                sandbox.notes.append(f"Mist refused topology {body.get('name')} in full, so it went back as members and roles")

        for device_id, config in data["devices"].items():
            if device_id in (removed.get("devices") or []):
                config = _with_root_password(config, password)
            self.mist.put_device(site_id, device_id, config)
        sandbox.notes.append(f"Mist reverted to {label}" + (f"; removed topology {', '.join(topologies_removed)}" if topologies_removed else ""))
        self._save(sandbox)
        return {
            "label": label,
            "site_id": site_id,
            "site_setting_restored": True,
            "topologies_restored": len(data["evpn_topologies"]),
            "topologies_removed": topologies_removed,
            "devices_restored": len(data["devices"]),
        }

    def _require_site(self, sandbox: Sandbox) -> str:
        if not sandbox.mist_site_id:
            raise GuardrailViolation(
                "This sandbox has no Mist site yet.",
                detail="Create a sandbox site first. Each sandbox gets its own site so two people "
                "never overwrite the same fabric.",
            )
        self.guard.check_site(sandbox.mist_site_id)
        return sandbox.mist_site_id

    def mist_health(self, sandbox: Sandbox) -> dict:
        """Read-only: are the sandbox switches up, committed and reachable?"""
        site_id = self._require_site(sandbox)
        # The device list is configuration only; how each switch is doing lives in its stats.
        stats = {row.get("id"): row for row in self.mist.device_stats(site_id, "switch")}
        devices = []
        for device in self.mist.devices(site_id, "switch"):
            row = stats.get(device["id"])
            devices.append(
                {
                    "id": device["id"],
                    "name": device.get("name"),
                    "serial": device.get("serial"),
                    "connected": row.get("status") == "connected" if row else None,
                    **{k: (row or {}).get(k) for k in ("config_status", "last_seen", "version", "uptime", "ip")},
                }
            )
        return {
            "site_id": site_id,
            "site_name": sandbox.mist_site_name,
            "checked_at": self.clock(),
            "devices": devices,
            "topologies": [t.get("name") for t in self.mist.evpn_topologies(site_id)],
        }

    # -- check cabling ------------------------------------------------------------
    # Three views of every cable: Proxmox (the bridge and both NICs), LLDP (what
    # each switch sees on the port, as Mist last heard it) and the Mist topology.
    # Only the Proxmox side is ever fixed. ``repair`` True fixes it, or is refused
    # when SimRack may not change the lab; False only looks; None (the web page's
    # button) fixes it whenever SimRack may.

    def fabric_check(self, sandbox: Sandbox, repair: bool | None = None) -> dict:
        refusal = self.access.lab_refusal()
        if repair:
            raise_if(refusal)
        look_only = repair is False
        repair = not look_only and refusal is None
        notes: list[str] = []
        nodes = {n.name: n for n in sandbox.nodes}
        configs: dict[int, dict | None] = {}

        def config(node: Node) -> dict | None:
            if node.vmid not in configs:
                try:
                    configs[node.vmid] = self.proxmox.get_vm(node.vmid)
                except LabError as error:
                    configs[node.vmid] = None
                    notes.append(f"Proxmox could not read {node.name} (vmid {node.vmid}): {error.message}")
            return configs[node.vmid]

        park = self.settings.park_bridge
        park_missing = False
        if any(n.kind in PORT_KINDS for n in sandbox.nodes) and not self.proxmox.bridge_exists(park):
            if repair:
                self._ensure_park()
                notes.append(f"The park bridge {park} was missing; made it again.")
                sandbox.notes.append(f"Check cabling made the park bridge {park} again")
            else:
                park_missing = True
                then = (
                    "Fixing the cabling makes it again."
                    if look_only
                    else f"{describe(refusal)} Check again once SimRack may change the lab, and it makes the bridge."
                )
                notes.append(f"The park bridge {park} is missing, so the switches cannot start. {then}")

        rows = [self._check_cable(sandbox, link, nodes, config, repair) for link in sandbox.links]
        parked, missing = self._check_strays(sandbox, config, repair)
        if missing or any("no NIC" in r["proxmox_detail"] for r in rows):
            notes.append("Some ports have no NIC in Proxmox. The check never adds NICs: remove the switch and add it again to get every port back.")

        extra = self._check_mist(sandbox, rows, nodes, notes)
        hinted = [r for r in rows if r["lldp"] == "wrong" and r["proxmox"] == "ok"]
        if hinted:
            notes.append(
                f"LLDP disagrees on {_plural(len(hinted), 'cable')} that Proxmox has right. "
                "Check the NIC-to-port mapping: net1 should be ge-0/0/0."
            )

        def count(key, value):
            return sum(1 for r in rows if r[key] == value)

        summary = {
            "cables": len(rows),
            "fixed": count("proxmox", "fixed"),
            "broken": count("proxmox", "broken"),
            "lldp_ok": count("lldp", "ok"),
            "lldp_wrong": count("lldp", "wrong"),
            "lldp_waiting": count("lldp", "waiting"),
            "linked": count("mist", "linked"),
            "not_linked": count("mist", "not linked"),
            "parked": len(parked),
            "missing": len(missing),
            "extra": len(extra),
        }
        summary["healthy"] = (
            all(r["proxmox"] in ("ok", "fixed") for r in rows)
            and not summary["lldp_wrong"]
            and not summary["not_linked"]
            and not extra
            and not missing
            and all(p["fixed"] for p in parked)
            and not park_missing
        )
        parked_fixed = sum(1 for p in parked if p["fixed"])
        if summary["fixed"] or parked_fixed:
            done = [f"fixed {_plural(summary['fixed'], 'cable')}"] if summary["fixed"] else []
            done += [f"parked {_plural(parked_fixed, 'stray port')}"] if parked_fixed else []
            sandbox.notes.append("Check cabling " + " and ".join(done))
        result = {
            "checked_at": self.clock(),
            "repair": repair,
            "cables": rows,
            "parked": parked,
            "missing": missing,
            "extra": extra,
            "notes": notes,
            "summary": summary,
        }
        sandbox.fabric_check = result
        self._save(sandbox)
        return result

    def _check_cable(self, sandbox: Sandbox, link: Link, nodes: dict, config, repair: bool) -> dict:
        """The Proxmox view of one cable, fixed in place when ``repair``."""
        row = {
            "bridge": link.bridge,
            "a_node": link.a_node,
            "a_port": link.a_port,
            "b_node": link.b_node,
            "b_port": link.b_port,
        }
        ends = [(nodes.get(link.a_node), link.a_node, link.a_port), (nodes.get(link.b_node), link.b_node, link.b_port)]
        gone = [name for node, name, _ in ends if node is None]
        if gone:
            return {**row, "proxmox": "broken", "proxmox_detail": f"{' and '.join(gone)} is no longer in the sandbox."}
        if any(config(node) is None for node, _, _ in ends):
            return {**row, "proxmox": "unknown", "proxmox_detail": "Proxmox could not read this cable's guests."}
        absent = [
            f"{name} {port} has no NIC (net{_net_index(port)})"
            for node, name, port in ends
            if not config(node).get(f"net{_net_index(port)}")
        ]
        if absent:
            return {**row, "proxmox": "broken", "proxmox_detail": "; ".join(absent) + "."}

        if not self.proxmox.bridge_exists(link.bridge):
            if not repair:
                return {**row, "proxmox": "broken", "proxmox_detail": f"Bridge {link.bridge} is missing."}
            self.proxmox.create_bridge(self.guard.check_bridge(link.bridge), mtu=self.settings.fabric_mtu)
            for node, _, port in ends:
                self.guard.check_node_is_sandbox(sandbox, node.name)
                # Off the bridge and back on, so Proxmox makes a fresh tap on the new bridge.
                self._park(node, _net_index(port), config(node))
                self._attach(sandbox, node, port, link.bridge)
            return {**row, "proxmox": "fixed", "proxmox_detail": f"Bridge {link.bridge} was missing; made it again and plugged both ends back in."}

        off = []
        for node, name, port in ends:
            options = _nic_options(config(node).get(f"net{_net_index(port)}"))
            bridge = options.get("bridge")
            if bridge == self.settings.park_bridge:
                off.append((node, port, f"{name} {port} {'was' if repair else 'is'} parked"))
            elif bridge != link.bridge:
                off.append((node, port, f"{name} {port} {'was' if repair else 'is'} on {bridge or 'no bridge'}"))
            elif options.get("link_down") == "1":
                off.append((node, port, f"{name} {port} {'was' if repair else 'is'} link-down"))
        if not off:
            return {**row, "proxmox": "ok", "proxmox_detail": ""}
        detail = "; ".join(text for _, _, text in off)
        if not repair:
            return {**row, "proxmox": "broken", "proxmox_detail": detail + "."}
        for node, port, _ in off:
            self.guard.check_node_is_sandbox(sandbox, node.name)
            self._attach(sandbox, node, port, link.bridge)
        return {**row, "proxmox": "fixed", "proxmox_detail": f"{detail}; plugged back in."}

    def _check_strays(self, sandbox: Sandbox, config, repair: bool) -> tuple[list, list]:
        """Every uncabled switch port should be parked link-down. net0 (fxp0) is never looked at."""
        cabled: dict[str, set] = {}
        for link in sandbox.links:
            cabled.setdefault(link.a_node, set()).add(_net_index(link.a_port))
            cabled.setdefault(link.b_node, set()).add(_net_index(link.b_port))
        parked, missing = [], []
        for node in sandbox.nodes:
            if node.kind not in PORT_KINDS or config(node) is None:
                continue
            for index in range(1, self.settings.switch_ports + 1):
                if index in cabled.get(node.name, ()):
                    continue
                port = f"ge-0/0/{index - 1}"
                value = config(node).get(f"net{index}")
                if not value:
                    missing.append({"node": node.name, "port": port})
                    continue
                options = _nic_options(value)
                if options.get("bridge") == self.settings.park_bridge and options.get("link_down") == "1":
                    continue
                if repair:
                    self.guard.check_node_is_sandbox(sandbox, node.name)
                    self._park(node, index, config(node))
                parked.append({"node": node.name, "port": port, "bridge": options.get("bridge") or "", "fixed": repair})
        return parked, missing

    def _check_mist(self, sandbox: Sandbox, rows: list[dict], nodes: dict, notes: list[str]) -> list[dict]:
        """Fill in each row's LLDP and Mist columns. Returns the links the topology has
        between sandbox switches that no cable joins."""
        for row in rows:
            row.update(lldp="unknown", lldp_detail="", mist="unknown", mist_detail="")
        site_id = sandbox.mist_site_id
        if not site_id:
            notes.append("This sandbox has no Mist site, so LLDP and the Mist topology were not checked.")
            return []
        if not self.mist.configured():
            notes.append("Mist is not configured, so LLDP and the Mist topology were not checked.")
            return []
        try:
            self.guard.check_site(site_id)
            found, _ = self._mist_switches(site_id, [n for n in sandbox.nodes if n.kind in PORT_KINDS])
            macs = {name: mac for name, (_, mac) in found.items()}
            heard = {}
            for stat in self.mist.port_stats(site_id):
                heard[(fabric.normalise_mac(stat.get("mac")), str(stat.get("port_id") or ""))] = stat
            topology = self._sandbox_topology(site_id, sandbox, notes)
        except LabError as error:
            notes.append(f"Could not read Mist ({error.message}), so LLDP and the Mist topology were not checked.")
            return []
        pairs = fabric.topology_pairs(topology) if topology is not None else set()
        lowered = {name.lower() for name in nodes}

        for row in rows:
            if row["proxmox"] == "fixed":
                row.update(lldp="waiting", lldp_detail="Plugged back in just now; LLDP catches up within a few minutes.")
            else:
                row["lldp"], row["lldp_detail"] = self._lldp(row, nodes, macs, heard, lowered)
            row["mist"], row["mist_detail"] = self._mist_link(row, nodes, macs, topology, pairs)

        extra = []
        if topology is not None:
            name_of = {mac: name for name, mac in macs.items()}
            order = {n.name: i for i, n in enumerate(sandbox.nodes)}
            cabled = {frozenset((link.a_node, link.b_node)) for link in sandbox.links}
            for pair in pairs:
                names = [name_of.get(mac) for mac in pair]
                if len(names) != 2 or None in names or frozenset(names) in cabled:
                    continue
                first, second = sorted(names, key=order.__getitem__)
                extra.append({"a_node": first, "b_node": second})
            extra.sort(key=lambda e: (order[e["a_node"]], order[e["b_node"]]))
        return extra

    @staticmethod
    def _lldp(row: dict, nodes: dict, macs: dict, heard: dict, lowered: set) -> tuple[str, str]:
        ends = [(row["a_node"], row["a_port"], row["b_node"]), (row["b_node"], row["b_port"], row["a_node"])]
        ends = [end for end in ends if nodes.get(end[0]) is not None and nodes[end[0]].kind in PORT_KINDS]
        if not ends:
            return "unknown", "No switch on this cable to ask."
        unknown = [name for name, _, _ in ends if name not in macs]
        if unknown:
            return "unknown", f"{' and '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not in the Mist site."
        seen = []
        for name, port, peer in ends:
            stat = heard.get((macs[name], port)) or {}
            neighbour = str(stat.get("neighbor_system_name") or "").strip().split(".")[0]
            far = nodes.get(peer)
            if not stat.get("up") or not neighbour:
                seen.append(("waiting", ""))
            elif neighbour.lower() == peer.lower():
                seen.append(("ok", f"{name} {port} sees {peer}"))
            # A switch is in Mist under its name only once it carries it, so a switch peer announces its name.
            elif neighbour.lower() in lowered or (far is not None and far.kind in PORT_KINDS):
                seen.append(("wrong", f"{name} {port} sees {neighbour}, not {peer}"))
            else:
                seen.append(("unknown", f"{name} {port} sees {neighbour}; {peer} is not a switch, so SimRack cannot tell what name it announces"))
        for state in ("wrong", "ok", "unknown"):
            said = [text for got, text in seen if got == state]
            if said:
                return state, "; ".join(said) + "."
        said = [text for _, text in seen if text]
        return "waiting", ("; ".join(said) + ".") if said else "Nothing seen yet."

    @staticmethod
    def _mist_link(row: dict, nodes: dict, macs: dict, topology: dict | None, pairs: set) -> tuple[str, str]:
        a, b = nodes.get(row["a_node"]), nodes.get(row["b_node"])
        if a is None or b is None or a.kind not in PORT_KINDS or b.kind not in PORT_KINDS:
            return "skipped", "Mist only links switches."
        unknown = [n.name for n in (a, b) if n.name not in macs]
        if unknown:
            return "unknown", f"{' and '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not in the Mist site."
        outside = [n for n in (a, b) if n.role not in fabric.MIST_ROLES]
        if outside:
            return "skipped", f"{outside[0].name} is not a fabric role ({outside[0].role})."
        if fabric.link_kind(a.role, b.role) is None:
            reason = fabric.not_a_link(a.role, b.role)
            return "skipped", f"{reason[0].upper()}{reason[1:]}; Mist leaves it alone."
        if topology is None:
            return "none", "No fabric topology in Mist yet."
        if frozenset((macs[a.name], macs[b.name])) in pairs:
            return "linked", ""
        return "not linked", f"The Mist topology does not link {a.name} and {b.name}."

    def adopt_config(self, sandbox: Sandbox) -> dict:
        """The outbound-ssh command a sandbox switch needs to join the site."""
        return self.mist.adopt_config(sandbox.recipe.org_id or self.settings.org_id, site_id=sandbox.mist_site_id)

    def adopt_switch(self, sandbox: Sandbox, name: str, *, console=None) -> dict:
        """Join one sandbox switch to the sandbox's Mist site over its serial console:
        root password, SSH, host name, DHCP on fxp0 and the Mist lines, one commit.
        Everything is checked before Mist is asked or the console is touched."""
        self.access.check_lab()
        node = self.guard.check_node_is_sandbox(sandbox, name)
        if node.kind != "switch":
            raise GuardrailViolation(f"{name} is not a switch.", detail="Only vJunos switches join a Mist site.")
        if self.proxmox.vm_status(node.vmid).get("status") != "running":
            raise GuardrailViolation(f"{name} is not running.", detail="Start it and give Junos a few minutes to boot.")
        self.access.check_mist()
        site_id = self._require_site(sandbox)
        path = os.path.join(self.settings.serial_dir, f"{node.vmid}.serial0")
        if console is None and not os.path.exists(path):
            raise NotFound(
                f"No serial console for {name}.",
                detail=f"Expected {path}. The switch must be running with serial0=socket.",
            )

        cmd = self.mist.adopt_config(sandbox.recipe.org_id or self.settings.org_id, site_id=site_id)
        lines = mist_lines((cmd or {}).get("cmd", ""))
        if not lines:
            raise BackendError("Mist sent no adoption lines.", detail="The outbound-ssh command came back empty.")

        port = console if console is not None else SerialConsole(path)
        try:
            result = console_adopt(port, node.name, self._root_password(sandbox), lines, tries=6, wait=5.0)
        finally:
            if console is None:
                port.close()

        node.adopted_at = self.clock()
        mgmt_ip = result.get("mgmt_ip")
        if mgmt_ip:
            node.mgmt_ip = mgmt_ip
        note = f"Adopted {name} into Mist site {sandbox.mist_site_name}"
        sandbox.notes.append(note if mgmt_ip else note + "; fxp0 has no DHCP address yet")
        self._save(sandbox)
        return {"node": name, "adopted_at": node.adopted_at, "mgmt_ip": mgmt_ip, "site": sandbox.mist_site_name}

    # -- teardown ---------------------------------------------------------------

    def teardown(self, sandbox: Sandbox, *, keep_mist: bool = False) -> dict:
        """Undo a whole sandbox. The live lab is never in scope."""
        self.access.check_lab()
        removed = {"nodes": [], "bridges": [], "mist_site": None, "complete": True, "failed": []}
        # Guests first: deleting them drops their taps, so no NIC hot-unplug is needed.
        for node in list(sandbox.nodes):
            try:
                self._destroy_vm(node.vmid, node.name)
                removed["nodes"].append(node.name)
                sandbox.nodes = [n for n in sandbox.nodes if n.name != node.name]
            except LabError as error:
                removed["failed"].append(f"{node.name} (vmid {node.vmid}): {error.message}")
        alive = {n.name for n in sandbox.nodes}
        for link in list(sandbox.links):
            if alive & {link.a_node, link.b_node}:
                continue
            try:
                self.guard.check_bridge(link.bridge)
                self.proxmox.delete_bridge(link.bridge)
                removed["bridges"].append(link.bridge)
                sandbox.links = [cable for cable in sandbox.links if cable.bridge != link.bridge]
            except LabError as error:
                removed["failed"].append(f"{link.bridge}: {error.message}")

        if sandbox.nodes or sandbox.links:
            removed["complete"] = False
            removed["mist_site"] = "left in place: teardown incomplete" if sandbox.mist_site_id and not keep_mist else None
            sandbox.notes.append("Teardown incomplete: " + "; ".join(removed["failed"]))
            self._save(sandbox)
            return removed

        if sandbox.mist_site_id and not keep_mist:
            try:
                self.guard.check_site(sandbox.mist_site_id)
                self.access.check_mist()
                self.mist.delete_site(sandbox.mist_site_id)
                removed["mist_site"] = sandbox.mist_site_name
            except LabError as error:
                removed["mist_site"] = f"left in place: {error.message}"
        self.sandboxes.pop(sandbox.name, None)
        path = self._path(sandbox.name)
        if os.path.exists(path):
            os.remove(path)
        self._forget_secret(sandbox.name)
        shutil.rmtree(self._mist_snapshot_dir(sandbox.name), ignore_errors=True)
        self._drop_park_if_unused()
        return removed

    # -- console ----------------------------------------------------------------

    def console_command(self, sandbox: Sandbox, name: str, command: str, *, settle: float = 1.0) -> str:
        """Send a command to a guest's serial console (used for Mist adoption)."""
        self.access.check_lab()
        if not command.strip() or len(command) > 2000:
            raise GuardrailViolation("Console command must be 1-2000 characters.")
        node = self.guard.check_node_is_sandbox(sandbox, name)
        path = os.path.join(self.settings.serial_dir, f"{node.vmid}.serial0")
        if not os.path.exists(path):
            raise NotFound(
                f"No serial socket for {name}.",
                detail=f"Expected {path}. The guest must be running with serial0=socket.",
            )
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as console:
            console.settimeout(2)
            console.connect(path)
            time.sleep(settle)
            console.sendall((command.rstrip() + "\n").encode())
            time.sleep(1.0)
            try:
                output = console.recv(65535).decode("utf-8", "replace")
            except socket.timeout:
                output = ""
        return output
