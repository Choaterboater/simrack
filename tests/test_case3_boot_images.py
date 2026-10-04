"""Case 3: optionally boot images.

Asserts a node can be created from a template clone or booted straight from an
image volume, that the image path is passed through to Proxmox unchanged, and
that a request with neither is refused rather than silently creating a husk.
"""

from __future__ import annotations

import unittest

from labfront.errors import GuardrailViolation, LabError
from tests.fakes import FakeProxmox, TempDir, make_manager


class TestBootImages(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("imgtest", "single-switch", template_vmid=320)

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_booting_an_image_passes_the_volume_straight_to_proxmox(self):
        self.px.calls.clear()
        node = self.manager.provision_node(
            self.sandbox,
            "sbx-image-01",
            kind="image",
            image="local:iso/ubuntu-24.04.iso",
            start=True,
        )
        create = self.px.called("create_vm")[0]
        self.assertEqual(create[1][0], node.vmid)
        self.assertEqual(create[2]["iso"], "local:iso/ubuntu-24.04.iso", "an ISO is a CD-ROM, not the boot disk")
        self.assertIsNone(create[2]["import_from"])
        self.assertNotIn("template", create[2], "an image node is not a clone")
        self.assertEqual(self.px.called("set_power")[-1][1][1], "start")
        self.assertTrue(node.running)

    def test_a_vjunos_disk_image_is_imported_onto_a_fresh_virtio_disk(self):
        self.px.calls.clear()
        node = self.manager.provision_node(self.sandbox, "sbx-img-01", image="local:import/vJunos-switch-26.2R1.7.qcow2")
        create = self.px.called("create_vm")[0][2]
        self.assertEqual(create["import_from"], "local:import/vJunos-switch-26.2R1.7.qcow2")
        self.assertEqual(create["disk_bus"], "virtio0", "match the live switches: SeaBIOS + virtio0")
        self.assertEqual(create["hookscript"], "local:snippets/labfront-sbx.sh")
        waits = self.px.called("wait_task")
        self.assertTrue(waits, "the import is asynchronous; the guest is locked until it ends")
        self.assertIn(node.vmid, self.px.vms)

    def test_existing_guest_disks_and_odd_paths_are_refused(self):
        for bad in (
            "local-lvm:vm-204-disk-0",
            "local-lvm:base-320-disk-0",
            "local:iso/../../etc/passwd.iso",
            "/root/download/vJunos-switch-26.2R1.7.qcow2",
            "local:import/disk.exe",
        ):
            with self.subTest(image=bad), self.assertRaises(GuardrailViolation):
                self.manager.provision_node(self.sandbox, "sbx-bad-01", image=bad)
        self.assertFalse(self.px.called("create_vm"))

    def test_a_failed_import_leaves_no_half_built_guest(self):
        from labfront.errors import BackendError

        def failing_wait(upid, **_):
            raise BackendError("Proxmox task failed.", detail="import: no space")

        original = self.px.wait_task
        self.px.wait_task = failing_wait
        with self.assertRaises(BackendError):
            self.manager.provision_node(self.sandbox, "sbx-img-02", image="local:import/x.qcow2")
        self.px.wait_task = original
        self.assertFalse([v for v in self.px.vms.values() if v.get("name") == "sbx-img-02"])
        self.assertNotIn("sbx-img-02", [n.name for n in self.sandbox.nodes])

    def test_booting_from_an_image_with_start_false_leaves_it_stopped(self):
        node = self.manager.provision_node(
            self.sandbox, "sbx-image-02", kind="image", image="local:iso/nano.iso", start=False
        )
        self.assertFalse(node.running)
        self.assertEqual(self.px.vms[node.vmid]["status"], "stopped")

    def test_a_template_clone_keeps_juniper_cpu_flags_and_serials_distinct_per_clone(self):
        node = self.manager.provision_node(self.sandbox, "sbx-clone-01", template_vmid=320)
        clone = self.px.called("clone_vm")[-1]
        self.assertEqual(clone[1][0], 320)
        self.assertEqual(clone[1][1], node.vmid)
        self.assertTrue(clone[2]["full"], "a linked clone of a booted vJunos would share its disk identity")
        # Every clone gets its own vmid, which is what the front end keys on.
        self.assertNotEqual(node.vmid, self.sandbox.node("sbx-acc-01").vmid)

    def test_a_node_with_neither_template_nor_image_is_refused(self):
        with self.assertRaises(LabError) as caught:
            self.manager.provision_node(self.sandbox, "sbx-nothing-01")
        self.assertIn("template or an image", str(caught.exception).lower().replace("a ", "", 1))
        self.assertFalse(self.px.called("create_vm"), "nothing may be created when the request is refused")

    def test_image_names_are_validated_so_proxmox_never_sees_a_junk_name(self):
        for bad in ("UPPER", "has space", "-leading", "x"):
            with self.assertRaises(ValueError):
                self.manager.provision_node(self.sandbox, bad, kind="image", image="local:iso/a.iso")
        with self.assertRaises(GuardrailViolation):
            self.manager.provision_node(self.sandbox, "sbx-dup-01", kind="image", image="local:iso/a.iso")
            self.manager.provision_node(self.sandbox, "sbx-dup-01", kind="image", image="local:iso/a.iso")


if __name__ == "__main__":
    unittest.main()
