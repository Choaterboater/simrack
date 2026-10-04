"""Case 12: build a sandbox from a shape and adopt its switches (the "run" stage).

A shape imported from Mist becomes a sandbox: one vJunos per ticked switch,
cabled port for port like the live fabric, with every unused port parked on a
dead bridge. Each sandbox gets its own Mist site and its own root password, and
a switch joins that site over its serial console, so nobody has to type the
outbound-ssh lines by hand.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from simrack.api import serve
from simrack.config import Settings
from simrack.console import CLI, INCORRECT, LOGIN, SerialConsole, Session, adopt
from simrack.errors import BackendError, GuardrailViolation, LabError, NotConfigured, NotFound
from simrack.mist import MistClient
from simrack.service import _bridge_name
from simrack.shapes import plan_build, shape_from_mist
from tests.fakes import FakeConsole, FakeMist, FakeProxmox, TempDir, make_manager, mist_may_only_read, proxmox_may_only_look, read_state
from tests.test_case9_shapes import LIVE_CABLES, bundle

PASSWORD_SHAPE = r"^[A-HJ-NP-Za-km-z2-9]{4}(?:-[A-HJ-NP-Za-km-z2-9]{4}){3}$"
PARKED = "bridge=sbxpark,firewall=0,link_down=1"
LINES = [line.strip() for line in FakeMist.ADOPT_CMD.splitlines() if line.strip() and not line.strip().startswith("#")]
IMAGE = "local:import/vJunos-switch-26.2R1.7.qcow2"


def cabled(sandbox) -> set:
    return {frozenset(link.endpoints()) for link in sandbox.links}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.px = FakeProxmox(free_mb=60000, templates=[320])
        self.mist = FakeMist()
        self.manager = self.restart()

    def restart(self):
        return make_manager(self.tmp, proxmox=self.px, mist=self.mist, serial_dir=os.path.join(self.tmp, "run"))


class TestSwitchPorts(Base):
    """Every switch has fxp0 plus ge-0/0/0-9, and a port with no cable is parked."""

    def test_an_image_build_gets_management_and_ten_parked_ports(self):
        sandbox = self.manager.create_sandbox("img", "single-switch", image=IMAGE)
        vmid = sandbox.node("sbx-acc-01").vmid
        self.assertEqual(vmid, 321)
        nics = self.px.nics(vmid)
        self.assertEqual(sorted(nics, key=lambda key: int(key[3:])), [f"net{i}" for i in range(11)])
        self.assertEqual(nics["net0"], f"virtio={self.px.mac(vmid, 0)},bridge=vmbr0,tag=5")
        for i in range(1, 11):
            self.assertEqual(nics[f"net{i}"], f"virtio={self.px.mac(vmid, i)},{PARKED}")
        self.assertEqual(self.px.called("create_bridge")[0], ("create_bridge", ("sbxpark",), {"mtu": 9216}))
        order = [call[0] for call in self.px.calls]
        self.assertLess(order.index("create_bridge"), order.index("create_vm"))

    def test_a_clone_is_rewired_in_one_config_call(self):
        self.px.vms[320].update(
            {
                "net0": "virtio=02:00:00:AA:00:00,bridge=vmbr1",
                "net1": "virtio=02:00:00:AA:00:01,bridge=lab1,firewall=1",
                "net12": "e1000=02:00:00:AA:00:0C,bridge=vmbr0",
            }
        )
        self.manager.create_sandbox("cln", "single-switch", template_vmid=320)
        calls = self.px.config_calls_for(321)
        self.assertEqual(len(calls), 1, "one set_vm_config: memory, hookscript and every NIC together")
        fields = calls[0][2]
        self.assertEqual(fields["memory"], 5120)
        self.assertEqual(fields["net0"], f"virtio={self.px.mac(321, 0)},bridge=vmbr0,tag=5")
        self.assertEqual(fields["net1"], f"virtio={self.px.mac(321, 1)},{PARKED}")
        self.assertEqual(fields["net2"], f"virtio,{PARKED}")
        self.assertIn("net12", fields)
        self.assertIsNone(fields["net12"])
        nics = self.px.nics(321)
        self.assertEqual(sorted(nics, key=lambda key: int(key[3:])), [f"net{i}" for i in range(11)])
        macs = [value.split(",")[0] for value in nics.values()]
        self.assertEqual(len(macs), len(set(macs)))

    def test_a_client_keeps_its_single_management_nic(self):
        sandbox = self.manager.create_sandbox("cli", "single-switch", template_vmid=320)
        node = self.manager.provision_node(sandbox, "sbx-cli-01", role="client", kind="client", template_vmid=320)
        self.assertEqual(self.px.nics(node.vmid), {"net0": "virtio=02:00:00:CC:00:00,bridge=vmbr0,tag=5"})

    def test_unplugging_parks_both_ends_and_keeps_their_macs(self):
        sandbox = self.manager.create_sandbox("rmv", "collapsed-core", template_vmid=320)
        link = sandbox.links[0]
        self.manager.remove_cable(sandbox, link.bridge)
        for name in ("sbx-core-01", "sbx-acc-01"):
            vmid = sandbox.node(name).vmid
            self.assertEqual(self.px.nics(vmid)["net3"], f"virtio={self.px.mac(vmid, 3)},{PARKED}")
        self.assertNotIn(link.bridge, self.px.networks)
        self.assertIn("sbxpark", self.px.networks)

    def test_moving_an_end_along_the_same_switch_parks_the_old_port(self):
        sandbox = self.manager.create_sandbox("mov", "collapsed-core", template_vmid=320)
        link = sandbox.links[0]
        core = sandbox.node("sbx-core-01").vmid
        self.manager.move_cable(sandbox, link.bridge, "sbx-core-01", "ge-0/0/5")
        nics = self.px.nics(core)
        self.assertEqual(nics["net3"], f"virtio={self.px.mac(core, 3)},{PARKED}")
        self.assertTrue(nics["net6"].startswith(f"virtio={self.px.mac(core, 6)},bridge={link.bridge},"), nics["net6"])
        self.assertNotIn("link_down", nics["net6"])
        macs = [value.split(",")[0] for value in nics.values()]
        self.assertEqual(len(macs), len(set(macs)))

    def test_a_cabled_port_is_always_virtio_and_keeps_its_mac(self):
        sandbox = self.manager.create_sandbox("vio", "collapsed-core", template_vmid=320)
        acc = sandbox.node("sbx-acc-01").vmid
        self.px.vms[acc]["net5"] = "e1000=02:00:00:00:00:05,bridge=sbxpark,firewall=0,link_down=1"
        link = self.manager.cable(sandbox, "sbx-core-01", "ge-0/0/4", "sbx-acc-01", "ge-0/0/4")
        self.assertEqual(self.px.nics(acc)["net5"], f"virtio=02:00:00:00:00:05,bridge={link.bridge},firewall=0")

    def test_ports_past_ge_0_0_9_are_refused_before_proxmox_is_touched(self):
        sandbox = self.manager.create_sandbox("rng", "collapsed-core", template_vmid=320)
        self.px.calls.clear()
        for a_port, b_port in (("ge-0/0/10", "ge-0/0/4"), ("ge-0/0/4", "ge-0/0/12")):
            with self.subTest(a_port=a_port, b_port=b_port):
                with self.assertRaises(GuardrailViolation) as caught:
                    self.manager.cable(sandbox, "sbx-core-01", a_port, "sbx-acc-01", b_port)
                self.assertIn("ge-0/0/9", caught.exception.message)
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.move_cable(sandbox, sandbox.links[0].bridge, "sbx-core-01", "ge-0/0/10")
        self.assertIn("ge-0/0/9", caught.exception.message)
        self.assertEqual(self.px.calls, [])

    def test_a_bridge_name_does_not_depend_on_which_end_is_named_first(self):
        self.assertEqual(_bridge_name(321, 322, "ge-0/0/2", "ge-0/0/3"), "sbx321_322_23")
        self.assertEqual(_bridge_name(322, 321, "ge-0/0/3", "ge-0/0/2"), "sbx321_322_23")
        self.assertEqual(_bridge_name(322, 321, "ge-0/0/2", "ge-0/0/3"), "sbx321_322_32")

    def test_two_sandboxes_get_their_own_addresses_and_site_names(self):
        a = self.manager.create_sandbox("cust-a", "collapsed-core", template_vmid=320, with_mist_site=True)
        b = self.manager.create_sandbox("cust-b", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.assertEqual(
            [n.mgmt_ip for n in a.nodes + b.nodes],
            ["192.0.2.200", "192.0.2.201", "192.0.2.202", "192.0.2.203"],
        )
        self.assertEqual((a.mist_site_name, b.mist_site_name), ("Sandbox cust-a", "Sandbox cust-b"))
        self.assertEqual((a.recipe.site_name, b.recipe.site_name), ("Sandbox cust-a", "Sandbox cust-b"))
        self.assertEqual(self.mist.site_records[b.mist_site_id]["name"], "Sandbox cust-b")

    def test_the_park_bridge_is_shared_and_goes_with_the_last_sandbox(self):
        a = self.manager.create_sandbox("pka", "single-switch", template_vmid=320)
        b = self.manager.create_sandbox("pkb", "single-switch", template_vmid=320)
        parks = [call for call in self.px.called("create_bridge") if call[1][0] == "sbxpark"]
        self.assertEqual(parks, [("create_bridge", ("sbxpark",), {"mtu": 9216})])
        first = self.manager.teardown(a)
        self.assertTrue(first["complete"])
        self.assertIn("sbxpark", self.px.networks)
        self.assertNotIn("sbxpark", first["bridges"])
        second = self.manager.teardown(b)
        self.assertNotIn("sbxpark", self.px.networks)
        self.assertNotIn("sbxpark", second["bridges"])

    def test_a_reboot_brings_back_the_park_bridge_first(self):
        self.assertEqual(self.manager.ensure_bridges(), [])
        sandbox = self.manager.create_sandbox("ebr", "collapsed-core", template_vmid=320)
        self.px.networks.clear()
        self.assertEqual(self.manager.ensure_bridges(), ["sbxpark", sandbox.links[0].bridge])

    def _fail_clone(self, at):
        original = self.px.clone_vm
        count = {"n": 0}

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] == at:
                raise BackendError("Proxmox API POST /qemu/320/clone failed (500).")
            return original(*args, **kwargs)

        self.px.clone_vm = flaky

    def test_a_failed_build_drops_the_park_bridge_it_made(self):
        self._fail_clone(at=2)
        with self.assertRaises(BackendError):
            self.manager.create_sandbox("fail", "collapsed-core", template_vmid=320)
        self.assertNotIn("sbxpark", self.px.networks)

    def test_a_failed_build_keeps_the_park_bridge_others_use(self):
        self.manager.create_sandbox("keep", "single-switch", template_vmid=320)
        self._fail_clone(at=2)
        with self.assertRaises(BackendError):
            self.manager.create_sandbox("fail", "collapsed-core", template_vmid=320)
        self.assertIn("sbxpark", self.px.networks)


class TestPlanBuild(unittest.TestCase):
    """Which switches to build and which cables survive the selection."""

    def setUp(self):
        self.shape = shape_from_mist(bundle(), now="2026-10-01T00:00:00Z")

    def test_the_whole_shape_is_a_copy_with_every_cable(self):
        plan = plan_build(self.shape)
        self.assertEqual([n["name"] for n in plan["nodes"]], [n["name"] for n in self.shape["nodes"]])
        self.assertEqual(plan["links"], self.shape["links"])
        self.assertEqual(plan["dropped"], [])
        plan["nodes"][0]["name"] = "changed"
        plan["links"][0]["a_port"] = "changed"
        self.assertEqual(self.shape["nodes"][0]["name"], "sbx-bl-01")
        self.assertNotEqual(self.shape["links"][0]["a_port"], "changed")

    def test_a_slice_keeps_shape_order_and_says_what_it_left_out(self):
        plan = plan_build(self.shape, ["sbx-acc-01", "sbx-core-01"])
        self.assertEqual([n["name"] for n in plan["nodes"]], ["sbx-core-01", "sbx-acc-01"])
        self.assertEqual(
            [(link["a_node"], link["a_port"], link["b_node"], link["b_port"]) for link in plan["links"]],
            [("sbx-core-01", "ge-0/0/2", "sbx-acc-01", "ge-0/0/0")],
        )
        self.assertEqual(
            [(d["a_node"], d["b_node"], d["reason"]) for d in plan["dropped"]],
            [
                ("sbx-bl-01", "sbx-core-01", "sbx-bl-01 not built"),
                ("sbx-bl-02", "sbx-core-01", "sbx-bl-02 not built"),
                ("sbx-core-01", "sbx-acc-02", "sbx-acc-02 not built"),
                ("sbx-core-02", "sbx-acc-01", "sbx-core-02 not built"),
            ],
        )

    def test_naming_a_switch_twice_builds_it_once(self):
        plan = plan_build(self.shape, ["sbx-acc-01", "sbx-acc-01"])
        self.assertEqual([n["name"] for n in plan["nodes"]], ["sbx-acc-01"])

    def test_bad_selections_are_refused(self):
        for switches, words in ((["sbx-acc-09"], "not in shape"), ([], "at least one"), ("sbx-acc-01", "list of"), ([1], "list of")):
            with self.subTest(switches=switches):
                with self.assertRaises(LabError) as caught:
                    plan_build(self.shape, switches)
                self.assertIn(words, caught.exception.message)

    def test_cables_vjunos_cannot_carry_are_left_out_with_a_reason(self):
        def link(a, ap, b, bp):
            return {"a_node": a, "a_port": ap, "b_node": b, "b_port": bp, "via": "lldp"}

        tiny = {
            "name": "tiny",
            "nodes": [{"name": n, "role": "access", "pod": None, "from": n, "ports": []} for n in ("sbx-a-01", "sbx-b-01", "sbx-c-01")],
            "links": [
                link("sbx-a-01", "ge-0/0/0", "sbx-b-01", "ge-0/0/0"),
                link("sbx-a-01", "ge-0/0/0", "sbx-c-01", "ge-0/0/1"),
                link("sbx-b-01", "ge-0/0/5", "sbx-c-01", "ge-0/0/2"),
                link("sbx-b-01", "et-0/0/50", "sbx-c-01", "ge-0/0/3"),
                link("sbx-c-01", "ge-0/0/1", "sbx-c-01", "ge-0/0/2"),
            ],
        }
        plan = plan_build(tiny, ports=4)
        self.assertEqual(len(plan["links"]), 1)
        self.assertEqual(
            [d["reason"] for d in plan["dropped"]],
            [
                "sbx-a-01 ge-0/0/0 already cabled",
                "ge-0/0/5 is past ge-0/0/3",
                "et-0/0/50 is not a sandbox port",
                "sbx-c-01 is cabled to itself",
            ],
        )


class TestBuildFromShape(Base):
    def setUp(self):
        super().setUp()
        self.shape = self.manager.import_shape(bundle())

    def test_the_whole_shape_builds_cabled_like_the_live_fabric(self):
        result = self.manager.build_from_shape("campus-ip-clos", "park-a", template_vmid=320)
        sandbox = result["sandbox"]
        self.assertEqual(result["dropped"], [])
        self.assertEqual([n.name for n in sandbox.nodes], [n["name"] for n in self.shape["nodes"]])
        self.assertEqual([n.vmid for n in sandbox.nodes], list(range(321, 327)))
        self.assertEqual([(n.role, n.pod) for n in sandbox.nodes], [(n["role"], n["pod"]) for n in self.shape["nodes"]])
        self.assertEqual(cabled(sandbox), LIVE_CABLES)
        for link in sandbox.links:
            for name, port in link.endpoints():
                nic = self.px.nics(sandbox.node(name).vmid)[f"net{int(port.split('/')[-1]) + 1}"]
                self.assertIn(f"bridge={link.bridge},", nic)
        core = sandbox.node("sbx-core-01").vmid
        for i in range(5, 11):
            self.assertIn(PARKED, self.px.nics(core)[f"net{i}"])
        recipe = sandbox.recipe
        self.assertEqual(
            (recipe.shape, recipe.topology_kind, recipe.routed_at, recipe.overlay_as, recipe.underlay_as_base),
            ("campus-ip-clos", "IP Clos", "edge", 65000, 65001),
        )
        self.assertEqual((sandbox.mist_site_name, recipe.site_name), ("Sandbox park-a", "Sandbox park-a"))
        self.assertIsNone(sandbox.mist_site_id)
        self.assertIn("Built from shape campus-ip-clos (6 of 6 switches).", sandbox.notes)

        again = self.restart().get("park-a")
        self.assertEqual([n.pod for n in again.nodes], [n.pod for n in sandbox.nodes])
        self.assertEqual(again.recipe.shape, "campus-ip-clos")
        self.assertEqual(len(again.links), 8)

    def test_a_slice_builds_what_was_ticked_and_notes_what_was_left_out(self):
        result = self.manager.build_from_shape("campus-ip-clos", "slice", switches=["sbx-core-01", "sbx-acc-01"], template_vmid=320)
        sandbox = result["sandbox"]
        self.assertEqual(len(sandbox.links), 1)
        self.assertEqual(len(result["dropped"]), 4)
        description = "Built from shape campus-ip-clos (2 of 6 switches)."
        self.assertEqual(sandbox.recipe.description, description)
        self.assertIn(description, sandbox.notes)
        left = [note for note in sandbox.notes if note.startswith("Left out 4 cable(s):")]
        self.assertEqual(len(left), 1)
        self.assertIn("sbx-bl-01 ge-0/0/0 - sbx-core-01 ge-0/0/0 (sbx-bl-01 not built)", left[0])

    def test_a_shape_too_big_for_memory_is_refused_before_anything_is_made(self):
        self.px.free_mb = 20000
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.build_from_shape("campus-ip-clos", "big", template_vmid=320)
        self.assertIn("Not enough free memory", caught.exception.message)
        self.assertEqual(self.px.calls, [])
        self.assertNotIn("big", self.manager.sandboxes)

    def test_bad_requests_are_refused_before_anything_is_made(self):
        self.manager.create_sandbox("taken", "single-switch", template_vmid=320)
        self.px.calls.clear()
        cases = [
            ("bad name", dict(name="Bad Name", template_vmid=320), ValueError, ""),
            ("taken", dict(name="taken", template_vmid=320), GuardrailViolation, "already exists"),
            ("unknown shape", dict(shape="nope", template_vmid=320), NotFound, ""),
            ("unknown switch", dict(switches=["sbx-acc-09"], template_vmid=320), LabError, "not in shape"),
            ("no source", dict(), LabError, "template or an image"),
            ("live guest", dict(template_vmid=204), GuardrailViolation, "live lab guest"),
            ("live disk", dict(image="local:images/vm-204-disk-0.qcow2"), GuardrailViolation, "not a bootable image"),
        ]
        for label, kwargs, error, words in cases:
            with self.subTest(label):
                shape = kwargs.pop("shape", "campus-ip-clos")
                name = kwargs.pop("name", "fresh")
                with self.assertRaises(error) as caught:
                    self.manager.build_from_shape(shape, name, **kwargs)
                if words:
                    self.assertIn(words, caught.exception.message)
        with self.subTest("read-only"), proxmox_may_only_look(self.manager):
            with self.assertRaises(GuardrailViolation):
                self.manager.build_from_shape("campus-ip-clos", "fresh", template_vmid=320)
        self.assertEqual(self.px.calls, [])
        self.assertEqual(sorted(self.manager.sandboxes), ["taken"])

    def test_a_failed_build_leaves_nothing_behind(self):
        original = self.px.clone_vm
        count = {"n": 0}

        def flaky(*args, **kwargs):
            count["n"] += 1
            if count["n"] == 3:
                raise BackendError("Proxmox API POST /qemu/320/clone failed (500).")
            return original(*args, **kwargs)

        self.px.clone_vm = flaky
        with self.assertRaises(BackendError):
            self.manager.build_from_shape("campus-ip-clos", "half", template_vmid=320)
        self.assertEqual(sorted(self.px.vms), [320])
        self.assertEqual(self.px.networks, {})
        self.assertNotIn("half", self.manager.sandboxes)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "sandboxes", "half.json")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "secrets", "half.json")))

    def test_a_build_can_make_its_own_mist_site(self):
        result = self.manager.build_from_shape("campus-ip-clos", "with-site", switches=["sbx-acc-01"], template_vmid=320, with_mist_site=True)
        sandbox = result["sandbox"]
        self.assertEqual(self.mist.site_records[sandbox.mist_site_id]["name"], "Sandbox with-site")
        self.assertEqual(read_state(self.manager, "with-site")["mist_site_id"], sandbox.mist_site_id)

    def test_an_image_build_boots_nothing_when_asked_not_to(self):
        self.manager.build_from_shape("campus-ip-clos", "img", switches=["sbx-core-01", "sbx-acc-01"], image=IMAGE, start=False)
        made = self.px.called("create_vm")
        self.assertEqual([call[1][1] for call in made], ["sbx-core-01", "sbx-acc-01"])
        self.assertTrue(all(call[2]["import_from"] == IMAGE for call in made))
        self.assertEqual(self.px.called("clone_vm"), [])
        self.assertEqual(self.px.called("set_power"), [])


class TestRootPassword(Base):
    def test_each_sandbox_gets_its_own_strong_root_password(self):
        a = self.manager.create_sandbox("pw-a", "single-switch", template_vmid=320)
        self.manager.import_shape(bundle())
        b = self.manager.build_from_shape("campus-ip-clos", "pw-b", switches=["sbx-acc-01"], template_vmid=320)["sandbox"]
        one = self.manager.reveal_root_password(a)
        two = self.manager.reveal_root_password(b)
        for secret in (one, two):
            self.assertEqual(secret["user"], "root")
            password = secret["root_password"]
            self.assertRegex(password, PASSWORD_SHAPE)
            for kind in ("[A-Z]", "[a-z]", "[0-9]"):
                self.assertRegex(password, kind)
        self.assertNotEqual(one["root_password"], two["root_password"])
        folder = os.path.join(self.tmp, "secrets")
        self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, "pw-a.json")).st_mode), 0o600)

    def test_the_password_never_reaches_state_or_the_api(self):
        sandbox = self.manager.create_sandbox("pwd", "single-switch", template_vmid=320)
        password = self.manager.reveal_root_password(sandbox)["root_password"]
        with open(os.path.join(self.tmp, "sandboxes", "pwd.json"), encoding="utf-8") as handle:
            self.assertNotIn(password, handle.read())
        self.assertNotIn(password, json.dumps(self.manager.state(), default=str))
        self.assertNotIn(password, json.dumps(sandbox.to_dict(), default=str))

    def test_a_missing_password_is_made_once_and_then_kept(self):
        sandbox = self.manager.create_sandbox("pwd", "single-switch", template_vmid=320)
        os.remove(os.path.join(self.tmp, "secrets", "pwd.json"))
        first = self.manager.reveal_root_password(sandbox)["root_password"]
        self.assertRegex(first, PASSWORD_SHAPE)
        self.assertEqual(self.manager.reveal_root_password(sandbox)["root_password"], first)
        again = self.restart()
        self.assertEqual(again.reveal_root_password(again.get("pwd"))["root_password"], first)

    def test_reveal_works_read_only_and_calls_nothing(self):
        sandbox = self.manager.create_sandbox("pwd", "single-switch", template_vmid=320)
        self.px.calls.clear()
        self.mist.calls.clear()
        with proxmox_may_only_look(self.manager):
            self.assertRegex(self.manager.reveal_root_password(sandbox)["root_password"], PASSWORD_SHAPE)
        self.assertEqual(self.px.calls, [])
        self.assertEqual(self.mist.calls, [])

    def test_the_password_outlives_a_stuck_teardown(self):
        sandbox = self.manager.create_sandbox("pwd", "single-switch", template_vmid=320)
        path = os.path.join(self.tmp, "secrets", "pwd.json")
        original = self.px.delete_vm

        def stuck(vmid, *, purge=True):
            raise BackendError(f"Proxmox API DELETE /qemu/{vmid} failed (500).")

        self.px.delete_vm = stuck
        self.assertFalse(self.manager.teardown(sandbox)["complete"])
        self.assertTrue(os.path.exists(path))
        self.px.delete_vm = original
        self.assertTrue(self.manager.teardown(sandbox)["complete"])
        self.assertFalse(os.path.exists(path))


class ScriptedPort:
    """Hands back fixed chunks of console output; time moves only on empty reads."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.now = 0.0

    def read(self, timeout):
        if self.chunks:
            return self.chunks.pop(0)
        self.now += timeout
        return ""

    def send(self, data):
        self.sent.append(data)

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class TestSession(unittest.TestCase):
    def test_a_carriage_return_split_across_reads_is_one_newline(self):
        session = Session(ScriptedPort(["abc\r", "\nlogin: "]))
        self.assertEqual(session.expect([LOGIN], 5), (0, "abc\nlogin: "))

    def test_colour_codes_split_across_reads_are_removed(self):
        session = Session(ScriptedPort(["\x1b[0", "1;32mroot@sw> "]))
        self.assertEqual(session.expect([CLI], 5), (0, "root@sw> "))

    def test_the_earliest_match_wins_and_the_rest_waits(self):
        session = Session(ScriptedPort(["Login incorrect\nlogin: "]))
        self.assertEqual(session.expect([LOGIN, INCORRECT], 5), (1, "Login incorrect"))
        self.assertEqual(session.expect([LOGIN, INCORRECT], 5), (0, "\nlogin: "))

    def test_a_tie_goes_to_the_first_pattern(self):
        session = Session(ScriptedPort(["root@sw> "]))
        self.assertEqual(session.expect([CLI, r"root"], 5), (0, "root@sw> "))

    def test_a_timeout_returns_what_arrived_and_clears_it(self):
        port = ScriptedPort(["partial output"])
        session = Session(port)
        self.assertEqual(session.expect([LOGIN], 3), (-1, "partial output"))
        self.assertEqual(session.drain(), "")
        self.assertGreaterEqual(port.now, 3)

    def test_send_ends_a_line_with_a_carriage_return(self):
        port = ScriptedPort([])
        Session(port).send("show version")
        self.assertEqual(port.sent, ["show version\r"])

    def test_control_characters_are_dropped(self):
        session = Session(ScriptedPort(["\x07\x00ro\x08ot@sw> "]))
        self.assertEqual(session.expect([CLI], 5), (0, "root@sw> "))


PASSWORD = "Abcd-Efgh-2345-Jkmn"
HOST = "sbx-acc-01"


class TestAdopt(unittest.TestCase):
    """The console conversation that turns a fresh vJunos into a Mist switch."""

    def test_a_fresh_switch_is_adopted(self):
        device = FakeConsole()
        self.assertEqual(adopt(device, HOST, PASSWORD, LINES), {"mgmt_ip": "192.0.2.37"})
        self.assertEqual(device.root_password, PASSWORD)
        self.assertEqual(device.host, HOST)
        self.assertEqual(
            device.config,
            [
                "set system services ssh root-login allow",
                f"set system host-name {HOST}",
                "set interfaces fxp0 unit 0 family inet dhcp",
                *LINES,
            ],
        )
        self.assertEqual(device.state, "login")
        self.assertIn("commit and-quit", device.lines)
        self.assertIn("delete chassis auto-image-upgrade", device.lines)

    def test_a_switch_that_already_has_our_password_logs_in(self):
        device = FakeConsole(root_password=PASSWORD)
        self.assertEqual(adopt(device, HOST, PASSWORD, LINES), {"mgmt_ip": "192.0.2.37"})
        self.assertIn(PASSWORD, device.lines)

    def test_an_unknown_password_stops_before_any_change(self):
        other = "Zzzz-Yyyy-9999-Xxxx"
        device = FakeConsole(root_password=other)
        with self.assertRaises(BackendError) as caught:
            adopt(device, HOST, PASSWORD, LINES)
        error = caught.exception
        self.assertIn("has a password SimRack doesn't know", error.message)
        self.assertIn("Reveal", error.detail)
        self.assertEqual((device.config, device.candidate), ([], []))
        text = f"{error} {error.message} {error.detail}"
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn(other, text)

    def test_a_switch_still_booting_does_not_answer(self):
        device = FakeConsole(booted=False)
        with self.assertRaises(BackendError) as caught:
            adopt(device, HOST, PASSWORD, LINES)
        self.assertIn("did not answer on its serial console", caught.exception.message)
        self.assertEqual(device.lines, [])

    def test_a_console_left_in_configuration_mode_is_not_touched(self):
        device = FakeConsole(start="config")
        with self.assertRaises(BackendError) as caught:
            adopt(device, HOST, PASSWORD, LINES)
        self.assertIn("configuration mode", caught.exception.message)
        self.assertEqual(device.lines, [""])

    def test_a_refused_mist_line_is_rolled_back_and_kept_secret(self):
        device = FakeConsole(fail_on="device-id")
        with self.assertRaises(BackendError) as caught:
            adopt(device, HOST, PASSWORD, LINES)
        error = caught.exception
        self.assertIn("refused Mist adoption line 2 of 3", error.message)
        self.assertIn("rolled the change back", error.detail)
        text = f"{error} {error.message} {error.detail}"
        for secret in ("fakeSecret", "$9$", "ABC123", PASSWORD):
            self.assertNotIn(secret, text)
        self.assertIn("rollback 0", device.lines)
        self.assertEqual(device.config, [])
        self.assertIsNone(device.root_password)
        self.assertEqual(device.state, "login")

    def test_a_refused_commit_is_rolled_back(self):
        device = FakeConsole(commit_fails=True)
        with self.assertRaises(BackendError) as caught:
            adopt(device, HOST, PASSWORD, LINES)
        self.assertIn("refused the commit", caught.exception.message)
        self.assertIn("rollback 0", device.lines)
        self.assertEqual(device.config, [])
        self.assertEqual(device.state, "login")

    def test_dhcp_is_polled_until_fxp0_has_an_address(self):
        device = FakeConsole(dhcp_after=2)
        self.assertEqual(adopt(device, HOST, PASSWORD, LINES), {"mgmt_ip": "192.0.2.37"})
        self.assertEqual(device.shows, 3)

    def test_no_dhcp_address_is_not_a_failure(self):
        device = FakeConsole(dhcp_ip=None)
        self.assertEqual(adopt(device, HOST, PASSWORD, LINES), {"mgmt_ip": None})
        self.assertEqual(device.shows, 3)
        self.assertEqual(device.root_password, PASSWORD)

    def test_a_console_already_in_the_cli_is_left_there(self):
        device = FakeConsole(start="cli")
        adopt(device, HOST, PASSWORD, LINES)
        self.assertEqual(device.state, "cli")
        self.assertNotIn("exit", device.lines)

    def test_a_console_at_the_shell_is_left_at_the_shell(self):
        device = FakeConsole(start="shell")
        adopt(device, HOST, PASSWORD, LINES)
        self.assertEqual(device.state, "shell")

    def test_a_half_typed_login_is_recovered(self):
        device = FakeConsole(start="password")
        self.assertEqual(adopt(device, HOST, PASSWORD, LINES), {"mgmt_ip": "192.0.2.37"})
        self.assertEqual(device.state, "login")


class TestSerialConsole(unittest.TestCase):
    """The QEMU serial0 socket on the Proxmox host."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lfsock-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, "s")

    def _serve(self, *, close_at_once=False):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(self.path)
        listener.listen(1)
        self.addCleanup(listener.close)
        received = bytearray()

        def run():
            conn, _ = listener.accept()
            with conn:
                if close_at_once:
                    return
                # The client may hang up without reading the banner. The write
                # then fails, or the stream ends in a reset instead of EOF, but
                # everything the client sent is already queued, so keep reading.
                try:
                    conn.sendall(b"login: ")
                except BrokenPipeError:
                    pass
                conn.settimeout(2)
                while True:
                    try:
                        data = conn.recv(4096)
                    except (socket.timeout, ConnectionResetError):
                        break
                    if not data:
                        break
                    received.extend(data)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, received

    def test_it_reads_and_writes_the_guest_serial_socket(self):
        thread, received = self._serve()
        with SerialConsole(self.path) as port:
            self.assertEqual(port.read(2), "login: ")
            port.send("root\r")
        thread.join(5)
        self.assertEqual(bytes(received), b"root\r")

    def test_long_lines_go_out_in_paced_chunks(self):
        thread, received = self._serve()
        with mock.patch("simrack.console.time.sleep") as pause:
            with SerialConsole(self.path) as port:
                port.send("x" * 300)
        thread.join(5)
        self.assertEqual(bytes(received), b"x" * 300)
        self.assertEqual(pause.call_args_list, [mock.call(0.05), mock.call(0.05)])

    def test_a_console_that_closes_is_a_backend_error(self):
        thread, _ = self._serve(close_at_once=True)
        with SerialConsole(self.path) as port:
            thread.join(5)
            with self.assertRaises(BackendError):
                port.read(2)

    def test_a_missing_socket_is_a_backend_error(self):
        with self.assertRaises(BackendError):
            SerialConsole(os.path.join(self.dir, "missing"))


class TestAdoptSwitch(Base):
    def setUp(self):
        super().setUp()
        self.sandbox = self.manager.create_sandbox("adopt", "collapsed-core", template_vmid=320, with_mist_site=True)
        self.mist.calls.clear()
        self.device = FakeConsole()

    def test_adopting_a_switch_sets_its_password_and_joins_the_site(self):
        result = self.manager.adopt_switch(self.sandbox, "sbx-acc-01", console=self.device)
        node = self.sandbox.node("sbx-acc-01")
        self.assertIsNotNone(node.adopted_at)
        self.assertEqual(
            result,
            {"node": "sbx-acc-01", "adopted_at": node.adopted_at, "mgmt_ip": "192.0.2.37", "site": "Sandbox adopt"},
        )
        self.assertEqual(node.mgmt_ip, "192.0.2.37")
        self.assertEqual(self.mist.calls, [("adopt_config", ("org-example",), {"site_id": self.sandbox.mist_site_id})])
        password = self.manager.reveal_root_password(self.sandbox)["root_password"]
        self.assertEqual(self.device.root_password, password)
        self.assertIn("Adopted sbx-acc-01 into Mist site Sandbox adopt", self.sandbox.notes)
        state = read_state(self.manager, "adopt")
        saved = next(n for n in state["nodes"] if n["name"] == "sbx-acc-01")
        self.assertEqual(saved["adopted_at"], node.adopted_at)
        self.assertNotIn(password, json.dumps(state))
        self.assertFalse(self.device.closed, "a console the caller passed in is the caller's to close")

    def test_without_dhcp_the_planned_address_stays(self):
        device = FakeConsole(dhcp_ip=None)
        planned = self.sandbox.node("sbx-acc-01").mgmt_ip
        result = self.manager.adopt_switch(self.sandbox, "sbx-acc-01", console=device)
        self.assertIsNone(result["mgmt_ip"])
        self.assertEqual(self.sandbox.node("sbx-acc-01").mgmt_ip, planned)
        self.assertIn("Adopted sbx-acc-01 into Mist site Sandbox adopt; fxp0 has no DHCP address yet", self.sandbox.notes)

    def refused(self, error, sandbox=None, node="sbx-acc-01"):
        with self.assertRaises(error):
            self.manager.adopt_switch(sandbox or self.sandbox, node, console=self.device)
        self.assertEqual(self.device.lines, [])
        self.assertEqual(self.mist.calls, [])

    def test_adoption_is_refused_before_anything_is_sent(self):
        with self.subTest("read-only"), proxmox_may_only_look(self.manager):
            self.refused(GuardrailViolation)
        with self.subTest("unknown switch"):
            self.refused(GuardrailViolation, node="sbx-acc-09")
        with self.subTest("a client"):
            self.manager.provision_node(self.sandbox, "sbx-cli-01", role="client", kind="client", template_vmid=320)
            self.refused(GuardrailViolation, node="sbx-cli-01")
        with self.subTest("stopped"):
            self.manager.set_power(self.sandbox, "sbx-acc-01", "stop")
            self.refused(GuardrailViolation)
            self.manager.set_power(self.sandbox, "sbx-acc-01", "start")
        with self.subTest("no Mist token"):
            self.mist.token = ""
            self.refused(NotConfigured)
            self.mist.token = "fake-token"
        with self.subTest("Mist token may only read"), mist_may_only_read(self.manager):
            self.refused(GuardrailViolation)
        with self.subTest("no Mist site"):
            plain = self.manager.create_sandbox("plain", "single-switch", template_vmid=320)
            self.refused(GuardrailViolation, sandbox=plain)

    def test_a_switch_without_a_serial_socket_is_not_found(self):
        with self.assertRaises(NotFound) as caught:
            self.manager.adopt_switch(self.sandbox, "sbx-acc-01")
        error = caught.exception
        self.assertIn("serial", (error.message + " " + error.detail).lower())
        self.assertEqual(self.mist.calls, [])

    def test_a_refused_line_leaves_the_switch_unadopted(self):
        device = FakeConsole(fail_on="device-id")
        with self.assertRaises(BackendError):
            self.manager.adopt_switch(self.sandbox, "sbx-acc-01", console=device)
        self.assertIsNone(self.sandbox.node("sbx-acc-01").adopted_at)
        self.assertFalse(any(note.startswith("Adopted") for note in self.sandbox.notes))


class TestMistAdoptQuery(unittest.TestCase):
    def test_the_adoption_command_can_name_the_site(self):
        class Recording(MistClient):
            def __init__(self, settings):
                super().__init__(settings)
                self.paths = []

            def _request(self, method, path, payload=None, *, write=False):
                self.paths.append((method, path))
                return {"cmd": ""}

        client = Recording(Settings(mist_token="t", org_id="org-1"))
        client.adopt_config(site_id="site-9")
        client.adopt_config()
        self.assertEqual(
            client.paths,
            [
                ("GET", "/orgs/org-1/ocdevices/outbound_ssh_cmd?site_id=site-9"),
                ("GET", "/orgs/org-1/ocdevices/outbound_ssh_cmd"),
            ],
        )


class TestRunApi(Base):
    def setUp(self):
        super().setUp()
        self.manager.import_shape(bundle())
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _call(self, path, body=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_build_from_a_shape_over_http(self):
        with proxmox_may_only_look(self.manager):
            status, _ = self._call("/api/shapes/campus-ip-clos/build", {"name": "web", "template_vmid": 320})
        self.assertEqual(status, 409)
        status, body = self._call(
            "/api/shapes/campus-ip-clos/build",
            {"name": "web", "switches": ["sbx-core-01", "sbx-acc-01"], "template_vmid": 320},
        )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["sandbox"]["name"], "web")
        self.assertEqual(len(body["dropped"]), 4)

    def test_adopt_over_http_says_why_it_cannot(self):
        sandbox = self.manager.create_sandbox("web2", "collapsed-core", template_vmid=320, with_mist_site=True)
        status, body = self._call("/api/sandboxes/web2/nodes/sbx-acc-01/adopt", {})
        self.assertEqual(status, 404, body)
        self.manager.set_power(sandbox, "sbx-acc-01", "stop")
        status, body = self._call("/api/sandboxes/web2/nodes/sbx-acc-01/adopt", {})
        self.assertEqual(status, 409, body)

    def test_reveal_does_not_wait_for_a_long_change(self):
        self.manager.create_sandbox("web3", "single-switch", template_vmid=320)
        lock = self.httpd.RequestHandlerClass.write_lock
        lock.acquire()
        try:
            status, body = self._call("/api/sandboxes/web3/reveal", {})
        finally:
            lock.release()
        self.assertEqual(status, 200, body)
        self.assertRegex(body["root_password"], PASSWORD_SHAPE)
        _, state = self._call("/api/state")
        self.assertNotIn(body["root_password"], json.dumps(state))

    def _raw(self, method, path, host, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        self.addCleanup(conn.close)
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host)
        data = json.dumps(body).encode() if body is not None else b""
        if body is not None:
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(data)))
        conn.endheaders(data or None)
        reply = conn.getresponse()
        reply.read()
        return reply.status

    def test_an_unknown_host_header_is_refused(self):
        self.assertEqual(self._raw("GET", "/api/state", "evil.example"), 403)
        self.assertEqual(self._raw("GET", "/", "evil.example"), 403)
        self.assertEqual(self._raw("POST", "/api/sandboxes", "evil.example:80", {"name": "evil1", "recipe": "single-switch", "template_vmid": 320}), 403)
        self.assertNotIn("evil1", self.manager.sandboxes)
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}", f"127.0.0.1:{self.port}"):
            with self.subTest(host=host):
                self.assertEqual(self._raw("GET", "/api/state", host), 200)


if __name__ == "__main__":
    unittest.main()
