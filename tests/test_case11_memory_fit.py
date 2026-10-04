"""Case 11: memory, not a fixed count, decides what fits on the host.

Free memory is MemAvailable, read live from Proxmox, so a RAM upgrade needs no
code change. The size of one vJunos switch is set once on the server and the UI
reads it from /api/state. A powered-off switch costs nothing until it starts,
so every path that starts one checks memory first.
"""

from __future__ import annotations

import os
import unittest

from labfront.config import Settings
from labfront.errors import GuardrailViolation
from labfront.proxmox import ProxmoxClient
from tests.fakes import FakeProxmox, TempDir, make_manager

MIB = 1024 * 1024
STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "labfront", "static")


class StubProxmox(ProxmoxClient):
    def __init__(self, memory):
        super().__init__(Settings(pve_token="t"))
        self.memory = memory

    def node_status(self):
        return {"memory": self.memory}


class TestHostMemory(unittest.TestCase):
    def test_free_memory_is_mem_available_not_mem_free(self):
        # A real host's reading: 12.4 GB MemFree, 19.8 GB MemAvailable, 62 GB in all.
        client = StubProxmox({"free": 12452 * MIB, "available": 19850 * MIB, "total": 63734 * MIB, "used": 43883 * MIB})
        self.assertEqual(client.free_memory_mb(), 19850)
        self.assertEqual(client.memory_mb(), {"free": 19850, "total": 63734})

    def test_an_older_proxmox_without_available_falls_back_to_free(self):
        client = StubProxmox({"free": 12452 * MIB, "total": 63734 * MIB})
        self.assertEqual(client.free_memory_mb(), 12452)

    def test_a_garbled_reply_counts_as_no_free_memory(self):
        self.assertEqual(StubProxmox({"available": "lots"}).free_memory_mb(), 0)
        self.assertEqual(StubProxmox(None).memory_mb(), {"free": 0, "total": 0})


class TestOneSwitchSize(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_the_switch_size_is_set_once_and_drives_the_build_and_the_guard(self):
        px = FakeProxmox(free_mb=200000, templates=[320])
        manager = make_manager(self.tmp, proxmox=px, switch_mem_mb=6144)
        sandbox = manager.create_sandbox("sizes", "single-switch", template_vmid=320)
        self.assertEqual(px.vms[sandbox.nodes[0].vmid]["memory"], 6144)

        manager.guard.check_ram(6144 * 3, 2)  # two switches leave exactly the 6 GB reserve
        with self.assertRaises(GuardrailViolation):
            manager.guard.check_ram(6144 * 3 - 1, 2)

    def test_the_default_size_is_junipers_minimum_for_vjunos(self):
        self.assertEqual(Settings().switch_mem_mb, 5120)

    def test_state_reports_the_switch_size_and_host_total_and_no_cap(self):
        px = FakeProxmox(free_mb=19850, total_mb=63734)
        state = make_manager(self.tmp, proxmox=px).state()
        self.assertEqual(state["limits"]["switch_mem_mb"], 5120)
        self.assertNotIn("max_switches", state["limits"])
        self.assertEqual(state["host"]["free_ram_mb"], 19850)
        self.assertEqual(state["host"]["total_ram_mb"], 63734)

    def test_the_ui_takes_the_switch_size_from_the_server(self):
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        self.assertNotIn("5120", js)
        self.assertNotIn("max_switches", js)
        self.assertIn("switch_mem_mb", js)
        self.assertIn("total_ram_mb", js)


class TestStartingNeedsMemory(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.px = FakeProxmox(free_mb=40000, templates=[320])
        self.manager = make_manager(self.tmp, proxmox=self.px)
        self.sandbox = self.manager.create_sandbox("cold", "collapsed-core", template_vmid=320, start=False)
        self.name = self.sandbox.nodes[0].name

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def _power_calls(self):
        return [c for c in self.px.calls if c[0] in ("set_power", "rollback_snapshot")]

    def test_starting_a_powered_off_switch_checks_memory_first(self):
        self.px.free_mb = 8000
        self.px.calls.clear()
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.set_power(self.sandbox, self.name, "start")
        self.assertIn("free memory", str(caught.exception))
        self.assertEqual(self._power_calls(), [])

    def test_a_powered_off_switch_starts_when_memory_allows(self):
        self.assertEqual(self.manager.set_power(self.sandbox, self.name, "start")["status"], "running")

    def test_a_running_switch_needs_no_memory_to_start_again_or_to_stop(self):
        self.manager.set_power(self.sandbox, self.name, "start")
        self.px.free_mb = 100
        self.manager.set_power(self.sandbox, self.name, "start")
        self.assertEqual(self.manager.set_power(self.sandbox, self.name, "shutdown")["status"], "stopped")

    def test_revert_checks_memory_for_the_switches_it_will_start(self):
        self.manager.snapshot(self.sandbox, "clean")
        self.px.free_mb = 8000
        self.px.calls.clear()
        with self.assertRaises(GuardrailViolation):
            self.manager.revert(self.sandbox, "clean")
        self.assertEqual(self._power_calls(), [])

    def test_revert_of_running_switches_needs_no_extra_memory(self):
        for node in self.sandbox.nodes:
            self.manager.set_power(self.sandbox, node.name, "start")
        self.manager.snapshot(self.sandbox, "warm")
        self.px.free_mb = 100
        self.assertEqual(len(self.manager.revert(self.sandbox, "warm")["nodes"]), len(self.sandbox.nodes))


if __name__ == "__main__":
    unittest.main()
