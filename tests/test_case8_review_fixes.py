"""Case 8: the fixes from the October review.

Covers the real HTTP clients (with the network stubbed), read-only defaults,
recipe cabling, partial teardown, and the API's auth and same-origin checks.
"""

from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from simrack.api import serve
from simrack.config import Settings
from simrack.errors import BackendError, GuardrailViolation
from simrack.mist import MistClient
from simrack.proxmox import ProxmoxClient
from tests.fakes import FakeProxmox, TempDir, make_manager


class RecordingProxmox(ProxmoxClient):
    def __init__(self, replies=None):
        super().__init__(Settings(pve_token="t"))
        self.sent = []
        self.replies = list(replies or [])

    def _request(self, method, path, params=None):
        self.sent.append((method, path, params))
        return self.replies.pop(0) if self.replies else None


class TestProxmoxClient(unittest.TestCase):
    def _capture(self, method, path, params):
        client = ProxmoxClient(Settings(pve_token="t"))
        seen = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"data": null}'

        def fake_urlopen(request, timeout, context):
            seen["url"], seen["data"], seen["method"] = request.full_url, request.data, request.get_method()
            return Reply()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            client._request(method, path, params)
        return seen

    def test_get_and_delete_send_params_in_the_query_string(self):
        seen = self._capture("DELETE", "/nodes/pve1/qemu/321", {"purge": 1, "destroy-unreferenced-disks": 1})
        self.assertIn("?purge=1&destroy-unreferenced-disks=1", seen["url"])
        self.assertIsNone(seen["data"])
        seen = self._capture("GET", "/x", {"a": "b"})
        self.assertTrue(seen["url"].endswith("/x?a=b"))

    def test_put_sends_a_form_body(self):
        seen = self._capture("PUT", "/x", {"memory": 5120, "delete": "net3"})
        self.assertEqual(seen["data"], b"memory=5120&delete=net3")

    def test_none_means_delete_the_field(self):
        client = RecordingProxmox()
        client.set_vm_config(321, net3=None, net4=None, memory=5120)
        method, _, params = client.sent[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(params, {"memory": 5120, "delete": "net3,net4"})

    def test_delete_vm_purges_and_drops_unreferenced_disks(self):
        client = RecordingProxmox()
        client.delete_vm(321)
        self.assertEqual(client.sent[0][2], {"purge": 1, "destroy-unreferenced-disks": 1})

    def test_wait_task_polls_until_stopped_and_raises_on_failure(self):
        client = RecordingProxmox(replies=[{"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])
        client.wait_task("UPID:pve1:1:2:3:qmclone:321:root@pam:", poll=0)
        self.assertEqual(len(client.sent), 2)
        self.assertIn("UPID%3Apve1", client.sent[0][1])
        bad = RecordingProxmox(replies=[{"status": "stopped", "exitstatus": "unable to create VM 321"}])
        with self.assertRaises(BackendError):
            bad.wait_task("UPID:x", poll=0)
        RecordingProxmox().wait_task(None)  # sync calls return no UPID

    def test_create_vm_matches_the_live_switch_shape(self):
        client = RecordingProxmox()
        client.create_vm(321, "sbx-acc-01", memory_mb=5120, cores=4, import_from="local:import/vj.qcow2", smbios_product="VM-VEX", cpu="host")
        params = client.sent[0][2]
        self.assertEqual(params["virtio0"], "local-lvm:0,import-from=local:import/vj.qcow2,iothread=1")
        self.assertEqual(params["boot"], "order=virtio0")
        self.assertNotIn("bios", params, "vJunos boots with SeaBIOS like the live switches")
        self.assertNotIn("ide2", params)

        client = RecordingProxmox()
        client.create_vm(322, "sbx-iso-01", memory_mb=2048, cores=4, iso="local:iso/a.iso", disk_bus="scsi0")
        params = client.sent[0][2]
        self.assertEqual(params["ide2"], "local:iso/a.iso,media=cdrom")
        self.assertEqual(params["scsi0"], "local-lvm:32,iothread=1")
        self.assertEqual(params["boot"], "order=ide2;scsi0")


class TestMistAndSettings(unittest.TestCase):
    def test_device_type_filter_is_a_query_string(self):
        client = MistClient(Settings(mist_token="m"))
        with mock.patch.object(client, "_request", return_value=[]) as sent:
            client.devices("site-1", "switch")
        args = sent.call_args[0]
        self.assertEqual(args[:2], ("GET", "/sites/site-1/devices?type=switch"))
        self.assertEqual(len(args), 2, "GET must not carry a JSON body")

    def test_the_app_starts_read_only(self):
        self.assertFalse(Settings().allow_writes)
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(Settings.from_env().allow_writes)
        example = os.path.join(os.path.dirname(__file__), os.pardir, "lab-profile.example.toml")
        with mock.patch.dict(os.environ, {"SIMRACK_PROFILE": example, "SIMRACK_ALLOW_WRITES": "1", "SIMRACK_TOKEN": "abc"}, clear=True):
            settings = Settings.from_env()
            self.assertTrue(settings.allow_writes)
            self.assertEqual(settings.extras["token"], "abc")


class TestCablingAndTeardown(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_ip_clos_is_a_full_mesh_with_no_port_used_twice(self):
        sandbox = self.manager.create_sandbox("clos", "ip-clos", template_vmid=320)
        self.assertEqual(len(sandbox.links), 4, "2 cores x 2 access")
        ends = [end for link in sandbox.links for end in link.endpoints()]
        self.assertEqual(len(ends), len(set(ends)))
        self.assertFalse([n for n in sandbox.notes if n.startswith("Skipped")])

    def test_teardown_stops_running_guests_before_deleting(self):
        sandbox = self.manager.create_sandbox("run", "single-switch", template_vmid=320)
        vmid = sandbox.nodes[0].vmid
        self.assertEqual(self.px.vms[vmid]["status"], "running")
        result = self.manager.teardown(sandbox, confirm=True)
        self.assertTrue(result["complete"])
        names = [c[0] for c in self.px.calls if c[0] in ("set_power", "delete_vm")]
        self.assertEqual(names[-2:], ["set_power", "delete_vm"])

    def test_a_failed_delete_keeps_the_sandbox_and_its_mist_site(self):
        sandbox = self.manager.create_sandbox("stuck", "collapsed-core", template_vmid=320, with_mist_site=True)
        stuck = sandbox.nodes[0]

        real_delete = self.px.delete_vm

        def delete(vmid, **kw):
            if vmid == stuck.vmid:
                raise BackendError("Proxmox API DELETE failed (500).", detail="VM is locked (clone)")
            return real_delete(vmid, **kw)

        self.px.delete_vm = delete
        result = self.manager.teardown(sandbox, confirm=True)
        self.assertFalse(result["complete"])
        self.assertIn("stuck", self.manager.sandboxes, "the record must stay so the teardown can be retried")
        self.assertEqual([n.name for n in sandbox.nodes], [stuck.name])
        self.assertIn(sandbox.mist_site_id, self.manager.mist.sites)
        self.assertEqual(len(sandbox.links), 1, "a cable whose switch still exists is kept")

        self.px.delete_vm = real_delete
        self.assertTrue(self.manager.teardown(sandbox, confirm=True)["complete"])
        self.assertNotIn("stuck", self.manager.sandboxes)

    def test_bridges_lost_in_a_reboot_are_recreated(self):
        sandbox = self.manager.create_sandbox("boot", "collapsed-core", template_vmid=320)
        self.px.networks.clear()
        self.assertEqual(self.manager.ensure_bridges(), ["sbxpark", sandbox.links[0].bridge])

    def test_console_is_a_write(self):
        sandbox = self.manager.create_sandbox("con", "single-switch", template_vmid=320)
        self.manager.settings.allow_writes = False
        with self.assertRaises(GuardrailViolation):
            self.manager.console_command(sandbox, "sbx-acc-01", "show version")


class TestApi(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.manager = make_manager(self.tmp, proxmox=FakeProxmox(templates=[320]))
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.__exit__(None, None, None)

    def _post(self, path, body, headers):
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=json.dumps(body).encode(), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status
        except urllib.error.HTTPError as error:
            return error.code

    def test_cross_site_and_form_posts_are_refused(self):
        body = {"name": "cust-x", "recipe": "single-switch", "template_vmid": 320}
        self.assertEqual(self._post("/api/sandboxes", body, {"Content-Type": "text/plain"}), 403)
        self.assertEqual(self._post("/api/sandboxes", body, {"Content-Type": "application/json", "Origin": "http://evil.example"}), 403)
        self.assertNotIn("cust-x", self.manager.sandboxes)
        ok = self._post("/api/sandboxes", body, {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(ok, 200)

    def test_bearer_token_is_checked(self):
        self.httpd.RequestHandlerClass.token = "s3cret"
        url = f"http://127.0.0.1:{self.port}/api/state"
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        request = urllib.request.Request(url, headers={"Authorization": "Bearer s3cret"})
        with urllib.request.urlopen(request, timeout=5) as reply:
            self.assertEqual(reply.status, 200)


if __name__ == "__main__":
    unittest.main()
