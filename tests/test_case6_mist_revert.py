"""Case 6: a revert button for Mist.

Asserts a Mist snapshot captures the site setting, the topology and every
device, and that revert puts all three back byte-for-byte after a customer has
messed with them.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from labfront.errors import GuardrailViolation, NotFound
from tests.fakes import FakeMist, FakeProxmox, TempDir, make_manager


class TestMistRevert(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=40000, templates=[320])
        self.mist = FakeMist()
        self.manager = make_manager(self.tmp, proxmox=self.px, mist=self.mist)
        self.sandbox = self.manager.create_sandbox("reverttest", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.site = self.sandbox.mist_site_id
        self.mist.add_switch(self.site, "sbx-core-01")
        self.device = self.mist.add_switch(self.site, "sbx-acc-01", config={"port_config": {"ge-0/0/2": {"profile": "access-v10"}}})
        self.manager.mist_build_fabric(self.sandbox)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_snapshot_captures_site_topology_and_every_device(self):
        result = self.manager.mist_snapshot(self.sandbox, "good")
        self.assertEqual(result["devices"], 2)
        self.assertTrue(os.path.exists(result["path"]))
        with open(result["path"], encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["site_id"], self.site)
        self.assertIn("networks", data["site_setting"])
        self.assertEqual(len(data["evpn_topologies"]), 1)
        self.assertIn(self.device, data["devices"])
        self.assertIn("cli", data["device_cli"]["sbx-acc-01"])

    def test_revert_restores_the_site_setting_the_topology_and_the_device(self):
        result = self.manager.mist_snapshot(self.sandbox, "good")
        self.manager.mist_build_fabric(self.sandbox)
        good_setting = copy.deepcopy(self.mist.site_setting(self.site))
        good_device = copy.deepcopy(self.mist.device(self.site, self.device))

        # A customer breaks it through the API.
        self.mist.put_site_setting(self.site, {"networks": {"data": {"vlan": 999, "subnet": "10.66.66.0/24"}}})
        self.mist.put_device(self.site, self.device, {"port_config": {"ge-0/0/2": {"profile": "virtualuplink"}}, "discarded": True})
        self.mist.put_evpn_topology(self.site, {"name": "rogue", "type": "collapsed-core"})
        self.assertNotEqual(self.mist.site_setting(self.site), good_setting)

        result = self.manager.mist_revert(self.sandbox, "good")
        self.assertEqual(result["devices_restored"], 2)
        self.assertEqual(self.mist.site_setting(self.site), good_setting, "site networks and VRF must come back")
        self.assertEqual(self.mist.device(self.site, self.device), good_device, "device config must come back")
        restored = {t["name"] for t in self.mist.evpn_topologies(self.site)}
        self.assertEqual(restored, {"reverttest"}, "the snapshot's topology comes back and the rogue one goes")
        self.assertEqual(result["topologies_removed"], ["rogue"])

    def test_revert_uses_a_real_mist_put_not_a_local_undo(self):
        self.manager.mist_build_fabric(self.sandbox)
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.calls.clear()
        self.manager.mist_revert(self.sandbox, "good")
        names = [c[0] for c in self.mist.calls]
        self.assertIn("put_site_setting", names)
        self.assertIn("put_evpn_topology", names)
        self.assertIn("put_device", names)

    def test_reverting_an_unknown_label_is_refused(self):
        with self.assertRaises(NotFound) as caught:
            self.manager.mist_revert(self.sandbox, "never-taken")
        self.assertIn("No Mist snapshot", str(caught.exception))

    def test_proxmox_revert_restores_every_guest_in_the_sandbox(self):
        self.manager.snapshot(self.sandbox, "good")
        before = {n.name: dict(self.px.vms[n.vmid]) for n in self.sandbox.nodes}
        self.manager.set_power(self.sandbox, "sbx-acc-01", "shutdown")
        self.px.set_vm_config(self.sandbox.node("sbx-acc-01").vmid, memory=1024)

        result = self.manager.revert(self.sandbox, "good")
        self.assertEqual(set(result["nodes"]), set(before))
        self.assertEqual(self.px.vms[self.sandbox.node("sbx-acc-01").vmid]["memory"], 5120)
        self.assertEqual(self.px.vms[self.sandbox.node("sbx-acc-01").vmid]["status"], "running")

    def test_revert_refuses_to_target_the_live_site(self):
        self.manager.mist_snapshot(self.sandbox, "good")
        record = self.sandbox.mist_snapshots["good"]
        record["path"] = os.path.join(self.tmp, "evil.json")
        with open(record["path"], "w", encoding="utf-8") as handle:
            json.dump({"site_id": "00000000-0000-0000-0000-000000000002", "site_setting": {}, "evpn_topologies": [], "devices": {}}, handle)
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_revert(self.sandbox, "good")
        self.assertIn("Mist site 00000000-0000-0000-0000-000000000002 is live", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
