"""Case 9: import a fabric shape from Mist (the "walk" stage).

A shape is the design of a live fabric, read from Mist's EVPN topology: which
switches, in which roles and pods, cabled port to port. Importing one is
read-only. It must never touch Proxmox or Mist, and it must drop everything that
ties it to the live lab: addresses, MACs, and config.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import unittest
import urllib.error
import urllib.request

from simrack.api import serve
from simrack.errors import LabError, NotFound
from simrack.shapes import shape_from_mist
from tests.fakes import PVE_AUDITOR, FakeMist, FakeProxmox, TempDir, make_manager

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "campus.json")

# A live campus fabric's cabling, as LLDP saw it, in sandbox names.
LIVE_CABLES = {
    frozenset({("sbx-core-01", "ge-0/0/0"), ("sbx-bl-01", "ge-0/0/0")}),
    frozenset({("sbx-core-01", "ge-0/0/1"), ("sbx-bl-02", "ge-0/0/0")}),
    frozenset({("sbx-core-02", "ge-0/0/0"), ("sbx-bl-01", "ge-0/0/1")}),
    frozenset({("sbx-core-02", "ge-0/0/1"), ("sbx-bl-02", "ge-0/0/1")}),
    frozenset({("sbx-core-01", "ge-0/0/2"), ("sbx-acc-01", "ge-0/0/0")}),
    frozenset({("sbx-core-01", "ge-0/0/3"), ("sbx-acc-02", "ge-0/0/0")}),
    frozenset({("sbx-core-02", "ge-0/0/2"), ("sbx-acc-01", "ge-0/0/1")}),
    frozenset({("sbx-core-02", "ge-0/0/3"), ("sbx-acc-02", "ge-0/0/1")}),
}


def bundle() -> dict:
    with open(FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


def cables(shape: dict) -> set:
    return {frozenset({(link["a_node"], link["a_port"]), (link["b_node"], link["b_port"])}) for link in shape["links"]}


def nodes(shape: dict) -> dict:
    return {n["name"]: n for n in shape["nodes"]}


class TestShapeFromMist(unittest.TestCase):
    def test_roles_become_sandbox_names_numbered_by_live_name(self):
        shape = shape_from_mist(bundle(), now="2026-10-01T00:00:00Z")
        mapping = {n["name"]: (n["from"], n["role"]) for n in shape["nodes"]}
        self.assertEqual(
            mapping,
            {
                "sbx-bl-01": ("bl-4650-01", "border"),
                "sbx-bl-02": ("bl-4650-02", "border"),
                "sbx-core-01": ("core-4650-01", "core"),
                "sbx-core-02": ("core-4650-02", "core"),
                "sbx-acc-01": ("acc-4400-01", "access"),
                "sbx-acc-02": ("acc-4400-02", "access"),
            },
        )

    def test_access_switches_keep_their_mist_pod_name(self):
        shape = shape_from_mist(bundle())
        by_name = nodes(shape)
        self.assertEqual(by_name["sbx-acc-01"]["pod"], "Pod 1")
        self.assertEqual(by_name["sbx-acc-02"]["pod"], "Pod 1")
        self.assertIsNone(by_name["sbx-core-01"]["pod"])
        self.assertIsNone(by_name["sbx-bl-01"]["pod"])
        self.assertEqual(shape["pods"], [{"id": "1", "name": "Pod 1"}])

    def test_lldp_gives_the_exact_live_cabling(self):
        shape = shape_from_mist(bundle())
        self.assertEqual(cables(shape), LIVE_CABLES)
        self.assertEqual(shape["ports_source"], "lldp")
        self.assertEqual(nodes(shape)["sbx-core-01"]["ports"], ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2", "ge-0/0/3"])
        self.assertEqual(nodes(shape)["sbx-acc-02"]["ports"], ["ge-0/0/0", "ge-0/0/1"])

    def test_wan_and_mist_edge_ports_are_noted_not_cabled(self):
        shape = shape_from_mist(bundle())
        self.assertNotIn(frozenset({"sbx-bl-01", "sbx-bl-02"}), {frozenset({link["a_node"], link["b_node"]}) for link in shape["links"]})
        text = " ".join(shape["notes"])
        self.assertIn("inet", text)
        self.assertIn("me-uplink", text)
        self.assertIn("sbx-bl-01", text)

    def test_without_port_stats_the_ports_come_from_port_config(self):
        doc = bundle()
        del doc["ports"]
        shape = shape_from_mist(doc)
        self.assertEqual(shape["ports_source"], "guessed")
        self.assertEqual(cables(shape), LIVE_CABLES, "Mist's port config order matches the live cabling")
        self.assertTrue(any("guess" in n.lower() for n in shape["notes"]))

    def test_with_no_port_config_either_ports_are_numbered_in_order(self):
        doc = bundle()
        del doc["ports"]
        for switch in doc["topology"]["switches"]:
            switch.pop("config")
        shape = shape_from_mist(doc)
        self.assertEqual(len(shape["links"]), 8)
        for node in shape["nodes"]:
            self.assertEqual(len(node["ports"]), len(set(node["ports"])), f"{node['name']} reuses a port")
        self.assertEqual(nodes(shape)["sbx-core-01"]["ports"], ["ge-0/0/0", "ge-0/0/1", "ge-0/0/2", "ge-0/0/3"])

    def test_live_ports_vjunos_lacks_are_renumbered_and_noted_in_node_order(self):
        doc = bundle()
        names = {d["mac"]: d["name"] for d in doc["devices"]}
        for row in doc["ports"]:
            if not names[row["mac"]].startswith("bl-") and row["port_id"].startswith("ge-0/0/"):
                row["port_id"] = "et-0/0/" + str(48 + int(row["port_id"].rsplit("/", 1)[1]))
        shape = shape_from_mist(doc)
        self.assertEqual(cables(shape), LIVE_CABLES)
        live = {(link[e + "_node"], link[e + "_port"]): link.get(e + "_live_port") for link in shape["links"] for e in ("a", "b")}
        self.assertEqual(live[("sbx-core-01", "ge-0/0/2")], "et-0/0/50")
        self.assertIsNone(live[("sbx-bl-01", "ge-0/0/0")])
        renumbered = [n.split()[0] for n in shape["notes"] if " become " in n]
        self.assertEqual(renumbered, ["sbx-core-01", "sbx-core-02", "sbx-acc-01", "sbx-acc-02"])

    def test_without_device_names_the_mac_identifies_the_live_switch(self):
        doc = bundle()
        del doc["devices"]
        shape = shape_from_mist(doc)
        macs = {s["mac"] for s in doc["topology"]["switches"]}
        self.assertEqual({n["from"] for n in shape["nodes"]}, macs)
        self.assertEqual(sorted(nodes(shape)), ["sbx-acc-01", "sbx-acc-02", "sbx-bl-01", "sbx-bl-02", "sbx-core-01", "sbx-core-02"])
        self.assertEqual(len(shape["links"]), 8)

    def test_kind_and_evpn_summary(self):
        shape = shape_from_mist(bundle())
        self.assertEqual(shape["kind"], "IP Clos")
        self.assertEqual(shape["evpn"], {"routed_at": "edge", "overlay_as": 65000, "underlay_as_base": 65001})
        self.assertEqual(shape["source"]["kind"], "mist")
        self.assertEqual(shape["source"]["topology"], "campus-IP-Clos")

    def test_the_shape_carries_no_addresses_macs_or_config(self):
        doc = bundle()
        text = json.dumps(shape_from_mist(doc))
        for planted in ("198.51.100.", "198.18.", "203.0.113.", "192.0.2.", "port_config", "networks", "router_id", "subnet"):
            self.assertNotIn(planted, text)
        for switch in doc["topology"]["switches"]:
            self.assertNotIn(switch["mac"], text)
        for row in doc["ports"]:
            if row.get("neighbor_mac"):
                self.assertNotIn(row["neighbor_mac"], text)

    def test_name_is_a_safe_slug_and_can_be_overridden(self):
        self.assertEqual(shape_from_mist(bundle())["name"], "campus-ip-clos")
        doc = bundle()
        doc["topology"]["name"] = "  Blue Lake / IP_Clos!! "
        self.assertEqual(shape_from_mist(doc)["name"], "blue-lake-ip-clos")
        doc["name"] = "demo-fabric"
        self.assertEqual(shape_from_mist(doc)["name"], "demo-fabric")
        doc["name"] = "../etc"
        with self.assertRaises(ValueError):
            shape_from_mist(doc)

    def test_separate_files_are_recognised_whatever_the_order(self):
        doc = bundle()
        mixed = {"documents": [{"results": doc["ports"], "limit": 1000}, doc["devices"], doc["topology"]]}
        self.assertEqual(cables(shape_from_mist(mixed)), LIVE_CABLES)
        self.assertEqual(nodes(shape_from_mist(mixed))["sbx-bl-01"]["from"], "bl-4650-01")
        bare = shape_from_mist(doc["topology"])
        self.assertEqual(len(bare["nodes"]), 6)

    def test_the_topology_list_is_refused_with_the_by_id_hint(self):
        listing = [{"id": "00000000-0000-0000-0000-000000000003", "name": "campus-IP-Clos", "site_id": "s1"}]
        with self.assertRaises(LabError) as caught:
            shape_from_mist(listing)
        said = caught.exception.message + " " + caught.exception.detail
        self.assertIn("evpn_topologies/00000000-0000-0000-0000-000000000003", said)
        self.assertIn("campus-IP-Clos", said)

    def test_things_that_are_not_a_topology_are_refused(self):
        for junk in ({"foo": 1}, [], "text", {"documents": []}, {"topology": {"name": "x", "switches": []}}):
            with self.assertRaises(LabError, msg=repr(junk)):
                shape_from_mist(junk)

    def test_two_topologies_at_once_are_refused(self):
        doc = bundle()
        second = copy.deepcopy(doc["topology"])
        second["name"] = "other"
        with self.assertRaises(LabError) as caught:
            shape_from_mist({"topologies": [doc["topology"], second]})
        self.assertIn("other", caught.exception.message + caught.exception.detail)

    def test_collapsed_core_is_evpn_multihoming(self):
        core = [{"mac": f"c{i}", "role": "collapsed-core", "uplinks": [], "downlinks": [], "esilaglinks": ["a1", "a2"]} for i in (1, 2)]
        acc = [{"mac": f"a{i}", "role": "esilag-access", "uplinks": [], "downlinks": [], "esilaglinks": ["c1", "c2"]} for i in (1, 2)]
        shape = shape_from_mist({"name": "mh", "evpn_options": {"routed_at": "core"}, "switches": core + acc})
        self.assertEqual(shape["kind"], "EVPN multihoming")
        self.assertEqual(sorted(nodes(shape)), ["sbx-acc-01", "sbx-acc-02", "sbx-core-01", "sbx-core-02"])
        self.assertEqual(len(shape["links"]), 4)
        self.assertEqual(shape["name"], "mist-mh")


class TestShapeStore(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()

    def tearDown(self):
        self._tmp.__exit__(None, None, None)

    def test_import_saves_lists_and_reimport_overwrites(self):
        manager = make_manager(self.tmp)
        shape = manager.import_shape(bundle())
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "shapes", "campus-ip-clos.json")))
        manager.import_shape(bundle())
        self.assertEqual([s["name"] for s in manager.list_shapes()], ["campus-ip-clos"])
        self.assertEqual(manager.get_shape("campus-ip-clos")["links"], shape["links"])
        self.assertEqual([s["name"] for s in manager.state()["shapes"]], ["campus-ip-clos"])

    def test_import_works_read_only_and_touches_nothing(self):
        proxmox, mist = FakeProxmox(privileges=PVE_AUDITOR), FakeMist(role="read")
        manager = make_manager(self.tmp, proxmox=proxmox, mist=mist)
        manager.import_shape(bundle())
        self.assertEqual(proxmox.calls, [])
        self.assertEqual(mist.calls, [])
        self.assertEqual(manager.sandboxes, {})

    def test_shapes_survive_a_restart_and_can_be_deleted(self):
        make_manager(self.tmp).import_shape(bundle())
        again = make_manager(self.tmp)
        self.assertEqual([s["name"] for s in again.list_shapes()], ["campus-ip-clos"])
        self.assertTrue(again.delete_shape("campus-ip-clos"))
        self.assertEqual(again.list_shapes(), [])
        with self.assertRaises(NotFound):
            again.delete_shape("campus-ip-clos")
        with self.assertRaises(NotFound):
            again.get_shape("campus-ip-clos")

    def test_a_damaged_shape_file_does_not_stop_the_app(self):
        os.makedirs(os.path.join(self.tmp, "shapes"))
        with open(os.path.join(self.tmp, "shapes", "broken.json"), "w", encoding="utf-8") as handle:
            handle.write("{not json")
        self.assertEqual(make_manager(self.tmp).list_shapes(), [])

    def test_bad_shape_names_are_refused(self):
        manager = make_manager(self.tmp)
        for bad in ("../etc", "A", "x/y"):
            with self.assertRaises(ValueError):
                manager.get_shape(bad)


class TestShapeApi(unittest.TestCase):
    def setUp(self):
        self._tmp = TempDir()
        self.tmp = self._tmp.__enter__()
        self.manager = make_manager(self.tmp, proxmox=FakeProxmox(privileges=PVE_AUDITOR))
        self.httpd = serve(self.manager, "127.0.0.1", 0, token="")
        self.httpd.log = lambda message: None
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._tmp.__exit__(None, None, None)

    def _call(self, path, body=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = urllib.request.Request(self.base + path, data=data, method="POST" if data is not None else "GET", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as reply:
                return reply.status, json.loads(reply.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def test_import_view_and_delete_over_http_in_read_only_mode(self):
        status, shape = self._call("/api/shapes", bundle())
        self.assertEqual(status, 200)
        self.assertEqual(shape["name"], "campus-ip-clos")
        self.assertEqual(self._call("/api/shapes/campus-ip-clos")[1]["kind"], "IP Clos")
        self.assertEqual([s["name"] for s in self._call("/api/state")[1]["shapes"]], ["campus-ip-clos"])
        self.assertEqual(self._call("/api/shapes/campus-ip-clos/delete", {})[0], 200)
        self.assertEqual(self._call("/api/shapes/campus-ip-clos")[0], 404)

    def test_a_bad_document_is_a_400_with_a_reason(self):
        status, reply = self._call("/api/shapes", {"documents": [[{"id": "abc", "name": "x"}]]})
        self.assertEqual(status, 400)
        self.assertIn("evpn_topologies/abc", reply["error"] + reply.get("detail", ""))

    def test_oversized_bodies_are_refused(self):
        status, reply = self._call("/api/shapes", raw=b'{"pad": "' + b"x" * (5 * 1024 * 1024) + b'"}')
        self.assertEqual(status, 413)
        self.assertEqual(self.manager.list_shapes(), [])


if __name__ == "__main__":
    unittest.main()
