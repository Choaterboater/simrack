"""In-memory stand-ins for Proxmox and Mist so tests never touch the live lab."""

from __future__ import annotations

import contextlib
import dataclasses
import http.server
import json
import os
import re
import shutil
import tempfile
import threading
from unittest import mock

from simrack.access import POOL
from simrack.config import PROFILE_FILE, SANDBOX_BRIDGE_PREFIX, Settings
from simrack.errors import BackendError
from simrack.service import SandboxManager


#: Proxmox's built-in roles (pveum role list).
PVE_ADMINISTRATOR = frozenset(
    {
        "Datastore.Allocate", "Datastore.AllocateSpace", "Datastore.AllocateTemplate", "Datastore.Audit",
        "Group.Allocate", "Mapping.Audit", "Mapping.Modify", "Mapping.Use", "Permissions.Modify",
        "Pool.Allocate", "Pool.Audit", "Realm.Allocate", "Realm.AllocateUser",
        "SDN.Allocate", "SDN.Audit", "SDN.Use",
        "Sys.Audit", "Sys.Console", "Sys.Incoming", "Sys.Modify", "Sys.PowerMgmt", "Sys.Syslog", "User.Modify",
        "VM.Allocate", "VM.Audit", "VM.Backup", "VM.Clone", "VM.Config.CDROM", "VM.Config.CPU",
        "VM.Config.Cloudinit", "VM.Config.Disk", "VM.Config.HWType", "VM.Config.Memory", "VM.Config.Network",
        "VM.Config.Options", "VM.Console", "VM.Migrate", "VM.Monitor", "VM.PowerMgmt", "VM.Snapshot",
        "VM.Snapshot.Rollback",
    }
)
PVE_AUDITOR = frozenset({"Datastore.Audit", "Mapping.Audit", "Pool.Audit", "SDN.Audit", "Sys.Audit", "VM.Audit"})
PVE_DATASTORE_USER = frozenset({"Datastore.AllocateSpace", "Datastore.Audit"})
PVE_SDN_USER = frozenset({"SDN.Audit", "SDN.Use"})
PVE_TEMPLATE_USER = frozenset({"VM.Audit", "VM.Clone"})
BUILT_IN_ROLES = {
    "Administrator": PVE_ADMINISTRATOR,
    "PVEAuditor": PVE_AUDITOR,
    "PVEDatastoreUser": PVE_DATASTORE_USER,
    "PVESDNUser": PVE_SDN_USER,
    "PVETemplateUser": PVE_TEMPLATE_USER,
}

#: The privilege PVE checks to set each guest config field SimRack sends
#: (qemu-server check_vm_modify_config_perm and its serial port check).
CONFIG_PRIVILEGES = (
    (r"memory", "VM.Config.Memory"),
    (r"cores|cpu|sockets", "VM.Config.CPU"),
    (r"name|onboot|ostype", "VM.Config.Options"),
    (r"smbios1|scsihw|serial\d+", "VM.Config.HWType"),
    (r"boot", "VM.Config.Disk"),
    (r"net\d+", "VM.Config.Network"),
)


class FakeProxmox:
    """Records every write. Free and total memory are set by the test."""

    #: Only root@pam may set or clear these, and an API token is never root@pam
    #: (qemu-server check_vm_modify_config_perm: "only root can set 'args' config").
    ROOT_ONLY = ("args", "hookscript")

    def __init__(
        self,
        free_mb: int = 40000,
        templates: list[int] | None = None,
        total_mb: int = 65536,
        privileges: frozenset | dict[str, frozenset] = PVE_ADMINISTRATOR,
        token: str = "fake",
    ) -> None:
        self.token = token
        #: hostnet opens LACP only on a tap in a bridge with the lab profile's prefix.
        self.bridge_prefix = SANDBOX_BRIDGE_PREFIX
        #: What the token holds: one set on every path, or an ACL of path -> set.
        self.privileges = privileges
        #: Resource pools and the guests in them. The setup commands made SimRack's.
        self.pools: dict[str, set[int]] = {POOL: set()}
        self.permission_reads = 0
        self.free_mb = free_mb
        self.total_mb = total_mb
        self.vms: dict[int, dict] = {}
        self.containers: dict[int, dict] = {}
        #: Host bridges: the ones in /etc/network/interfaces carry "cidr" and "gateway".
        self.networks: dict[str, dict] = {}
        self.snapshots: dict[int, dict[str, dict]] = {}
        self.calls: list[tuple] = []
        #: (vmid, net index) whose tap passes LACP. A start gives a guest new taps.
        self.lacp_open: set[tuple[int, int]] = set()
        #: Guests whose reboot task has ended with them down; qmeventd starts them.
        self.reboot_requests: set[int] = set()
        self.templates = templates or []
        for vmid in self.templates:
            self.vms[vmid] = {"vmid": vmid, "name": f"tmpl-{vmid}", "template": 1, "maxmem": 5120 * 1024 * 1024}

    def _log(self, _method, *args, **kwargs):
        self.calls.append((_method, args, kwargs))

    def use(self, settings):
        """The token the setup page saved; the fake answers whatever it is."""
        self.token = settings.pve_token
        self.bridge_prefix = settings.sandbox_bridge_prefix

    @staticmethod
    def mac(vmid, index):
        vmid = int(vmid)
        return f"BC:24:11:{vmid >> 8:02X}:{vmid & 0xFF:02X}:{int(index):02X}"

    def _with_macs(self, vmid, fields):
        """PVE gives a NIC written as ``virtio,...`` a fresh MAC; so does the fake."""
        out = {}
        for key, value in fields.items():
            found = re.fullmatch(r"net(\d+)", key)
            if found and isinstance(value, str) and value.startswith("virtio,"):
                value = f"virtio={self.mac(vmid, found.group(1))}," + value[len("virtio,"):]
            out[key] = value
        return out

    def nics(self, vmid):
        vm = self.vms.get(int(vmid), {})
        return {k: v for k, v in vm.items() if re.fullmatch(r"net\d+", k)}

    # -- what the token may do --------------------------------------------------
    def held(self, path):
        """What the token holds on ``path``, worked out as PVE does: the deepest
        grant at or above it wins, and a guest in a pool also holds what the pool
        grants (AccessControl::roles, RPCEnvironment::compile_acl_path)."""
        if not isinstance(self.privileges, dict):
            return frozenset(self.privileges)
        above = [acl for acl in self.privileges if acl == "/" or path == acl or path.startswith(acl + "/")]
        held = frozenset(self.privileges[max(above, key=len)]) if above else frozenset()
        guest = re.fullmatch(r"/vms/(\d+)", path)
        for pool, members in self.pools.items():
            if guest and int(guest.group(1)) in members:
                held |= self.held(f"/pool/{pool}")
        return held

    def _check(self, path, *privileges, any_of=False):
        """Refuse as PVE's permission check does: 403, naming the path."""
        missing = [privilege for privilege in privileges if privilege not in self.held(path)]
        if missing and not (any_of and len(missing) < len(privileges)):
            raise BackendError(
                "Proxmox API call failed (403).", detail=f"Permission check failed ({path}, {', '.join(missing)})", status=403
            )

    def _may_make(self, vmid, pool):
        """A new guest needs VM.Allocate on its vmid or on the pool it goes in, and a
        vmid no guest has: VMs and containers share one set (check_vmid_unused)."""
        if pool not in self.pools:
            raise BackendError("Proxmox API call failed (403).", detail=f"pool '{pool}' does not exist", status=403)
        if "VM.Allocate" not in self.held(f"/pool/{pool}"):
            self._check(f"/vms/{vmid}", "VM.Allocate")
        for kind, guests in (("VM", self.vms), ("CT", self.containers)):
            if int(vmid) in guests:
                raise BackendError("Proxmox API call failed (500).", detail=f"{kind} {int(vmid)} already exists on node 'pve1'", status=500)

    def _may_set(self, vmid, fields, pool=None):
        for key in fields:
            privilege = next((held for pattern, held in CONFIG_PRIVILEGES if re.fullmatch(pattern, key)), None)
            assert privilege, f"the fake does not know what PVE checks to set {key}"
            if not (pool and privilege in self.held(f"/pool/{pool}")):
                self._check(f"/vms/{vmid}", privilege)

    def _may_use(self, nics):
        """SDN.Use on each NIC's bridge, or on its VLAN when it has a tag (check_vnet_access)."""
        for value in nics:
            bridge = re.search(r"bridge=([^,]+)", value or "")
            if bridge:
                tag = re.search(r"tag=(\d+)", value)
                self._check(f"/sdn/zones/localnetwork/{bridge.group(1)}" + (f"/{tag.group(1)}" if tag else ""), "SDN.Use")

    # -- reads ------------------------------------------------------------------
    def permissions(self, path):
        self.permission_reads += 1
        return {privilege: 1 for privilege in self.held(path)}

    def node_status(self):
        mib = 1024 * 1024
        return {"memory": {"free": self.free_mb * mib, "available": self.free_mb * mib, "total": self.total_mb * mib}}

    def memory_mb(self):
        return {"free": self.free_mb, "total": self.total_mb}

    def free_memory_mb(self):
        return self.free_mb

    def _need_token(self):
        if not self.token:
            from simrack.errors import NotConfigured

            raise NotConfigured("Proxmox API token is not set.", detail="Add one on SimRack's setup page.")

    def list_vms(self):
        self._need_token()
        return [vm for vmid, vm in self.vms.items() if "VM.Audit" in self.held(f"/vms/{vmid}")]

    def get_vm(self, vmid):
        if int(vmid) in self.vms:
            self._check(f"/vms/{int(vmid)}", "VM.Audit")
        return dict(self.vms.get(int(vmid), {}))

    def list_lxc(self):
        self._need_token()
        return [ct for ctid, ct in self.containers.items() if "VM.Audit" in self.held(f"/vms/{ctid}")]

    def bridges(self):
        self._need_token()
        return [dict(bridge) for bridge in self.networks.values() if bridge.get("type") == "bridge"]

    def list_images(self, storage="local"):
        return [
            {"volid": "local:import/vJunos-switch-26.2R1.7.qcow2", "content": "import", "size": 4645126144},
            {"volid": "local:iso/Virtual-Mist-Edge-Deb12-1.0.iso", "content": "iso", "size": 1 << 30},
        ]

    # -- writes -----------------------------------------------------------------
    def _refuse_root_only(self, verb, path, fields):
        refused = [key for key in self.ROOT_ONLY if key in fields]
        if refused:
            from simrack.errors import BackendError

            raise BackendError(f"Proxmox API {verb} {path} failed (500).", detail=f"only root can set '{refused[0]}' config")

    def create_vm(self, vmid, name, *, pool, **kwargs):
        self._refuse_root_only("POST", "/qemu", [key for key, value in kwargs.items() if value])
        nics = {k: v for k, v in (kwargs.get("extra") or {}).items() if re.fullmatch(r"net\d+", k)}
        self._may_make(vmid, pool)
        sent = {"cpu": kwargs.get("cpu"), "smbios1": kwargs.get("smbios_product"), "scsihw": kwargs.get("disk_bus") == "scsi0"}
        fields = ["name", "memory", "cores", "sockets", "ostype", "serial0", "onboot", "boot", *nics]
        self._may_set(vmid, fields + [key for key, value in sent.items() if value], pool=pool)
        self._may_use(nics.values())
        self._check(f"/storage/{kwargs.get('storage') or 'local-lvm'}", "Datastore.AllocateSpace")
        for volume in filter(None, (kwargs.get("iso"), kwargs.get("import_from"))):
            self._check(f"/storage/{volume.split(':')[0]}", "Datastore.AllocateSpace", "Datastore.Audit", any_of=True)
        self._log("create_vm", vmid, name, pool=pool, **kwargs)
        self.pools[pool].add(int(vmid))
        self.vms[int(vmid)] = {"vmid": int(vmid), "name": name, "config": kwargs, "status": "stopped"}
        self.vms[int(vmid)].update(self._with_macs(vmid, nics))
        return "UPID:x"

    def clone_vm(self, source_vmid, new_vmid, *, name, pool, full=True):
        # PVE copies the template's NICs and always gives each one a new MAC.
        source = self.vms.get(int(source_vmid), {})
        nics = {}
        for key, value in source.items():
            found = re.fullmatch(r"net(\d+)", key)
            if found:
                rest = value.split(",", 1)[1] if "," in value else ""
                nics[key] = f"virtio={self.mac(new_vmid, found.group(1))}" + ("," + rest if rest else "")
        nics = nics or {"net0": "virtio=02:00:00:CC:00:00,bridge=vmbr0,tag=5"}
        self._check(f"/vms/{int(source_vmid)}", "VM.Clone")
        self._may_make(new_vmid, pool)
        self._may_use(nics.values())  # the copy keeps the template's bridges
        self._check("/storage/local-lvm", "Datastore.AllocateSpace")  # where the template's disk is
        self._log("clone_vm", source_vmid, new_vmid, name=name, pool=pool, full=full)
        self.pools[pool].add(int(new_vmid))
        self.vms[int(new_vmid)] = {"vmid": int(new_vmid), "name": name, "template_of": int(source_vmid), "status": "stopped", **nics}
        return "UPID:x"

    def delete_vm(self, vmid, *, purge=True):
        self._check(f"/vms/{int(vmid)}", "VM.Allocate")
        self._log("delete_vm", vmid, purge=purge)
        if self.vms.get(int(vmid), {}).get("status") == "running":
            raise BackendError(f"Proxmox API DELETE /qemu/{vmid} failed (500).", detail=f"VM {vmid} is running - destroy failed")
        self.vms.pop(int(vmid), None)
        for members in self.pools.values():
            members.discard(int(vmid))
        return "UPID:x"

    def wait_task(self, upid, *, timeout=600, poll=1.0):
        self._log("wait_task", upid, timeout=timeout)

    def set_vm_config(self, vmid, **fields):
        self._refuse_root_only("PUT", f"/qemu/{vmid}/config", fields)
        self._may_set(vmid, fields)
        self._may_use(value for key, value in fields.items() if re.fullmatch(r"net\d+", key))
        self._log("set_vm_config", vmid, **fields)
        vm = self.vms.setdefault(int(vmid), {"vmid": int(vmid), "name": f"vm{vmid}"})
        for key, value in self._with_macs(vmid, fields).items():
            found = re.fullmatch(r"net(\d+)", key)
            if found:  # a NIC moved to another bridge is a new bridge port
                self.lacp_open.discard((int(vmid), int(found.group(1))))
            if value is None:
                vm.pop(key, None)
            else:
                vm[key] = value

    def set_power(self, vmid, action, *, timeout=120):
        self._check(f"/vms/{int(vmid)}", "VM.PowerMgmt")
        self._log("set_power", vmid, action, timeout=timeout)
        vm = self.vms.setdefault(int(vmid), {"vmid": int(vmid), "name": f"vm{vmid}"})
        if action == "reboot":
            # PVE reboots only a running guest. The task shuts it down and ends;
            # qmeventd starts it again a moment later (see qmeventd below).
            if vm.get("status") == "running":
                vm["status"] = "stopped"
                self.reboot_requests.add(int(vmid))
        else:
            vm["status"] = "running" if action in ("start", "reset") else "stopped"
        if action != "reset":  # reset keeps the QEMU process and its taps
            self.lacp_open = {port for port in self.lacp_open if port[0] != int(vmid)}
        return "UPID:x"

    def qmeventd(self):
        """Time passes: PVE starts each rebooted guest again, on new taps."""
        for vmid in self.reboot_requests:
            self.vms[vmid]["status"] = "running"
        self.reboot_requests.clear()

    def vm_status(self, vmid):
        return {"status": self.vms.get(int(vmid), {}).get("status", "unknown")}

    def create_bridge(self, name, *, mtu=9216):
        self._log("create_bridge", name, mtu=mtu)
        self.networks[name] = {"iface": name, "type": "bridge", "mtu": mtu}

    def bridge_exists(self, name):
        return name in self.networks

    def tune_port(self, vmid, net_index):
        self._log("tune_port", vmid, net_index=net_index)
        vm = self.vms.get(int(vmid), {})
        bridge = re.search(r"bridge=([^,]+)", vm.get(f"net{net_index}") or "")
        if vm.get("status") != "running" or not bridge or not bridge.group(1).startswith(self.bridge_prefix):
            return False
        self.lacp_open.add((int(vmid), int(net_index)))
        return True

    def passes_lacp(self, vmid, net_index):
        return (int(vmid), int(net_index)) in self.lacp_open

    def delete_bridge(self, name):
        self._log("delete_bridge", name)
        self.networks.pop(name, None)

    def create_snapshot(self, vmid, name):
        self._check(f"/vms/{int(vmid)}", "VM.Snapshot")
        self._log("create_snapshot", vmid, name)
        self.snapshots.setdefault(int(vmid), {})[name] = {"config": dict(self.vms.get(int(vmid), {}))}
        return "UPID:x"

    def rollback_snapshot(self, vmid, name):
        self._check(f"/vms/{int(vmid)}", "VM.Snapshot", "VM.Snapshot.Rollback", any_of=True)
        self._log("rollback_snapshot", vmid, name)
        snapshot = self.snapshots.get(int(vmid), {}).get(name)
        if snapshot:
            self.vms[int(vmid)] = dict(snapshot["config"])
        return "UPID:x"

    # -- assertions helpers -----------------------------------------------------
    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    def config_calls_for(self, vmid):
        return [c for c in self.called("set_vm_config") if c[1][0] == int(vmid)]


class SlowProxmox(FakeProxmox):
    """A clone that waits to be let go, so a change can be caught mid-way."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cloning, self.let_go = threading.Event(), threading.Event()

    def clone_vm(self, *args, **kwargs):
        self.cloning.set()
        self.let_go.wait(10)
        return super().clone_vm(*args, **kwargs)


class FakeMist:
    #: Roles Mist lets change an org: Super User (admin) and Network Admin (write).
    WRITE_ROLES = ("admin", "write")

    def __init__(self, *, token="fake-token", role="write", org_id="org-example") -> None:
        self.token = token
        #: The token's role on ``org_id``, as GET /self reports it; None lists no privileges at all.
        self.role = role
        self.org_id = org_id
        #: What GET /self lists instead, when a test needs more than one role on ``org_id``.
        self.privileges: list | None = None
        self.self_reads = 0
        #: What GET /orgs/{id} says holds ``org_id``: its MSP and the org groups it is in.
        self.msp_id = "msp-example"
        self.orggroup_ids = ["og-example"]
        self.org_reads = 0
        #: Mist refuses GET /orgs/{id} with this status, e.g. 403 for a token it hides the org from.
        self.org_status: int | None = None
        self.site_records: dict[str, dict] = {}
        self.settings_by_site: dict[str, dict] = {}
        self.topologies: dict[str, list[dict]] = {}
        self.devices_by_site: dict[str, list[dict]] = {}
        self.device_config: dict[tuple[str, str], dict] = {}
        self.cli: dict[tuple[str, str], dict] = {}
        #: site -> rows from GET /sites/{id}/stats/ports/search (LLDP neighbours per port)
        self.ports_by_site: dict[str, list[dict]] = {}
        #: "detailed" refuses a topology with links or switch_configs, "all" refuses any
        self.reject_topology: str | None = None
        self.reject_status = 400
        #: every read fails the way an unreachable Mist does
        self.fail_reads = False
        self.calls: list[tuple] = []
        self._n = 0
        self._topologies_made = 0
        self._macs = 0

    def _log(self, _method, *args, **kwargs):
        self.calls.append((_method, args, kwargs))

    def _read(self):
        if self.fail_reads:
            from simrack.errors import BackendError

            raise BackendError("Cannot reach the Mist API.", detail="fake: fail_reads")

    def configured(self):
        return bool(self.token)

    def use(self, settings):
        """The token the setup page saved. ``org_id`` stays the org the token has a role on."""
        self.token = settings.mist_token

    def whoami(self):
        self._read()
        self.self_reads += 1
        if self.privileges is not None:
            return {"email": "lab@example.com", "privileges": self.privileges}
        if self.role is None:
            return {"email": "lab@example.com", "privileges": []}
        return {
            "email": "lab@example.com",
            "privileges": [{"scope": "org", "org_id": self.org_id, "name": "Example Org", "role": self.role}],
        }

    def org(self, org_id=None):
        self._read()
        self.org_reads += 1
        status = self.org_status or (403 if org_id != self.org_id else None)
        if status:
            from simrack.errors import BackendError

            raise BackendError(f"Mist API GET /orgs/{org_id} failed ({status}).", status=status)
        return {"id": self.org_id, "name": "Example Org", "msp_id": self.msp_id, "orggroup_ids": list(self.orggroup_ids)}

    def sites(self, org_id=None):
        """Only the org the token has a role on answers."""
        self._read()
        if org_id != self.org_id:
            from simrack.errors import BackendError

            raise BackendError(f"Mist API GET /orgs/{org_id}/sites failed (403).", status=403)
        return list(self.site_records.values())

    def _guard(self, what):
        """Mist refuses a write from a token whose role may only look."""
        if self.role not in self.WRITE_ROLES:
            from simrack.errors import BackendError

            raise BackendError(
                f"Mist API {what} failed (403).",
                detail='{"detail": "You do not have permission to perform this action."}',
                status=403,
            )

    def create_site(self, name, org_id=None):
        self._guard("create_site")
        self._log("create_site", name)
        self._n += 1
        site_id = f"site-{self._n}"
        self.site_records[site_id] = {"id": site_id, "name": name}
        self.settings_by_site[site_id] = {}
        self.topologies[site_id] = []
        self.devices_by_site[site_id] = []
        return self.site_records[site_id]

    def delete_site(self, site_id):
        self._guard("delete_site")
        self._log("delete_site", site_id)
        self.site_records.pop(site_id, None)

    def site_setting(self, site_id):
        self._read()
        return dict(self.settings_by_site.get(site_id, {}))

    def put_site_setting(self, site_id, setting):
        self._guard("put_site_setting")
        self._log("put_site_setting", site_id, setting)
        self.settings_by_site[site_id] = json.loads(json.dumps(setting))

    def evpn_topologies(self, site_id):
        """Like Mist, the list has no ``switches`` or ``switch_configs``; get one topology for those."""
        self._read()
        return [{k: v for k, v in json.loads(json.dumps(t)).items() if k not in ("switches", "switch_configs")} for t in self.topologies.get(site_id, [])]

    def evpn_topology(self, site_id, topology_id):
        self._read()
        for topology in self.topologies.get(site_id, []):
            if topology.get("id") == topology_id:
                return json.loads(json.dumps(topology))
        from simrack.errors import BackendError

        raise BackendError(f"Mist API GET /sites/{site_id}/evpn_topologies/{topology_id} failed (404).", status=404)

    def _refuses(self, topology):
        if self.reject_topology == "all":
            return True
        if self.reject_topology == "detailed":
            linked = any(s.get(k) for s in topology.get("switches", []) for k in ("uplinks", "downlinks", "esilaglinks"))
            return bool(topology.get("switch_configs")) or linked
        return False

    def put_evpn_topology(self, site_id, topology):
        """PUT replaces the topology with that id; without an id it is a new one (POST)."""
        self._guard("put_evpn_topology")
        self._log("put_evpn_topology", site_id, topology.get("name"), topology.get("id"))
        if self._refuses(topology):
            from simrack.errors import BackendError

            verb, path = ("PUT", f"/sites/{site_id}/evpn_topologies/{topology['id']}") if topology.get("id") else ("POST", f"/sites/{site_id}/evpn_topologies")
            raise BackendError(
                f"Mist API {verb} {path} failed ({self.reject_status}).",
                detail='{"detail": "fake: topology refused"}',
                status=self.reject_status,
            )
        stored = json.loads(json.dumps(topology))
        existing = self.topologies.setdefault(site_id, [])
        if stored.get("id"):
            for index, current in enumerate(existing):
                if current.get("id") == stored["id"]:
                    existing[index] = stored
                    return json.loads(json.dumps(stored))
            from simrack.errors import BackendError

            raise BackendError(f"Mist API PUT /sites/{site_id}/evpn_topologies/{stored['id']} failed (404).", status=404)
        self._topologies_made += 1
        stored["id"] = f"topo-{self._topologies_made}"
        existing.append(stored)
        return json.loads(json.dumps(stored))

    def delete_evpn_topology(self, site_id, topology_id):
        self._guard("delete_evpn_topology")
        self._log("delete_evpn_topology", site_id, topology_id)
        existing = self.topologies.get(site_id, [])
        if not any(t.get("id") == topology_id for t in existing):
            from simrack.errors import BackendError

            raise BackendError(f"Mist API DELETE /sites/{site_id}/evpn_topologies/{topology_id} failed (404).", status=404)
        self.topologies[site_id] = [t for t in existing if t.get("id") != topology_id]

    def devices(self, site_id, device_type="switch"):
        self._read()
        return list(self.devices_by_site.get(site_id, []))

    def device(self, site_id, device_id):
        self._read()
        return dict(self.device_config.get((site_id, device_id), {}))

    def device_cli(self, site_id, device_id):
        self._read()
        return dict(self.cli.get((site_id, device_id), {"cli": ["set version 26.2R1.7"]}))

    def port_stats(self, site_id, mac=None, limit=1000):
        self._read()
        return [dict(r) for r in self.ports_by_site.get(site_id, []) if mac is None or r.get("mac") == mac]

    def put_device(self, site_id, device_id, config):
        self._guard("put_device")
        self._log("put_device", site_id, device_id, sorted(config)[:5])
        self.device_config[(site_id, device_id)] = json.loads(json.dumps(config))

    #: What GET /orgs/{org}/ocdevices/outbound_ssh_cmd returns: set lines in ``cmd``.
    ADOPT_CMD = (
        "set system services ssh protocol-version v2\n"
        "set system services outbound-ssh client mist device-id ABC123 secret \"$9$fakeSecretXyZ\"\n"
        "\n"
        "# keep-alive\n"
        "set system services outbound-ssh client mist oc-term.mist.com port 2200 timeout 60 retry 1000\n"
    )

    def adopt_config(self, org_id=None, site_id=None):
        self._log("adopt_config", org_id, site_id=site_id)
        return {"cmd": self.ADOPT_CMD}

    def add_switch(self, site_id, name, *, device_id=None, config=None, mac=None):
        """A switch adopted into the site, the way Mist lists it: id, name and MAC."""
        device_id = device_id or f"dev-{name}"
        self._macs += 1
        mac = mac or f"5c5b3500{self._macs:04x}"
        self.devices_by_site.setdefault(site_id, []).append({"id": device_id, "name": name, "mac": mac, "connected": True})
        config = dict(config or {"port_config": {}, "name": name})
        config.setdefault("mac", mac)
        self.device_config[(site_id, device_id)] = config
        return device_id


class FakeConsole:
    """A vJunos serial console: login, FreeBSD shell, CLI and configuration mode.

    It answers the way the real console does closely enough for ``console.adopt``:
    it echoes what is typed (never passwords), prints ``{master:0}`` above CLI
    prompts and hands output back in small chunks. Time only moves when a read
    times out or ``sleep`` is called, so tests never wait.
    """

    BANNER = "--- JUNOS 26.2R1.7 Kernel 64-bit  JNPR-15.0-20260714.1c0a4c6_buil\r\n"

    def __init__(self, *, root_password=None, booted=True, start="login", fail_on=None,
                 commit_fails=False, dhcp_ip="192.0.2.37", dhcp_after=0, host=""):
        self.root_password = root_password
        self.booted = booted
        self.state = start
        self.fail_on = fail_on
        self.commit_fails = commit_fails
        self.dhcp_ip = dhcp_ip
        self.dhcp_after = dhcp_after
        self.host = host
        self.now = 0.0
        self.pending = ""
        self.lines: list[str] = []
        self.candidate: list = []
        self.config: list[str] = []
        self.shows = 0
        self.closed = False
        self._typed = ""
        self._user = None
        self._first_entry = None

    # -- what adopt() uses ------------------------------------------------------
    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def close(self):
        self.closed = True

    def send(self, data):
        if not self.booted:
            return
        self._typed += data
        while "\r" in self._typed:
            line, self._typed = self._typed.split("\r", 1)
            self.lines.append(line)
            self._line(line)

    def read(self, timeout):
        if self.pending:
            chunk, self.pending = self.pending[:64], self.pending[64:]
            return chunk
        self.now += timeout
        return ""

    # -- the device ---------------------------------------------------------------
    def _who(self):
        return f"root@{self.host}" if self.host else "root"

    def _prompt(self):
        if self.state == "login":
            return "login: "
        if self.state == "shell":
            return f"root@{self.host}:~ # "
        if self.state == "cli":
            return "{master:0}\r\n" + f"{self._who()}> "
        return "{master:0}[edit]\r\n" + f"{self._who()}# "

    def _out(self, text):
        self.pending += text

    def _to(self, state, before=""):
        self.state = state
        self._out(before + self._prompt())

    def _line(self, line):
        secret = self.state in ("password", "new_password", "retype")
        self._out("\r\n" if secret else line + "\r\n")
        getattr(self, f"_{self.state}")(line.strip())

    def _login(self, line):
        if not line:
            return self._to("login")
        self._user = line
        if line == "root" and not self.root_password:
            return self._to("shell", self.BANNER)
        self.state = "password"
        self._out("Password:")

    def _password(self, line):
        if self._user == "root" and self.root_password and line == self.root_password:
            return self._to("shell", self.BANNER)
        self._to("login", "Login incorrect\r\n")

    def _shell(self, line):
        if line == "cli":
            return self._to("cli")
        if line == "exit":
            return self._to("login", "logout\r\n\r\n")
        self._to("shell", f"{line.split()[0]}: Command not found.\r\n" if line else "")

    def _cli(self, line):
        if line == "configure":
            return self._to("config", "Entering configuration mode\r\n\r\n")
        if line == "exit":
            return self._to("shell")
        if line.startswith("show interfaces terse fxp0.0"):
            self.shows += 1
            local = f"{self.dhcp_ip}/24" if self.dhcp_ip and self.shows > self.dhcp_after else ""
            table = (
                "Interface               Admin Link Proto    Local                 Remote\r\n"
                f"fxp0.0                  up    up   inet     {local}\r\n\r\n"
            )
            return self._to("cli", table)
        self._to("cli", "                  ^\r\nunknown command.\r\n\r\n" if line else "")

    def _config(self, line):
        if not line:
            return self._to("config")
        if self.fail_on and self.fail_on in line:
            return self._to("config", "                                        ^\r\nsyntax error.\r\n\r\n")
        if line == "delete chassis auto-image-upgrade":
            return self._to("config", "warning: statement not found\r\n\r\n")
        if line == "set system root-authentication plain-text-password":
            self.state = "new_password"
            return self._out("New password:")
        if line.startswith(("set ", "delete ")):
            self.candidate.append(line)
            return self._to("config", "\r\n")
        if line.startswith("commit"):
            if self.commit_fails:
                return self._to("config", "error: configuration check-out failed\r\n\r\n")
            for item in self.candidate:
                if isinstance(item, tuple):
                    self.root_password = item[1]
                    continue
                if item.startswith("set system host-name "):
                    self.host = item.split()[-1]
                self.config.append(item)
            self.candidate = []
            return self._to("cli", "commit complete\r\nExiting configuration mode\r\n\r\n")
        if line == "rollback 0":
            self.candidate = []
            return self._to("config", "load complete\r\n\r\n")
        if line.startswith("exit"):
            if self.candidate:
                self.state = "discard"
                return self._out(
                    "The configuration has been changed but not committed\r\n"
                    "Exit with uncommitted changes? [yes,no] (yes) "
                )
            return self._to("cli", "Exiting configuration mode\r\n\r\n")
        self._to("config", "syntax error.\r\n\r\n")

    def _new_password(self, line):
        self._first_entry = line
        self.state = "retype"
        self._out("Retype new password:")

    def _retype(self, line):
        if line != self._first_entry:
            return self._to("config", "error: Passwords do not match\r\n\r\n")
        self.candidate.append(("root-password", line))
        self._to("config", "\r\n")

    def _discard(self, line):
        if line in ("", "yes"):
            self.candidate = []
            return self._to("cli", "Exiting configuration mode\r\n\r\n")
        self._to("config", "\r\n")


#: The placeholder lab every manager-level test runs against.
LAB_PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "lab-profile.toml")


def lab_settings(tmpdir: str, *, profile: str = LAB_PROFILE, **overrides) -> Settings:
    """Settings loaded the way the service loads them, from a lab profile, then overridden."""
    shutil.copyfile(profile, os.path.join(tmpdir, PROFILE_FILE))
    settings = Settings.load({"SIMRACK_STATE_DIR": tmpdir})
    return dataclasses.replace(settings, **overrides)


def make_manager(tmpdir: str, *, proxmox=None, mist=None, **settings_kwargs) -> SandboxManager:
    settings = lab_settings(tmpdir, **{"pve_token": "fake", "mist_token": "fake-token", **settings_kwargs})
    proxmox = proxmox or FakeProxmox()
    proxmox.use(settings)  # as the service builds its Proxmox client from the settings
    return SandboxManager(settings, proxmox=proxmox, mist=mist or FakeMist())


@contextlib.contextmanager
def proxmox_may_only_look(manager: SandboxManager):
    """As if someone cut the Proxmox token down to look-only, then put it back."""
    manager.proxmox.privileges = PVE_AUDITOR
    manager.access.forget()
    try:
        yield
    finally:
        manager.proxmox.privileges = PVE_ADMINISTRATOR
        manager.access.forget()


@contextlib.contextmanager
def mist_may_only_read(manager: SandboxManager):
    """As if someone cut the Mist token's role on the org down to read, then put it back."""
    role, manager.mist.role = manager.mist.role, "read"
    manager.access.forget()
    try:
        yield
    finally:
        manager.mist.role = role
        manager.access.forget()


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="simrack-test-")
        return self.path

    def __exit__(self, *exc):
        import shutil

        shutil.rmtree(self.path, ignore_errors=True)
        return False


def read_state(manager: SandboxManager, name: str) -> dict:
    with open(os.path.join(manager.state_dir, "sandboxes", f"{name}.json"), encoding="utf-8") as handle:
        return json.load(handle)


class RedirectingPair:
    """Two loopback web servers. ``saved`` is the address a token was saved for;
    it answers every request with a 302 to the other one, which answers ``body``.
    ``carried`` notes, per server and per request, whether a token came along."""

    def __init__(self, body: bytes = b"{}"):
        self.body = body
        self.carried: dict[str, list[bool]] = {"saved": [], "elsewhere": []}

    def __enter__(self):
        self._no_proxy = mock.patch.dict(os.environ, {"no_proxy": "*"})
        self._no_proxy.start()
        self._elsewhere = self._serve("elsewhere", self._answer)
        self._saved = self._serve("saved", self._redirect)
        self.saved = "http://127.0.0.1:%d" % self._saved.server_address[1]
        return self

    def __exit__(self, *exc):
        for server in (self._saved, self._elsewhere):
            server.shutdown()
            server.server_close()
        self._no_proxy.stop()
        return False

    def _serve(self, name: str, answer):
        carried = self.carried[name]

        class Handler(http.server.BaseHTTPRequestHandler):
            def handle_any(self):
                carried.append("Authorization" in self.headers)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                answer(self)

            do_GET = do_POST = do_PUT = do_DELETE = handle_any

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _redirect(self, handler):
        handler.send_response(302)
        handler.send_header("Location", "http://127.0.0.1:%d%s" % (self._elsewhere.server_address[1], handler.path))
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    def _answer(self, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(self.body)))
        handler.end_headers()
        handler.wfile.write(self.body)
