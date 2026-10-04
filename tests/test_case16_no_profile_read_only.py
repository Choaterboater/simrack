"""Case 16: with no lab profile SimRack cannot tell what is live, so it changes nothing.

That holds whatever the Proxmox and Mist tokens may do, and the status view says
why, so the fix is one look away. The old SIMRACK_ALLOW_WRITES and
SIMRACK_MIST_WRITES switches decide nothing any more: the tokens do.
"""

from __future__ import annotations

import json
import os
import shutil
import unittest
from unittest import mock

from simrack.config import PROFILE_FILE, TOKENS_FILE, Settings
from simrack.errors import GuardrailViolation
from simrack.service import SandboxManager
from tests.fakes import PVE_AUDITOR, FakeMist, FakeProxmox, TempDir

EXAMPLE = os.path.join(os.path.dirname(__file__), os.pardir, "lab-profile.example.toml")
EXAMPLE_ORG = "00000000-0000-0000-0000-000000000001"


class TestNoProfileMeansReadOnly(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def manager(self, *, profile: str = "", environ: dict | None = None, **fakes) -> SandboxManager:
        """A manager with both tokens set up, and the lab profile only if one is given."""
        if profile:
            shutil.copyfile(profile, os.path.join(self.tmp, PROFILE_FILE))
        with open(os.path.join(self.tmp, TOKENS_FILE), "w", encoding="utf-8") as handle:
            json.dump({"proxmox": "p", "mist": "t"}, handle)
        settings = Settings.load({**(environ or {}), "SIMRACK_STATE_DIR": self.tmp})
        return SandboxManager(settings, proxmox=fakes.get("proxmox") or FakeProxmox(), mist=fakes.get("mist"))

    def test_the_status_view_says_why_it_is_read_only(self):
        state = self.manager(mist=FakeMist()).state()

        self.assertFalse(state["writes_enabled"])
        self.assertFalse(state["mist"]["writes_enabled"])
        self.assertFalse(state["profile"]["loaded"])
        self.assertIn("lab profile", state["read_only_reason"])
        self.assertIn("lab profile", state["mist"]["read_only_reason"])

    def test_every_write_is_refused_naming_the_missing_profile(self):
        manager = self.manager()

        with self.assertRaises(GuardrailViolation) as caught:
            manager.create_sandbox("demo", "single-switch", template_vmid=320)
        self.assertIn("lab profile", caught.exception.message)
        self.assertEqual(manager.proxmox.calls, [], "nothing reached Proxmox")

        with mock.patch("urllib.request.urlopen") as sent, self.assertRaises(GuardrailViolation) as caught:
            manager.mist.create_site("demo")
        self.assertIn("lab profile", caught.exception.message)
        sent.assert_not_called()

    def test_with_a_profile_the_reason_moves_on_to_the_token(self):
        state = self.manager(profile=EXAMPLE, proxmox=FakeProxmox(privileges=PVE_AUDITOR), mist=FakeMist(org_id=EXAMPLE_ORG)).state()

        self.assertEqual(state["profile"], {"loaded": True, "path": os.path.join(self.tmp, PROFILE_FILE)})
        self.assertFalse(state["writes_enabled"])
        self.assertIn("Proxmox token may not change the lab", state["read_only_reason"])
        self.assertNotIn("lab profile", state["read_only_reason"])
        self.assertTrue(state["mist"]["writes_enabled"])

    def test_the_old_write_switches_decide_nothing(self):
        for switch in ({"SIMRACK_ALLOW_WRITES": "0", "SIMRACK_MIST_WRITES": "0"}, {"SIMRACK_ALLOW_WRITES": "1", "SIMRACK_MIST_WRITES": "1"}):
            with self.subTest(switch):
                writable = self.manager(profile=EXAMPLE, environ=switch, mist=FakeMist(org_id=EXAMPLE_ORG)).state()
                self.assertTrue(writable["writes_enabled"])
                self.assertTrue(writable["mist"]["writes_enabled"])
                look_only = self.manager(profile=EXAMPLE, environ=switch, proxmox=FakeProxmox(privileges=PVE_AUDITOR), mist=FakeMist(role="read", org_id=EXAMPLE_ORG)).state()
                self.assertFalse(look_only["writes_enabled"])
                self.assertFalse(look_only["mist"]["writes_enabled"])


if __name__ == "__main__":
    unittest.main()
