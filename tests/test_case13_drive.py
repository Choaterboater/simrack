"""Case 13: drive the fabric from SimRack (the "drive" stage).

"Build fabric in Mist" turns a cabled sandbox into the Mist campus fabric that
matches it: site networks and VRF in the live lab's format, the root password,
every switch Mist-managed, and an EVPN topology whose links and fabric ports
follow the sandbox's cables. "Check cabling" proves the wiring three ways:
Proxmox (each cable's bridge and both NICs), LLDP (what each switch sees on the
port) and Mist (whether the topology links the pair), and fixes the Proxmox
side when writes are on.
"""

from __future__ import annotations

import copy
import io
import ipaddress
import json
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from simrack import fabric
from simrack.api import build_router, serve
from simrack.config import Settings
from simrack.errors import BackendError, GuardrailViolation, NotConfigured
from simrack.mist import MistClient
from simrack.models import Link, Node, Sandbox
from simrack.profile import load_profile
from simrack.recipes import ip_clos_sandbox
from simrack.service import _bridge_name
from tests.fakes import LAB_PROFILE, mist_may_only_read, proxmox_may_only_look, read_state
from tests.test_case12_build_from_shape import Base
from tests.test_case9_shapes import bundle

PARKED = "bridge=sbxpark,firewall=0,link_down=1"
UP = "evpn_uplink"
DOWN = "evpn_downlink"


def pure(nodes, cables, **recipe):
    """A sandbox on paper. ``nodes`` are (name, role[, kind[, pod]]); ``cables`` are (a, a_port, b, b_port)."""
    base = ip_clos_sandbox()
    for key, value in recipe.items():
        setattr(base, key, value)
    built = []
    for index, spec in enumerate(nodes):
        name, role = spec[0], spec[1]
        kind = spec[2] if len(spec) > 2 else "switch"
        pod = spec[3] if len(spec) > 3 else None
        built.append(Node(name=name, vmid=330 + index, role=role, kind=kind, pod=pod))
    vmid = {n.name: n.vmid for n in built}
    links = [Link(a, ap, b, bp, bridge=_bridge_name(vmid[a], vmid[b], ap, bp)) for a, ap, b, bp in cables]
    return Sandbox(name="pure", created_at="2026-10-02T00:00:00Z", recipe=base, nodes=built, links=links)


def macs_of(sandbox, *leave_out):
    return {n.name: f"5c5b35{n.vmid:06x}" for n in sandbox.nodes if n.kind == "switch" and n.name not in leave_out}


def by_mac(body):
    return {s["mac"]: s for s in body["switches"]}


#: The live subnets in the placeholder lab profile the tests run against.
LIVE_SUBNETS = load_profile(LAB_PROFILE)["production_subnets"]


def overlaps_live(cidr):
    net = ipaddress.ip_network(cidr)
    return [live for live in LIVE_SUBNETS if net.overlaps(ipaddress.ip_network(live))]


class TestFabricBody(unittest.TestCase):
    """The EVPN topology body is worked out from the cables alone."""

    def setUp(self):
        # The campus shape, cabled exactly like the live fabric.
        self.sandbox = pure(
            [
                ("sbx-bl-01", "border"),
                ("sbx-bl-02", "border"),
                ("sbx-core-01", "core"),
                ("sbx-core-02", "core"),
                ("sbx-acc-01", "access", "switch", "Pod 1"),
                ("sbx-acc-02", "access", "switch", "Pod 1"),
            ],
            [
                ("sbx-bl-01", "ge-0/0/0", "sbx-core-01", "ge-0/0/0"),
                ("sbx-bl-01", "ge-0/0/1", "sbx-core-02", "ge-0/0/0"),
                ("sbx-bl-02", "ge-0/0/0", "sbx-core-01", "ge-0/0/1"),
                ("sbx-bl-02", "ge-0/0/1", "sbx-core-02", "ge-0/0/1"),
                ("sbx-core-01", "ge-0/0/2", "sbx-acc-01", "ge-0/0/0"),
                ("sbx-core-01", "ge-0/0/3", "sbx-acc-02", "ge-0/0/0"),
                ("sbx-core-02", "ge-0/0/2", "sbx-acc-01", "ge-0/0/1"),
                ("sbx-core-02", "ge-0/0/3", "sbx-acc-02", "ge-0/0/1"),
            ],
            name="campus-ip-clos",
            shape="campus-ip-clos",
            routed_at="edge",
            overlay_as=65000,
            underlay_as_base=65001,
        )
        self.macs = macs_of(self.sandbox)
        self.pods = [{"id": "1", "name": "Pod 1"}]

    def body(self, sandbox=None, macs=None, pods=None):
        sandbox = sandbox or self.sandbox
        return fabric.topology_body(sandbox, macs if macs is not None else macs_of(sandbox), pods=pods, protected=LIVE_SUBNETS)

    def test_links_and_fabric_ports_match_the_live_fabric(self):
        body, info = self.body(pods=self.pods)
        m = self.macs
        self.assertEqual(body["name"], "pure")
        self.assertIs(body["overwrite"], True)
        self.assertEqual([s["mac"] for s in body["switches"]], [m[n] for n in ("sbx-bl-01", "sbx-bl-02", "sbx-core-01", "sbx-core-02", "sbx-acc-01", "sbx-acc-02")])
        self.assertEqual(info["members"], ["sbx-bl-01", "sbx-bl-02", "sbx-core-01", "sbx-core-02", "sbx-acc-01", "sbx-acc-02"])
        self.assertEqual(info["skipped"], [])
        switches = by_mac(body)
        self.assertEqual(
            switches[m["sbx-bl-01"]],
            {"mac": m["sbx-bl-01"], "role": "border", "uplinks": [], "downlinks": [m["sbx-core-01"], m["sbx-core-02"]], "esilaglinks": []},
        )
        self.assertEqual(
            switches[m["sbx-core-01"]],
            {
                "mac": m["sbx-core-01"],
                "role": "core",
                "uplinks": [m["sbx-bl-01"], m["sbx-bl-02"]],
                "downlinks": [m["sbx-acc-01"], m["sbx-acc-02"]],
                "esilaglinks": [],
            },
        )
        self.assertEqual(
            switches[m["sbx-acc-02"]],
            {"mac": m["sbx-acc-02"], "role": "access", "uplinks": [m["sbx-core-01"], m["sbx-core-02"]], "downlinks": [], "esilaglinks": [], "pod": 1},
        )
        self.assertEqual(body["pod_names"], {"1": "Pod 1"})
        ports = {mac: conf.get("port_config") for mac, conf in body["switch_configs"].items()}
        self.assertEqual(ports[m["sbx-bl-02"]], {"ge-0/0/0,ge-0/0/1": {"usage": DOWN}})
        self.assertEqual(ports[m["sbx-core-02"]], {"ge-0/0/0,ge-0/0/1": {"usage": UP}, "ge-0/0/2,ge-0/0/3": {"usage": DOWN}})
        self.assertEqual(ports[m["sbx-acc-01"]], {"ge-0/0/0,ge-0/0/1": {"usage": UP}})

    def test_gateways_sit_on_the_switches_that_route(self):
        body, _ = self.body()
        m, configs = self.macs, body["switch_configs"]
        irbs = {
            "data": {"type": "static", "ip": "10.60.10.1", "netmask": "255.255.255.0", "evpn_anycast": True},
            "voice": {"type": "static", "ip": "10.60.20.1", "netmask": "255.255.255.0", "evpn_anycast": True},
        }
        for name in ("sbx-acc-01", "sbx-acc-02"):
            self.assertEqual(configs[m[name]]["other_ip_configs"], irbs, "routed at the edge: the access switches carry the gateways")
            self.assertEqual(configs[m[name]]["vrf_config"], {"enabled": True})
        for name in ("sbx-bl-01", "sbx-core-01"):
            self.assertEqual(set(configs[m[name]]), {"port_config"})

        self.sandbox.recipe.routed_at = "core"
        body, _ = self.body()
        configs = body["switch_configs"]
        self.assertEqual(configs[m["sbx-core-02"]]["other_ip_configs"], irbs)
        self.assertEqual(configs[m["sbx-core-02"]]["vrf_config"], {"enabled": True})
        self.assertEqual(set(configs[m["sbx-acc-01"]]), {"port_config"})
        self.assertEqual(body["evpn_options"]["routed_at"], "core")

    def test_evpn_options_mirror_live_on_subnets_clear_of_it(self):
        body, info = self.body()
        options = body["evpn_options"]
        self.assertEqual(
            options,
            {
                "routed_at": "edge",
                "overlay": {"as": 65000},
                "underlay": {"as_base": 65001, "subnet": "10.255.224.0/20"},
                "auto_router_id_subnet": "172.31.0.0/23",
                "auto_loopback_subnet": "172.31.2.0/24",
            },
        )
        for cidr in (options["underlay"]["subnet"], options["auto_router_id_subnet"], options["auto_loopback_subnet"]):
            self.assertEqual(overlaps_live(cidr), [], f"{cidr} overlaps the live lab")
        self.assertEqual(info["notes"], [])

    def test_unsafe_subnets_fall_back_and_say_so(self):
        self.sandbox.recipe.underlay_cidr = "10.255.250.0/20"  # inside the live 10.255.240.0/20
        self.sandbox.recipe.loopback_cidr = "nonsense"
        self.sandbox.recipe.auto_loopback_cidr = "2001:db8::/64"
        body, info = self.body()
        options = body["evpn_options"]
        self.assertEqual(options["underlay"]["subnet"], fabric.SAFE_UNDERLAY)
        self.assertEqual(options["auto_router_id_subnet"], fabric.SAFE_ROUTER_IDS)
        self.assertEqual(options["auto_loopback_subnet"], fabric.SAFE_LOOPBACKS)
        self.assertTrue(any("overlaps the live lab" in n and "10.255.240.0/20" in n for n in info["notes"]), info["notes"])
        self.assertTrue(any("nonsense" in n for n in info["notes"]), info["notes"])
        self.assertTrue(any("2001:db8::/64" in n for n in info["notes"]), info["notes"])

    def test_where_the_fabric_routes_by_default(self):
        self.sandbox.recipe.routed_at = None
        self.assertEqual(self.body()[0]["evpn_options"]["routed_at"], "edge")
        multihoming = pure(
            [("sbx-cc-01", "collapsed-core"), ("sbx-cc-02", "collapsed-core"), ("sbx-esi-01", "esilag-access")],
            [("sbx-cc-01", "ge-0/0/2", "sbx-esi-01", "ge-0/0/0"), ("sbx-cc-02", "ge-0/0/2", "sbx-esi-01", "ge-0/0/1")],
        )
        self.assertEqual(self.body(multihoming)[0]["evpn_options"]["routed_at"], "core")
        self.sandbox.recipe.routed_at = "sideways"
        body, info = self.body()
        self.assertEqual(body["evpn_options"]["routed_at"], "edge")
        self.assertTrue(any("sideways" in n for n in info["notes"]), info["notes"])

    def test_cables_and_switches_mist_cannot_use_are_left_out_with_a_reason(self):
        sandbox = pure(
            [
                ("sbx-core-01", "core"),
                ("sbx-core-02", "core"),
                ("sbx-acc-01", "access"),
                ("sbx-acc-09", "access"),
                ("sbx-srx-01", "vsrx", "vsrx"),
                ("sbx-odd-01", "lab"),
            ],
            [
                ("sbx-core-01", "ge-0/0/4", "sbx-core-02", "ge-0/0/4"),
                ("sbx-core-01", "ge-0/0/5", "sbx-srx-01", "ge-0/0/0"),
                ("sbx-core-01", "ge-0/0/2", "sbx-acc-09", "ge-0/0/0"),
                ("sbx-core-01", "ge-0/0/3", "sbx-acc-01", "ge-0/0/0"),
            ],
        )
        macs = macs_of(sandbox, "sbx-acc-09")
        body, info = self.body(sandbox, macs)
        self.assertEqual(info["members"], ["sbx-core-01", "sbx-core-02", "sbx-acc-01"])
        skipped = info["skipped"]
        for needle in (
            ("sbx-acc-09", "not in the Mist site"),
            ("sbx-odd-01", "not a Mist fabric role"),
            ("sbx-core-01 ge-0/0/4", "not a fabric link"),
            ("sbx-core-01 ge-0/0/5", "sbx-srx-01 is not in the fabric"),
            ("sbx-core-01 ge-0/0/2", "sbx-acc-09 is not in the fabric"),
        ):
            self.assertTrue(any(all(part in s for part in needle) for s in skipped), f"{needle} not in {skipped}")
        self.assertFalse(any("sbx-srx-01:" in s for s in skipped), "a non-switch guest is not a fabric member to explain")
        core = body["switch_configs"][macs["sbx-core-01"]]
        self.assertEqual(core["port_config"], {"ge-0/0/3": {"usage": DOWN}})
        self.assertEqual(by_mac(body)[macs["sbx-core-02"]]["downlinks"], [])
        self.assertNotIn(macs["sbx-core-02"], body["switch_configs"])

    def test_esi_lag_links_are_listed_on_both_sides(self):
        sandbox = pure(
            [("sbx-cc-01", "collapsed-core"), ("sbx-cc-02", "collapsed-core"), ("sbx-esi-01", "esilag-access")],
            [
                ("sbx-cc-01", "ge-0/0/2", "sbx-esi-01", "ge-0/0/0"),
                ("sbx-cc-02", "ge-0/0/2", "sbx-esi-01", "ge-0/0/1"),
                ("sbx-cc-01", "ge-0/0/0", "sbx-cc-02", "ge-0/0/0"),
            ],
        )
        m = macs_of(sandbox)
        body, info = self.body(sandbox)
        switches = by_mac(body)
        self.assertEqual(switches[m["sbx-cc-01"]]["esilaglinks"], [m["sbx-esi-01"]])
        self.assertEqual(switches[m["sbx-esi-01"]]["esilaglinks"], [m["sbx-cc-01"], m["sbx-cc-02"]])
        self.assertEqual(switches[m["sbx-esi-01"]]["uplinks"], [])
        self.assertEqual(switches[m["sbx-esi-01"]]["pod"], 1)
        self.assertNotIn("pod", switches[m["sbx-cc-01"]])
        self.assertNotIn(m["sbx-esi-01"], body["switch_configs"], "ESI-LAG ports are Mist's to name")
        self.assertNotIn("port_config", body["switch_configs"][m["sbx-cc-01"]])
        self.assertIn("other_ip_configs", body["switch_configs"][m["sbx-cc-01"]], "a collapsed core routes")
        self.assertTrue(any("sbx-cc-01 ge-0/0/0" in s for s in info["skipped"]))

    def test_two_cables_between_one_pair_name_the_peer_once(self):
        sandbox = pure(
            [("sbx-core-01", "core"), ("sbx-acc-01", "access")],
            [("sbx-core-01", "ge-0/0/2", "sbx-acc-01", "ge-0/0/0"), ("sbx-core-01", "ge-0/0/3", "sbx-acc-01", "ge-0/0/1")],
        )
        m = macs_of(sandbox)
        body, _ = self.body(sandbox)
        self.assertEqual(by_mac(body)[m["sbx-core-01"]]["downlinks"], [m["sbx-acc-01"]])
        self.assertEqual(body["switch_configs"][m["sbx-core-01"]]["port_config"], {"ge-0/0/2,ge-0/0/3": {"usage": DOWN}})
        self.assertEqual(body["switch_configs"][m["sbx-acc-01"]]["port_config"], {"ge-0/0/0,ge-0/0/1": {"usage": UP}})

    def test_pods_follow_the_shape_then_the_name(self):
        sandbox = pure(
            [
                ("sbx-core-01", "core"),
                ("sbx-acc-01", "access", "switch", "East"),
                ("sbx-acc-02", "access", "switch", "3"),
                ("sbx-acc-03", "access", "switch", "West"),
                ("sbx-acc-04", "access"),
                ("sbx-acc-05", "access", "switch", "pod 9"),
                ("sbx-acc-06", "access", "switch", "West"),
            ],
            [],
        )
        m = macs_of(sandbox)
        body, _ = self.body(sandbox, pods=[{"id": "1", "name": "Pod 1"}, {"id": "7", "name": "East"}])
        pods = {name: by_mac(body)[m[name]].get("pod") for name in m}
        self.assertEqual(
            pods,
            {"sbx-core-01": None, "sbx-acc-01": 7, "sbx-acc-02": 3, "sbx-acc-03": 2, "sbx-acc-04": 1, "sbx-acc-05": 9, "sbx-acc-06": 2},
        )
        self.assertEqual(body["pod_names"], {"1": "Pod 1", "2": "West", "3": "Pod 3", "7": "East", "9": "Pod 9"})

    def test_the_basic_body_keeps_only_what_mist_must_have(self):
        body, _ = self.body()
        body["id"] = "topo-1"
        before = copy.deepcopy(body)
        basic = fabric.basic_body(body)
        self.assertEqual(body, before, "the detailed body is not changed")
        self.assertEqual(set(basic), {"id", "name", "overwrite", "pod_names", "evpn_options", "switches"})
        self.assertEqual(basic["switches"][0], {"mac": self.macs["sbx-bl-01"], "role": "border"})
        self.assertEqual(basic["switches"][-1], {"mac": self.macs["sbx-acc-02"], "role": "access", "pod": 1})

    def test_switch_config_merges_into_a_device(self):
        current = {
            "name": "sbx-acc-01",
            "port_config": {"ge-0/0/9": {"usage": "default"}, "ge-0/0/0": {"usage": "old"}, "ge-0/0/4-6": {"usage": "ap"}},
            "other_ip_configs": {"mgmt": {"type": "dhcp"}},
            "vrf_config": {"enabled": False},
        }
        before = copy.deepcopy(current)
        conf = {
            "port_config": {"ge-0/0/0,ge-0/0/1": {"usage": UP}},
            "other_ip_configs": {"data": {"type": "static", "ip": "10.60.10.1", "netmask": "255.255.255.0", "evpn_anycast": True}},
            "vrf_config": {"enabled": True},
        }
        merged = fabric.merge_switch_config(current, conf)
        self.assertEqual(current, before)
        self.assertEqual(
            merged["port_config"],
            {"ge-0/0/9": {"usage": "default"}, "ge-0/0/4-6": {"usage": "ap"}, "ge-0/0/0,ge-0/0/1": {"usage": UP}},
            "a port the fabric now owns loses its old entry",
        )
        self.assertEqual(set(merged["other_ip_configs"]), {"mgmt", "data"})
        self.assertEqual(merged["vrf_config"], {"enabled": True})
        self.assertEqual(merged["name"], "sbx-acc-01")


class TestSiteSetting(unittest.TestCase):
    """Site networks and VRF go to Mist in the format the live site uses."""

    def test_the_recipe_speaks_the_live_format(self):
        setting = ip_clos_sandbox().mist_setting()
        self.assertEqual(
            setting,
            {
                "networks": {"data": {"vlan_id": 10, "subnet": "10.60.10.0/24"}, "voice": {"vlan_id": 20, "subnet": "10.60.20.0/24"}},
                "vrf_instances": {"LAB": {"networks": ["data", "voice"], "extra_routes": {}}},
            },
        )
        recipe = ip_clos_sandbox()
        recipe.vrf = None
        self.assertNotIn("vrf_instances", recipe.mist_setting())

    def test_the_merge_keeps_everything_else(self):
        current = {
            "networks": {"data": {"vlan": 99, "vlan_id": 99, "subnet": "10.9.9.0/24", "isolation": True}, "guest": {"vlan_id": 30, "subnet": "10.1.1.0/24"}},
            "vrf_instances": {"LAB": {"networks": {"guest": {}}, "loopback_address": ""}, "OTHER": {"networks": ["x"]}},
            "switch_mgmt": {"root_password": "old", "protect_re": {"enabled": True}},
            "rtsa": {"enabled": False},
        }
        before = copy.deepcopy(current)
        merged = fabric.site_setting(current, ip_clos_sandbox(), "pw-1")
        self.assertEqual(current, before, "the current setting is not changed")
        self.assertEqual(merged["networks"]["data"], {"vlan_id": 10, "subnet": "10.60.10.0/24", "isolation": True})
        self.assertEqual(merged["networks"]["guest"], {"vlan_id": 30, "subnet": "10.1.1.0/24"})
        self.assertEqual(merged["networks"]["voice"], {"vlan_id": 20, "subnet": "10.60.20.0/24"})
        self.assertEqual(merged["vrf_instances"]["LAB"], {"networks": ["guest", "data", "voice"], "loopback_address": "", "extra_routes": {}})
        self.assertEqual(merged["vrf_instances"]["OTHER"], {"networks": ["x"]})
        self.assertEqual(merged["switch_mgmt"], {"root_password": "pw-1", "protect_re": {"enabled": True}})
        self.assertEqual(merged["rtsa"], {"enabled": False})

    def test_an_empty_site_gets_the_recipe_and_the_password(self):
        merged = fabric.site_setting({}, ip_clos_sandbox(), "pw-2")
        self.assertEqual(set(merged), {"networks", "vrf_instances", "switch_mgmt"})
        self.assertEqual(merged["switch_mgmt"], {"root_password": "pw-2"})
        self.assertNotIn("management", merged["networks"], "management stays out of band on fxp0")
        for network in merged["networks"].values():
            self.assertEqual(overlaps_live(network["subnet"]), [])


class Built(Base):
    """The campus shape built with its own Mist site, every switch in Mist."""

    def setUp(self):
        super().setUp()
        self.manager.import_shape(bundle())
        self.sandbox = self.manager.build_from_shape("campus-ip-clos", "park-a", template_vmid=320, with_mist_site=True)["sandbox"]
        self.site = self.sandbox.mist_site_id
        self.ids = {n.name: self.mist.add_switch(self.site, n.name) for n in self.sandbox.nodes}
        self.macs = {d["name"]: d["mac"] for d in self.mist.devices(self.site)}
        self.mist.calls.clear()
        self.px.calls.clear()

    def vmid(self, name):
        return self.sandbox.node(name).vmid

    def nic(self, name, index):
        return self.px.nics(self.vmid(name))[f"net{index}"]

    def topology(self):
        topologies = self.mist.evpn_topologies(self.site)
        self.assertEqual(len(topologies), 1)
        return self.mist.evpn_topology(self.site, topologies[0]["id"])

    def writes(self):
        return [c for c in self.mist.calls if c[0].startswith(("put_", "create_", "delete_"))]


class TestBuildFabric(Built):
    def test_the_site_then_the_switches_then_the_topology(self):
        result = self.manager.mist_build_fabric(self.sandbox)
        order = [c[0] for c in self.writes()]
        self.assertEqual(order, ["put_site_setting"] + ["put_device"] * 6 + ["put_evpn_topology"])

        password = self.manager.reveal_root_password(self.sandbox)["root_password"]
        setting = self.mist.site_setting(self.site)
        self.assertEqual(setting["networks"]["data"], {"vlan_id": 10, "subnet": "10.60.10.0/24"})
        self.assertEqual(setting["vrf_instances"]["LAB"]["networks"], ["data", "voice"])
        self.assertEqual(setting["switch_mgmt"]["root_password"], password)
        for name, device_id in self.ids.items():
            device = self.mist.device(self.site, device_id)
            self.assertIs(device["managed"], True, name)
            self.assertIs(device["mist_configured"], True, name)
            self.assertEqual(device["name"], name)

        topology = self.topology()
        self.assertEqual(topology["name"], "park-a")
        self.assertEqual(len(topology["switches"]), 6)
        self.assertEqual(set(topology["switch_configs"]), set(self.macs.values()))
        self.assertEqual(topology["evpn_options"]["overlay"], {"as": 65000})
        core = next(s for s in topology["switches"] if s["mac"] == self.macs["sbx-core-01"])
        self.assertEqual(core["downlinks"], [self.macs["sbx-acc-01"], self.macs["sbx-acc-02"]])

        self.assertEqual(result["site_id"], self.site)
        self.assertEqual(result["topology_id"], topology["id"])
        self.assertEqual(result["form"], "detailed")
        self.assertEqual(result["switches"], ["sbx-bl-01", "sbx-bl-02", "sbx-core-01", "sbx-core-02", "sbx-acc-01", "sbx-acc-02"])
        self.assertEqual(result["skipped"], [])
        self.assertEqual(result["devices_changed"], 6)
        self.assertEqual(result["summary"], {"networks": ["data", "voice"], "vrfs": ["LAB"], "root_password": "set"})

    def test_the_root_password_never_leaves_the_server(self):
        password = self.manager.reveal_root_password(self.sandbox)["root_password"]
        result = self.manager.mist_build_fabric(self.sandbox)
        self.assertNotIn(password, json.dumps(result))
        self.assertNotIn(password, json.dumps(read_state(self.manager, "park-a")))
        self.assertNotIn(password, json.dumps(self.manager.state()))

    def test_a_snapshot_comes_first_so_revert_undoes_it(self):
        result = self.manager.mist_build_fabric(self.sandbox)
        label = result["snapshot"]
        self.assertTrue(label.startswith("before-fabric-"), label)
        self.assertIn(label, self.sandbox.mist_snapshots)
        with open(self.sandbox.mist_snapshots[label]["path"], encoding="utf-8") as handle:
            taken = json.load(handle)
        self.assertEqual(taken["site_setting"], {}, "taken before anything was written")
        self.assertEqual(taken["evpn_topologies"], [])

    def test_a_rebuild_updates_the_same_topology(self):
        first = self.manager.mist_build_fabric(self.sandbox)
        self.mist.calls.clear()
        second = self.manager.mist_build_fabric(self.sandbox)
        self.assertEqual(second["topology_id"], first["topology_id"])
        self.topology()
        self.assertEqual([c[1][2] for c in self.mist.calls if c[0] == "put_evpn_topology"], [first["topology_id"]])
        self.assertEqual(second["devices_changed"], 0)
        self.assertNotIn("put_device", [c[0] for c in self.mist.calls], "switches already managed are left alone")

    def test_a_switch_that_left_the_sandbox_leaves_the_topology(self):
        self.manager.mist_build_fabric(self.sandbox)
        gone = self.macs["sbx-acc-02"]
        self.manager.delete_node(self.sandbox, "sbx-acc-02")
        self.manager.mist_build_fabric(self.sandbox)
        switches = by_mac(self.topology())
        self.assertEqual(switches[gone], {"mac": gone, "role": "none"}, "role none is how Mist removes a switch")
        self.assertEqual(switches[self.macs["sbx-core-01"]]["downlinks"], [self.macs["sbx-acc-01"]])

    def test_switches_mist_does_not_have_are_named_and_nothing_is_written(self):
        self.mist.devices_by_site[self.site] = [d for d in self.mist.devices_by_site[self.site] if d["name"] not in ("sbx-acc-01", "sbx-acc-02")]
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertIn("sbx-acc-01, sbx-acc-02", caught.exception.message)
        self.assertIn("Adopt", caught.exception.detail)
        self.assertIn("sbx-core-01", caught.exception.detail, "says what Mist does see")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.sandbox.mist_snapshots, {})

    def test_mist_names_match_without_case_and_a_missing_mac_is_looked_up(self):
        records = {d["name"]: d for d in self.mist.devices_by_site[self.site]}
        records["sbx-core-01"]["name"] = "SBX-CORE-01"
        records["sbx-core-02"]["name"] = ""
        records["sbx-core-02"]["hostname"] = "sbx-core-02"
        del records["sbx-acc-01"]["mac"]
        result = self.manager.mist_build_fabric(self.sandbox)
        self.assertEqual(len(result["switches"]), 6)
        self.assertIn(self.macs["sbx-acc-01"], self.topology()["switch_configs"])

    def test_read_only_tokens_or_no_site_are_refused_before_any_write(self):
        with proxmox_may_only_look(self.manager), self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertIn("read-only", str(caught.exception))

        with mist_may_only_read(self.manager), self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertIn("role on this org is read", str(caught.exception))

        token, self.mist.token = self.mist.token, ""
        with self.assertRaises(NotConfigured):
            self.manager.mist_build_fabric(self.sandbox)
        self.mist.token = token

        site, self.sandbox.mist_site_id = self.sandbox.mist_site_id, None
        with self.assertRaises(GuardrailViolation) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertIn("no Mist site", str(caught.exception))
        self.sandbox.mist_site_id = site
        self.assertEqual(self.writes(), [])

    def test_a_refused_detailed_topology_falls_back_to_basic_plus_switch_ports(self):
        self.mist.reject_topology = "detailed"
        result = self.manager.mist_build_fabric(self.sandbox)
        self.assertEqual(result["form"], "basic")
        topology = self.topology()
        self.assertNotIn("switch_configs", topology)
        self.assertTrue(all(set(s) <= {"mac", "role", "pod"} for s in topology["switches"]))
        core = self.mist.device(self.site, self.ids["sbx-core-01"])
        self.assertEqual(core["port_config"], {"ge-0/0/0,ge-0/0/1": {"usage": UP}, "ge-0/0/2,ge-0/0/3": {"usage": DOWN}})
        access = self.mist.device(self.site, self.ids["sbx-acc-01"])
        self.assertEqual(set(access["other_ip_configs"]), {"data", "voice"})
        self.assertEqual(access["vrf_config"], {"enabled": True})
        self.assertIs(access["mist_configured"], True)
        self.assertTrue(any("basic" in n for n in result["notes"]), result["notes"])

    def test_a_refused_basic_topology_says_how_to_undo(self):
        self.mist.reject_topology = "all"
        with self.assertRaises(BackendError) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertEqual(caught.exception.message, "Mist refused the fabric topology.")
        self.assertIn("Revert to before-fabric-", caught.exception.detail)
        self.assertIsNone(self.sandbox.fabric_built_at)

    def test_other_mist_errors_are_not_retried(self):
        self.mist.reject_topology, self.mist.reject_status = "detailed", 500
        with self.assertRaises(BackendError) as caught:
            self.manager.mist_build_fabric(self.sandbox)
        self.assertEqual(len([c for c in self.mist.calls if c[0] == "put_evpn_topology"]), 1)
        self.assertIn("Revert to before-fabric-", caught.exception.detail)

    def test_the_collapsed_core_recipe_builds(self):
        sandbox = self.manager.create_sandbox("cc-lab", "collapsed-core", template_vmid=320, with_mist_site=True)
        ids = {n.name: self.mist.add_switch(sandbox.mist_site_id, n.name) for n in sandbox.nodes}
        macs = {d["name"]: d["mac"] for d in self.mist.devices(sandbox.mist_site_id)}
        result = self.manager.mist_build_fabric(sandbox)
        self.assertEqual(result["form"], "detailed")
        topology = self.mist.evpn_topology(sandbox.mist_site_id, result["topology_id"])
        switches = by_mac(topology)
        self.assertEqual(switches[macs["sbx-core-01"]]["downlinks"], [macs["sbx-acc-01"]])
        self.assertEqual(switches[macs["sbx-acc-01"]]["uplinks"], [macs["sbx-core-01"]])
        self.assertEqual(switches[macs["sbx-acc-01"]]["pod"], 1)
        self.assertEqual(topology["switch_configs"][macs["sbx-core-01"]]["port_config"], {"ge-0/0/2": {"usage": DOWN}})
        self.assertEqual(topology["switch_configs"][macs["sbx-acc-01"]]["port_config"], {"ge-0/0/2": {"usage": UP}})
        for cidr in (topology["evpn_options"]["underlay"]["subnet"], topology["evpn_options"]["auto_router_id_subnet"]):
            self.assertEqual(overlaps_live(cidr), [])
        self.assertEqual(len(ids), 2)

    def test_a_single_switch_gets_networks_but_no_topology(self):
        sandbox = self.manager.create_sandbox("solo", "single-switch", template_vmid=320, with_mist_site=True)
        self.mist.add_switch(sandbox.mist_site_id, "sbx-acc-01")
        result = self.manager.mist_build_fabric(sandbox)
        self.assertIsNone(result["topology_id"])
        self.assertIsNone(result["form"])
        self.assertEqual(self.mist.evpn_topologies(sandbox.mist_site_id), [])
        self.assertIn("data", self.mist.site_setting(sandbox.mist_site_id)["networks"])
        self.assertTrue(any("at least two switches" in n for n in result["notes"]), result["notes"])

    def test_the_build_and_its_check_are_saved(self):
        result = self.manager.mist_build_fabric(self.sandbox)
        self.assertIsNotNone(self.sandbox.fabric_built_at)
        self.assertEqual(result["check"]["summary"]["cables"], 8)
        saved = read_state(self.manager, "park-a")
        self.assertEqual(saved["fabric_built_at"], self.sandbox.fabric_built_at)
        self.assertEqual(saved["fabric_check"]["checked_at"], result["check"]["checked_at"])
        listed = next(s for s in self.manager.list_sandboxes() if s["name"] == "park-a")
        self.assertEqual(listed["fabric_built_at"], self.sandbox.fabric_built_at)


class TestFabricCheck(Built):
    def cable(self, result, bridge):
        return next(c for c in result["cables"] if c["bridge"] == bridge)

    def test_a_healthy_fabric_is_left_alone(self):
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual({c["proxmox"] for c in result["cables"]}, {"ok"})
        self.assertEqual(len(result["cables"]), 8)
        self.assertEqual(result["parked"], [])
        self.assertEqual(result["missing"], [])
        self.assertIs(result["repair"], True)
        self.assertTrue(result["summary"]["healthy"])
        self.assertEqual([c for c in self.px.calls if c[0] not in ("wait_task",)], [], "a healthy fabric gets no writes")
        self.assertEqual(self.writes(), [], "the check never writes to Mist")

    def test_a_missing_bridge_is_made_again_and_both_ends_replugged(self):
        bridge = "sbx323_325_20"
        before = {name: self.nic(name, index) for name, index in (("sbx-core-01", 3), ("sbx-acc-01", 1))}
        del self.px.networks[bridge]
        result = self.manager.fabric_check(self.sandbox)
        row = self.cable(result, bridge)
        self.assertEqual(row["proxmox"], "fixed")
        self.assertIn(bridge, row["proxmox_detail"])
        self.assertEqual(self.px.called("create_bridge"), [("create_bridge", (bridge,), {"mtu": 9216})])
        for name, index in (("sbx-core-01", 3), ("sbx-acc-01", 1)):
            values = [c[2][f"net{index}"] for c in self.px.config_calls_for(self.vmid(name))]
            self.assertEqual(len(values), 2, f"{name}: park, then plug back in, so Proxmox makes a new tap")
            self.assertIn(PARKED, values[0])
            self.assertEqual(self.nic(name, index), before[name], "same MAC, same bridge")
        self.assertEqual(len(self.px.called("tune_port")), 2)
        self.px.calls.clear()
        again = self.manager.fabric_check(self.sandbox)
        self.assertEqual(self.cable(again, bridge)["proxmox"], "ok")
        self.assertEqual(self.px.called("set_vm_config"), [])

    def test_a_nic_on_the_wrong_bridge_or_link_down_is_plugged_back_in(self):
        core, acc = self.vmid("sbx-core-01"), self.vmid("sbx-acc-02")
        self.px.vms[core]["net4"] = f"virtio={self.px.mac(core, 4)},{PARKED}"
        self.px.vms[acc]["net1"] = f"virtio={self.px.mac(acc, 1)},bridge=sbx323_326_30,firewall=0,link_down=1"
        result = self.manager.fabric_check(self.sandbox)
        row = self.cable(result, "sbx323_326_30")
        self.assertEqual(row["proxmox"], "fixed")
        self.assertEqual(self.nic("sbx-core-01", 4), f"virtio={self.px.mac(core, 4)},bridge=sbx323_326_30,firewall=0")
        self.assertEqual(self.nic("sbx-acc-02", 1), f"virtio={self.px.mac(acc, 1)},bridge=sbx323_326_30,firewall=0")
        self.assertEqual(self.px.called("create_bridge"), [])
        self.assertTrue(any("1 cable" in n for n in self.sandbox.notes[-1:]), self.sandbox.notes[-1:])

    def test_read_only_reports_without_fixing(self):
        del self.px.networks["sbx323_325_20"]
        acc = self.vmid("sbx-acc-02")
        self.px.vms[acc]["net1"] = f"virtio={self.px.mac(acc, 1)},bridge=sbx323_326_30,firewall=0,link_down=1"
        with proxmox_may_only_look(self.manager):
            result = self.manager.fabric_check(self.sandbox)
        self.assertIs(result["repair"], False)
        self.assertEqual(self.cable(result, "sbx323_325_20")["proxmox"], "broken")
        self.assertIn("missing", self.cable(result, "sbx323_325_20")["proxmox_detail"])
        self.assertIn("link-down", self.cable(result, "sbx323_326_30")["proxmox_detail"])
        self.assertFalse(result["summary"]["healthy"])
        self.assertEqual(self.px.calls, [])

    def test_a_missing_park_bridge_it_may_not_make_says_why_in_words(self):
        del self.px.networks[self.manager.settings.park_bridge]
        with proxmox_may_only_look(self.manager):
            _, message, detail = self.manager.access.lab_refusal()
            result = self.manager.fabric_check(self.sandbox)
        note = next(n for n in result["notes"] if n.startswith("The park bridge"))
        self.assertIn(f"cannot start. {message} {detail} Check again", note)
        self.assertNotIn("<class", note)
        self.assertFalse(result["summary"]["healthy"])
        self.assertEqual(self.px.calls, [])

    def test_a_check_asked_only_to_look_fixes_nothing_even_when_it_may(self):
        del self.px.networks["sbx323_325_20"]
        del self.px.networks[self.manager.settings.park_bridge]
        result = self.manager.fabric_check(self.sandbox, repair=False)
        self.assertIs(result["repair"], False)
        self.assertEqual(self.cable(result, "sbx323_325_20")["proxmox"], "broken")
        note = next(n for n in result["notes"] if n.startswith("The park bridge"))
        self.assertTrue(note.endswith("cannot start. Fixing the cabling makes it again."), note)
        self.assertEqual(self.px.calls, [])

    def test_a_fix_simrack_may_not_make_is_refused_and_nothing_is_checked(self):
        del self.px.networks["sbx323_325_20"]
        with proxmox_may_only_look(self.manager):
            refused, message, _ = self.manager.access.lab_refusal()
            with self.assertRaises(refused) as caught:
                self.manager.fabric_check(self.sandbox, repair=True)
        self.assertEqual(caught.exception.message, message)
        self.assertIsNone(self.sandbox.fabric_check)
        self.assertEqual(self.px.calls, [])

    def test_stray_ports_are_parked_and_fxp0_is_left_alone(self):
        core = self.vmid("sbx-core-01")
        self.px.vms[core]["net6"] = f"virtio={self.px.mac(core, 6)},bridge=sbx999_998_00,firewall=0"
        self.px.vms[core]["net0"] = f"virtio={self.px.mac(core, 0)},bridge=vmbr1"
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual(result["parked"], [{"node": "sbx-core-01", "port": "ge-0/0/5", "bridge": "sbx999_998_00", "fixed": True}])
        self.assertEqual(self.nic("sbx-core-01", 6), f"virtio={self.px.mac(core, 6)},{PARKED}")
        self.assertEqual(self.nic("sbx-core-01", 0), f"virtio={self.px.mac(core, 0)},bridge=vmbr1", "net0 is never touched")
        self.assertFalse(any("net0" in c[2] for c in self.px.called("set_vm_config")))

    def test_a_missing_nic_is_reported_never_added(self):
        core, acc = self.vmid("sbx-core-01"), self.vmid("sbx-acc-01")
        del self.px.vms[core]["net8"]
        del self.px.vms[acc]["net1"]
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual(result["missing"], [{"node": "sbx-core-01", "port": "ge-0/0/7"}])
        row = self.cable(result, "sbx323_325_20")
        self.assertEqual(row["proxmox"], "broken")
        self.assertIn("no NIC", row["proxmox_detail"])
        self.assertNotIn("net8", self.px.nics(core))
        self.assertNotIn("net1", self.px.nics(acc))
        self.assertTrue(any("NIC" in n for n in result["notes"]), result["notes"])

    def test_lldp_says_what_each_switch_sees(self):
        m = self.macs
        self.sandbox.node("sbx-acc-02").adopted_at = "2026-10-02T00:00:00Z"
        self.mist.ports_by_site[self.site] = [
            # core-01 ge-0/0/2 <-> acc-01 ge-0/0/0: both ends see each other
            {"mac": m["sbx-core-01"], "port_id": "ge-0/0/2", "up": True, "neighbor_system_name": "sbx-acc-01"},
            {"mac": m["sbx-acc-01"], "port_id": "ge-0/0/0", "up": True, "neighbor_system_name": "SBX-CORE-01.lab.local"},
            # core-01 ge-0/0/3 <-> acc-02 ge-0/0/0: core-01 sees the wrong switch
            {"mac": m["sbx-core-01"], "port_id": "ge-0/0/3", "up": True, "neighbor_system_name": "sbx-acc-01"},
            # Mist knows a switch by its name only once it carries it, so a stranger at a
            # switch's end is wrong whoever adopted it: acc-01 by hand, acc-02 by SimRack.
            {"mac": m["sbx-core-02"], "port_id": "ge-0/0/2", "up": True, "neighbor_system_name": "Amnesiac"},
            {"mac": m["sbx-core-02"], "port_id": "ge-0/0/3", "up": True, "neighbor_system_name": "Amnesiac"},
        ]
        result = self.manager.fabric_check(self.sandbox)
        lldp = {c["bridge"]: c["lldp"] for c in result["cables"]}
        self.assertEqual(lldp["sbx323_325_20"], "ok")
        self.assertEqual(lldp["sbx323_326_30"], "wrong")
        self.assertIn("sees sbx-acc-01", self.cable(result, "sbx323_326_30")["lldp_detail"])
        self.assertEqual(lldp["sbx324_325_21"], "wrong")
        self.assertIn("sees Amnesiac, not sbx-acc-01", self.cable(result, "sbx324_325_21")["lldp_detail"])
        self.assertEqual(lldp["sbx324_326_31"], "wrong")
        self.assertEqual(lldp["sbx321_323_00"], "waiting", "nothing seen yet")
        self.assertTrue(any("NIC" in n and "port" in n for n in result["notes"]), "LLDP wrong over a good Proxmox cable hints at the port mapping")
        self.assertFalse(result["summary"]["healthy"])

    def test_lldp_past_a_guest_that_is_not_a_switch_is_unknown_unless_it_names_a_sandbox_switch(self):
        for kind, port in (("client", "ge-0/0/5"), ("vsrx", "ge-0/0/6")):
            with self.subTest(kind=kind):
                guest = self.manager.provision_node(self.sandbox, f"sbx-{kind}-01", role=kind, kind=kind, template_vmid=320)
                link = self.manager.cable(self.sandbox, "sbx-acc-02", port, guest.name, "ge-0/0/0")
                for heard, expected in (("ubuntu", "unknown"), (guest.name, "ok"), ("sbx-core-01", "wrong")):
                    self.mist.ports_by_site[self.site] = [
                        {"mac": self.macs["sbx-acc-02"], "port_id": port, "up": True, "neighbor_system_name": heard},
                    ]
                    result = self.manager.fabric_check(self.sandbox)
                    row = self.cable(result, link.bridge)
                    self.assertEqual(row["lldp"], expected, heard)
                    if expected == "unknown":
                        self.assertIn(f"sees ubuntu; {guest.name} is not a switch", row["lldp_detail"])
                        self.assertNotIn("adopted", row["lldp_detail"])
                        self.assertTrue(result["summary"]["healthy"], "a name SimRack cannot judge is no fault")

    def test_a_switch_mist_does_not_know_is_unknown(self):
        self.mist.devices_by_site[self.site] = [d for d in self.mist.devices_by_site[self.site] if d["name"] != "sbx-bl-01"]
        self.mist.ports_by_site[self.site] = [
            {"mac": self.macs["sbx-core-01"], "port_id": "ge-0/0/0", "up": True, "neighbor_system_name": "sbx-bl-01"},
        ]
        result = self.manager.fabric_check(self.sandbox)
        row = self.cable(result, "sbx321_323_00")
        self.assertEqual(row["lldp"], "unknown")
        self.assertEqual(row["mist"], "unknown")

    def test_the_mist_topology_is_compared_with_the_cables(self):
        before = self.manager.fabric_check(self.sandbox)
        self.assertEqual({c["mist"] for c in before["cables"]}, {"none"}, "no topology yet")
        self.manager.mist_build_fabric(self.sandbox)
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual({c["mist"] for c in result["cables"]}, {"linked"})
        self.assertEqual(result["extra"], [])

        m = self.macs
        topology = self.mist.topologies[self.site][0]
        switches = by_mac(topology)
        switches[m["sbx-core-01"]]["downlinks"].remove(m["sbx-acc-01"])
        switches[m["sbx-acc-01"]]["uplinks"].remove(m["sbx-core-01"])
        switches[m["sbx-acc-02"]]["uplinks"].append(m["sbx-acc-01"])
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual(self.cable(result, "sbx323_325_20")["mist"], "not linked")
        self.assertEqual(result["extra"], [{"a_node": "sbx-acc-01", "b_node": "sbx-acc-02"}])
        self.assertFalse(result["summary"]["healthy"])

    def test_a_cable_mist_has_no_use_for_is_skipped(self):
        self.manager.cable(self.sandbox, "sbx-acc-01", "ge-0/0/5", "sbx-acc-02", "ge-0/0/5")
        self.manager.mist_build_fabric(self.sandbox)
        result = self.manager.fabric_check(self.sandbox)
        bridge = next(link.bridge for link in self.sandbox.links if link.a_port == "ge-0/0/5")
        self.assertEqual(self.cable(result, bridge)["mist"], "skipped")
        self.assertTrue(result["summary"]["healthy"])

    def test_without_a_site_lldp_and_mist_are_unknown(self):
        sandbox = self.manager.build_from_shape("campus-ip-clos", "park-b", template_vmid=320)["sandbox"]
        result = self.manager.fabric_check(sandbox)
        self.assertEqual({c["lldp"] for c in result["cables"]}, {"unknown"})
        self.assertEqual({c["mist"] for c in result["cables"]}, {"unknown"})
        self.assertTrue(any("no Mist site" in n for n in result["notes"]), result["notes"])

    def test_an_unreachable_mist_does_not_stop_the_check(self):
        self.mist.fail_reads = True
        result = self.manager.fabric_check(self.sandbox)
        self.assertEqual({c["proxmox"] for c in result["cables"]}, {"ok"})
        self.assertEqual({c["lldp"] for c in result["cables"]}, {"unknown"})
        self.assertTrue(any("Mist" in n for n in result["notes"]), result["notes"])

    def test_the_check_is_saved_and_survives_a_restart(self):
        result = self.manager.fabric_check(self.sandbox)
        again = self.restart().get("park-a")
        self.assertEqual(again.fabric_check["checked_at"], result["checked_at"])
        self.assertEqual(again.fabric_check["summary"], result["summary"])


class TestDriveMistClient(unittest.TestCase):
    def client(self):
        class Recording(MistClient):
            def __init__(self, settings):
                super().__init__(settings)
                self.paths = []

            def _request(self, method, path, payload=None, *, write=False):
                self.paths.append((method, path))
                return {"results": [{"port_id": "ge-0/0/0"}]} if "/stats/" in path else {"id": "t1"}

        return Recording(Settings(mist_token="t", org_id="org-1"))

    def test_the_read_paths(self):
        client = self.client()
        self.assertEqual(client.evpn_topology("s1", "t1"), {"id": "t1"})
        self.assertEqual(client.port_stats("s1"), [{"port_id": "ge-0/0/0"}])
        client.port_stats("s1", mac="020000000301")
        self.assertEqual(
            client.paths,
            [
                ("GET", "/sites/s1/evpn_topologies/t1"),
                ("GET", "/sites/s1/stats/ports/search?limit=1000"),
                ("GET", "/sites/s1/stats/ports/search?mac=020000000301&limit=1000"),
            ],
        )

    def test_a_mist_error_keeps_its_status(self):
        client = MistClient(Settings(mist_token="t", org_id="org-1"), write_gate=lambda: None)
        refused = urllib.error.HTTPError("https://api.mist.com/x", 400, "Bad Request", {}, io.BytesIO(b'{"detail": "invalid switch_configs"}'))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(BackendError) as caught:
                client.put_evpn_topology("s1", {"name": "x"})
        self.assertEqual(caught.exception.status, 400)
        self.assertIn("invalid switch_configs", caught.exception.detail)
        self.assertIsNone(BackendError("plain").status)


class TestDriveApi(Built):
    def setUp(self):
        super().setUp()
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def _post(self, path, body=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(body or {}).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_the_routes(self):
        surface = set(build_router(self.manager).patterns)
        self.assertIn(("POST", "/api/sandboxes/{name}/mist/fabric"), surface)
        self.assertIn(("POST", "/api/sandboxes/{name}/fabric/check"), surface)
        self.assertNotIn(("POST", "/api/sandboxes/{name}/mist/apply"), surface, "Push recipe is gone")

    def test_build_and_check_over_http(self):
        status, body = self._post("/api/sandboxes/park-a/mist/fabric")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["form"], "detailed")
        status, body = self._post("/api/sandboxes/park-a/fabric/check")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["summary"]["cables"], 8)

    def test_the_check_works_read_only_and_the_build_does_not(self):
        with proxmox_may_only_look(self.manager):
            status, body = self._post("/api/sandboxes/park-a/fabric/check")
            self.assertEqual(status, 200, body)
            self.assertIs(body["repair"], False)
            status, body = self._post("/api/sandboxes/park-a/mist/fabric")
        self.assertEqual(status, 409, body)

    def test_a_check_can_be_asked_to_only_look_or_to_fix(self):
        del self.px.networks["sbx323_325_20"]
        status, body = self._post("/api/sandboxes/park-a/fabric/check", {"repair": False})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["repair"], body["summary"]["fixed"]), (False, 0))
        self.assertEqual(self.px.calls, [])
        with proxmox_may_only_look(self.manager):
            status, body = self._post("/api/sandboxes/park-a/fabric/check", {"repair": True})
        self.assertEqual(status, 409, body)
        status, body = self._post("/api/sandboxes/park-a/fabric/check", {"repair": True})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["repair"], body["summary"]["fixed"]), (True, 1))

    def test_repair_must_be_true_or_false_and_anything_else_checks_nothing(self):
        del self.px.networks["sbx323_325_20"]
        for repair in ("false", 0, None):
            with self.subTest(repair=repair):
                status, body = self._post("/api/sandboxes/park-a/fabric/check", {"repair": repair})
                self.assertEqual(status, 400, body)
                self.assertIn('{"repair": false}', body["error"])
        self.assertEqual(self.px.calls, [])
        self.assertIsNone(read_state(self.manager, "park-a")["fabric_check"])


if __name__ == "__main__":
    unittest.main()
