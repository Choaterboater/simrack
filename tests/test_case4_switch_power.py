"""Case 4: optionally bring extra switches up or down.

Asserts switches can be added beyond the recipe and powered from the front end
without hand-editing Proxmox, that the recorded state follows the real power
state, and that free memory, not a fixed switch count, sets the limit.
"""

from __future__ import annotations

import unittest

from labfront.errors import GuardrailViolation
from tests.fakes import FakeProxmox, TempDir, make_manager


class TestSwitchPower(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=40000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("powertest", "collapsed-core", template_vmid=320)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_an_extra_switch_beyond_the_recipe_can_be_added_and_started(self):
        self.px.calls.clear()
        node = self.manager.provision_node(self.sandbox, "sbx-acc-03", role="access", template_vmid=320, start=True)
        self.assertTrue(node.running)
        self.assertEqual(self.px.vms[node.vmid]["status"], "running")
        self.assertEqual(len([n for n in self.sandbox.nodes if n.kind == "switch"]), 3)

    def test_power_actions_go_to_proxmox_and_the_saved_state_follows(self):
        node = self.manager.provision_node(self.sandbox, "sbx-acc-03", template_vmid=320, start=True)

        up = self.manager.set_power(self.sandbox, "sbx-acc-03", "start")
        self.assertEqual(up["status"], "running")
        self.assertTrue(self.manager.get("powertest").node("sbx-acc-03").running)

        down = self.manager.set_power(self.sandbox, "sbx-acc-03", "shutdown")
        self.assertEqual(down["status"], "stopped")
        self.assertFalse(self.manager.get("powertest").node("sbx-acc-03").running)
        self.assertIn(("set_power", (node.vmid, "shutdown"), {"timeout": 120}), self.px.calls)

    def test_state_survives_a_restart_of_the_front_end(self):
        self.manager.set_power(self.sandbox, "sbx-acc-01", "shutdown")
        reloaded = make_manager(self.tmp, proxmox=self.px)
        self.assertFalse(reloaded.get("powertest").node("sbx-acc-01").running)

    def test_memory_not_a_fixed_count_limits_the_switches(self):
        for index in range(3, 8):
            self.manager.provision_node(self.sandbox, f"sbx-acc-0{index}", template_vmid=320)
        self.assertEqual(len([n for n in self.sandbox.nodes if n.kind == "switch"]), 7)

    def test_building_past_the_ram_reserve_is_refused(self):
        self.px.free_mb = 8000
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.provision_node(self.sandbox, "sbx-acc-09", template_vmid=320)
        self.assertIn("free memory", str(caught.exception))

    def test_power_actions_on_live_lab_guests_are_refused(self):
        live = self.manager.sandboxes["powertest"]
        live.nodes[0].vmid = 200  # a live switch smuggled into the sandbox record
        with self.assertRaises(GuardrailViolation):
            self.manager.set_power(live, live.nodes[0].name, "shutdown")


if __name__ == "__main__":
    unittest.main()
