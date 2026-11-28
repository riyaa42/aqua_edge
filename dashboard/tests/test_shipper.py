"""Unit tests and mock HTTP server integration tests for dashboard.shipper."""
import hmac
import http.server
import json
import os
import socketserver
import tempfile
import threading
import unittest

from dashboard.shipper import DashboardShipper, MAX_BATCH_SIZE, OutboxStorageError, SCHEMA_VERSION
from gateway.core import Gateway
from gateway.topology import Topology
from hal.packet import sim_key


def _minimal_snapshot(t=1000.0, status="NORMAL"):
    return {
        "t": t,
        "building_status": status,
        "maintenance_queue": [],
        "active_incidents": [],
        "incident_history": [],
        "water_quality": {"class": "NORMAL", "compliant": True},
        "leakage": {"pipe_leak_active": False, "flat_leaks": []},
        "supply": {"state": "NORMAL"},
        "abnormal_consumption": [],
        "network_health": {"silent_nodes": []},
        "nodes": {},
    }


class _MockIngestState:
    def __init__(self, token="secret-token"):
        self.token = token
        self.outage = False
        self.drop_ack_once = False
        self.frames = {}       # (gateway_id, seq) -> record
        self.batches = []      # raw envelopes received (valid auth)
        self.lock = threading.Lock()


def _make_handler(state):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass

        def _send_json(self, code, obj):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != "/api/v1/ingest":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            auth = self.headers.get("Authorization", "")
            expected = f"Bearer {state.token}"
            if not hmac.compare_digest(auth, expected):
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return
            if state.outage:
                self._send_json(503, {"ok": False, "error": "simulated internet outage"})
                return

            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            env = json.loads(raw.decode("utf-8"))
            if env.get("schema_version") != SCHEMA_VERSION:
                self._send_json(422, {"ok": False, "error": "unsupported schema_version"})
                return
            if env.get("mode") not in ("edge", "cloud"):
                self._send_json(422, {"ok": False, "error": "invalid mode"})
                return

            frames = env.get("frames", [])
            if not (1 <= len(frames) <= MAX_BATCH_SIZE):
                self._send_json(400, {"ok": False, "error": "invalid batch size"})
                return

            gid = env["gateway_id"]
            accepted, duplicates, max_seq = 0, 0, 0
            with state.lock:
                state.batches.append(env)
                for fr in frames:
                    seq = int(fr["seq"])
                    max_seq = max(max_seq, seq)
                    key = (gid, seq)
                    if key in state.frames:
                        duplicates += 1
                    else:
                        state.frames[key] = {**fr, "mode": env["mode"]}
                        accepted += 1
                drop_now = state.drop_ack_once
                state.drop_ack_once = False

            if drop_now:
                # Simulate lost HTTP ACK after server committed the frame (close without response).
                self.close_connection = True
                return

            self._send_json(
                200,
                {
                    "ok": True,
                    "gateway_id": gid,
                    "mode": env["mode"],
                    "acked_seq": max_seq,
                    "accepted": accepted,
                    "duplicates": duplicates,
                },
            )

    return Handler


class _ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


class TestShipperAndOutbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aqua_shipper_")
        self.addCleanup(self.tmp.cleanup)

    def test_restart_persistence_and_corrupt_tail_recovery(self):
        now = [1000.0]
        s1 = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            clock_fn=lambda: now[0],
        )
        seq1 = s1.enqueue(_minimal_snapshot(1000.0), created_at_utc=1000.0)
        seq2 = s1.enqueue(_minimal_snapshot(1060.0), created_at_utc=1060.0)
        self.assertEqual((seq1, seq2), (1, 2))

        # Simulate unclean power cut leaving a truncated trailing line in outbox.jsonl
        with open(s1.outbox_path, "a", encoding="utf-8") as f:
            f.write('{"seq": 3, "snapshot": {"broken')

        s2 = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            clock_fn=lambda: 1120.0,
        )
        self.assertEqual(s2.next_seq, 3)
        self.assertEqual([r["seq"] for r in s2.queue], [1, 2])
        self.assertTrue(all(r["buffered"] for r in s2.queue))
        m = s2.metrics()
        self.assertEqual(m["pending_frames"], 2)
        self.assertEqual(m["oldest_queued_created_at_utc"], 1000.0)
        self.assertEqual(m["oldest_queued_age_s"], 120.0)

    def test_backoff_and_invalid_ack_never_deletes(self):
        now = [100.0]
        s = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            base_backoff_s=2.0,
            max_backoff_s=30.0,
            jitter_fn=lambda: 1.0,
            clock_fn=lambda: now[0],
        )
        s.enqueue(_minimal_snapshot(100.0), created_at_utc=100.0)

        # Fake _post_batch returning invalid ACK (wrong gateway_id)
        s._post_batch = lambda batch: {"ok": True, "gateway_id": "other-gw", "acked_seq": 1}
        self.assertFalse(s.flush_once())
        self.assertEqual(len(s.queue), 1)
        self.assertTrue(s.queue[0]["buffered"])
        self.assertAlmostEqual(s.next_attempt_at, 102.0)

        # Should skip attempt while inside backoff window unless forced
        self.assertFalse(s.flush_once())
        now[0] = 102.0
        self.assertFalse(s.flush_once())
        self.assertAlmostEqual(s.next_attempt_at, 106.0)  # 2s -> 4s
        self.assertEqual(len(s.queue), 1)
        self.assertEqual(s.metrics()["retries"], 2)

    def test_bounded_batch_order_of_120_frames(self):
        sent_batches = []
        s = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            max_batch=50,
            clock_fn=lambda: 5000.0,
        )
        for i in range(120):
            s.enqueue(_minimal_snapshot(1000.0 + i), created_at_utc=1000.0 + i)

        def fake_post(batch):
            sent_batches.append([r["seq"] for r in batch])
            return {"ok": True, "gateway_id": "building-B", "acked_seq": batch[-1]["seq"]}

        s._post_batch = fake_post
        self.assertTrue(s.flush_all())
        self.assertEqual([len(b) for b in sent_batches], [50, 50, 20])
        self.assertEqual(sent_batches[0][0], 1)
        self.assertEqual(sent_batches[-1][-1], 120)
        self.assertEqual(s.metrics()["pending_frames"], 0)
        self.assertEqual(s.metrics()["delivered_frames"], 120)

    def test_mock_http_server_outage_lost_ack_auth_and_modes(self):
        state = _MockIngestState(token="good-token")
        srv = _ThreadedHTTPServer(("127.0.0.1", 0), _make_handler(state))
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(srv.server_close)

        url = f"http://127.0.0.1:{port}/api/v1/ingest"
        now = [2000.0]

        # 1. Invalid auth leaves frame safely queued in outbox
        bad_shipper = DashboardShipper(
            endpoint=url,
            storage_dir=os.path.join(self.tmp.name, "bad_auth"),
            gateway_id="building-B",
            mode="edge",
            auth_token="wrong-token",
            clock_fn=lambda: now[0],
        )
        bad_shipper.publish(_minimal_snapshot(2000.0), created_at_utc=2000.0)
        self.assertEqual(bad_shipper.metrics()["pending_frames"], 1)
        self.assertIn("401", bad_shipper.metrics()["last_error"])

        # 2. Live delivery using an actual empty Gateway.snapshot()
        class _DummyHAL:
            key = staticmethod(sim_key)
            def now(self): return 2000.0
            def receive(self): return []
            def send(self, *a, **kw): pass

        gw = Gateway(_DummyHAL(), Topology.load())
        empty_snap = gw.snapshot(now=2000.0)

        shipper = DashboardShipper(
            endpoint=url,
            storage_dir=os.path.join(self.tmp.name, "edge"),
            gateway_id="building-B",
            mode="edge",
            auth_token="good-token",
            jitter_fn=lambda: 1.0,
            clock_fn=lambda: now[0],
        )
        shipper.publish(empty_snap, created_at_utc=2000.0)
        self.assertEqual(shipper.metrics()["pending_frames"], 0)
        self.assertEqual(shipper.metrics()["delivered_frames"], 1)
        self.assertFalse(state.frames[("building-B", 1)]["buffered"])

        # 3. Internet outage after live delivery -> frames queue with original created_at_utc
        state.outage = True
        now[0] = 2060.0
        shipper.publish(_minimal_snapshot(2060.0, "HIGH"), created_at_utc=2060.0, force=True)
        now[0] = 2120.0
        shipper.publish(_minimal_snapshot(2120.0, "CRITICAL"), created_at_utc=2120.0, force=True)
        self.assertEqual(shipper.metrics()["pending_frames"], 2)

        # 4. Reconnect with a lost ACK on first flush -> retry safely deduplicates
        state.outage = False
        state.drop_ack_once = True
        now[0] = 2200.0
        self.assertFalse(shipper.flush_once(force=True))
        self.assertEqual(shipper.metrics()["pending_frames"], 2)
        self.assertIn(("building-B", 2), state.frames)
        self.assertIn(("building-B", 3), state.frames)

        # Second attempt gets the ACK and clears outbox without duplicating server records
        now[0] = 2205.0
        self.assertTrue(shipper.flush_once(force=True))
        self.assertEqual(shipper.metrics()["pending_frames"], 0)
        self.assertEqual(shipper.metrics()["delivered_frames"], 3)
        self.assertEqual(len(state.frames), 3)
        self.assertTrue(state.frames[("building-B", 2)]["buffered"])
        self.assertEqual(state.frames[("building-B", 2)]["created_at_utc"], 2060.0)

        # 5. Cloud mode uses the identical snapshot schema with mode="cloud"
        cloud_shipper = DashboardShipper(
            endpoint=url,
            storage_dir=os.path.join(self.tmp.name, "cloud"),
            gateway_id="building-B-cloud",
            mode="cloud",
            auth_token="good-token",
            clock_fn=lambda: 3000.0,
        )
        cloud_shipper.publish(empty_snap, created_at_utc=3000.0)
        self.assertEqual(cloud_shipper.metrics()["delivered_frames"], 1)
        self.assertEqual(state.frames[("building-B-cloud", 1)]["mode"], "cloud")
        self.assertEqual(state.frames[("building-B-cloud", 1)]["snapshot"], empty_snap)

    def test_sequence_crash_consistency(self):
        # Simulate: frame 104 written to outbox.jsonl, crash before shipper_state.json updated
        state_path = os.path.join(self.tmp.name, "shipper_state.json")
        outbox_path = os.path.join(self.tmp.name, "outbox.jsonl")
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump({"next_seq": 104, "delivered_frames": 103, "retries": 0}, f)
        with open(outbox_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({
                "seq": 104, "mode": "edge", "created_at_utc": 1000.0,
                "buffered": False, "snapshot": _minimal_snapshot(1000.0), "new_events": []
            }) + "\n")

        s = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            clock_fn=lambda: 1060.0,
        )
        self.assertEqual(s.next_seq, 105)
        seq_new = s.enqueue(_minimal_snapshot(1060.0), created_at_utc=1060.0)
        self.assertEqual(seq_new, 105)

    def test_partial_contiguous_ack(self):
        s = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            clock_fn=lambda: 2000.0,
        )
        for i in range(3):
            s.enqueue(_minimal_snapshot(1000.0 + i), created_at_utc=1000.0 + i)
        # Backend accepted 1, rejected 2 -> returns contiguous acked_seq=1
        s._post_batch = lambda batch: {"ok": True, "gateway_id": "building-B", "acked_seq": 1}
        self.assertFalse(s.flush_once())
        self.assertEqual([r["seq"] for r in s.queue], [2, 3])
        self.assertEqual(s.metrics()["delivered_frames"], 1)

    def test_mode_switch_preserves_queued_frame_mode(self):
        state = _MockIngestState(token="t")
        srv = _ThreadedHTTPServer(("127.0.0.1", 0), _make_handler(state))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/v1/ingest"

        s = DashboardShipper(
            endpoint=url,
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            auth_token="t",
            clock_fn=lambda: 1000.0,
        )
        s.enqueue(_minimal_snapshot(1000.0), created_at_utc=1000.0)
        s.enqueue(_minimal_snapshot(1060.0), created_at_utc=1060.0)
        s.set_mode("cloud")
        s.enqueue(_minimal_snapshot(1120.0), created_at_utc=1120.0)

        self.assertTrue(s.flush_all(force=True))
        self.assertEqual(state.frames[("building-B", 1)]["mode"], "edge")
        self.assertEqual(state.frames[("building-B", 2)]["mode"], "edge")
        self.assertEqual(state.frames[("building-B", 3)]["mode"], "cloud")
        self.assertEqual(s.next_seq, 4)

    def test_disk_protection_never_deletes_old_frames(self):
        now = [1000.0]
        s = DashboardShipper(
            endpoint="http://127.0.0.1:9/api/v1/ingest",
            storage_dir=self.tmp.name,
            gateway_id="building-B",
            mode="edge",
            max_outbox_bytes=900,
            warn_queue_frames=2,
            warn_oldest_age_s=60.0,
            clock_fn=lambda: now[0],
        )
        s.enqueue(_minimal_snapshot(1000.0), created_at_utc=1000.0)
        s.enqueue(_minimal_snapshot(1060.0), created_at_utc=1060.0)
        now[0] = 1200.0
        m = s.metrics()
        self.assertTrue(m["storage_warning"])
        self.assertIn("queue_size_high", m["warnings"])
        self.assertIn("oldest_frame_stale", m["warnings"])

        with self.assertRaises(OutboxStorageError):
            for i in range(10):
                s.enqueue(_minimal_snapshot(1200.0 + i), created_at_utc=1200.0 + i)
        # Old queued frames are never silently deleted
        self.assertEqual(s.queue[0]["seq"], 1)
        self.assertTrue(len(s.queue) >= 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
