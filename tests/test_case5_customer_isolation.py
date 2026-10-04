"""Case 5: let customers play with the lab without changes in Proxmox.

The whole point is that everything a customer does goes through the front end
and stays inside a sandbox. This asserts that across a full session - build,
cable, power, revert, tear down - no production vmid, bridge or Mist site is
ever written, and that the write switch really does stop changes.
"""

from __future__ import annotations

import unittest

from simrack.errors import GuardrailViolation
from tests.fakes import FakeMist, FakeProxmox, TempDir, make_manager, proxmox_may_only_look


class TestCustomerIsolation(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=40000, templates=[320])
        self.mist = FakeMist()
        self.manager = make_manager(self.tmp, proxmox=self.px, mist=self.mist)
        self.live_vmids = self.manager.settings.production_vmids
        self.live_bridges = self.manager.settings.production_bridges
        # Seed the fake host with the live lab so we can prove it survives intact.
        for vmid in sorted(self.live_vmids):
            self.px.vms[vmid] = {"vmid": vmid, "name": f"live-{vmid}", "status": "running", "net0": "virtio,bridge=vmbr0,tag=5"}
        for bridge in sorted(self.live_bridges):
            self.px.networks[bridge] = {"iface": bridge, "type": "bridge", "mtu": 9216 if bridge.startswith("lab") else 1500}
        self.live_before = ({v: dict(c) for v, c in self.px.vms.items() if v in self.live_vmids},
                            {b: dict(c) for b, c in self.px.networks.items() if b in self.live_bridges})

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _assert_live_lab_untouched(self):
        vms = {v: dict(c) for v, c in self.px.vms.items() if v in self.live_vmids}
        bridges = {b: dict(c) for b, c in self.px.networks.items() if b in self.live_bridges}
        self.assertEqual(vms, self.live_before[0], "a live lab guest config changed")
        self.assertEqual(bridges, self.live_before[1], "a live lab bridge changed")

    def test_a_full_customer_session_never_touches_proxmox_outside_the_sandbox(self):
        sandbox = self.manager.create_sandbox("customer-a", "collapsed-core", template_vmid=320, with_mist_site=True)
        for node in sandbox.nodes:
            self.mist.add_switch(sandbox.mist_site_id, node.name)
        self.manager.mist_build_fabric(sandbox)
        self.manager.mist_snapshot(sandbox, "start")
        self.manager.cable(sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        self.manager.provision_node(sandbox, "sbx-acc-03", template_vmid=320)
        self.manager.set_power(sandbox, "sbx-acc-03", "shutdown")
        self.manager.snapshot(sandbox, "checkpoint")
        self.manager.mist_revert(sandbox, "start")
        self.manager.teardown(sandbox)

        self._assert_live_lab_untouched()
        for _, args, _ in self.px.calls:
            for value in args:
                if isinstance(value, int):
                    self.assertTrue(320 <= value <= 399, f"the front end wrote outside the sandbox range: {value}")
        for call in self.px.called("create_bridge") + self.px.called("delete_bridge"):
            self.assertTrue(call[1][0].startswith("sbx"))

    def test_two_customers_get_separate_sites_and_separate_guests(self):
        one = self.manager.create_sandbox("customer-a", "single-switch", template_vmid=320, with_mist_site=True)
        two = self.manager.create_sandbox("customer-b", "single-switch", template_vmid=320, with_mist_site=True)
        self.assertNotEqual(one.mist_site_id, two.mist_site_id)
        self.assertFalse(set(n.vmid for n in one.nodes) & set(n.vmid for n in two.nodes))
        self.mist.add_switch(one.mist_site_id, "sbx-acc-01")
        self.manager.mist_build_fabric(one)
        self.assertEqual(self.mist.site_setting(two.mist_site_id), {}, "one customer's recipe must not land in the other's site")

    def test_a_live_mist_site_is_refused(self):
        sandbox = self.manager.create_sandbox("customer-c", "single-switch", template_vmid=320)
        live_site = sorted(self.manager.settings.production_mist_sites)[0]
        sandbox.mist_site_id = live_site
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_build_fabric(sandbox)
        self.assertIn(f"Mist site {live_site} is live", str(caught.exception))

    def test_read_only_mode_stops_every_change(self):
        sandbox = self.manager.create_sandbox("customer-d", "single-switch", template_vmid=320)
        for action in (
            lambda: self.manager.create_sandbox("customer-e", "single-switch", template_vmid=320),
            lambda: self.manager.cable(sandbox, "sbx-acc-01", "ge-0/0/2", "sbx-acc-01", "ge-0/0/3"),
            lambda: self.manager.set_power(sandbox, "sbx-acc-01", "shutdown"),
            lambda: self.manager.snapshot(sandbox, "x"),
            lambda: self.manager.teardown(sandbox),
        ):
            with proxmox_may_only_look(self.manager), self.assertRaises(GuardrailViolation) as caught:
                action()
            self.assertIn("read-only", str(caught.exception))
        self._assert_live_lab_untouched()

    def test_deleting_a_node_removes_only_its_own_cables(self):
        sandbox = self.manager.create_sandbox("customer-f", "ip-clos", template_vmid=320)
        self.manager.cable(sandbox, "sbx-acc-01", "ge-0/0/0", "sbx-core-01", "ge-0/0/0")
        self.manager.delete_node(sandbox, "sbx-acc-02")
        names = [n.name for n in sandbox.nodes]
        self.assertNotIn("sbx-acc-02", names)
        self.assertIn("sbx-acc-01", names)
        for link in sandbox.links:
            self.assertNotIn("sbx-acc-02", (link.a_node, link.b_node))
        self.assertIn("sbx-acc-01", sandbox.links[0].endpoints()[0] + sandbox.links[0].endpoints()[1], "the other cable survives")
        self._assert_live_lab_untouched()


if __name__ == "__main__":
    unittest.main()
