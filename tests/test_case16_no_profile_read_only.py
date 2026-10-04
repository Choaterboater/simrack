"""Case 16: with no lab profile SimRack cannot tell what is live, so it changes nothing.

Writes stay off even when the operator set SIMRACK_ALLOW_WRITES=1, Mist writes
too, and the status view says why, so the fix is one look away.
"""

from __future__ import annotations

import os
import unittest

from simrack.config import Settings
from simrack.errors import GuardrailViolation
from simrack.mist import MistClient
from simrack.service import SandboxManager
from tests.fakes import FakeProxmox, TempDir

EXAMPLE = os.path.join(os.path.dirname(__file__), os.pardir, "lab-profile.example.toml")
WANTS_WRITES = {"SIMRACK_ALLOW_WRITES": "1", "SIMRACK_MIST_WRITES": "1", "MIST_TOKEN": "t", "SIMRACK_PVE_TOKEN": "p"}


class TestNoProfileMeansReadOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def manager(self, environ: dict) -> SandboxManager:
        settings = Settings.from_env({**environ, "SIMRACK_STATE_DIR": self.tmp})
        return SandboxManager(settings, proxmox=FakeProxmox(), mist=MistClient(settings))

    def test_the_status_view_says_why_it_is_read_only(self):
        state = self.manager(WANTS_WRITES).state()

        self.assertFalse(state["writes_enabled"])
        self.assertFalse(state["mist"]["writes_enabled"])
        self.assertFalse(state["profile"]["loaded"])
        self.assertIn("SIMRACK_PROFILE", state["read_only_reason"])

    def test_every_write_is_refused_naming_the_missing_profile(self):
        manager = self.manager(WANTS_WRITES)

        with self.assertRaises(GuardrailViolation) as caught:
            manager.create_sandbox("demo", "single-switch", template_vmid=320)
        self.assertIn("lab profile", caught.exception.message)
        self.assertIn("SIMRACK_PROFILE", caught.exception.detail)
        self.assertEqual(manager.proxmox.calls, [], "nothing reached Proxmox")

        with self.assertRaises(GuardrailViolation) as caught:
            manager.mist.create_site("demo")
        self.assertIn("lab profile", caught.exception.message)

    def test_a_profile_lets_the_operator_switch_writes_on(self):
        state = self.manager({**WANTS_WRITES, "SIMRACK_PROFILE": EXAMPLE}).state()

        self.assertTrue(state["writes_enabled"])
        self.assertTrue(state["mist"]["writes_enabled"])
        self.assertEqual(state["profile"], {"loaded": True, "path": EXAMPLE})
        self.assertEqual(state["read_only_reason"], "")

    def test_with_a_profile_but_writes_off_the_reason_is_the_switch(self):
        state = self.manager({"SIMRACK_PROFILE": EXAMPLE}).state()

        self.assertFalse(state["writes_enabled"])
        self.assertIn("SIMRACK_ALLOW_WRITES=1", state["read_only_reason"])
        self.assertNotIn("SIMRACK_PROFILE", state["read_only_reason"])


if __name__ == "__main__":
    unittest.main()
