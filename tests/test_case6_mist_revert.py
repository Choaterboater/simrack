"""Case 6: a revert button for Mist.

Asserts a Mist snapshot captures the site setting, the topology and every
device, and that revert puts all three back after a customer has messed with
them. Mist's PUT keeps any top-level field it is not sent (seen on a real
switch, Oct 2026), so a field made since the snapshot goes back empty.
"""

from __future__ import annotations

import copy
import json
import os
import unittest

from simrack.errors import GuardrailViolation, NotFound
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
        self.mist.put_device(self.site, self.device, {
            "port_config": {"ge-0/0/2": {"profile": "virtualuplink"}},
            "additional_config_cmds": ["set system host-name rogue"],
        })
        self.mist.put_evpn_topology(self.site, {"name": "rogue", "type": "collapsed-core"})
        self.assertNotEqual(self.mist.site_setting(self.site), good_setting)

        result = self.manager.mist_revert(self.sandbox, "good")
        self.assertEqual(result["devices_restored"], 2)
        self.assertEqual(self.mist.site_setting(self.site), good_setting, "site networks and VRF must come back")
        device = self.mist.device(self.site, self.device)
        self.assertEqual(device.pop("additional_config_cmds"), [], "a field the snapshot lacked goes back empty")
        self.assertEqual(device, good_device, "device config must come back")
        restored = {t["name"] for t in self.mist.evpn_topologies(self.site)}
        self.assertEqual(restored, {"reverttest"}, "the snapshot's topology comes back and the rogue one goes")
        self.assertEqual(result["topologies_removed"], ["rogue"])

    def test_a_setting_made_since_the_snapshot_goes_back_empty(self):
        """The crawl on real gear: Mist kept every field the snapshot lacked, since its PUT leaves out none it is not sent."""
        self.manager.mist_snapshot(self.sandbox, "good")
        good_device = copy.deepcopy(self.mist.device(self.site, self.device))
        good_setting = copy.deepcopy(self.mist.site_setting(self.site))
        self.mist.put_device(self.site, self.device, {
            "additional_config_cmds": ["set system host-name rogue"],
            "networks": {"rogue": {"vlan_id": 999}},
            "notes": "rogue",
            "vars": {},
        })
        self.mist.put_site_setting(self.site, {"vars": {"rogue": "1"}})

        result = self.manager.mist_revert(self.sandbox, "good")
        device = self.mist.device(self.site, self.device)
        self.assertEqual(device.pop("additional_config_cmds"), [])
        self.assertEqual(device.pop("networks"), {})
        self.assertEqual(device.pop("notes"), "")
        self.assertEqual(device.pop("vars"), {}, "one already empty, as the Mist page leaves them, is left alone")
        self.assertEqual(device, good_device)
        setting = self.mist.site_setting(self.site)
        self.assertEqual(setting.pop("vars"), {})
        self.assertEqual(setting, good_setting)
        self.assertEqual(result["fields_cleared"], {"site": ["vars"], "sbx-acc-01": ["additional_config_cmds", "networks", "notes"]})
        self.assertEqual(result["fields_kept"], {})
        self.assertIn("emptied 4 settings made since", self.sandbox.notes[-1])

    def test_a_yes_no_setting_made_since_stays_and_is_named(self):
        """No yes/no is empty: a guess could switch something on, so it stays and the notes say so."""
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.put_device(self.site, self.device, {"use_router_id_as_source_ip": True})
        result = self.manager.mist_revert(self.sandbox, "good")
        self.assertIs(self.mist.device(self.site, self.device)["use_router_id_as_source_ip"], True)
        self.assertEqual(result["fields_kept"], {"sbx-acc-01": ["use_router_id_as_source_ip"]})
        self.assertIn("sbx-acc-01 use_router_id_as_source_ip", self.sandbox.notes[-1])

    def test_a_field_mist_will_not_take_empty_stays_and_the_rest_still_go(self):
        """An ID or an address may not be "": revert sends again without the words and names what stayed."""
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.put_device(self.site, self.device, {"router_id": "10.255.0.9", "networks": {"rogue": {"vlan_id": 999}}})
        self.mist.refuse_empty = {"router_id"}
        result = self.manager.mist_revert(self.sandbox, "good")
        device = self.mist.device(self.site, self.device)
        self.assertEqual(device["networks"], {}, "what Mist does take empty still goes")
        self.assertEqual(device["router_id"], "10.255.0.9")
        self.assertEqual(result["fields_cleared"], {"sbx-acc-01": ["networks"]})
        self.assertEqual(result["fields_kept"], {"sbx-acc-01": ["router_id"]})

    def test_undoing_the_fabric_build_keeps_the_root_password(self):
        """The build's own point has no switch_mgmt; emptying it would take the switches' root password out of Mist."""
        label = next(name for name in self.sandbox.mist_snapshots if name.startswith("before-fabric"))
        self.assertIn("root_password", self.mist.site_setting(self.site)["switch_mgmt"], "the build put it there")
        result = self.manager.mist_revert(self.sandbox, label)
        setting = self.mist.site_setting(self.site)
        password = self.manager.reveal_root_password(self.sandbox)["root_password"]
        self.assertEqual(setting["switch_mgmt"], {"root_password": password})
        self.assertEqual(setting["networks"], {}, "the build's networks go")
        self.assertNotIn("switch_mgmt", result["fields_cleared"]["site"], "only the password was there, and it stays")
        self.assertEqual(self.mist.device(self.site, self.device)["vrf_config"], {})
        self.assertIs(self.mist.device(self.site, self.device)["managed"], True, "Mist keeps managing the switch, or the revert never reaches it")
        self.assertNotIn("sbx-acc-01", result["fields_kept"])

    def test_a_switch_mgmt_setting_made_since_goes_and_the_root_password_stays(self):
        label = next(name for name in self.sandbox.mist_snapshots if name.startswith("before-fabric"))
        mgmt = self.mist.site_setting(self.site)["switch_mgmt"]
        self.mist.put_site_setting(self.site, {"switch_mgmt": {**mgmt, "protect_re": {"enabled": True}}})
        result = self.manager.mist_revert(self.sandbox, label)
        password = self.manager.reveal_root_password(self.sandbox)["root_password"]
        self.assertEqual(self.mist.site_setting(self.site)["switch_mgmt"], {"root_password": password})
        self.assertIn("switch_mgmt", result["fields_cleared"]["site"])

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
