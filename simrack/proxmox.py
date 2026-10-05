"""Proxmox VE API client (standard library only).

Talks to the local PVE API over HTTPS with an API token. No shell
out, so a user-supplied name cannot become a command.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import hostnet
from .config import Settings
from .errors import BackendError, NotConfigured

#: PVE returns this when a config field was not accepted.
_UNSET = object()


def _tls_for(base: str) -> ssl.SSLContext:
    """Check the certificate unless the API is this host's own pveproxy: its
    certificate is self-signed, and loopback traffic never leaves the machine."""
    host = urllib.parse.urlsplit(base).hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return ssl._create_unverified_context() if loopback else ssl.create_default_context()


class ProxmoxClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.use(settings or Settings())

    def use(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.pve_api_base.rstrip("/")
        self.node = settings.pve_node
        self.token = settings.pve_token
        self._ssl = _tls_for(self.base)

    # -- transport --------------------------------------------------------------

    def _request(self, method: str, path: str, params: dict | None = None) -> object:
        if not self.token:
            raise NotConfigured(
                "Proxmox API token is not set.",
                detail="Add one on SimRack's setup page.",
            )
        url = f"{self.base}{path}"
        data = None
        payload = {
            k: ("1" if v is True else "0" if v is False else str(v))
            for k, v in (params or {}).items()
            if v is not _UNSET and v is not None
        }
        if payload:
            encoded = urllib.parse.urlencode(payload)
            if method in ("GET", "DELETE"):
                url = f"{url}?{encoded}"
            else:
                data = encoded.encode()
        request = urllib.request.Request(url, data=data, method=method)
        request.add_unredirected_header("Authorization", f"PVEAPIToken={self.token}")
        request.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60, context=self._ssl) as reply:
                body = reply.read()
        except urllib.error.HTTPError as error:  # PVE errors are JSON with a message
            raw = error.read().decode("utf-8", "replace")
            message = raw
            try:
                message = json.loads(raw).get("errors", raw)
            except json.JSONDecodeError:
                pass
            raise BackendError(
                f"Proxmox API {method} {path} failed ({error.code}).", detail=str(message)[:500], status=error.code
            ) from error
        except urllib.error.URLError as error:
            detail = str(error.reason)
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                detail += (
                    ". SimRack runs on the Proxmox host, so on the setup page set the Proxmox address to "
                    "https://127.0.0.1:8006/api2/json and paste the token again, or give that host a certificate this one trusts."
                )
            raise BackendError(f"Cannot reach the Proxmox API at {self.base}.", detail=detail) from error
        if not body:
            return None
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise BackendError("Proxmox API returned a non-JSON response.") from error
        return payload.get("data") if isinstance(payload, dict) else payload

    # -- read -------------------------------------------------------------------

    def permissions(self, path: str) -> dict:
        """What this token may do on ``path``: privilege -> propagate. Any token may ask about itself."""
        reply = self._request("GET", "/access/permissions", {"path": path}) or {}
        return reply.get(path, {}) if isinstance(reply, dict) else {}

    def node_status(self) -> dict:
        return self._request("GET", f"/nodes/{self.node}/status") or {}

    def memory_mb(self) -> dict:
        """Host memory in MB. "free" is what new guests can use: MemAvailable, which
        counts the page cache the kernel hands back. MemFree alone undercounts it.
        Older Proxmox releases report only "free", so fall back to that."""
        try:
            memory = self.node_status().get("memory") or {}
            free = memory.get("available", memory.get("free", 0))
            return {"free": int(free) // (1024 * 1024), "total": int(memory.get("total", 0)) // (1024 * 1024)}
        except (AttributeError, TypeError, ValueError):
            return {"free": 0, "total": 0}

    def free_memory_mb(self) -> int:
        return self.memory_mb()["free"]

    def list_vms(self) -> list[dict]:
        return self._request("GET", f"/nodes/{self.node}/qemu") or []

    def list_lxc(self) -> list[dict]:
        return self._request("GET", f"/nodes/{self.node}/lxc") or []

    def get_vm(self, vmid: int) -> dict:
        return self._request("GET", f"/nodes/{self.node}/qemu/{int(vmid)}/config") or {}

    def wait_task(self, upid: object, *, timeout: int = 600, poll: float = 1.0) -> None:
        """Block until an async PVE task ends. Clone, create, stop, delete and
        rollback all return a UPID and keep the guest locked until they finish."""
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        deadline = time.monotonic() + timeout
        path = f"/nodes/{self.node}/tasks/{urllib.parse.quote(upid, safe='')}/status"
        while True:
            status = self._request("GET", path) or {}
            if status.get("status") == "stopped":
                if status.get("exitstatus") not in ("OK", None) and not str(status.get("exitstatus", "")).startswith("WARNINGS"):
                    raise BackendError("Proxmox task failed.", detail=f"{upid}: {status.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise BackendError("Proxmox task timed out.", detail=f"{upid} still running after {timeout}s.")
            time.sleep(poll)

    def list_images(self, storage: str = "local") -> list[dict]:
        """ISOs and importable disk images a sandbox node can boot from. Proxmox
        before 8.2 has no import content type and answers 400 when asked for it."""
        found = []
        for content in ("iso", "import"):
            try:
                items = self._request("GET", f"/nodes/{self.node}/storage/{storage}/content", {"content": content}) or []
            except BackendError as error:
                if content == "import" and error.status == 400:
                    continue
                raise
            for item in items:
                found.append({"volid": item.get("volid", ""), "content": content, "size": item.get("size", 0)})
        return found

    # -- guests -----------------------------------------------------------------

    def create_vm(
        self,
        vmid: int,
        name: str,
        *,
        pool: str,
        memory_mb: int,
        cores: int,
        storage: str = "local-lvm",
        disk_gb: int = 32,
        iso: str | None = None,
        import_from: str | None = None,
        disk_bus: str = "virtio0",
        smbios_product: str | None = None,
        cpu: str | None = None,
        start: bool = False,
        extra: dict | None = None,
    ) -> str:
        """Create a guest shaped like the live vJunos switches (SeaBIOS, virtio
        disk, serial console). ``import_from`` copies a disk image in;
        ``iso`` boots an installer from a blank disk.

        Only fields an API token may set are sent: PVE lets just root@pam set
        ``args`` or ``hookscript``, so the SMBIOS product goes in ``smbios1``.
        The guest is made in ``pool``, where SimRack's token may change it."""
        if disk_bus not in ("virtio0", "scsi0", "sata0"):
            raise BackendError(f"Unsupported disk bus {disk_bus!r}.")
        if import_from and iso:
            raise BackendError("Give an ISO or a disk image, not both.")
        params = {
            "vmid": vmid,
            "name": name,
            "pool": pool,
            "memory": memory_mb,
            "cores": cores,
            "sockets": 1,
            "ostype": "l26",
            "serial0": "socket",
            "onboot": 0,
            "cpu": cpu or _UNSET,
            "start": 1 if start else _UNSET,
        }
        if smbios_product:
            # smbios1 text must be base64 to carry a "-", and PVE only fills in
            # a uuid when smbios1 is absent, so give one.
            product = base64.b64encode(smbios_product.encode()).decode()
            params["smbios1"] = f"base64=1,product={product},uuid={uuid.uuid4()}"
        if disk_bus == "scsi0":
            params["scsihw"] = "virtio-scsi-single"
        if import_from:
            params[disk_bus] = f"{storage}:0,import-from={import_from}" + (",iothread=1" if disk_bus != "sata0" else "")
            params["boot"] = f"order={disk_bus}"
        else:
            params[disk_bus] = f"{storage}:{int(disk_gb)}" + (",iothread=1" if disk_bus != "sata0" else "")
            if iso:
                params["ide2"] = f"{iso},media=cdrom"
                params["boot"] = f"order=ide2;{disk_bus}"
            else:
                params["boot"] = f"order={disk_bus}"
        params.update(extra or {})
        return self._request("POST", f"/nodes/{self.node}/qemu", params) or ""

    def clone_vm(self, source_vmid: int, new_vmid: int, *, name: str, pool: str, full: bool = True) -> str:
        """Clone into ``pool``, where SimRack's token may change the copy."""
        return (
            self._request(
                "POST",
                f"/nodes/{self.node}/qemu/{int(source_vmid)}/clone",
                {"newid": int(new_vmid), "name": name, "pool": pool, "full": 1 if full else 0},
            )
            or ""
        )

    def delete_vm(self, vmid: int, *, purge: bool = True) -> str:
        return self._request("DELETE", f"/nodes/{self.node}/qemu/{int(vmid)}", {"purge": 1 if purge else 0, "destroy-unreferenced-disks": 1}) or ""

    def set_vm_config(self, vmid: int, **fields) -> None:
        """Set fields; a value of None removes that field (PVE ``delete=``)."""
        clean = {k: v for k, v in fields.items() if v is not None}
        removed = sorted(k for k, v in fields.items() if v is None)
        if removed:
            clean["delete"] = ",".join(removed)
        if clean:
            self._request("PUT", f"/nodes/{self.node}/qemu/{int(vmid)}/config", clean)

    def set_power(self, vmid: int, action: str, *, timeout: int = 120) -> str:
        if action not in {"start", "stop", "shutdown", "reboot", "reset"}:
            raise BackendError(f"Unknown power action {action!r}.", detail="Use start, stop, shutdown, reboot or reset.")
        params = {"timeout": timeout} if action in {"stop", "shutdown"} else None
        return self._request("POST", f"/nodes/{self.node}/qemu/{int(vmid)}/status/{action}", params) or ""

    def vm_status(self, vmid: int) -> dict:
        return self._request("GET", f"/nodes/{self.node}/qemu/{int(vmid)}/status/current") or {}

    # -- bridges ----------------------------------------------------------------
    # Sandbox bridges are runtime-only Linux bridges (see hostnet). SimRack only
    # reads the PVE network API: applying a change there rewrites
    # /etc/network/interfaces.

    def get_network(self) -> list[dict]:
        return hostnet.list_bridges()

    def bridges(self) -> list[dict]:
        """The bridges set up in /etc/network/interfaces, with their "cidr" and "gateway"."""
        return self._request("GET", f"/nodes/{self.node}/network", {"type": "any_bridge"}) or []

    def create_bridge(self, name: str, *, mtu: int = 9216) -> None:
        hostnet.create(name, mtu)

    def delete_bridge(self, name: str) -> None:
        hostnet.delete(name)

    def bridge_exists(self, name: str) -> bool:
        return hostnet.exists(name)

    def tune_port(self, vmid: int, net_index: int) -> bool:
        return hostnet.tune_port(vmid, net_index)

    # -- snapshots --------------------------------------------------------------

    def create_snapshot(self, vmid: int, name: str) -> str:
        return self._request("POST", f"/nodes/{self.node}/qemu/{int(vmid)}/snapshot", {"snapname": name}) or ""

    def rollback_snapshot(self, vmid: int, name: str) -> str:
        return self._request("POST", f"/nodes/{self.node}/qemu/{int(vmid)}/snapshot/{name}/rollback") or ""

    def delete_snapshot(self, vmid: int, name: str) -> str:
        return self._request("DELETE", f"/nodes/{self.node}/qemu/{int(vmid)}/snapshot/{name}") or ""
