"""In-memory stand-ins for Proxmox and Mist so tests never touch the live lab."""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile

from labfront.config import Settings
from labfront.service import SandboxManager


class FakeProxmox:
    """Records every call. Free and total memory are set by the test."""

    def __init__(self, free_mb: int = 40000, templates: list[int] | None = None, total_mb: int = 65536) -> None:
        self.free_mb = free_mb
        self.total_mb = total_mb
        self.vms: dict[int, dict] = {}
        self.networks: dict[str, dict] = {}
        self.snapshots: dict[int, dict[str, dict]] = {}
        self.calls: list[tuple] = []
        self.templates = templates or []
        for vmid in self.templates:
            self.vms[vmid] = {"vmid": vmid, "name": f"tmpl-{vmid}", "template": 1, "maxmem": 5120 * 1024 * 1024}

    def _log(self, _method, *args, **kwargs):
        self.calls.append((_method, args, kwargs))

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

    # -- reads ------------------------------------------------------------------
    def node_status(self):
        mib = 1024 * 1024
        return {"memory": {"free": self.free_mb * mib, "available": self.free_mb * mib, "total": self.total_mb * mib}}

    def memory_mb(self):
        return {"free": self.free_mb, "total": self.total_mb}

    def free_memory_mb(self):
        return self.free_mb

    def list_vms(self):
        return list(self.vms.values())

    def get_vm(self, vmid):
        return dict(self.vms.get(int(vmid), {}))

    def get_network(self):
        return list(self.networks.values())

    def list_images(self, storage="local"):
        return [
            {"volid": "local:import/vJunos-switch-26.2R1.7.qcow2", "content": "import", "size": 4645126144},
            {"volid": "local:iso/Virtual-Mist-Edge-Deb12-1.0.iso", "content": "iso", "size": 1 << 30},
        ]

    def storage_free_gb(self, storage="local-lvm"):
        return 800.0

    # -- writes -----------------------------------------------------------------
    def create_vm(self, vmid, name, **kwargs):
        self._log("create_vm", vmid, name, **kwargs)
        self.vms[int(vmid)] = {"vmid": int(vmid), "name": name, "config": kwargs, "status": "stopped"}
        nics = {k: v for k, v in (kwargs.get("extra") or {}).items() if re.fullmatch(r"net\d+", k)}
        self.vms[int(vmid)].update(self._with_macs(vmid, nics))
        return "UPID:x"

    def clone_vm(self, source_vmid, new_vmid, *, name, full=True):
        self._log("clone_vm", source_vmid, new_vmid, name=name, full=full)
        # PVE copies the template's NICs and always gives each one a new MAC.
        source = self.vms.get(int(source_vmid), {})
        nics = {}
        for key, value in source.items():
            found = re.fullmatch(r"net(\d+)", key)
            if found:
                rest = value.split(",", 1)[1] if "," in value else ""
                nics[key] = f"virtio={self.mac(new_vmid, found.group(1))}" + ("," + rest if rest else "")
        self.vms[int(new_vmid)] = {
            "vmid": int(new_vmid),
            "name": name,
            "template_of": int(source_vmid),
            "status": "stopped",
            **(nics or {"net0": "virtio=02:00:00:CC:00:00,bridge=vmbr0,tag=5"}),
        }
        return "UPID:x"

    def delete_vm(self, vmid, *, purge=True):
        from labfront.errors import BackendError

        self._log("delete_vm", vmid, purge=purge)
        if self.vms.get(int(vmid), {}).get("status") == "running":
            raise BackendError(f"Proxmox API DELETE /qemu/{vmid} failed (500).", detail=f"VM {vmid} is running - destroy failed")
        self.vms.pop(int(vmid), None)
        return "UPID:x"

    def wait_task(self, upid, *, timeout=600, poll=1.0):
        self._log("wait_task", upid, timeout=timeout)

    def set_vm_config(self, vmid, **fields):
        self._log("set_vm_config", vmid, **fields)
        vm = self.vms.setdefault(int(vmid), {"vmid": int(vmid), "name": f"vm{vmid}"})
        for key, value in self._with_macs(vmid, fields).items():
            if value is None:
                vm.pop(key, None)
            else:
                vm[key] = value

    def set_power(self, vmid, action, *, timeout=120):
        self._log("set_power", vmid, action, timeout=timeout)
        vm = self.vms.setdefault(int(vmid), {"vmid": int(vmid), "name": f"vm{vmid}"})
        vm["status"] = "running" if action == "start" else "stopped"
        return "UPID:x"

    def vm_status(self, vmid):
        return {"status": self.vms.get(int(vmid), {}).get("status", "unknown")}

    def create_bridge(self, name, *, mtu=9216):
        self._log("create_bridge", name, mtu=mtu)
        self.networks[name] = {"iface": name, "type": "bridge", "mtu": mtu}

    def bridge_exists(self, name):
        return name in self.networks

    def tune_port(self, vmid, net_index):
        self._log("tune_port", vmid, net_index=net_index)
        return self.vms.get(int(vmid), {}).get("status") == "running"

    def delete_bridge(self, name):
        self._log("delete_bridge", name)
        self.networks.pop(name, None)

    def create_snapshot(self, vmid, name):
        self._log("create_snapshot", vmid, name)
        self.snapshots.setdefault(int(vmid), {})[name] = {"config": dict(self.vms.get(int(vmid), {}))}
        return "UPID:x"

    def rollback_snapshot(self, vmid, name):
        self._log("rollback_snapshot", vmid, name)
        snapshot = self.snapshots.get(int(vmid), {}).get(name)
        if snapshot:
            self.vms[int(vmid)] = dict(snapshot["config"])
        return "UPID:x"

    def delete_snapshot(self, vmid, name):
        self.snapshots.get(int(vmid), {}).pop(name, None)
        return ""

    # -- assertions helpers -----------------------------------------------------
    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    def config_calls_for(self, vmid):
        return [c for c in self.called("set_vm_config") if c[1][0] == int(vmid)]


class FakeMist:
    def __init__(self, *, token="fake-token", writes=True) -> None:
        self.token = token
        self.settings = Settings(mist_token=token, mist_writes_enabled=writes)
        self.sites: dict[str, dict] = {}
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
        self._macs = 0

    def _log(self, _method, *args, **kwargs):
        self.calls.append((_method, args, kwargs))

    def _read(self):
        if self.fail_reads:
            from labfront.errors import BackendError

            raise BackendError("Cannot reach the Mist API.", detail="fake: fail_reads")

    def configured(self):
        return bool(self.token)

    def writes_enabled(self):
        return bool(self.token) and self.settings.mist_writes_enabled

    def _guard(self, what):
        if not self.writes_enabled():
            from labfront.errors import GuardrailViolation

            raise GuardrailViolation("Mist writes are disabled.", detail=f"refused {what}")

    def create_site(self, name, org_id=None):
        self._guard("create_site")
        self._log("create_site", name)
        self._n += 1
        site_id = f"site-{self._n}"
        self.sites[site_id] = {"id": site_id, "name": name}
        self.settings_by_site[site_id] = {}
        self.topologies[site_id] = []
        self.devices_by_site[site_id] = []
        return self.sites[site_id]

    def delete_site(self, site_id):
        self._guard("delete_site")
        self._log("delete_site", site_id)
        self.sites.pop(site_id, None)

    def site_setting(self, site_id):
        self._read()
        return dict(self.settings_by_site.get(site_id, {}))

    def put_site_setting(self, site_id, setting):
        self._guard("put_site_setting")
        self._log("put_site_setting", site_id, setting)
        self.settings_by_site[site_id] = json.loads(json.dumps(setting))

    def evpn_topologies(self, site_id):
        self._read()
        return list(self.topologies.get(site_id, []))

    def evpn_topology(self, site_id, topology_id):
        self._read()
        for topology in self.topologies.get(site_id, []):
            if topology.get("id") == topology_id:
                return json.loads(json.dumps(topology))
        from labfront.errors import BackendError

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
            from labfront.errors import BackendError

            verb, path = ("PUT", f"/sites/{site_id}/evpn_topologies/{topology['id']}") if topology.get("id") else ("POST", f"/sites/{site_id}/evpn_topologies")
            raise BackendError(
                f"Mist API {verb} {path} failed ({self.reject_status}).",
                detail='{"detail": "fake: topology refused"}',
                status=self.reject_status,
            )
        stored = json.loads(json.dumps(topology))
        existing = self.topologies.setdefault(site_id, [])
        for index, current in enumerate(existing):
            if stored.get("id") and current.get("id") == stored["id"]:
                existing[index] = stored
                return json.loads(json.dumps(stored))
        stored.setdefault("id", f"topo-{len(existing) + 1}")
        existing.append(stored)
        return json.loads(json.dumps(stored))

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
    settings = Settings.from_env({"LABFRONT_PROFILE": profile, "LABFRONT_STATE_DIR": tmpdir})
    return dataclasses.replace(settings, **overrides)


def make_manager(tmpdir: str, *, proxmox=None, mist=None, **settings_kwargs) -> SandboxManager:
    settings = lab_settings(
        tmpdir,
        pve_token="fake",
        mist_token="fake-token",
        mist_writes_enabled=True,
        allow_writes=settings_kwargs.pop("allow_writes", True),
        **settings_kwargs,
    )
    return SandboxManager(
        settings,
        proxmox=proxmox or FakeProxmox(),
        mist=mist or FakeMist(),
    )


class TempDir:
    def __enter__(self):
        self.path = tempfile.mkdtemp(prefix="labfront-test-")
        return self.path

    def __exit__(self, *exc):
        import shutil

        shutil.rmtree(self.path, ignore_errors=True)
        return False


def read_state(manager: SandboxManager, name: str) -> dict:
    with open(os.path.join(manager.state_dir, "sandboxes", f"{name}.json"), encoding="utf-8") as handle:
        return json.load(handle)
