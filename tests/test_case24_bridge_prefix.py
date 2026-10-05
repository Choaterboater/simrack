"""Case 24: sandbox bridges follow the lab profile's bridge_prefix.

Linux caps an interface name at 15 characters. A cable bridge is
<prefix><vmid>_<vmid>_<ports>, so the prefix and the sandbox vmids share that
budget. The park bridge is the prefix and a word, so it is never a cable's name.
"""

from __future__ import annotations

import dataclasses
import os
import subprocess
import unittest
from unittest import mock

from simrack import hostnet
from simrack.config import Settings
from simrack.errors import GuardrailViolation
from simrack.proxmox import ProxmoxClient
from tests.fakes import LAB_PROFILE, FakeProxmox, TempDir, make_manager

CABLE = r"^rk[0-9]+_[0-9]+_[0-9]{2}$"


class TestSandboxBridgesFollowThePrefix(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def build(self, sandbox_keys: str, shape: str = "collapsed-core", **overrides):
        with open(LAB_PROFILE, encoding="utf-8") as handle:
            text = handle.read()
        profile = os.path.join(self.tmp, "profile-in.toml")
        with open(profile, "w", encoding="utf-8") as handle:
            handle.write(f"{text}\n[sandbox]\n{sandbox_keys}\n")
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px, profile=profile, **overrides)
        return self.manager.create_sandbox("pre", shape, template_vmid=320)

    def test_cables_and_the_park_bridge_carry_the_prefix(self):
        sandbox = self.build('bridge_prefix = "rk"')
        self.assertTrue(sandbox.links)
        for link in sandbox.links:
            self.assertRegex(link.bridge, CABLE)
        self.assertEqual(sorted(self.px.networks), sorted([link.bridge for link in sandbox.links] + ["rkpark"]))
        link = sandbox.links[0]
        net = int(link.a_port.split("/")[-1]) + 1
        self.assertTrue(self.px.passes_lacp(sandbox.node(link.a_node).vmid, net), "LACP opens on a prefixed bridge")

        self.manager.teardown(sandbox)
        self.assertEqual(self.px.networks, {}, "teardown removes every bridge the sandbox made")

    def test_four_digit_vmids_cable(self):
        sandbox = self.build("vmids = [1000, 1099]\nlxc = [1100, 1149]")
        self.assertTrue(sandbox.links)
        for link in sandbox.links:
            self.assertRegex(link.bridge, r"^sbx1[0-9]{3}_1[0-9]{3}_[0-9]{2}$")

    def test_a_bridge_name_past_the_linux_limit_is_refused_in_words(self):
        """The profile refuses a prefix this long; settings made any other way meet this."""
        with self.assertRaises(GuardrailViolation) as caught:
            self.build('bridge_prefix = "rk"', sandbox_bridge_prefix="sandboxes", park_bridge="sandboxespark")
        self.assertIn("15", caught.exception.message)
        self.assertIn("bridge_prefix", caught.exception.detail)


class TestTheHostOnlyTouchesPrefixedBridges(unittest.TestCase):
    """hostnet runs ip(8) as root, so it checks every name itself."""

    def setUp(self):
        self.ran: list[list[str]] = []

        def run(argv, **_):
            self.ran.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "", "")

        for patch in (
            mock.patch.object(hostnet.subprocess, "run", side_effect=run),
            mock.patch.object(hostnet, "_write"),
            mock.patch.object(hostnet, "exists", return_value=False),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        self.client = ProxmoxClient(dataclasses.replace(Settings(), sandbox_bridge_prefix="rk", park_bridge="rkpark"))

    def test_it_makes_bridges_named_with_the_prefix(self):
        for name in ("rk321_322_12", "rkpark"):
            with self.subTest(name=name):
                self.client.create_bridge(name, mtu=9216)
                self.assertIn(["ip", "link", "add", "name", name, "mtu", "9216", "type", "bridge", "stp_state", "0"], self.ran)

    def test_it_refuses_every_other_name(self):
        for name in ("vmbr0", "sbx321_322_12", "rk", "rk-1", "rk321_322_123456", ""):
            with self.subTest(name=name):
                with self.assertRaises(GuardrailViolation):
                    self.client.create_bridge(name, mtu=9216)
                with self.assertRaises(GuardrailViolation):
                    self.client.delete_bridge(name)
        self.assertEqual(self.ran, [], "ip(8) never ran")

    def test_no_prefix_means_no_bridge(self):
        self.client.use(dataclasses.replace(self.client.settings, sandbox_bridge_prefix=""))
        with self.assertRaises(GuardrailViolation):
            self.client.create_bridge("sbx321_322_12", mtu=9216)
        self.assertEqual(self.ran, [])

    def test_lacp_opens_only_on_a_tap_in_a_prefixed_bridge(self):
        for bridge, opened in (("rk321_322_12", True), ("sbx321_322_12", False), ("vmbr0", False)):
            with self.subTest(bridge=bridge), mock.patch.object(hostnet.os.path, "isdir", return_value=True), mock.patch.object(
                hostnet.os.path, "realpath", return_value=f"/sys/devices/virtual/net/{bridge}"
            ):
                self.assertEqual(self.client.tune_port(321, 3), opened)


if __name__ == "__main__":
    unittest.main()
