"""Case 25: the systemd unit lets SimRack write only its own state.

SimRack runs as root (ip link, the adoption console socket), so the unit makes
the host read-only to it: ProtectSystem=strict, with ReadWritePaths opening the
state folder alone. /sys stays writable under strict, for the bridge settings,
and a unix socket still connects on a read-only mount.
"""

import re
import unittest
from pathlib import Path

from simrack.config import STATE_DIR

UNIT = Path(__file__).resolve().parents[1] / "deploy" / "simrack.service"


class TestTheServiceUnit(unittest.TestCase):
    def setUp(self):
        self.settings = dict(re.findall(r"^(\w+)=(.*)$", UNIT.read_text(encoding="utf-8"), re.M))

    def test_the_host_is_read_only_but_for_simracks_state(self):
        self.assertEqual(self.settings.get("ProtectSystem"), "strict")
        self.assertEqual(self.settings.get("ReadWritePaths"), STATE_DIR)

    def test_sys_stays_writable_for_the_bridge_settings(self):
        self.assertNotIn("ProtectKernelTunables", self.settings)
