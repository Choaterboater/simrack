"""Seam 4: sandbox switches get their management addresses from the lab profile.

The profile's [management] pool is the only source of fxp0 addresses, a full
pool is refused before anything is built, and a management network with no vlan
leaves fxp0 untagged.
"""

from __future__ import annotations

import unittest

from simrack.errors import GuardrailViolation
from simrack.service import SandboxManager
from tests.fakes import FakeMist, FakeProxmox, TempDir
from tests.test_case17_profile_guardrails import WRITES, profile_settings

#: Two addresses on an untagged management network.
SMALL_LAB = """
[proxmox]
node = "pve-lab"

[management]
bridge = "mgmt0"
cidr = "198.51.100.0/24"
pool = "198.51.100.10-198.51.100.11"

[protected]
"""


class TestManagementPool(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(templates=[320])
        settings = profile_settings(self.tmp, SMALL_LAB, **WRITES)
        self.manager = SandboxManager(settings, proxmox=self.px, mist=FakeMist())

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_switches_take_addresses_from_the_pool_in_order(self):
        sandbox = self.manager.create_sandbox("pool", "collapsed-core", template_vmid=320)
        self.assertEqual([n.mgmt_ip for n in sandbox.nodes], ["198.51.100.10", "198.51.100.11"])

    def test_a_full_pool_is_refused_before_anything_is_built(self):
        sandbox = self.manager.create_sandbox("pool", "collapsed-core", template_vmid=320)
        guests = set(self.px.vms)
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.provision_node(sandbox, "sbx-acc-02", template_vmid=320)
        self.assertIn("198.51.100.10-198.51.100.11", caught.exception.message)
        self.assertEqual(set(self.px.vms), guests, "no guest is cloned for a switch with no address")

    def test_an_untagged_management_network_leaves_fxp0_untagged(self):
        sandbox = self.manager.create_sandbox("pool", "collapsed-core", template_vmid=320)
        options = self.px.nics(sandbox.nodes[0].vmid)["net0"].split(",")[1:]
        self.assertEqual(options, ["bridge=mgmt0"])


if __name__ == "__main__":
    unittest.main()
