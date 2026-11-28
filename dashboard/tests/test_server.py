"""Phase 3 tests for FastAPI + SQLite dashboard backend (dashboard.server & dashboard.store)."""
import json
import os
import tempfile
import threading
import time
import unittest

from fastapi.testclient import TestClient

from dashboard.server import create_app
from dashboard.shipper import SCHEMA_VERSION
from gateway.core import Gateway
from gateway.topology import Topology
from hal.packet import sim_key


def _dummy_hal(t=1000.0):
    class _HAL:
        key = staticmethod(sim_key)
        def now(self): return t
        def receive(self): return []
        def send(self, *a, **kw): pass
    return _HAL()


def _topo_1_node():
    return {
        "schema_version": 1,
        "building": "SingleNodeDemo",
        "root": {
            "id": "B-TANK",
            "type": "tank",
            "valve": True,
            "children": [
                {"id": "B-101", "type": "flat", "valve": True},
            ],
        },
    }


def _topo_4_nodes():
    # 1 root + 3 flat children = 4 nodes total (1 physical + 3 virtual)
    return {
        "schema_version": 1,
        "building": "Hybrid4Node",
        "root": {
            "id": "B-TANK",
            "type": "tank",
            "valve": True,
            "children": [
                {"id": "B-101", "type": "flat", "valve": True},
                {"id": "B-102", "type": "flat", "valve": True},
                {"id": "B-103", "type": "flat", "valve": True},
            ],
        },
    }


def _topo_branched_6_nodes():
    return {
        "schema_version": 1,
        "building": "BranchedBuilding",
        "root": {
            "id": "B-TANK",
            "type": "tank",
            "valve": True,
            "children": [
                {
                    "id": "B-D4",
                    "type": "junction",
                    "label": "west",
                    "valve": True,
                    "children": [
                        {"id": "B-401", "type": "flat", "valve": True},
                        {"id": "B-402", "type": "flat", "valve": True},
                    ],
                },
                {
                    "id": "B-D2",
                    "type": "junction",
                    "label": "east",
                    "valve": False,
                    "children": [
                        {"id": "B-201", "type": "flat", "valve": True},
                        {"id": "B-202", "type": "flat", "valve": True},
                        {"id": "B-203", "type": "flat", "valve": True},
                    ],
                },
            ],
        },
    }


def _hdr(token="test-token"):
    return {"Authorization": f"Bearer {token}"}


class TestDashboardBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aqua_srv_")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "dash.db")
        self.topo_path = os.path.join(self.tmp.name, "topo.json")
        with open(self.topo_path, "w", encoding="utf-8") as f:
            json.dump(_topo_4_nodes(), f)

    def _make_client(self, topo_path=None):
        tp = self.topo_path if topo_path is ... else topo_path
        app = create_app(db_path=self.db_path, auth_token="test-token", topology_path=tp)
        return TestClient(app)

    def test_one_node_empty_and_null_mass_balance(self):
        one_topo_path = os.path.join(self.tmp.name, "one.json")
        with open(one_topo_path, "w", encoding="utf-8") as f:
            json.dump(_topo_1_node(), f)

        # Single-node snapshot with only 1 reporting node, empty incidents/queue, null balance_pct
        snap_1 = {
            "t": 1000.0,
            "building_status": "NORMAL",
            "maintenance_queue": [],
            "active_incidents": [],
            "incident_history": [],
            "water_quality": {"class": "NORMAL", "compliant": True},
            "leakage": {"pipe_leak_active": False, "last_balance_pct": None, "flat_leaks": []},
            "supply": {"state": "NORMAL", "level_m": None, "inflow_lps": None},
            "abnormal_consumption": [],
            "network_health": {"silent_nodes": []},
            "nodes": {"B-101": {"status": "ok", "last_seen": 1000.0, "last_sum": None}},
        }
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=one_topo_path)) as c:
            r = c.post(
                "/api/v1/ingest",
                headers=_hdr(),
                json={
                    "schema_version": SCHEMA_VERSION,
                    "gateway_id": "pi-solo",
                    "mode": "edge",
                    "frames": [{"seq": 1, "created_at_utc": 1000.0, "buffered": False, "snapshot": snap_1}],
                },
            )
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["acked_seq"], 1)

            latest = c.get("/api/v1/snapshots/latest", params={"gateway_id": "pi-solo"}).json()["frame"]
            self.assertEqual(latest["node_count"], 1)
            self.assertIsNone(latest["snapshot"]["leakage"]["last_balance_pct"])
            self.assertEqual(latest["snapshot"]["active_incidents"], [])
            self.assertEqual(latest["snapshot"]["maintenance_queue"], [])

            tel = c.get("/api/v1/telemetry", params={"gateway_id": "pi-solo"}).json()["points"]
            self.assertEqual(len(tel), 1)
            # Crucial: null mass balance must remain None, never fabricated 0.0
            self.assertIsNone(tel[0]["balance_pct"])
            self.assertIsNone(tel[0]["tank_level_m"])

    def test_four_node_and_branched_topology_retrieval(self):
        # 4-node Gateway.snapshot() ingestion
        gw4 = Gateway(_dummy_hal(2000.0), Topology(_topo_4_nodes()))
        snap4 = gw4.snapshot(now=2000.0)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c:
            r = c.post(
                "/api/v1/ingest",
                headers=_hdr(),
                json={
                    "schema_version": SCHEMA_VERSION,
                    "gateway_id": "gw-4node",
                    "mode": "edge",
                    "frames": [{"seq": 1, "created_at_utc": 2000.0, "buffered": False, "snapshot": snap4}],
                },
            )
            self.assertEqual(r.status_code, 200)
            latest = c.get("/api/v1/snapshots/latest", params={"gateway_id": "gw-4node"}).json()["frame"]
            self.assertEqual(latest["node_count"], 4)
            topo = c.get("/api/v1/topology").json()["topology"]
            self.assertEqual(topo["building"], "Hybrid4Node")
            self.assertEqual(len(topo["root"]["children"]), 3)

        # Branched 6-node topology served dynamically without code changes
        br_path = os.path.join(self.tmp.name, "branched.json")
        with open(br_path, "w", encoding="utf-8") as f:
            json.dump(_topo_branched_6_nodes(), f)
        gw_br = Gateway(_dummy_hal(3000.0), Topology(_topo_branched_6_nodes()))
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=br_path)) as c2:
            r2 = c2.post(
                "/api/v1/ingest",
                headers=_hdr(),
                json={
                    "schema_version": SCHEMA_VERSION,
                    "gateway_id": "gw-branched",
                    "mode": "cloud",
                    "frames": [{"seq": 1, "created_at_utc": 3000.0, "buffered": False, "snapshot": gw_br.snapshot(3000.0)}],
                },
            )
            self.assertEqual(r2.status_code, 200)
            self.assertEqual(c2.get("/api/v1/snapshots/latest", params={"gateway_id": "gw-branched"}).json()["frame"]["node_count"], 6)
            topo_br = c2.get("/api/v1/topology").json()["topology"]
            self.assertEqual([ch["label"] for ch in topo_br["root"]["children"]], ["west", "east"])

    def test_edge_cloud_same_schema_auth_and_outage_sim(self):
        gw = Gateway(_dummy_hal(4000.0), Topology(_topo_4_nodes()))
        snap = gw.snapshot(now=4000.0)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c:
            # Invalid auth -> 401
            r_unauth = c.post(
                "/api/v1/ingest",
                headers=_hdr("bad"),
                json={"schema_version": 1, "gateway_id": "gw", "mode": "edge",
                      "frames": [{"seq": 1, "created_at_utc": 1.0, "buffered": False, "snapshot": snap}]},
            )
            self.assertEqual(r_unauth.status_code, 401)

            # Invalid schema version -> 422
            r_ver = c.post(
                "/api/v1/ingest",
                headers=_hdr(),
                json={"schema_version": 99, "gateway_id": "gw", "mode": "edge",
                      "frames": [{"seq": 1, "created_at_utc": 1.0, "buffered": False, "snapshot": snap}]},
            )
            self.assertEqual(r_ver.status_code, 422)

            # Simulated Internet outage requires auth -> 401 without token, 200 with token
            self.assertEqual(c.post("/api/v1/sim/outage", json={"enabled": True}).status_code, 401)
            self.assertEqual(c.post("/api/v1/sim/outage", headers=_hdr(), json={"enabled": True}).status_code, 200)
            r_out = c.post(
                "/api/v1/ingest",
                headers=_hdr(),
                json={"schema_version": 1, "gateway_id": "gw", "mode": "edge",
                      "frames": [{"seq": 1, "created_at_utc": 1.0, "buffered": False, "snapshot": snap}]},
            )
            self.assertEqual(r_out.status_code, 503)
            self.assertEqual(c.post("/api/v1/sim/outage", headers=_hdr(), json={"enabled": False}).status_code, 200)

            # Oversized body (> 1 MiB) rejected with 413 via streaming limit
            oversized = b'{"x":"' + (b"a" * 1_050_000) + b'"}'
            r_big = c.post("/api/v1/ingest", headers={**_hdr(), "Content-Type": "application/json"}, content=oversized)
            self.assertEqual(r_big.status_code, 413)

            # Edge and cloud ingested with identical snapshot schema & independent cursors
            c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-edge", "mode": "edge",
                "frames": [{"seq": 1, "created_at_utc": 4000.0, "buffered": False, "snapshot": snap}],
            })
            c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-cloud", "mode": "cloud",
                "frames": [
                    {"seq": 1, "created_at_utc": 4000.0, "buffered": False, "snapshot": snap},
                    {"seq": 2, "created_at_utc": 4060.0, "buffered": False, "snapshot": snap},
                ],
            })
            f_edge = c.get("/api/v1/snapshots/latest", params={"gateway_id": "gw-edge"}).json()["frame"]
            f_cloud = c.get("/api/v1/snapshots/latest", params={"gateway_id": "gw-cloud"}).json()["frame"]
            self.assertEqual(f_edge["mode"], "edge")
            self.assertEqual(f_cloud["mode"], "cloud")
            self.assertEqual(f_edge["snapshot"], f_cloud["snapshot"])
            gws = {g["gateway_id"]: g["acked_seq"] for g in c.get("/api/v1/gateways").json()["gateways"]}
            self.assertEqual(gws, {"gw-edge": 1, "gw-cloud": 2})

        # Outage simulation disabled -> 403
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path, enable_outage_sim=False)) as c_prod:
            self.assertEqual(c_prod.post("/api/v1/sim/outage", headers=_hdr(), json={"enabled": True}).status_code, 403)

    def test_duplicates_out_of_order_and_contiguous_ack(self):
        snap = Gateway(_dummy_hal(1000.0), Topology(_topo_1_node())).snapshot(1000.0)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c:
            # 1 arrives -> acked_seq = 1
            r1 = c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-ooo", "mode": "edge",
                "frames": [{"seq": 1, "created_at_utc": 1001.0, "buffered": False, "snapshot": snap}],
            }).json()
            self.assertEqual((r1["accepted"], r1["duplicates"], r1["acked_seq"]), (1, 0, 1))

            # 3 arrives out-of-order (2 missing) -> stored, but acked_seq MUST stay 1!
            r3 = c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-ooo", "mode": "edge",
                "frames": [{"seq": 3, "created_at_utc": 1003.0, "buffered": True, "snapshot": snap}],
            }).json()
            self.assertEqual((r3["accepted"], r3["duplicates"], r3["acked_seq"]), (1, 0, 1))

            # Duplicate batch of [1, 3] -> 0 accepted, 2 duplicates, acked_seq still 1
            r_dup = c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-ooo", "mode": "edge",
                "frames": [
                    {"seq": 1, "created_at_utc": 1001.0, "buffered": False, "snapshot": snap},
                    {"seq": 3, "created_at_utc": 1003.0, "buffered": True, "snapshot": snap},
                ],
            }).json()
            self.assertEqual((r_dup["accepted"], r_dup["duplicates"], r_dup["acked_seq"]), (0, 2, 1))

            # 2 arrives -> contiguous floor jumps to 3!
            r2 = c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-ooo", "mode": "edge",
                "frames": [{"seq": 2, "created_at_utc": 1002.0, "buffered": True, "snapshot": snap}],
            }).json()
            self.assertEqual((r2["accepted"], r2["duplicates"], r2["acked_seq"]), (1, 0, 3))

    def test_restart_persistence_and_incident_history(self):
        gw = Gateway(_dummy_hal(5000.0), Topology(_topo_4_nodes()))
        gw.decide(5000.0, "PIPE_LEAK", suspected_floor=None, error_pct=32.0)
        snap_active = gw.snapshot(now=5060.0)
        gw.analytics.clear_pipe_leak(t_resolved=5300.0)
        snap_resolved = gw.snapshot(now=5310.0)

        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c1:
            r = c1.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-persist", "mode": "edge",
                "frames": [
                    {"seq": 1, "created_at_utc": 5060.0, "buffered": False, "snapshot": snap_active},
                    {"seq": 2, "created_at_utc": 5310.0, "buffered": False, "snapshot": snap_resolved},
                ],
            })
            self.assertEqual(r.json()["acked_seq"], 2)

        # Simulate server restart with a brand-new FastAPI app pointing at the same SQLite DB
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c2:
            gws = c2.get("/api/v1/gateways").json()["gateways"]
            self.assertEqual(gws[0]["acked_seq"], 2)
            latest = c2.get("/api/v1/snapshots/latest", params={"gateway_id": "gw-persist"}).json()["frame"]
            self.assertEqual(latest["seq"], 2)
            hist = c2.get("/api/v1/incidents", params={"gateway_id": "gw-persist", "status": "resolved"}).json()["incidents"]
            self.assertEqual(len(hist), 1)
            self.assertEqual(hist[0]["resolution_reason"], "mass_balance_rearmed")

    def test_sse_reconnect_recovery_and_live_stream(self):
        snap = Gateway(_dummy_hal(6000.0), Topology(_topo_1_node())).snapshot(6000.0)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)) as c:
            c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-sse", "mode": "edge",
                "frames": [
                    {"seq": 1, "created_at_utc": 6001.0, "buffered": False, "snapshot": snap},
                    {"seq": 2, "created_at_utc": 6002.0, "buffered": False, "snapshot": snap},
                ],
            })
            # Reconnect with Last-Event-ID: 1 -> replays missed id 2 from SQLite
            sse_resp = c.get(
                "/api/v1/stream",
                params={"gateway_id": "gw-sse", "once": "true"},
                headers={"Last-Event-ID": "1"},
            )
            self.assertEqual(sse_resp.status_code, 200)
            self.assertIn("id: 2\n", sse_resp.text)
            self.assertIn("event: snapshot\n", sse_resp.text)
            self.assertNotIn("id: 1\n", sse_resp.text)

            # Live stream: subscribe via max_events=1 while background thread posts seq 3
            def _post_live():
                time.sleep(0.15)
                c.post("/api/v1/ingest", headers=_hdr(), json={
                    "schema_version": 1, "gateway_id": "gw-sse", "mode": "edge",
                    "frames": [{"seq": 3, "created_at_utc": 6003.0, "buffered": False, "snapshot": snap}],
                })

            t = threading.Thread(target=_post_live, daemon=True)
            t.start()
            live_resp = c.get("/api/v1/stream", params={"gateway_id": "gw-sse", "since_id": 2, "max_events": 1})
            t.join(timeout=2.0)
            self.assertIn("id: 3\n", live_resp.text)

    def test_missing_or_invalid_topology_no_hardcoded_fallback(self):
        # 1. None configured -> 404 (never falls back to building_B.json)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=None)) as c_none:
            self.assertEqual(c_none.get("/api/v1/topology").status_code, 404)
            self.assertFalse(c_none.get("/api/v1/health").json()["topology_configured"])

        # 2. Nonexistent path -> 404
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=os.path.join(self.tmp.name, "nope.json"))) as c_miss:
            self.assertEqual(c_miss.get("/api/v1/topology").status_code, 404)

        # 3. Invalid JSON/schema -> 422
        bad_topo = os.path.join(self.tmp.name, "bad_topo.json")
        with open(bad_topo, "w", encoding="utf-8") as f:
            json.dump({"schema_version": 99, "building": "X", "root": {}}, f)
        with TestClient(create_app(db_path=self.db_path, auth_token="test-token", topology_path=bad_topo)) as c_bad:
            self.assertEqual(c_bad.get("/api/v1/topology").status_code, 422)

    def test_sse_overflow_terminates_for_replay(self):
        from dashboard.store import EventBroker, OVERFLOW_SENTINEL

        # Broker with tiny max_queue=2: pushing 3 events puts OVERFLOW_SENTINEL on queue
        broker = EventBroker(max_queue=2)
        q = broker.subscribe()
        broker.publish({"id": 1, "event": "snapshot", "data": {}})
        broker.publish({"id": 2, "event": "snapshot", "data": {}})
        broker.publish({"id": 3, "event": "snapshot", "data": {}})
        self.assertIs(q.get_nowait(), OVERFLOW_SENTINEL)
        broker.unsubscribe(q)

        # End-to-end stream: inject OVERFLOW_SENTINEL on live connection -> stream emits resync and terminates,
        # then client reconnects with Last-Event-ID and recovers all missed frames from SQLite.
        snap = Gateway(_dummy_hal(7000.0), Topology(_topo_1_node())).snapshot(7000.0)
        app = create_app(db_path=self.db_path, auth_token="test-token", topology_path=self.topo_path)
        app.state.store.broker._max_queue = 2
        with TestClient(app) as c:
            c.post("/api/v1/ingest", headers=_hdr(), json={
                "schema_version": 1, "gateway_id": "gw-ovf", "mode": "edge",
                "frames": [{"seq": 1, "created_at_utc": 7001.0, "buffered": False, "snapshot": snap}],
            })

            def _flood():
                time.sleep(0.1)
                c.post("/api/v1/ingest", headers=_hdr(), json={
                    "schema_version": 1, "gateway_id": "gw-ovf", "mode": "edge",
                    "frames": [
                        {"seq": 2, "created_at_utc": 7002.0, "buffered": False, "snapshot": snap},
                        {"seq": 3, "created_at_utc": 7003.0, "buffered": False, "snapshot": snap},
                        {"seq": 4, "created_at_utc": 7004.0, "buffered": False, "snapshot": snap},
                    ],
                })

            t = threading.Thread(target=_flood, daemon=True)
            t.start()
            # Stream terminates cleanly on overflow instead of silently dropping events
            ovf_resp = c.get("/api/v1/stream", params={"gateway_id": "gw-ovf", "since_id": 1})
            t.join(timeout=2.0)
            self.assertIn("event: resync\n", ovf_resp.text)

            # Reconnect with Last-Event-ID: 1 recovers frames 2, 3, 4 from SQLite
            rec_resp = c.get(
                "/api/v1/stream",
                params={"gateway_id": "gw-ovf", "once": "true"},
                headers={"Last-Event-ID": "1"},
            )
            self.assertIn("id: 2\n", rec_resp.text)
            self.assertIn("id: 3\n", rec_resp.text)
            self.assertIn("id: 4\n", rec_resp.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
