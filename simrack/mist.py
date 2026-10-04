"""Juniper Mist API client (standard library only).

Read-only unless ``Settings.mist_writes_enabled`` is set by the operator, and
even then every write is scoped to a sandbox site that the caller owns.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

from .config import NO_PROFILE, Settings
from .errors import BackendError, GuardrailViolation, NotConfigured


class MistClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.base = self.settings.mist_api_base.rstrip("/")
        self.token = self.settings.mist_token
        self.org_id = self.settings.org_id
        self._ssl = ssl.create_default_context()

    # -- transport --------------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None, *, write: bool = False) -> object:
        if not self.token:
            raise NotConfigured(
                "Mist API token is not set.",
                detail="Export MIST_TOKEN (an org admin token) in the service environment. "
                "Without it the front end is read-only and every Mist action is disabled.",
            )
        if write and not self.settings.mist_writes_enabled:
            if not self.settings.profile_path:
                raise GuardrailViolation("Mist writes are off: no lab profile is loaded.", detail=NO_PROFILE)
            raise GuardrailViolation(
                "Mist writes are disabled.",
                detail="Set SIMRACK_MIST_WRITES=1 to let the front end push fabric changes. "
                "Take a snapshot first: the revert button needs one.",
            )
        url = f"{self.base}{path}"
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Token {self.token}")
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

    def configured(self) -> bool:
        return bool(self.token)

    def writes_enabled(self) -> bool:
        return bool(self.token) and self.settings.mist_writes_enabled

    # -- org / site -------------------------------------------------------------

    def orgs(self) -> list[dict]:
        return self._request("GET", "/orgs") or []

    def sites(self, org_id: str | None = None) -> list[dict]:
        return self._request("GET", f"/orgs/{org_id or self.org_id}/sites") or []

    def create_site(self, name: str, org_id: str | None = None) -> dict:
        return self._request("POST", f"/orgs/{org_id or self.org_id}/sites", {"name": name}, write=True) or {}

    def delete_site(self, site_id: str) -> None:
        self._request("DELETE", f"/sites/{site_id}", write=True)

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

    # -- organisations ----------------------------------------------------------

    def organizations(self) -> list[dict]:
        return self._request("GET", "/orgs") or []
