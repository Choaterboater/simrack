"""Case 17: the guardrails protect what the lab profile lists, and nothing is hard-coded.

The profile here is deliberately unlike any real lab: other guest numbers, a
protected bridge that even carries the sandbox prefix, two live Mist sites.
"""

from __future__ import annotations

import json
import os
import tempfile
import textwrap
import unittest

from simrack.config import PROFILE_FILE, TOKENS_FILE, Settings
from simrack.errors import GuardrailViolation
from simrack.guardrails import Guardrails
from simrack.service import SandboxManager
from tests.fakes import FakeMist, FakeProxmox, TempDir

SITE_A = "00000000-0000-0000-0000-00000000000a"
SITE_B = "00000000-0000-0000-0000-00000000000b"

LAB = f"""
[proxmox]
node = "pve-lab"

[management]
bridge = "vmbr0"
cidr = "192.0.2.0/24"
pool = "192.0.2.200-192.0.2.249"

[protected]
vmids = [101]
lxc = [150]
bridges = ["vmbr0", "sbxlive"]
mist_sites = ["{SITE_A}", "{SITE_B}"]
subnets = ["10.77.0.0/16"]

[sandbox]
vmids = [100, 299]
"""


def profile_settings(tmp: str, text: str = LAB, *, tokens: bool = False) -> Settings:
    with open(os.path.join(tmp, PROFILE_FILE), "w", encoding="utf-8") as handle:
        handle.write(textwrap.dedent(text))
    if tokens:
        with open(os.path.join(tmp, TOKENS_FILE), "w", encoding="utf-8") as handle:
            json.dump({"proxmox": "p", "mist": "t"}, handle)
    return Settings.load({"SIMRACK_STATE_DIR": tmp})


class TestGuardsFollowTheProfile(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.guard = Guardrails(profile_settings(self._tmp.__enter__()))

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_the_profile_decides_which_guests_are_live(self):
        for vmid in (101, 150):
            with self.subTest(vmid=vmid), self.assertRaises(GuardrailViolation):
                self.guard.check_vmid(vmid)
        with self.assertRaises(GuardrailViolation):
            self.guard.check_template(101)
        self.assertEqual(self.guard.check_vmid(200), 200, "only the profile says what is live")

    def test_a_protected_bridge_is_refused_even_with_the_sandbox_prefix(self):
        with self.assertRaises(GuardrailViolation) as caught:
            self.guard.check_bridge("sbxlive")
        self.assertIn("sbxlive", caught.exception.message)
        self.assertEqual(self.guard.check_bridge("sbxlab"), "sbxlab")

    def test_every_protected_mist_site_is_refused(self):
        for site in (SITE_A, SITE_B):
            with self.subTest(site=site), self.assertRaises(GuardrailViolation) as caught:
                self.guard.check_site(site)
            self.assertIn(site, caught.exception.message)
        self.guard.check_site("00000000-0000-0000-0000-00000000000c")
        self.guard.check_site(None)


BUILD_LAB = """
[proxmox]
node = "pve-lab"

[mist]
org_id = "org-example"

[management]
bridge = "vmbr0"
cidr = "192.0.2.0/24"
pool = "192.0.2.200-192.0.2.249"

[protected]
subnets = ["{subnet}"]
"""

class TestSandboxSubnetsStayOffTheProfile(unittest.TestCase):
    """The ip-clos recipe uses 10.255.224.0/20 underlay, 172.31.0.0/23 router IDs,
    172.31.2.0/24 loopbacks and 10.60.10.0/24 data: each one is tried against a profile."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def ready_to_build(self, protected: str):
        lab = tempfile.mkdtemp(dir=self.tmp)
        settings = profile_settings(lab, BUILD_LAB.format(subnet=protected), tokens=True)
        manager = SandboxManager(settings, proxmox=FakeProxmox(templates=[320]), mist=FakeMist())
        sandbox = manager.create_sandbox("demo", "ip-clos", template_vmid=320)
        manager.mist_create_site(sandbox)
        for node in sandbox.nodes:
            manager.mist.add_switch(sandbox.mist_site_id, node.name)
        return manager, sandbox

    def test_a_fabric_on_a_protected_subnet_is_refused_before_mist_changes(self):
        """A data network is the shape's choice, so it is refused, not moved. The
        fabric's own ranges move, so they are refused only with nowhere left."""
        for protected, what in (("10.60.10.0/24", "data"), ("10.0.0.0/8", "underlay")):
            with self.subTest(protected=protected):
                manager, sandbox = self.ready_to_build(protected)
                writes_before = len(manager.mist.calls)
                with self.assertRaises(GuardrailViolation) as caught:
                    manager.mist_build_fabric(sandbox)
                self.assertIn(what, caught.exception.message)
                self.assertIn(protected, caught.exception.message)
                self.assertEqual(len(manager.mist.calls), writes_before, "Mist is untouched")
                self.assertEqual(sandbox.mist_snapshots, {}, "nothing changed, so nothing to revert")

    def test_fabric_ranges_step_off_a_protected_subnet(self):
        for protected, moved in (
            ("10.255.0.0/16", {"underlay": "10.254.240.0/20"}),
            ("172.31.0.0/16", {"router ID": "172.30.254.0/23", "loopback": "172.30.253.0/24"}),
        ):
            with self.subTest(protected=protected):
                manager, sandbox = self.ready_to_build(protected)
                result = manager.mist_build_fabric(sandbox)
                options = manager.mist.evpn_topologies(sandbox.mist_site_id)[0]["evpn_options"]
                used = {
                    "underlay": options["underlay"]["subnet"],
                    "router ID": options["auto_router_id_subnet"],
                    "loopback": options["auto_loopback_subnet"],
                }
                self.assertEqual({what: used[what] for what in moved}, moved)
                for what, cidr in moved.items():
                    self.assertTrue(any(what in note and protected in note and cidr in note for note in result["notes"]), result["notes"])

    def test_a_fabric_clear_of_the_profile_builds(self):
        manager, sandbox = self.ready_to_build("10.255.240.0/20")
        manager.mist_build_fabric(sandbox)
        options = manager.mist.evpn_topologies(sandbox.mist_site_id)[0]["evpn_options"]
        self.assertEqual(options["underlay"]["subnet"], "10.255.224.0/20")


if __name__ == "__main__":
    unittest.main()
