"""Phase 5 end-to-end tests for standalone dashboard.demo_feeder."""
import json
import os
import socketserver
import tempfile
import threading
import time
import unittest
import uvicorn

from fastapi.testclient import TestClient

from dashboard.demo_feeder import DEFAULT_4NODE_TOPOLOGY, run_demo
from dashboard.server import create_app


class _LiveServerThread:
    """Runs FastAPI uvicorn server on an ephemeral localhost port for real HTTP + SSE tests."""

    def __init__(self, app):
        self.app = app
        self.server = None
        self.thread = None
        self.port = None

    def start(self):
        # Find an ephemeral free port
        with socketserver.TCPServer(("127.0.0.1", 0), None) as s:
            self.port = s.server_address[1]
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 5.0
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        if self.server:
            self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=3.0)


class TestDemoFeederEndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aqua_demo_")
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "demo.db")
        self.token = "demo-secret"

    def _start_live(self, topo_path=None):
        app = create_app(
            db_path=self.db_path,
            auth_token=self.token,
            topology_path=topo_path,
            enable_outage_sim=True,
        )
        srv = _LiveServerThread(app)
        base = srv.start()
        self.addCleanup(srv.stop)
        return base, app

    def test_1_single_node_and_unavailable_metrics(self):
        base, _ = self._start_live(topo_path=None)
        s_dir = os.path.join(self.tmp.name, "outbox_1node")
        shipper = run_demo(
            scenario="1node",
            mode="edge",
            interval_s=0.0,
            gateway_id="demo-1node",
            endpoint=f"{base}/api/v1/ingest",
            auth_token=self.token,
            storage_dir=s_dir,
            steps=4,
            log=lambda *_: None,
        )
        self.assertEqual(shipper.metrics()["pending_frames"], 0)
        self.assertEqual(shipper.metrics()["delivered_frames"], 4)

        with TestClient(create_app(db_path=self.db_path, auth_token=self.token, topology_path=None)) as c:
            latest = c.get("/api/v1/snapshots/latest", params={"gateway_id": "demo-1node"}).json()["frame"]
            self.assertEqual(latest["node_count"], 1)
            self.assertIsNone(latest["snapshot"]["leakage"]["last_balance_pct"])
            tel = c.get("/api/v1/telemetry", params={"gateway_id": "demo-1node"}).json()["points"]
            self.assertEqual(len(tel), 4)
            self.assertTrue(all(p["balance_pct"] is None for p in tel))

    def test_2_four_node_and_mode_switching_same_schema(self):
        base, _ = self._start_live(topo_path=DEFAULT_4NODE_TOPOLOGY)
        run_demo(
            scenario="4node",
            mode="edge",
            interval_s=0.0,
            gateway_id="demo-4-edge",
            topology_path=DEFAULT_4NODE_TOPOLOGY,
            endpoint=f"{base}/api/v1/ingest",
            auth_token=self.token,
            storage_dir=os.path.join(self.tmp.name, "outbox_4_edge"),
            steps=5,
            log=lambda *_: None,
        )
        run_demo(
            scenario="4node",
            mode="cloud",
            interval_s=0.0,
            gateway_id="demo-4-cloud",
            topology_path=DEFAULT_4NODE_TOPOLOGY,
            endpoint=f"{base}/api/v1/ingest",
            auth_token=self.token,
            storage_dir=os.path.join(self.tmp.name, "outbox_4_cloud"),
            steps=5,
            log=lambda *_: None,
        )
        with TestClient(create_app(db_path=self.db_path, auth_token=self.token, topology_path=DEFAULT_4NODE_TOPOLOGY)) as c:
            f_edge = c.get("/api/v1/snapshots/latest", params={"gateway_id": "demo-4-edge"}).json()["frame"]
            f_cloud = c.get("/api/v1/snapshots/latest", params={"gateway_id": "demo-4-cloud"}).json()["frame"]
            self.assertEqual(f_edge["node_count"], 4)
            self.assertEqual(f_cloud["node_count"], 4)
            self.assertEqual(f_edge["mode"], "edge")
            self.assertEqual(f_cloud["mode"], "cloud")
            self.assertEqual(set(f_edge["snapshot"].keys()), set(f_cloud["snapshot"].keys()))
            self.assertEqual(c.get("/api/v1/topology").json()["topology"]["building"], "Demo-4Node")

    def test_3_outage_buffering_ordered_recovery_sse_and_restart(self):
        base, app = self._start_live(topo_path=DEFAULT_4NODE_TOPOLOGY)
        s_dir = os.path.join(self.tmp.name, "outbox_outage")

        # Run 6 steps with outage at step 2..4 (steps 2,3,4 buffered; step 5+final flush recovers)
        shipper = run_demo(
            scenario="4node",
            mode="edge",
            interval_s=0.0,
            gateway_id="demo-outage",
            topology_path=DEFAULT_4NODE_TOPOLOGY,
            endpoint=f"{base}/api/v1/ingest",
            auth_token=self.token,
            storage_dir=s_dir,
            steps=6,
            outage_at=2,
            outage_steps=3,
            log=lambda *_: None,
        )
        self.assertEqual(shipper.metrics()["pending_frames"], 0)
        self.assertEqual(shipper.metrics()["delivered_frames"], 6)
        self.assertGreaterEqual(shipper.metrics()["retries"], 1)

        # Duplicate prevention: re-enqueuing an existing seq is deduplicated by server
        frames = app.state.store.get_frames_since(since_id=0, gateway_id="demo-outage")
        self.assertEqual([f["seq"] for f in frames], [1, 2, 3, 4, 5, 6])
        self.assertFalse(frames[0]["buffered"])
        self.assertTrue(frames[2]["buffered"])

        # SSE replay + historical persistence after backend restart
        with TestClient(create_app(db_path=self.db_path, auth_token=self.token, topology_path=DEFAULT_4NODE_TOPOLOGY)) as c2:
            sse = c2.get("/api/v1/stream", params={"gateway_id": "demo-outage", "once": "true"}, headers={"Last-Event-ID": "3"})
            self.assertEqual(sse.status_code, 200)
            self.assertIn("id: 4\n", sse.text)
            self.assertIn("id: 6\n", sse.text)
            hist = c2.get("/api/v1/incidents", params={"gateway_id": "demo-outage", "status": "resolved"}).json()["incidents"]
            self.assertGreaterEqual(len(hist), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
