"""Case 15: the lab profile says what is live, so the code holds no lab of its own.

One TOML file per host names the Proxmox node, the Mist cloud and org, the
management network and the guests, bridges, sites and subnets SimRack must
never touch. A profile with a mistake stops start-up and names the key, because
a typo in a protected list would otherwise leave something live unprotected.
"""

from __future__ import annotations

import io
import os
import textwrap
import unittest
from contextlib import redirect_stderr
from unittest import mock

from simrack.__main__ import main
from simrack.config import Settings
from simrack.profile import ProfileError
from tests.fakes import TempDir

FULL = """
[proxmox]
node = "pve-lab"
api = "https://192.0.2.5:8006/api2/json"

[mist]
api = "https://api.eu.mist.com/api/v1"
org_id = "00000000-0000-0000-0000-000000000001"

[management]
bridge = "vmbr9"
vlan = 7
cidr = "192.0.2.0/24"
pool = "192.0.2.200-192.0.2.249"

[protected]
vmids = [200, 201]
lxc = [303]
bridges = ["vmbr0", "lab1"]
mist_sites = ["00000000-0000-0000-0000-000000000002"]
subnets = ["10.10.10.0/24"]

[sandbox]
vmids = [500, 599]
lxc = [600, 649]
bridge_prefix = "tst"
park_bridge = "tstpark"
"""

MINIMAL = """
[proxmox]
node = "pve-lab"

[management]
bridge = "vmbr0"
cidr = "192.0.2.0/24"
pool = "192.0.2.200-192.0.2.249"

[protected]
"""


class TestLabProfile(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def write(self, text: str) -> str:
        path = os.path.join(self.tmp, "lab-profile.toml")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(textwrap.dedent(text))
        return path

    def test_a_profile_sets_the_lab_identity(self):
        path = self.write(FULL)
        settings = Settings.from_env({"SIMRACK_PROFILE": path})

        self.assertEqual(settings.profile_path, path)
        self.assertEqual(settings.pve_node, "pve-lab")
        self.assertEqual(settings.pve_api_base, "https://192.0.2.5:8006/api2/json")
        self.assertEqual(settings.mist_api_base, "https://api.eu.mist.com/api/v1")
        self.assertEqual(settings.org_id, "00000000-0000-0000-0000-000000000001")
        self.assertEqual((settings.mgmt_bridge, settings.mgmt_vlan), ("vmbr9", 7))
        self.assertEqual((settings.mgmt_cidr, settings.mgmt_pool), ("192.0.2.0/24", "192.0.2.200-192.0.2.249"))
        self.assertEqual(settings.production_vmids, frozenset({200, 201}))
        self.assertEqual(settings.production_lxc, frozenset({303}))
        self.assertEqual(settings.production_bridges, frozenset({"vmbr0", "lab1"}))
        self.assertEqual(settings.production_mist_sites, frozenset({"00000000-0000-0000-0000-000000000002"}))
        self.assertEqual(settings.production_subnets, ("10.10.10.0/24",))
        self.assertEqual((settings.sandbox_vmid_start, settings.sandbox_vmid_end), (500, 599))
        self.assertEqual((settings.sandbox_lxc_start, settings.sandbox_lxc_end), (600, 649))
        self.assertEqual((settings.sandbox_bridge_prefix, settings.park_bridge), ("tst", "tstpark"))

    def test_optional_keys_fall_back_to_defaults(self):
        settings = Settings.from_env({"SIMRACK_PROFILE": self.write(MINIMAL)})

        self.assertEqual(settings.pve_api_base, "https://127.0.0.1:8006/api2/json")
        self.assertEqual(settings.mist_api_base, "https://api.mist.com/api/v1")
        self.assertEqual(settings.org_id, "")
        self.assertIsNone(settings.mgmt_vlan, "no vlan means the management port is untagged")
        self.assertEqual(settings.production_vmids, frozenset())
        self.assertEqual(settings.production_bridges, frozenset())
        self.assertEqual((settings.sandbox_vmid_start, settings.sandbox_vmid_end), (320, 399))
        self.assertEqual((settings.sandbox_bridge_prefix, settings.park_bridge), ("sbx", "sbxpark"))

    def test_tokens_and_switches_still_come_from_the_environment(self):
        settings = Settings.from_env(
            {"SIMRACK_PROFILE": self.write(MINIMAL), "MIST_TOKEN": "t", "SIMRACK_PVE_TOKEN": "p", "SIMRACK_ALLOW_WRITES": "1"}
        )
        self.assertEqual((settings.mist_token, settings.pve_token, settings.allow_writes), ("t", "p", True))

    def test_a_mistake_names_the_file_and_the_key(self):
        cases = {
            "unknown key, a typo that would unprotect": (MINIMAL.replace("[protected]", "[protected]\nvmid = [200]"), "protected.vmid", "vmids"),
            "unknown section": (MINIMAL + "\n[sandbx]\n", "sandbx", "sandbox"),
            "required key missing": (MINIMAL.replace('node = "pve-lab"', ""), "proxmox.node", "required"),
            "protected section missing": (MINIMAL.replace("[protected]", ""), "protected", "required"),
            "wrong type": (MINIMAL.replace("[protected]", '[protected]\nvmids = "200"'), "protected.vmids", "whole numbers"),
            "a bool is not a number": (MINIMAL.replace("[protected]", "[protected]\nvmids = [true]"), "protected.vmids", "whole numbers"),
            "pool outside the management subnet": (MINIMAL.replace("192.0.2.200-192.0.2.249", "198.51.100.200-198.51.100.249"), "management.pool", "management.cidr"),
            "pool backwards": (MINIMAL.replace("192.0.2.200-192.0.2.249", "192.0.2.249-192.0.2.200"), "management.pool", "first"),
            "not a subnet": (MINIMAL.replace("[protected]", '[protected]\nsubnets = ["10.10.10.300/24"]'), "protected.subnets", "subnet"),
            "range backwards": (MINIMAL + "\n[sandbox]\nvmids = [399, 320]\n", "sandbox.vmids", "first"),
            "not TOML": ("[proxmox\nnode = 1", "TOML", "TOML"),
            "a hookscript, which an API token cannot set": (
                MINIMAL.replace('node = "pve-lab"', 'node = "pve-lab"\nhookscript = "local:snippets/simrack-sbx.sh"'),
                "proxmox.hookscript",
                "template",
            ),
        }
        for name, (text, key, hint) in cases.items():
            with self.subTest(name):
                path = self.write(text)
                with self.assertRaises(ProfileError) as caught:
                    Settings.from_env({"SIMRACK_PROFILE": path})
                message = f"{caught.exception.message} {caught.exception.detail}"
                self.assertIn(path, message)
                self.assertIn(key, message)
                self.assertIn(hint, message)

    def test_a_missing_file_is_named(self):
        path = os.path.join(self.tmp, "nope.toml")
        with self.assertRaises(ProfileError) as caught:
            Settings.from_env({"SIMRACK_PROFILE": path})
        self.assertIn(path, caught.exception.message)

    def test_a_bad_profile_stops_start_up(self):
        path = self.write(MINIMAL.replace("[protected]", "[protected]\nvmid = [200]"))
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"SIMRACK_PROFILE": path}), redirect_stderr(stderr):
            code = main(["state"])
        self.assertEqual(code, 2)
        self.assertIn("protected.vmid", stderr.getvalue())

    def test_the_shipped_example_profile_loads(self):
        example = os.path.join(os.path.dirname(__file__), os.pardir, "lab-profile.example.toml")
        settings = Settings.from_env({"SIMRACK_PROFILE": example})
        self.assertTrue(settings.production_bridges, "the example shows what a protected bridge looks like")


if __name__ == "__main__":
    unittest.main()
