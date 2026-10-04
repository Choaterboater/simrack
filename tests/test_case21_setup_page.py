"""Case 21: SimRack is set up from its own page, not by editing files.

The setup page saves the lab profile and the tokens in SimRack's state folder.
The environment names only that folder and the token that guards the page, so
nothing about the lab hides in a service file.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import textwrap
import threading
import unittest
import urllib.error
import urllib.request

from simrack.access import required_privileges
from simrack.api import serve
from simrack.config import Settings
from simrack.proxmox import ProxmoxClient
from simrack.service import SandboxManager
from simrack.setup_page import MIST_API
from tests.fakes import FakeMist, FakeProxmox, TempDir, make_manager
from tests.test_case19_real_gear import wire
from tests.test_case20_token_decides import shipped

PROFILE = """
[proxmox]
node = "pve-lab"

[management]
bridge = "vmbr0"
cidr = "192.0.2.0/24"
pool = "192.0.2.200-192.0.2.249"

[protected]
vmids = [200]
"""


#: What the page sends when the user saves it.
SAVED = {
    "proxmox": {"node": "pve-lab"},
    "management": {"bridge": "vmbr0", "cidr": "192.0.2.0/24", "pool": "192.0.2.200-192.0.2.249"},
    "protected": {"vmids": [200], "subnets": ["10.10.10.0/24"]},
    "sandbox": {"vmids": [320, 399], "lxc": [350, 399]},
}


class TestSettingsLiveInTheStateFolder(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def put(self, name: str, text: str) -> str:
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def test_the_profile_and_tokens_come_from_the_state_folder(self):
        profile = self.put("lab-profile.toml", textwrap.dedent(PROFILE))
        self.put("tokens.json", json.dumps({"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"}))

        settings = Settings.load({"SIMRACK_STATE_DIR": self.tmp, "SIMRACK_TOKEN": "abc"})

        self.assertEqual(settings.profile_path, profile)
        self.assertEqual(settings.pve_node, "pve-lab")
        self.assertEqual(settings.production_vmids, frozenset({200}))
        self.assertEqual((settings.pve_token, settings.mist_token), ("simrack@pve!simrack=secret-1", "secret-2"))
        self.assertEqual(settings.extras["token"], "abc", "the page's own guard token still comes from the environment")



class TestTheSetupPage(unittest.TestCase):
    """A fresh SimRack, set up over its own API the way the page does it."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.proxmox = FakeProxmox(token="")
        self.mist = FakeMist(token="")
        self.start()

    def start(self):
        """Start SimRack on the state folder, as the service does after a restart."""
        self.manager = SandboxManager(Settings.load({"SIMRACK_STATE_DIR": self.tmp}), proxmox=self.proxmox, mist=self.mist)
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method: str, path: str, payload=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.httpd.server_address[1]}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_tokens_are_kept_private_and_never_shown_again(self):
        status, reply = self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"})

        self.assertEqual(status, 200, reply)
        _, page = self.call("GET", "/api/setup")
        self.assertEqual(
            page["tokens"],
            {
                "proxmox": {"set": True, "id": "simrack@pve!simrack", "api": "https://127.0.0.1:8006/api2/json"},
                "mist": {"set": True, "api": "https://api.mist.com/api/v1"},
            },
            "with no address given, each token goes to the default one",
        )
        _, state = self.call("GET", "/api/state")
        for shown in (reply, page, state):
            self.assertNotIn("secret-1", json.dumps(shown))
            self.assertNotIn("secret-2", json.dumps(shown))
        saved = os.path.join(self.tmp, "tokens.json")
        self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600, "only SimRack's own user may read them")
        after_a_restart = Settings.load({"SIMRACK_STATE_DIR": self.tmp})
        self.assertEqual((after_a_restart.pve_token, after_a_restart.mist_token), ("simrack@pve!simrack=secret-1", "secret-2"))

    def test_each_token_is_sent_only_to_the_address_saved_with_it(self):
        status, reply = self.call(
            "POST",
            "/api/setup/tokens",
            {"proxmox": "simrack@pve!simrack=secret-1", "proxmox_api": "https://192.0.2.5:8006/api2/json", "mist": "secret-2", "mist_api": "https://api.eu.mist.com/api/v1"},
        )

        self.assertEqual(status, 200, reply)
        tokens = self.call("GET", "/api/setup")[1]["tokens"]
        self.assertEqual((tokens["proxmox"]["api"], tokens["mist"]["api"]), ("https://192.0.2.5:8006/api2/json", "https://api.eu.mist.com/api/v1"))
        after_a_restart = Settings.load({"SIMRACK_STATE_DIR": self.tmp})
        self.assertEqual((after_a_restart.pve_api_base, after_a_restart.mist_api_base), ("https://192.0.2.5:8006/api2/json", "https://api.eu.mist.com/api/v1"))

    def test_an_address_changes_only_with_its_token_pasted_again(self):
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"})

        for name, label, address in (("proxmox", "Proxmox", "https://198.51.100.7:8006/api2/json"), ("mist", "Mist", "https://api.eu.mist.com/api/v1")):
            with self.subTest(name):
                status, reply = self.call("POST", "/api/setup/tokens", {f"{name}_api": address})
                self.assertEqual(status, 400, reply)
                self.assertIn(f"{label} token", reply["error"])
                self.assertIn(address, reply["error"])

        tokens = self.call("GET", "/api/setup")[1]["tokens"]
        self.assertEqual((tokens["proxmox"]["api"], tokens["mist"]["api"]), ("https://127.0.0.1:8006/api2/json", "https://api.mist.com/api/v1"), "nothing was saved")
        status, reply = self.call("POST", "/api/setup/tokens", {"mist": "secret-2", "mist_api": "https://api.eu.mist.com/api/v1"})
        self.assertEqual(status, 200, "pasted again with it, the token may go there")

    def test_a_token_goes_only_over_https_and_a_mist_token_only_to_a_mist_cloud(self):
        cases = {
            "plain http": ("proxmox", "http://192.0.2.5:8006/api2/json", "https://"),
            "not the Proxmox API": ("proxmox", "https://192.0.2.5:8006/", "api2/json"),
            "another host hidden after an @": ("proxmox", "https://127.0.0.1:8006@198.51.100.7/api2/json", "@"),
            "not a Mist cloud": ("mist", "https://mist.example.com/api/v1", "api.mist.com"),
            "a lookalike of one": ("mist", "https://api.mist.com.example.net/api/v1", "api.mist.com"),
            "plain http to Mist": ("mist", "http://api.mist.com/api/v1", "api.mist.com"),
        }
        token = {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"}
        for name, (service, address, hint) in cases.items():
            with self.subTest(name):
                status, reply = self.call("POST", "/api/setup/tokens", {service: token[service], f"{service}_api": address})
                self.assertEqual(status, 400, reply)
                self.assertIn(hint, reply["error"])
                self.assertNotIn("secret-", json.dumps(reply))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "tokens.json")), "nothing was saved")

    def test_a_proxmox_token_without_its_id_is_refused_and_not_echoed(self):
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        status, reply = self.call("POST", "/api/setup/tokens", {"proxmox": "secret-only-3"})

        self.assertEqual(status, 400, reply)
        self.assertIn("Proxmox token", reply["error"])
        self.assertNotIn("secret-only-3", json.dumps(reply))
        self.assertEqual(self.call("GET", "/api/setup")[1]["tokens"]["proxmox"]["id"], "simrack@pve!simrack")

    def test_a_mist_token_pasted_with_its_header_word_is_refused_and_not_echoed(self):
        status, reply = self.call("POST", "/api/setup/tokens", {"mist": "Token secret-4"})

        self.assertEqual(status, 400, reply)
        self.assertIn("Mist token", reply["error"])
        self.assertNotIn("secret-4", json.dumps(reply))
        self.assertFalse(self.call("GET", "/api/setup")[1]["tokens"]["mist"]["set"])

    def test_a_corrupt_tokens_file_still_opens_the_page_to_save_them_again(self):
        with open(os.path.join(self.tmp, "tokens.json"), "w", encoding="utf-8") as handle:
            handle.write('{"proxmox": "simrack@pve!simrack=secret-5')
        self.start()

        status, page = self.call("GET", "/api/setup")

        self.assertEqual(status, 200, page)
        self.assertIn("token", json.dumps(page["problems"]))
        self.assertNotIn("secret-5", json.dumps(page))
        self.assertFalse(page["tokens"]["proxmox"]["set"])
        self.assertEqual(self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-6"})[0], 200)

    def test_what_the_page_suggests_saves_as_it_is(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}
        self.proxmox.networks["vmbr0"] = {"iface": "vmbr0", "type": "bridge", "cidr": "192.0.2.10/24", "gateway": "192.0.2.1"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})
        suggested = self.call("GET", "/api/setup")[1]["profile"]

        status, reply = self.call("POST", "/api/setup", {"profile": suggested})

        self.assertEqual(status, 200, reply)
        self.assertTrue(reply["done"])

    def test_saving_the_page_sets_simrack_up_without_a_restart(self):
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})
        self.assertFalse(self.call("GET", "/api/state")[1]["writes_enabled"])

        status, reply = self.call("POST", "/api/setup", {"profile": SAVED})

        self.assertEqual(status, 200, reply)
        self.assertTrue(self.call("GET", "/api/state")[1]["writes_enabled"])
        self.assertTrue(self.call("GET", "/api/setup")[1]["done"])
        after_a_restart = Settings.load({"SIMRACK_STATE_DIR": self.tmp})
        self.assertEqual(after_a_restart.production_vmids, frozenset({200}))
        self.assertEqual(after_a_restart.mgmt_pool, "192.0.2.200-192.0.2.249")

    def test_assistants_get_the_risky_tools_only_once_the_page_allows_them(self):
        self.call("POST", "/api/setup", {"profile": SAVED})
        self.assertIs(self.call("GET", "/api/state")[1]["assistants"]["risky"], False)

        status, reply = self.call("POST", "/api/setup", {"profile": {**SAVED, "assistants": {"risky": True}}})

        self.assertEqual(status, 200, reply)
        self.assertIs(reply["profile"]["assistants"]["risky"], True, "the page shows it ticked")
        self.assertIs(self.call("GET", "/api/state")[1]["assistants"]["risky"], True, "without a restart")
        self.assertIs(Settings.load({"SIMRACK_STATE_DIR": self.tmp}).assistant_risky, True, "and after one")
        self.call("POST", "/api/setup", {"profile": {**SAVED, "assistants": {"risky": False}}})
        self.assertIs(self.call("GET", "/api/state")[1]["assistants"]["risky"], False)

    def test_a_bad_value_is_refused_by_name_and_nothing_is_saved(self):
        bad = {**SAVED, "protected": {"vmids": ["two hundred"]}}

        status, reply = self.call("POST", "/api/setup", {"profile": bad})

        self.assertEqual(status, 400, reply)
        self.assertTrue(reply["error"].startswith("protected.vmids "), reply)
        self.assertNotIn("start SimRack again", reply["detail"])
        self.assertFalse(self.call("GET", "/api/setup")[1]["done"])

    def test_an_exported_profile_restores_by_import(self):
        self.call("POST", "/api/setup", {"profile": SAVED})
        backup = self.call("GET", "/api/setup/export")[1]["toml"]
        self.call("POST", "/api/setup", {"profile": {**SAVED, "protected": {"vmids": [201]}}})

        status, reply = self.call("POST", "/api/setup", {"toml": backup})

        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["profile"]["protected"]["vmids"], [200])

    def test_importing_a_file_that_is_not_a_profile_says_so_and_saves_nothing(self):
        status, reply = self.call("POST", "/api/setup", {"toml": "[proxmox\nnode = "})

        self.assertEqual(status, 400, reply)
        self.assertIn("not valid TOML", reply["error"])
        self.assertEqual(reply["detail"], "Fix the file and import it again. Nothing was saved.")
        self.assertFalse(self.call("GET", "/api/setup")[1]["done"])

    def test_an_imported_file_with_a_wrong_value_names_it_and_saves_nothing(self):
        wrong = textwrap.dedent(PROFILE).replace('cidr = "192.0.2.0/24"', 'cidr = "192.0.2.0/24"\nvlan = 5000')

        status, reply = self.call("POST", "/api/setup", {"toml": wrong})

        self.assertEqual(status, 400, reply)
        self.assertIn("management.vlan", reply["error"])
        self.assertEqual(reply["detail"], "Fix the file and import it again. Nothing was saved.")
        self.assertFalse(self.call("GET", "/api/setup")[1]["done"])

    def test_it_shows_how_to_make_a_proxmox_token_that_may_do_just_what_simrack_checks(self):
        self.call("POST", "/api/setup", {"profile": {**SAVED, "management": {**SAVED["management"], "vlan": 10}}})

        commands = [shlex.split(line.split("|")[0]) for line in self.call("GET", "/api/setup")[1]["proxmox_token_commands"]]

        role = next(c for c in commands if c[:4] == ["pveum", "role", "add", "SimRack"])
        privileges = set(role[role.index("--privs") + 1].split())
        acls = [c[3] for c in commands if c[:3] == ["pveum", "acl", "modify"] and c[4:] == ["--users", "simrack@pve", "--roles", "SimRack"]]
        for path, needed in required_privileges(self.manager.settings).items():
            with self.subTest(path):
                self.assertTrue(any(path == acl or path.startswith(acl + "/") for acl in acls), f"no grant covers {path}")
                self.assertLessEqual(set(needed), privileges)
        self.assertNotIn("/", acls, "never the whole host")
        self.assertIn(["pveum", "user", "add", "simrack@pve"], commands)
        token = next(c for c in commands if c[:4] == ["pveum", "user", "token", "add"])
        self.assertEqual(token[4:8], ["simrack@pve", "simrack", "--privsep", "0"], "the token has the user's grants")

    def test_a_profile_broken_by_an_upgrade_still_opens_the_page_and_changes_nothing(self):
        retired = textwrap.dedent(PROFILE).replace('node = "pve-lab"', 'node = "pve-lab"\nhookscript = "local:snippets/simrack-sbx.sh"')
        with open(os.path.join(self.tmp, "lab-profile.toml"), "w", encoding="utf-8") as handle:
            handle.write(retired)
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})
        self.start()

        status, page = self.call("GET", "/api/setup")

        self.assertEqual(status, 200, page)
        self.assertFalse(page["done"])
        shown = json.dumps(page["problems"])
        self.assertIn("proxmox.hookscript", shown)
        self.assertIn("setup page", shown, "it is fixed on the page, not by editing the file")
        self.assertNotIn("start SimRack again", shown)
        self.assertNotIn("Delete this line", shown)
        self.assertFalse(self.call("GET", "/api/state")[1]["writes_enabled"])

    def test_a_broken_profile_is_offered_back_to_fix_not_replaced_by_suggestions(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}
        self.proxmox.vms[201] = {"vmid": 201, "name": "edge-1"}
        retired = textwrap.dedent(PROFILE).replace('node = "pve-lab"', 'node = "pve-lab"\nhookscript = "local:snippets/simrack-sbx.sh"')
        with open(os.path.join(self.tmp, "lab-profile.toml"), "w", encoding="utf-8") as handle:
            handle.write(retired)
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})
        self.start()

        page = self.call("GET", "/api/setup")[1]

        self.assertEqual(page["profile"]["protected"]["vmids"], [200])

    def test_once_saved_the_page_shows_what_was_saved(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}
        self.proxmox.vms[201] = {"vmid": 201, "name": "edge-1"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})
        self.call("POST", "/api/setup", {"profile": SAVED})

        profile = self.call("GET", "/api/setup")[1]["profile"]

        self.assertEqual(profile["protected"]["vmids"], [200])
        self.assertEqual(profile["proxmox"]["node"], "pve-lab")

    def test_before_a_proxmox_token_the_page_opens_and_says_what_is_missing(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}

        status, page = self.call("GET", "/api/setup")

        self.assertEqual(status, 200, page)
        self.assertEqual([problem["type"] for problem in page["problems"]], ["NotConfigured"])
        self.assertEqual(page["profile"]["protected"]["vmids"], [], "it cannot see the host yet")

    def test_it_suggests_protecting_everything_already_on_the_host(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}
        self.proxmox.vms[201] = {"vmid": 201, "name": "edge-1"}
        self.proxmox.vms[320] = {"vmid": 320, "name": "vjunos-template", "template": 1}
        self.proxmox.containers[303] = {"vmid": 303, "name": "radius"}
        self.proxmox.networks.update(
            {
                "vmbr0": {"iface": "vmbr0", "type": "bridge", "cidr": "192.0.2.10/24", "gateway": "192.0.2.1"},
                "vmbr1": {"iface": "vmbr1", "type": "bridge", "cidr": "10.10.10.1/24"},
                "lab1": {"iface": "lab1", "type": "bridge"},
                "sbxpark": {"iface": "sbxpark", "type": "bridge"},
            }
        )
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        status, page = self.call("GET", "/api/setup")

        self.assertEqual(status, 200, page)
        self.assertFalse(page["done"])
        protected = page["profile"]["protected"]
        self.assertEqual(protected["vmids"], [200, 201], "a template is left out: SimRack only clones it")
        self.assertEqual(protected["lxc"], [303])
        self.assertEqual(protected["bridges"], ["lab1", "vmbr0", "vmbr1"], "a sandbox bridge is SimRack's own")
        self.assertEqual(protected["subnets"], ["10.10.10.0/24", "192.0.2.0/24"])

    def test_management_goes_on_the_bridge_with_the_gateway(self):
        self.proxmox.networks.update(
            {
                "vmbr0": {"iface": "vmbr0", "type": "bridge", "cidr": "10.10.10.1/24"},
                "vmbr1": {"iface": "vmbr1", "type": "bridge", "cidr": "198.51.100.7/24", "gateway": "198.51.100.1"},
            }
        )
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        _, page = self.call("GET", "/api/setup")

        management = page["profile"]["management"]
        self.assertEqual(management["bridge"], "vmbr1")
        self.assertEqual(management["cidr"], "198.51.100.0/24")
        self.assertEqual(management["pool"], "198.51.100.200-198.51.100.249", "high in the subnet, clear of the gateway and the host")

    def test_the_pool_steps_down_past_the_hosts_own_address(self):
        self.proxmox.networks["vmbr0"] = {"iface": "vmbr0", "type": "bridge", "cidr": "198.51.100.210/24", "gateway": "198.51.100.1"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        _, page = self.call("GET", "/api/setup")

        self.assertEqual(page["profile"]["management"]["pool"], "198.51.100.150-198.51.100.199")

    def test_the_sandbox_range_steps_past_the_hosts_own_guests(self):
        self.proxmox.vms[320] = {"vmid": 320, "name": "vjunos-template", "template": 1}
        self.proxmox.vms[330] = {"vmid": 330, "name": "monitor"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        _, page = self.call("GET", "/api/setup")

        sandbox = page["profile"]["sandbox"]
        self.assertEqual(sandbox["vmids"], [420, 499], "330 is the host's own; the template may stay, SimRack only clones it")
        self.assertEqual(sandbox["lxc"], [450, 499])

    def test_the_page_names_what_it_found_so_each_can_be_ticked(self):
        self.proxmox.vms[200] = {"vmid": 200, "name": "core-1"}
        self.proxmox.containers[303] = {"vmid": 303, "name": "radius"}
        self.proxmox.networks["vmbr1"] = {"iface": "vmbr1", "type": "bridge", "cidr": "10.10.10.1/24"}
        self.mist.org_id = "org-lab"
        self.mist.site_records["site-hq"] = {"id": "site-hq", "name": "HQ"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"})

        found = self.call("GET", "/api/setup")[1]["found"]

        self.assertEqual(found["vmids"], [{"id": 200, "label": "core-1"}])
        self.assertEqual(found["lxc"], [{"id": 303, "label": "radius"}])
        self.assertIn({"id": "vmbr1", "label": "10.10.10.1/24"}, found["bridges"])
        self.assertIn({"id": "10.10.10.0/24", "label": "vmbr1"}, found["subnets"])
        self.assertEqual(found["mist_sites"], [{"id": "site-hq", "label": "HQ"}])

    def test_mist_suggests_the_org_the_token_works_on_and_protects_its_sites(self):
        self.mist.org_id = "org-lab"
        self.mist.site_records["site-hq"] = {"id": "site-hq", "name": "HQ"}
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"})

        _, page = self.call("GET", "/api/setup")

        self.assertEqual(page["profile"]["mist"]["org_id"], "org-lab")
        self.assertEqual(page["profile"]["protected"]["mist_sites"], ["site-hq"])

    def test_a_sandboxs_own_mist_site_is_still_its_own_after_an_upgrade(self):
        self.proxmox.vms[320] = {"vmid": 320, "name": "vjunos-template", "template": 1, "maxmem": 5120 * 1024 * 1024}
        self.proxmox.token = self.mist.token = "from-the-old-environment"
        self.mist.site_records["site-hq"] = {"id": "site-hq", "name": "HQ"}
        builder = make_manager(self.tmp, proxmox=self.proxmox, mist=self.mist)
        builder.mist_create_site(builder.create_sandbox("old", "single-switch", template_vmid=320))
        os.remove(os.path.join(self.tmp, "lab-profile.toml"))
        self.start()
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1", "mist": "secret-2"})

        _, page = self.call("GET", "/api/setup")

        self.assertEqual(page["profile"]["protected"]["mist_sites"], ["site-hq"])

    def test_guests_simrack_built_before_an_upgrade_are_still_its_own(self):
        self.proxmox.vms[320] = {"vmid": 320, "name": "vjunos-template", "template": 1, "maxmem": 5120 * 1024 * 1024}
        self.proxmox.token = "from-the-old-environment"
        built = make_manager(self.tmp, proxmox=self.proxmox).create_sandbox("old", "collapsed-core", template_vmid=320)
        os.remove(os.path.join(self.tmp, "lab-profile.toml"))  # before the upgrade it lived outside the state folder
        self.start()
        self.call("POST", "/api/setup/tokens", {"proxmox": "simrack@pve!simrack=secret-1"})

        _, page = self.call("GET", "/api/setup")

        ours = {node["vmid"] for node in built.to_dict()["nodes"]}
        self.assertTrue(ours)
        self.assertFalse(ours & set(page["profile"]["protected"]["vmids"]), "protected, SimRack could never tear them down")


class TestTheProxmoxClientLooksAtTheHost(unittest.TestCase):
    def test_it_asks_for_the_containers_and_the_bridges_set_up_on_the_host(self):
        client = ProxmoxClient(Settings(pve_token="t", pve_node="pve-lab"))

        sent = wire(client, lambda c: (c.list_lxc(), c.bridges()), body=b'{"data": []}')

        self.assertEqual(
            [(request["method"], request["url"]) for request in sent],
            [
                ("GET", "https://127.0.0.1:8006/api2/json/nodes/pve-lab/lxc"),
                ("GET", "https://127.0.0.1:8006/api2/json/nodes/pve-lab/network?type=any_bridge"),
            ],
        )


class TestTheSetupPageAsShipped(unittest.TestCase):
    """The setup page's files as shipped. What it does when used is checked live in a browser."""

    def setUp(self):
        self.js, self.html = shipped("app.js"), shipped("index.html")
        self.buttons = re.findall(r"<button\b[^>]*>", self.js + self.html)

    def test_the_top_bar_opens_the_setup_page(self):
        top = re.search(r'<div class="top".*?\n</div>', self.html, re.S)
        self.assertRegex(top.group(0), r'<button[^>]*data-act="setup"')

    def test_saving_setup_asks_every_time_and_works_while_changes_are_off(self):
        saving = [b for b in self.buttons if 'data-ask="setup"' in b]
        self.assertGreaterEqual(len(saving), 3, "saving the connection, saving the lab profile, importing one")
        for button in saving:
            self.assertIn("data-ask-always", button)
            self.assertNotIn("data-write", button, "a read-only SimRack is set up from this page")

    def test_tokens_are_typed_into_fields_that_hide_them(self):
        for name in ("proxmox_token", "mist_token"):
            field = re.search(rf'<input\b[^>]*\bname="{name}"[^>]*>', self.js)
            self.assertIsNotNone(field, f"the {name} field")
            self.assertIn('type="password"', field.group(0))
            self.assertIn('autocomplete="off"', field.group(0))

    def test_every_mist_cloud_offered_is_one_a_token_may_be_sent_to(self):
        clouds = set(re.findall(r"https://api[\w.-]*\.mist\.com/api/v1", self.js))
        self.assertEqual(len(clouds), 12, "Juniper lists twelve Mist clouds")
        for cloud in clouds:
            self.assertTrue(MIST_API.fullmatch(cloud), cloud)


if __name__ == "__main__":
    unittest.main()
