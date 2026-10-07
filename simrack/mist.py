"""Juniper Mist API client (standard library only).

Every write asks the write gate first: SimRack's access check (access.py), which
asks Mist what the token may do. Without a gate no write goes out. Every write
is also scoped to a sandbox site that the caller owns.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from .access import Refusal, raise_if
from .config import Settings
from .errors import BackendError, GuardrailViolation, NotConfigured


class MistClient:
    def __init__(self, settings: Settings | None = None, *, write_gate: Callable[[], Refusal | None] | None = None) -> None:
        self.use(settings or Settings())
        self._ssl = ssl.create_default_context()
        #: Why a write may not go out, or None when it may.
        self.write_gate = write_gate or _no_gate

    def use(self, settings: Settings) -> None:
        self.settings = settings
        self.base = settings.mist_api_base.rstrip("/")
        self.token = settings.mist_token
        self.org_id = settings.org_id

    # -- transport --------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None, *, write: bool = False) -> object:
        if not self.token:
            raise NotConfigured(
                "Mist API token is not set.",
                detail="Add a Mist token on SimRack's setup page. Without one every Mist action is off.",
            )
        if write:
            raise_if(self.write_gate())
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_unredirected_header("Authorization", f"Token {self.token}")
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=60, context=self._ssl) as reply:
                body = reply.read()
        except urllib.error.HTTPError as error:
            raw = error.read().decode("utf-8", "replace")
            raise BackendError(f"Mist API {method} {path} failed ({error.code}).", detail=raw[:500], status=error.code) from error
        except urllib.error.URLError as error:
            detail = str(error.reason)
            if isinstance(error.reason, ssl.SSLCertVerificationError):
                detail += (
                    ". If this network inspects TLS, add its root CA to the host's trust store "
                    "(copy it to /usr/local/share/ca-certificates/ and run update-ca-certificates)."
                )
            raise BackendError("Cannot reach the Mist API.", detail=detail) from error
        if not body:
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise BackendError("Mist API returned a non-JSON response.") from error

    def whoami(self) -> dict:
        """The token's privileges: GET /self lists each scope and role, for a user or an org token."""
        reply = self._request("GET", "/self")
        return reply if isinstance(reply, dict) else {}

    def configured(self) -> bool:
        return bool(self.token)

    # -- org / site -------------------------------------------------------------

    def sites(self, org_id: str | None = None) -> list[dict]:
        return self._request("GET", f"/orgs/{org_id or self.org_id}/sites") or []

    def org(self, org_id: str | None = None) -> dict:
        """The org, with its ``msp_id`` and ``orggroup_ids``: what a role held above it would sit on."""
        reply = self._request("GET", f"/orgs/{org_id or self.org_id}")
        return reply if isinstance(reply, dict) else {}

    def create_site(self, name: str, org_id: str | None = None) -> dict:
        return self._request("POST", f"/orgs/{org_id or self.org_id}/sites", {"name": name}, write=True) or {}

    def delete_site(self, site_id: str) -> None:
        self._request("DELETE", f"/sites/{site_id}", write=True)

    def release_devices(self, serials: list[str], org_id: str | None = None) -> dict:
        """Release devices from the org's inventory by serial, which also takes them off their site.
        Mist answers 200 even when it keeps some: ``success`` lists the serials it released,
        ``error`` the ones it kept, with a ``reason`` each."""
        body = {"op": "delete", "serials": list(serials)}
        reply = self._request("PUT", f"/orgs/{org_id or self.org_id}/inventory", body, write=True)
        return reply if isinstance(reply, dict) else {}

    def site_setting(self, site_id: str) -> dict:
        return self._request("GET", f"/sites/{site_id}/setting") or {}

    def put_site_setting(self, site_id: str, setting: dict) -> None:
        self._request("PUT", f"/sites/{site_id}/setting", setting, write=True)

    # -- fabric -----------------------------------------------------------------

    def evpn_topologies(self, site_id: str) -> list[dict]:
        return self._request("GET", f"/sites/{site_id}/evpn_topologies") or []

    def evpn_topology(self, site_id: str, topology_id: str) -> dict:
        """One topology in full: its switches and their links."""
        return self._request("GET", f"/sites/{site_id}/evpn_topologies/{topology_id}") or {}

    def put_evpn_topology(self, site_id: str, topology: dict) -> dict:
        topology_id = topology.get("id")
        path = f"/sites/{site_id}/evpn_topologies/{topology_id}" if topology_id else f"/sites/{site_id}/evpn_topologies"
        return self._request("PUT" if topology_id else "POST", path, topology, write=True) or {}

    def delete_evpn_topology(self, site_id: str, topology_id: str) -> None:
        self._request("DELETE", f"/sites/{site_id}/evpn_topologies/{topology_id}", write=True)

    def devices(self, site_id: str, device_type: str = "switch") -> list[dict]:
        query = urllib.parse.urlencode({"type": device_type})
        return self._request("GET", f"/sites/{site_id}/devices?{query}") or []

    def device(self, site_id: str, device_id: str) -> dict:
        return self._request("GET", f"/sites/{site_id}/devices/{device_id}") or {}

    def device_stats(self, site_id: str, device_type: str = "switch") -> list[dict]:
        """What each device is doing: status, config_status, version, last_seen, uptime, ip."""
        query = urllib.parse.urlencode({"type": device_type})
        reply = self._request("GET", f"/sites/{site_id}/stats/devices?{query}") or []
        return list(reply.get("results") or []) if isinstance(reply, dict) else list(reply)

    def device_cli(self, site_id: str, device_id: str) -> dict:
        return self._request("GET", f"/sites/{site_id}/devices/{device_id}/config_cmd") or {}

    def put_device(self, site_id: str, device_id: str, config: dict) -> None:
        self._request("PUT", f"/sites/{site_id}/devices/{device_id}", config, write=True)

    def port_stats(self, site_id: str, mac: str | None = None, limit: int = 1000) -> list[dict]:
        """Switch ports as Mist last heard them: up/down and the LLDP neighbour on each."""
        query = urllib.parse.urlencode({**({"mac": mac} if mac else {}), "limit": limit})
        reply = self._request("GET", f"/sites/{site_id}/stats/ports/search?{query}") or {}
        return list(reply.get("results") or []) if isinstance(reply, dict) else list(reply)

    def adopt_config(self, org_id: str | None = None, site_id: str | None = None) -> dict:
        """The Junos lines that point a switch at Mist; with a site, it lands there."""
        query = "?" + urllib.parse.urlencode({"site_id": site_id}) if site_id else ""
        return self._request("GET", f"/orgs/{org_id or self.org_id}/ocdevices/outbound_ssh_cmd{query}") or {}


def _no_gate() -> Refusal:
    return (GuardrailViolation, "Mist changes are off.", "Nothing has checked what this Mist token may do.")
