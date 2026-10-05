"""The rules that keep SimRack off the live lab. All refusals raise GuardrailViolation.

What is live comes from the lab profile; see profile.py.
"""

from __future__ import annotations

import ipaddress
import re

from .config import IFNAME_MAX, IMAGE_PATTERNS, Settings
from .errors import GuardrailViolation
from .models import Node, Sandbox


class Guardrails:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    # -- vmid -------------------------------------------------------------------

    def check_vmid(self, vmid: int) -> int:
        vmid = int(vmid)
        if vmid in self.settings.production_vmids:
            raise GuardrailViolation(
                f"vmid {vmid} is part of the live lab.",
                detail="The lab profile lists it under [protected] vmids, so SimRack never touches it. "
                f"Sandboxes use {self.settings.sandbox_vmid_start}-{self.settings.sandbox_vmid_end}.",
            )
        if vmid in self.settings.production_lxc:
            raise GuardrailViolation(
                f"vmid {vmid} is a live lab container.",
                detail="The lab profile lists it under [protected] lxc, so SimRack never touches it.",
            )
        if not self.settings.sandbox_vmid_start <= vmid <= self.settings.sandbox_vmid_end:
            raise GuardrailViolation(
                f"vmid {vmid} is outside the sandbox range "
                f"{self.settings.sandbox_vmid_start}-{self.settings.sandbox_vmid_end}.",
                detail=f"Sandbox guests are {self.settings.sandbox_vmid_start}-{self.settings.sandbox_vmid_end} "
                "so they can never collide with the live lab. The lab profile's [sandbox] vmids sets the range.",
            )
        return vmid

    def check_template(self, template_vmid: int | None) -> int | None:
        """A clone source must not be a live lab guest."""
        if template_vmid is None:
            return None
        template_vmid = int(template_vmid)
        if template_vmid in self.settings.production_vmids or template_vmid in self.settings.production_lxc:
            raise GuardrailViolation(
                f"vmid {template_vmid} is a live lab guest, not a template.",
                detail="Make a template with qm template <vmid> from a vJunos that has never booted "
                "(ADVICE.md, step 2).",
            )
        return template_vmid

    def check_image(self, image: str) -> str:
        """Only an installer ISO or a disk image in an import area. Returns "iso" or "import"."""
        for kind, pattern in IMAGE_PATTERNS.items():
            if re.fullmatch(pattern, image or ""):
                return kind
        raise GuardrailViolation(
            f"{image!r} is not a bootable image the front end may use.",
            detail="Use local:iso/<installer>.iso or local:import/<disk>.qcow2 (also .img, .raw, .vmdk). "
            "Existing guest disks such as vm-204-disk-0 are never attached: that could be a live switch.",
        )

    # -- bridges ----------------------------------------------------------------

    def check_bridge(self, name: str) -> str:
        if name in self.settings.production_bridges:
            raise GuardrailViolation(
                f"bridge {name} carries the live lab.",
                detail="The lab profile lists it under [protected] bridges, so SimRack never modifies it.",
            )
        if not name.startswith(self.settings.sandbox_bridge_prefix):
            raise GuardrailViolation(
                f"bridge {name} is not a sandbox bridge.",
                detail=f"Sandbox bridges must start with {self.settings.sandbox_bridge_prefix!r}.",
            )
        if len(name) > IFNAME_MAX:
            raise GuardrailViolation(
                f"bridge {name} is {len(name)} characters, and Linux allows {IFNAME_MAX}.",
                detail="Use a shorter [sandbox] bridge_prefix, or lower sandbox vmids, on SimRack's setup page.",
            )
        return name

    # -- mist -------------------------------------------------------------------

    def check_site(self, site_id: str | None) -> None:
        if site_id and site_id in self.settings.production_mist_sites:
            raise GuardrailViolation(
                f"Mist site {site_id} is live.",
                detail="The lab profile lists it under [protected] mist_sites. "
                "SimRack writes only to sandbox sites. Build a sandbox site first.",
            )

    # -- subnets ----------------------------------------------------------------

    def check_subnets(self, named: dict[str, str]) -> None:
        """No sandbox subnet may overlap a subnet the lab profile protects."""
        for what, cidr in named.items():
            try:
                net = ipaddress.ip_network(str(cidr), strict=False)
            except ValueError:
                raise GuardrailViolation(f"The sandbox {what} subnet {cidr!r} is not a subnet.") from None
            for live in self.settings.production_subnets:
                if net.overlaps(ipaddress.ip_network(live)):
                    raise GuardrailViolation(
                        f"The sandbox {what} subnet {cidr} overlaps the protected subnet {live}.",
                        detail="The lab profile lists it under [protected] subnets, and SimRack never builds "
                        "on a live subnet. Use a recipe or shape on other subnets, or take the subnet out "
                        "of the profile if it is not live.",
                    )

    # -- resources --------------------------------------------------------------

    def check_ram(self, free_mb: int, switches_to_add: int = 1) -> None:
        per_switch = self.settings.switch_mem_mb
        needed = switches_to_add * per_switch
        if free_mb - needed < self.settings.min_free_ram_mb:
            raise GuardrailViolation(
                f"Not enough free memory: {free_mb} MB free, {needed} MB needed, "
                f"reserve is {self.settings.min_free_ram_mb} MB.",
                detail="Stop something, or use fewer switches. Do not balloon vJunos: it goes unstable.",
            )

    # -- nodes ------------------------------------------------------------------

    def check_node_is_sandbox(self, sandbox: Sandbox, node_name: str) -> Node:
        try:
            node = sandbox.node(node_name)
        except KeyError as error:
            raise GuardrailViolation(
                f"{node_name} is not part of sandbox {sandbox.name}.",
                detail="The front end can only change guests it created.",
            ) from error
        self.check_vmid(node.vmid)
        return node
