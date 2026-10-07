"""Case 19: what a real Proxmox and Mist would refuse, or would leak.

The fakes follow the real rules here: an API token cannot set or clear a
root-only field, and Mist can delete a topology.
"""

from __future__ import annotations

import io
import json
import os
import ssl
import stat
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from simrack.config import Settings
from simrack.errors import BackendError, GuardrailViolation
from simrack.mist import MistClient
from simrack.proxmox import ProxmoxClient
from tests.fakes import FakeMist, FakeProxmox, RedirectingPair, TempDir, make_manager


class Reply:
    def __init__(self, body=b'{"data": "UPID:pve1:1:2:3:qmcreate:321:root@pam:"}'):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


def wire(client, call, body=None):
    """Run ``call(client)`` with the network stubbed; return each request as it left.
    Each reply is ``body`` (bytes), or a Proxmox task id when it is None."""
    sent = []

    def fake_urlopen(request, timeout, context):
        form = urllib.parse.parse_qs((request.data or b"").decode())
        sent.append({"method": request.get_method(), "url": request.full_url, "form": form, "data": request.data, "context": context})
        return Reply() if body is None else Reply(body)

    with mock.patch("urllib.request.urlopen", fake_urlopen):
        call(client)
    return sent


class TestProxmoxClientWire(unittest.TestCase):
    def test_an_imported_vjunos_switch_is_created_with_token_settable_fields_only(self):
        client = ProxmoxClient(Settings(pve_token="t"))
        sent = wire(
            client,
            lambda c: c.create_vm(321, "sbx-acc-01", pool="simrack", memory_mb=5120, cores=4, import_from="local:import/vj.qcow2", smbios_product="VM-VEX", cpu="host"),
        )
        form = sent[0]["form"]
        for root_only in ("args", "hookscript"):
            self.assertNotIn(root_only, form, "only root@pam may set it, so the API token would be refused")
        self.assertEqual(form["cpu"], ["host"])
        self.assertRegex(form["smbios1"][0], r"^base64=1,product=Vk0tVkVY,uuid=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_a_guest_is_made_inside_the_simrack_pool(self):
        """The token may change only what is in its pool, so the clone or create must name it."""
        client = ProxmoxClient(Settings(pve_token="t"))
        sent = wire(
            client,
            lambda c: (
                c.clone_vm(320, 321, name="sbx-acc-01", pool="simrack"),
                c.create_vm(322, "sbx-img-01", pool="simrack", memory_mb=2048, cores=4, iso="local:iso/a.iso", disk_bus="scsi0"),
            ),
        )
        self.assertEqual([request["form"].get("pool") for request in sent], [["simrack"], ["simrack"]])

    def test_images_still_list_the_isos_on_a_proxmox_without_the_import_content_type(self):
        """Proxmox before 8.2 has no import content type, and refuses to be asked for it."""
        client = ProxmoxClient(Settings(pve_token="t"))
        isos = json.dumps({"data": [{"volid": "local:iso/vjunos.iso", "size": 1024}]}).encode()
        refusal = b'{"errors": {"content": "value \'import\' does not have a value in the enumeration"}, "data": null}'

        def answering(code):
            def fake_urlopen(request, timeout, context):
                if "content=import" in request.full_url:
                    raise urllib.error.HTTPError(request.full_url, code, "refused", {}, io.BytesIO(refusal))
                return Reply(isos)

            return fake_urlopen

        with mock.patch("urllib.request.urlopen", answering(400)):
            self.assertEqual(client.list_images(), [{"volid": "local:iso/vjunos.iso", "content": "iso", "size": 1024}])
        with mock.patch("urllib.request.urlopen", answering(500)), self.assertRaises(BackendError) as caught:
            client.list_images()
        self.assertEqual(caught.exception.status, 500, "any other failure is still told")


REMOTE_PVE = "https://pve.example.net:8006/api2/json"


class TestCertificates(unittest.TestCase):
    """A token only crosses a network over TLS that was checked. This host's own
    pveproxy is the one exception: its certificate is self-signed, and loopback
    traffic never leaves the machine."""

    def assert_verified(self, context):
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_the_mist_token_only_goes_to_a_mist_cloud_whose_certificate_checks_out(self):
        sent = wire(MistClient(Settings(mist_token="t")), lambda c: c.sites("org-1"))
        self.assert_verified(sent[0]["context"])

    def test_a_proxmox_api_on_another_host_is_verified(self):
        client = ProxmoxClient(Settings(pve_token="t", pve_api_base=REMOTE_PVE))
        self.assert_verified(wire(client, lambda c: c.vm_status(100))[0]["context"])

    def test_this_hosts_own_proxmox_api_keeps_its_self_signed_certificate(self):
        for base in ("https://127.0.0.1:8006/api2/json", "https://localhost:8006/api2/json", "https://[::1]:8006/api2/json"):
            with self.subTest(base=base):
                client = ProxmoxClient(Settings(pve_token="t", pve_api_base=base))
                self.assertEqual(wire(client, lambda c: c.vm_status(100))[0]["context"].verify_mode, ssl.CERT_NONE)

    def test_a_refused_proxmox_certificate_says_how_to_fix_it(self):
        client = ProxmoxClient(Settings(pve_token="t", pve_api_base=REMOTE_PVE))
        refused = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate"))
        with mock.patch("urllib.request.urlopen", side_effect=refused), self.assertRaises(BackendError) as caught:
            client.vm_status(100)
        self.assertIn("on the setup page set the Proxmox address to https://127.0.0.1:8006/api2/json and paste the token again", caught.exception.detail)
        self.assertNotIn("[proxmox] api", caught.exception.detail)

    def test_a_refused_mist_certificate_says_how_to_fix_it(self):
        refused = urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed: unable to get local issuer certificate"))
        with mock.patch("urllib.request.urlopen", side_effect=refused), self.assertRaises(BackendError) as caught:
            MistClient(Settings(mist_token="t")).sites("org-1")
        self.assertIn("update-ca-certificates", caught.exception.detail)


class TestTokensStayAtTheirAddress(unittest.TestCase):
    """A token goes only to the address it was saved for. When that address
    answers with a redirect, the request that follows it goes without the token."""

    def test_the_proxmox_token_does_not_follow_a_redirect(self):
        with RedirectingPair(b'{"data": {}}') as pair:
            ProxmoxClient(Settings(pve_token="t", pve_api_base=pair.saved + "/api2/json")).vm_status(100)
        self.assertEqual(pair.carried, {"saved": [True], "elsewhere": [False]})

    def test_the_mist_token_does_not_follow_a_redirect(self):
        with RedirectingPair(b"[]") as pair:
            MistClient(Settings(mist_token="t", mist_api_base=pair.saved + "/api/v1")).sites("org-1")
        self.assertEqual(pair.carried, {"saved": [True], "elsewhere": [False]})


class TestTokenSafeBuild(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_switches_and_clients_cloned_from_a_template_build_with_an_api_token(self):
        sandbox = self.manager.create_sandbox("tok", "single-switch", template_vmid=320)
        client = self.manager.provision_node(sandbox, "sbx-cli-01", role="client", kind="client", template_vmid=320)
        self.assertEqual(len(sandbox.nodes), 2)
        for node in (sandbox.nodes[0], client):
            self.assertIn(node.vmid, self.px.vms)

    def test_cabled_ports_pass_lacp_again_after_simrack_starts_a_switch(self):
        sandbox = self.manager.create_sandbox("lacp", "collapsed-core", template_vmid=320)
        link = sandbox.links[0]
        node = sandbox.node(link.a_node)
        net = int(link.a_port.split("/")[-1]) + 1  # net0 is fxp0, so ge-0/0/N is net N+1
        self.assertTrue(self.px.passes_lacp(node.vmid, net), "cabling a running switch opens LACP")
        for steps in (("stop", "start"), ("reboot",)):
            with self.subTest(steps=steps):
                for action in steps:
                    result = self.manager.set_power(sandbox, node.name, action)
                self.px.qmeventd()  # a moment later: PVE restarts a guest whose reboot task ended
                self.assertEqual(result["status"], "running")
                self.assertTrue(self.manager.get("lacp").node(node.name).running)
                self.assertTrue(self.px.passes_lacp(node.vmid, net), "a start gives the switch new taps; SimRack opens LACP again")
        with self.subTest(steps="revert"):
            self.manager.snapshot(sandbox, "base")
            self.manager.revert(sandbox, "base")
            self.assertTrue(self.px.passes_lacp(node.vmid, net), "revert starts the switch again, so LACP must reopen")

    def test_a_reboot_never_starts_a_stopped_switch(self):
        """Proxmox reboots only a running guest. A reboot that started a stopped
        switch would also skip the memory check every start must pass."""
        sandbox = self.manager.create_sandbox("reboot", "single-switch", template_vmid=320)
        node = sandbox.nodes[0]
        self.manager.set_power(sandbox, node.name, "stop")
        result = self.manager.set_power(sandbox, node.name, "reboot")
        self.px.qmeventd()
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(self.px.vm_status(node.vmid)["status"], "stopped")


class TestCloneOwnership(unittest.TestCase):
    """SimRack deletes only guests it made. A vmid is SimRack's once Proxmox
    accepts the create or clone: PVE takes the new id before it answers."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("own", "single-switch", template_vmid=320)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_a_clone_that_loses_its_vmid_to_another_guest_leaves_that_guest_alone(self):
        def lost_race(source_vmid, new_vmid, **_):
            self.px.vms[int(new_vmid)] = {"vmid": int(new_vmid), "name": "not-simracks", "status": "running"}
            raise BackendError(f"Proxmox API POST /qemu/{source_vmid}/clone failed (500).", detail=f"VM {new_vmid} already exists")

        self.px.clone_vm = lost_race
        with self.assertRaises(BackendError):
            self.manager.provision_node(self.sandbox, "sbx-acc-02", template_vmid=320)
        self.assertIn("not-simracks", [vm.get("name") for vm in self.px.vms.values()])

    def test_no_vmid_is_picked_while_proxmox_cannot_list_its_guests(self):
        for listing in ("list_vms", "list_lxc"):
            with self.subTest(listing), mock.patch.object(self.px, listing, side_effect=BackendError("Cannot reach the Proxmox API.")):
                before = len(self.px.calls)
                with self.assertRaises(BackendError):
                    self.manager.provision_node(self.sandbox, "sbx-acc-02", template_vmid=320)
                verbs = {call[0] for call in self.px.calls[before:]}
                self.assertFalse(verbs & {"clone_vm", "create_vm", "delete_vm", "set_power"})

    def test_a_container_in_the_sandbox_range_keeps_its_vmid(self):
        """Proxmox gives VMs and containers one set of vmids."""
        taken = max(node.vmid for node in self.sandbox.nodes) + 1
        self.px.containers[taken] = {"vmid": taken, "name": "not-simracks", "status": "running"}
        node = self.manager.provision_node(self.sandbox, "sbx-acc-02", template_vmid=320)
        self.assertEqual(node.vmid, taken + 1)
        self.assertEqual(self.px.containers[taken]["name"], "not-simracks")


class TestOnlyItsOwnGuestsAreDeleted(unittest.TestCase):
    """A sandbox record keeps a vmid. Something outside SimRack can rename that
    guest, or delete it and put another at the same vmid, so SimRack checks the
    name Proxmox has before it deletes."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("own", "single-switch", template_vmid=320)
        self.node = self.sandbox.nodes[0]

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def changes_since(self, before):
        return {call[0] for call in self.px.calls[before:]} & {"delete_vm", "set_power", "set_vm_config", "delete_bridge", "tune_port"}

    def test_teardown_leaves_a_guest_that_is_not_its_own(self):
        for other, said in (("web-01", "web-01"), (None, "a guest with no name")):
            with self.subTest(said):
                guest = {"vmid": self.node.vmid, "status": "running"} | ({"name": other} if other else {})
                self.px.vms[self.node.vmid] = guest
                before = len(self.px.calls)
                removed = self.manager.teardown(self.sandbox)
                self.assertFalse(removed["complete"])
                self.assertIn(said, removed["failed"][0])
                self.assertIs(self.px.vms[self.node.vmid], guest)
                self.assertEqual([node.name for node in self.sandbox.nodes], ["sbx-acc-01"])
                self.assertEqual(self.changes_since(before), set())

    def test_teardown_finishes_once_the_other_guest_is_gone(self):
        self.px.vms[self.node.vmid]["name"] = "web-01"
        self.assertFalse(self.manager.teardown(self.sandbox)["complete"])
        del self.px.vms[self.node.vmid]
        self.assertTrue(self.manager.teardown(self.sandbox)["complete"])

    def test_deleting_a_node_leaves_a_guest_that_is_not_its_own_and_its_cables(self):
        self.manager.provision_node(self.sandbox, "sbx-acc-02", template_vmid=320)
        link = self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/1", "sbx-acc-02", "ge-0/0/1")
        self.px.vms[self.node.vmid]["name"] = "web-01"
        before = len(self.px.calls)
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.delete_node(self.sandbox, "sbx-acc-01")
        self.assertIn("web-01", caught.exception.message)
        self.assertIn("delete it in Proxmox", caught.exception.detail)
        self.assertEqual(self.changes_since(before), set())
        self.assertEqual([cable.bridge for cable in self.sandbox.links], [link.bridge])
        self.assertIn("sbx-acc-01", [node.name for node in self.sandbox.nodes])

    def test_a_guest_list_in_an_unknown_shape_deletes_nothing(self):
        self.px.list_vms = mock.Mock(return_value=[{"id": f"qemu/{self.node.vmid}"}])
        before = len(self.px.calls)
        removed = self.manager.teardown(self.sandbox)
        self.assertFalse(removed["complete"])
        self.assertIn("shape", removed["failed"][0])
        self.assertEqual(self.changes_since(before), set())


class TestCloneSource(unittest.TestCase):
    def test_only_a_proxmox_template_is_ever_cloned(self):
        with TempDir() as tmp:
            px = FakeProxmox(free_mb=60000, templates=[320])
            px.vms[330] = {"vmid": 330, "name": "booted-vjunos", "status": "stopped"}
            manager = make_manager(tmp, proxmox=px)
            with self.assertRaises(GuardrailViolation) as caught:
                manager.create_sandbox("src", "single-switch", template_vmid=330)
            self.assertNotIn("clone_vm", [call[0] for call in px.calls])
            self.assertIn("qm template", caught.exception.detail)


class TestMistSnapshotSecrets(unittest.TestCase):
    """A Mist snapshot is readable only by SimRack's own user, never holds a
    password, and goes when its sandbox goes."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.mist = FakeMist()
        self.manager = make_manager(self.tmp, proxmox=FakeProxmox(free_mb=40000, templates=[320]), mist=self.mist)
        self.sandbox = self.manager.create_sandbox("secrets", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.site = self.sandbox.mist_site_id
        self.mist.add_switch(self.site, "sbx-core-01")
        self.device = self.mist.add_switch(self.site, "sbx-acc-01", config={"name": "sbx-acc-01", "switch_mgmt": {"root_password": "per-device-pw"}})
        self.mist.cli[(self.site, self.device)] = {
            "cli": [
                "set version 26.2R1.7",
                'set system root-authentication encrypted-password "$6$salt$hash"',
                'set access radius-server 192.0.2.10 secret "$9$abcDEF"',
                "set snmp community public authorization read-only",
            ]
        }
        self.manager.mist_build_fabric(self.sandbox)
        self.password = self.manager.reveal_root_password(self.sandbox)["root_password"]

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_a_mist_snapshot_is_private_and_holds_no_password(self):
        path = self.manager.mist_snapshot(self.sandbox, "good")["path"]
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        for secret in (self.password, "per-device-pw", "$6$salt$hash", "$9$abcDEF", "community public"):
            self.assertNotIn(secret, text)
        self.assertIn("set version 26.2R1.7", text, "only the lines that hold a secret go")

    def test_revert_puts_the_sandbox_root_password_back_where_it_was(self):
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.put_site_setting(self.site, {"networks": {}})
        self.mist.put_device(self.site, self.device, {"name": "sbx-acc-01"})
        self.manager.mist_revert(self.sandbox, "good")
        self.assertEqual(self.mist.site_setting(self.site)["switch_mgmt"]["root_password"], self.password)
        self.assertEqual(self.mist.device(self.site, self.device)["switch_mgmt"]["root_password"], self.password)
        core = self.mist.device(self.site, "dev-sbx-core-01")
        self.assertNotIn("root_password", core.get("switch_mgmt", {}), "a password goes back only where there was one")

    def test_teardown_removes_the_sandboxs_mist_snapshots(self):
        folder = os.path.dirname(self.manager.mist_snapshot(self.sandbox, "good")["path"])
        self.manager.teardown(self.sandbox)
        self.assertFalse(os.path.exists(folder))


class TestMistRevertTopologies(unittest.TestCase):
    """Revert puts the site's EVPN topologies back exactly: each one in full, and
    only the ones the snapshot had."""

    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.mist = FakeMist()
        self.manager = make_manager(self.tmp, proxmox=FakeProxmox(free_mb=40000, templates=[320]), mist=self.mist)
        self.sandbox = self.manager.create_sandbox("topo", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.site = self.sandbox.mist_site_id
        self.mist.add_switch(self.site, "sbx-core-01")
        self.mist.add_switch(self.site, "sbx-acc-01")
        self.built = self.manager.mist_build_fabric(self.sandbox)
        self.good = self.mist.evpn_topology(self.site, self.built["topology_id"])

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_revert_puts_each_topology_back_in_full(self):
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.put_evpn_topology(self.site, {"id": self.good["id"], "name": "topo", "switches": self.good["switches"][:1]})
        self.manager.mist_revert(self.sandbox, "good")
        self.assertEqual(self.mist.evpn_topology(self.site, self.good["id"]).get("switches"), self.good["switches"])

    def test_revert_to_before_the_build_removes_the_topology_the_build_made(self):
        result = self.manager.mist_revert(self.sandbox, self.built["snapshot"])
        self.assertEqual(self.mist.evpn_topologies(self.site), [])
        self.assertEqual(result["topologies_removed"], ["topo"])

    def test_a_topology_deleted_since_the_snapshot_comes_back(self):
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.delete_evpn_topology(self.site, self.good["id"])
        self.manager.mist_revert(self.sandbox, "good")
        (back,) = self.mist.evpn_topologies(self.site)
        self.assertEqual(self.mist.evpn_topology(self.site, back["id"]).get("switches"), self.good["switches"])

    def test_a_refused_full_topology_goes_back_as_members_and_roles(self):
        self.manager.mist_snapshot(self.sandbox, "good")
        self.mist.put_evpn_topology(self.site, {"id": self.good["id"], "name": "topo", "switches": []})
        self.mist.reject_topology = "detailed"
        result = self.manager.mist_revert(self.sandbox, "good")
        back = self.mist.evpn_topology(self.site, self.good["id"])
        self.assertEqual({(s["mac"], s["role"]) for s in back["switches"]}, {(s["mac"], s["role"]) for s in self.good["switches"]})
        self.assertEqual(result["devices_restored"], 2, "the switches still go back after the topology")

    def test_the_client_deletes_a_topology_only_when_its_write_gate_allows(self):
        client = MistClient(Settings(mist_token="t", org_id="org-1"), write_gate=lambda: None)
        sent = wire(client, lambda c: c.delete_evpn_topology("s1", "t1"))
        self.assertEqual([(s["method"], urllib.parse.urlparse(s["url"]).path) for s in sent], [("DELETE", "/api/v1/sites/s1/evpn_topologies/t1")])
        shut = MistClient(Settings(mist_token="t", org_id="org-1"), write_gate=lambda: (GuardrailViolation, "Changes are paused.", ""))
        with mock.patch("urllib.request.urlopen") as urlopen, self.assertRaises(GuardrailViolation):
            shut.delete_evpn_topology("s1", "t1")
        urlopen.assert_not_called()

    def test_the_client_releases_switches_only_when_its_write_gate_allows(self):
        client = MistClient(Settings(mist_token="t", org_id="org-1"), write_gate=lambda: None)
        answer = {"op": "delete", "success": ["SBX000000001"], "error": [], "reason": []}
        replies = []
        sent = wire(client, lambda c: replies.append(c.release_devices(["SBX000000001"])), json.dumps(answer).encode())
        self.assertEqual([(s["method"], urllib.parse.urlparse(s["url"]).path) for s in sent], [("PUT", "/api/v1/orgs/org-1/inventory")])
        self.assertEqual(json.loads(sent[0]["data"]), {"op": "delete", "serials": ["SBX000000001"]})
        self.assertEqual(replies, [answer], "Mist's reply comes back whole: what it released, what it kept and why")
        shut = MistClient(Settings(mist_token="t", org_id="org-1"), write_gate=lambda: (GuardrailViolation, "Changes are paused.", ""))
        with mock.patch("urllib.request.urlopen") as urlopen, self.assertRaises(GuardrailViolation):
            shut.release_devices(["SBX000000001"])
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
