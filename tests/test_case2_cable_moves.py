"""Case 2: move cables around and have the changes applied to Proxmox.

Asserts a plug creates an MTU 9216 bridge and rewrites the two NICs, and that
moving one end rewrites the NIC again and parks the port it left.
"""

from __future__ import annotations

import unittest

from simrack.errors import GuardrailViolation, NotFound
from tests.fakes import TempDir, make_manager


class TestCableMovesReachProxmox(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = None
        from tests.fakes import FakeProxmox

        self.px = FakeProxmox(templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("cabletest", "collapsed-core", template_vmid=320)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_plugging_a_cable_creates_a_9216_bridge_and_moves_both_nics(self):
        self.px.calls.clear()
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")

        self.assertEqual(link.mtu, 9216, "fabric bridges must be MTU 9216 or the overlay flaps")
        self.assertTrue(link.bridge.startswith("sbx"))
        self.assertLessEqual(len(link.bridge), 15, "Linux interface names cap at 15 characters")

        created = self.px.called("create_bridge")
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0][2]["mtu"], 9216)

        # ge-0/0/3 on the access switch is net4 (net0 is management).
        acc_config = [c for c in self.px.config_calls_for(self.sandbox.node("sbx-acc-01").vmid) if "net4" in c[2]][0]
        self.assertIn(f"bridge={link.bridge}", acc_config[2]["net4"])

        core_vmid = self.sandbox.node("sbx-core-01").vmid
        core_config = [c for c in self.px.config_calls_for(core_vmid) if "net4" in c[2]][0]
        self.assertIn(f"bridge={link.bridge}", core_config[2]["net4"])

    def test_moving_one_end_rewires_proxmox_and_frees_the_old_port(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        self.px.calls.clear()

        # Naming the core pulls its plug out of ge-0/0/3 and puts it in ge-0/0/1.
        result = self.manager.move_cable(self.sandbox, link.bridge, "sbx-core-01", "ge-0/0/1")

        self.assertEqual(result["link"]["b_port"], "ge-0/0/1")
        self.assertEqual(result["moved_from"], "sbx-core-01 ge-0/0/3")
        core_vmid = self.sandbox.node("sbx-core-01").vmid
        fields = {}
        for call in self.px.config_calls_for(core_vmid):
            fields.update(call[2])
        self.assertIn(f"bridge={link.bridge}", fields["net2"], "ge-0/0/1 is net2")
        self.assertIn("bridge=sbxpark", fields["net4"], "the old port ge-0/0/3 must be parked")
        self.assertIn("link_down=1", fields["net4"])

    def test_unplugging_deletes_the_bridge_and_parks_both_nics(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        acc_vmid = self.sandbox.node("sbx-acc-01").vmid
        self.px.calls.clear()
        self.manager.remove_cable(self.sandbox, link.bridge)

        self.assertEqual(self.px.called("delete_bridge")[0][1][0], link.bridge)
        fields = {}
        for call in self.px.config_calls_for(acc_vmid):
            fields.update(call[2])
        self.assertIn("bridge=sbxpark", fields["net4"], "the unplugged port must be parked")
        self.assertIn("link_down=1", fields["net4"])

    def test_cabling_a_live_lab_port_or_bridge_is_refused(self):
        with self.assertRaises(GuardrailViolation):
            self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "border-01", "ge-0/0/0")
        with self.assertRaises(GuardrailViolation):
            self.manager.move_cable(self.sandbox, "lab1", "sbx-acc-01", "ge-0/0/2")
        with self.assertRaises(NotFound):
            self.manager.move_cable(self.sandbox, "sbx999_999_11", "sbx-acc-01", "ge-0/0/2")

    def test_moving_to_an_occupied_port_or_a_stranger_node_is_refused(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        recipe = next(c for c in self.sandbox.links if ("sbx-acc-01", "ge-0/0/2") in ((c.a_node, c.a_port), (c.b_node, c.b_port)))
        far = recipe.b_node if recipe.a_node == "sbx-acc-01" else recipe.a_node
        with self.assertRaises(GuardrailViolation) as caught:
            # ge-0/0/2 on the access switch already carries the recipe cable.
            self.manager.move_cable(self.sandbox, link.bridge, "sbx-acc-01", "ge-0/0/2", from_node="sbx-core-01")
        self.assertIn(f"sbx-acc-01 ge-0/0/2 is already cabled to {far}.", str(caught.exception))
        self.manager.provision_node(self.sandbox, "sbx-acc-09", template_vmid=320)
        with self.assertRaises(GuardrailViolation):
            self.manager.move_cable(self.sandbox, link.bridge, "sbx-acc-09", "ge-0/0/2")
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.move_cable(self.sandbox, link.bridge, "sbx-core-01", "ge-0/0/3")
        self.assertIn("already is", str(caught.exception))

    def test_moving_an_end_to_another_switch_rewires_that_switch(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        self.manager.provision_node(self.sandbox, "sbx-acc-09", template_vmid=320)
        core_vmid = self.sandbox.node("sbx-core-01").vmid
        new_vmid = self.sandbox.node("sbx-acc-09").vmid
        self.px.calls.clear()

        # Same port number on a different switch is a real move, not "already there".
        result = self.manager.move_cable(self.sandbox, link.bridge, "sbx-acc-09", "ge-0/0/3", from_node="sbx-core-01")

        self.assertEqual((result["link"]["b_node"], result["link"]["b_port"]), ("sbx-acc-09", "ge-0/0/3"))
        new_fields, old_fields = {}, {}
        for call in self.px.config_calls_for(new_vmid):
            new_fields.update(call[2])
        for call in self.px.config_calls_for(core_vmid):
            old_fields.update(call[2])
        self.assertIn(f"bridge={link.bridge}", new_fields["net4"], "the new switch must get the plug")
        self.assertEqual(list(old_fields), ["net4"], "the old switch only lets go of its port")
        self.assertIn("bridge=sbxpark", old_fields["net4"])
        self.assertIn(link.bridge, self.px.get_vm(new_vmid)["net4"])
        self.assertIn("bridge=sbxpark", self.px.get_vm(core_vmid)["net4"])

    def test_moving_an_end_onto_the_other_end_is_refused(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.move_cable(self.sandbox, link.bridge, "sbx-acc-01", "ge-0/0/4", from_node="sbx-core-01")
        self.assertIn("itself", str(caught.exception))

    def test_an_occupied_port_is_refused_naming_the_switch_at_the_far_end(self):
        self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        for node, far in (("sbx-acc-01", "sbx-core-01"), ("sbx-core-01", "sbx-acc-01")):
            with self.subTest(end=node):
                with self.assertRaises(GuardrailViolation) as caught:
                    self.manager.cable(self.sandbox, node, "ge-0/0/3", far, "ge-0/0/4")
                self.assertIn(f"{node} ge-0/0/3 is already cabled to {far}.", str(caught.exception))

    def test_a_port_with_a_trailing_newline_is_refused_before_proxmox_is_touched(self):
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")
        self.px.calls.clear()
        with self.subTest("cable"), self.assertRaises(ValueError):
            # "ge-0/0/3\n" is not "ge-0/0/3", so it would pass as free and take over net4.
            self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3\n", "sbx-core-01", "ge-0/0/4")
        with self.subTest("move"), self.assertRaises(ValueError):
            self.manager.move_cable(self.sandbox, link.bridge, "sbx-core-01", "ge-0/0/1\n")
        self.assertEqual(self.px.calls, [])


if __name__ == "__main__":
    unittest.main()
