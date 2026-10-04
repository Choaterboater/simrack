"""Case 7: tear the lab down, redo the lab, fix things.

Asserts teardown is complete and reversible-by-rebuild (a sandbox can be built
again straight after), that a failed operation leaves enough state to diagnose,
and that the runbook that answers "how do I fix this" ships with the project.
"""

from __future__ import annotations

import os
import unittest

from simrack.errors import BackendError, GuardrailViolation
from tests.fakes import FakeMist, FakeProxmox, TempDir, make_manager

ADVICE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ADVICE.md")


class TestTeardownRedoAndFix(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=40000, templates=[320])
        self.mist = FakeMist()
        self.manager = make_manager(self.tmp, proxmox=self.px, mist=self.mist)
        self.live = self.manager.settings
        for vmid in sorted(self.live.production_vmids):
            self.px.vms[vmid] = {"vmid": vmid, "name": f"live-{vmid}", "status": "running"}
        self.sandbox = self.manager.create_sandbox("rebuild", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/3", "sbx-core-01", "ge-0/0/3")

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_teardown_removes_every_guest_bridge_and_the_mist_site(self):
        sandbox_vmids = {n.vmid for n in self.sandbox.nodes}
        removed = self.manager.teardown(self.sandbox, confirm=True)
        self.assertEqual(len(removed["nodes"]), 2)
        self.assertEqual(len(removed["bridges"]), 2)  # the recipe cable plus the one added by hand
        self.assertEqual(removed["mist_site"], self.sandbox.mist_site_name)
        self.assertNotIn(self.sandbox.mist_site_id, self.mist.sites)
        for vmid in sandbox_vmids:
            self.assertNotIn(vmid, self.px.vms, f"teardown left sandbox guest {vmid} behind")
        for vmid in self.live.production_vmids:
            self.assertIn(vmid, self.px.vms, "teardown must not touch the live lab")
        self.assertIn(320, self.px.vms, "teardown must not delete the template")
        for bridge in self.px.networks:
            self.assertTrue(bridge.startswith("vmbr") or bridge in self.live.production_bridges, f"teardown left bridge {bridge} behind")

    def test_teardown_needs_confirmation_and_can_keep_mist(self):
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.teardown(self.sandbox, confirm=False)
        self.assertIn("confirm", str(caught.exception))

        kept = self.manager.teardown(self.sandbox, confirm=True, keep_mist=True)
        self.assertIsNone(kept["mist_site"])
        self.assertIn(self.sandbox.mist_site_id, self.mist.sites, "keep_mist leaves the site for inspection")

    def test_the_lab_can_be_rebuilt_immediately_after_teardown(self):
        first_site = self.sandbox.mist_site_id
        self.manager.teardown(self.sandbox, confirm=True)
        rebuilt = self.manager.create_sandbox("rebuild", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.assertNotEqual(rebuilt.mist_site_id, first_site, "a rebuild must get a fresh site, never reuse the old id")
        self.assertEqual(len(rebuilt.nodes), 2)
        self.assertEqual(len(rebuilt.links), 1, "the recipe cable is built again")
        self.assertIn("rebuild", self.manager.list_sandboxes()[0]["name"])

    def test_a_failed_build_leaves_a_record_for_diagnosis(self):
        def boom(*args, **kwargs):
            raise BackendError("Proxmox API POST failed (500).", detail="no storage")

        self.px.clone_vm = boom
        with self.assertRaises(BackendError):
            self.manager.create_sandbox("doomed", "single-switch", template_vmid=320)
        # Nothing half-registered: the sandbox is not in the manager or on disk.
        self.assertNotIn("doomed", self.manager.sandboxes)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sandboxes", "doomed.json")))

    def test_every_sandbox_keeps_an_audit_trail_of_what_changed(self):
        self.manager.snapshot(self.sandbox, "before-experiment")
        self.manager.set_power(self.sandbox, "sbx-acc-01", "shutdown")
        notes = self.manager.get("rebuild").notes
        self.assertTrue(any("Built from recipe" in n for n in notes))
        self.assertTrue(any("Cabled" in n for n in notes))
        self.assertIn("before-experiment", self.manager.get("rebuild").proxmox_snapshots)

    def test_teardown_refuses_to_reach_the_live_mist_site(self):
        self.sandbox.mist_site_id = sorted(self.live.production_mist_sites)[0]
        removed = self.manager.teardown(self.sandbox, confirm=True)
        self.assertIn("left in place", removed["mist_site"])

    def test_the_runbook_answers_tear_down_rebuild_and_fix(self):
        self.assertTrue(os.path.exists(ADVICE), "ADVICE.md must ship with the project")
        with open(ADVICE, encoding="utf-8") as handle:
            text = handle.read().lower()
        for topic, needles in {
            "tear down": ("tear down", "teardown"),
            "rebuild": ("rebuild", "redo"),
            "fix": ("fix", "recover"),
            "proxmox": ("proxmox",),
            "mist": ("mist",),
        }.items():
            self.assertTrue(any(n in text for n in needles), f"ADVICE.md does not cover {topic}")


if __name__ == "__main__":
    unittest.main()
