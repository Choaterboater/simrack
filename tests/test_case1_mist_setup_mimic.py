"""Case 1: build a front end that mimics the Mist setup.

Asserts the front end exists as an HTTP surface, and that a sandbox built from
a recipe produces a Mist site whose networks, VRF and fabric topology mirror a
live lab on subnets the lab profile does not protect.
"""

from __future__ import annotations

import ipaddress
import unittest

from labfront.api import build_router
from tests.fakes import TempDir, make_manager


class TestMistSetupMimic(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.manager = make_manager(self.tmp)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_front_end_exposes_the_endpoints_needed_to_mimic_mist(self):
        router = build_router(self.manager)
        surface = set(router.patterns)
        for expected in (
            ("GET", "/api/state"),
            ("POST", "/api/sandboxes"),
            ("POST", "/api/sandboxes/{name}/mist/site"),
            ("POST", "/api/sandboxes/{name}/mist/fabric"),
        ):
            self.assertIn(expected, surface, f"the front end is missing {expected[0]} {expected[1]}")

    def test_recipe_mimics_the_live_lab_without_reusing_live_subnets(self):
        sandbox = self.manager.create_sandbox("demo1", "ip-clos", template_vmid=320)
        self.manager.mist_create_site(sandbox)
        for node in sandbox.nodes:
            self.manager.mist.add_switch(sandbox.mist_site_id, node.name)
        result = self.manager.mist_build_fabric(sandbox)

        setting = self.manager.mist.site_setting(sandbox.mist_site_id)
        # Same shape as the live lab: data and voice in VRF LAB. Management stays out of band on fxp0.
        self.assertNotIn("management", setting["networks"])
        self.assertEqual(setting["networks"]["data"]["vlan_id"], 10)
        self.assertIn("voice", setting["networks"])
        self.assertEqual(setting["vrf_instances"]["LAB"]["networks"], ["data", "voice"], "the live lab uses a VRF called LAB")

        topology = self.manager.mist.evpn_topologies(sandbox.mist_site_id)
        self.assertEqual(len(topology), 1)
        self.assertEqual(topology[0]["evpn_options"]["overlay"]["as"], 65200)
        self.assertEqual(len(topology[0]["switches"]), 4)
        self.assertEqual(result["form"], "detailed")

        # Nothing may overlap the live lab.
        options = topology[0]["evpn_options"]
        subnets = [n["subnet"] for n in setting["networks"].values()]
        subnets += [options["underlay"]["subnet"], options["auto_router_id_subnet"], options["auto_loopback_subnet"]]
        for subnet in subnets:
            for live in self.manager.settings.production_subnets:
                self.assertFalse(
                    ipaddress.ip_network(subnet).overlaps(ipaddress.ip_network(live)),
                    f"sandbox subnet {subnet} collides with the live lab {live}",
                )

    def test_state_reports_lab_limits_and_mist_mode(self):
        state = self.manager.state()
        self.assertEqual(state["limits"]["vmid_range"], [320, 399])
        self.assertTrue(state["mist"]["configured"])
        self.assertTrue(state["mist"]["writes_enabled"])
        self.assertIn(200, state["production"]["vmids"], "the live switches stay listed as protected")
        self.assertEqual(state["host"]["free_ram_mb"], 40000)

    def test_mist_writes_are_refused_when_disabled(self):
        sandbox = self.manager.create_sandbox("demo2", "single-switch", template_vmid=320)
        self.manager.mist_create_site(sandbox)
        self.manager.mist.settings.mist_writes_enabled = False
        with self.assertRaises(Exception) as caught:
            self.manager.mist_build_fabric(sandbox)
        self.assertIn("disabled", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
