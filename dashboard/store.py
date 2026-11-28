"""SQLite persistence and SSE event broker for the JalRakshak dashboard backend.

Preserves Gateway.snapshot() verbatim without recalculating detections, risk scores,
correlations, or fabricating zeroes for unavailable metrics.
"""
import json
import queue
import sqlite3
import threading
import time
import uuid

REQUEST_TTL_S = 120     # a reopen request the gateway has not collected in this long expires

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS valve_requests (
    id TEXT PRIMARY KEY,
    gateway_id TEXT NOT NULL,
    node TEXT NOT NULL,
    requested_at_utc REAL NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    updated_at_utc REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS frames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gateway_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    mode TEXT NOT NULL,
    created_at_utc REAL NOT NULL,
    received_at_utc REAL NOT NULL,
    buffered INTEGER NOT NULL,
    snapshot_t REAL,
    building_status TEXT,
    node_count INTEGER NOT NULL,
    snapshot_json TEXT NOT NULL,
    new_events_json TEXT NOT NULL,
    UNIQUE(gateway_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_frames_gw_time ON frames(gateway_id, created_at_utc);
CREATE INDEX IF NOT EXISTS idx_frames_gw_mode_time ON frames(gateway_id, mode, created_at_utc);

CREATE TABLE IF NOT EXISTS gateway_cursors (
    gateway_id TEXT PRIMARY KEY,
    acked_seq INTEGER NOT NULL DEFAULT 0,
    updated_at_utc REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_points (
    frame_id INTEGER PRIMARY KEY REFERENCES frames(id) ON DELETE CASCADE,
    gateway_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    mode TEXT NOT NULL,
    created_at_utc REAL NOT NULL,
    snapshot_t REAL,
    building_status TEXT,
    node_count INTEGER NOT NULL,
    balance_pct REAL,
    pipe_leak_active INTEGER,
    quality_class TEXT,
    quality_compliant INTEGER,
    quality_temporal_anomaly INTEGER,
    quality_viol_mask INTEGER,
    ph REAL,
    tds REAL,
    turb REAL,
    quality_score REAL,
    supply_state TEXT,
    tank_level_m REAL,
    tank_inflow_lps REAL,
    active_incident_count INTEGER,
    mean_delay_s REAL,
    max_delay_s REAL
);

CREATE INDEX IF NOT EXISTS idx_tel_gw_time ON telemetry_points(gateway_id, created_at_utc);

CREATE TABLE IF NOT EXISTS incidents (
    gateway_id TEXT NOT NULL,
    target TEXT NOT NULL,
    category TEXT NOT NULL,
    t_onset REAL NOT NULL,
    mode TEXT NOT NULL,
    status TEXT NOT NULL,
    t_last REAL NOT NULL,
    t_resolved REAL,
    resolution_reason TEXT,
    repeat_count INTEGER NOT NULL DEFAULT 1,
    affected_flats INTEGER,
    risk_score REAL,
    severity TEXT,
    symptoms_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    recommended_action TEXT,
    last_seq INTEGER NOT NULL,
    PRIMARY KEY (gateway_id, target, category, t_onset)
);

CREATE INDEX IF NOT EXISTS idx_inc_gw_status ON incidents(gateway_id, status, t_last);
"""


def _opt_float(val):
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _opt_int_bool(val):
    if val is None:
        return None
    return 1 if bool(val) else 0


OVERFLOW_SENTINEL = object()


class EventBroker:
    """Thread-safe fan-out queue for Server-Sent Events.

    If a subscriber's bounded queue fills, an overflow sentinel is placed on the
    queue so the SSE stream terminates and the client reconnects with Last-Event-ID
    to replay missed events from SQLite (never silently dropping events).
    """

    def __init__(self, max_queue=256):
        self._lock = threading.Lock()
        self._subs = set()
        self._max_queue = max_queue

    def subscribe(self):
        q = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subs.add(q)
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs.discard(q)

    def publish(self, event_obj):
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event_obj)
            except queue.Full:
                while True:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                try:
                    q.put_nowait(OVERFLOW_SENTINEL)
                except queue.Full:
                    pass


class DashboardStore:
    """Durable SQLite store for ingested gateway snapshots, telemetry, and incident history."""

    def __init__(self, db_path, broker=None):
        self.db_path = db_path
        self.broker = broker or EventBroker()
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL;")
            self.conn.execute("PRAGMA synchronous=NORMAL;")
            self.conn.executescript(SCHEMA_SQL)
            self._migrate_schema()
            self.conn.commit()

    def _migrate_schema(self):
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(telemetry_points)").fetchall()}
        if "quality_temporal_anomaly" not in cols:
            self.conn.execute("ALTER TABLE telemetry_points ADD COLUMN quality_temporal_anomaly INTEGER")
        if "quality_viol_mask" not in cols:
            self.conn.execute("ALTER TABLE telemetry_points ADD COLUMN quality_viol_mask INTEGER")

    def close(self):
        with self._lock:
            self.conn.close()

    def _compute_contiguous_acked_seq(self, cur, gateway_id, current_ack):
        ack = int(current_ack)
        while True:
            row = cur.execute(
                "SELECT 1 FROM frames WHERE gateway_id = ? AND seq = ?",
                (gateway_id, ack + 1),
            ).fetchone()
            if row is None:
                break
            ack += 1
        return ack

    def ingest_batch(self, gateway_id, mode, frames, received_at_utc=None):
        """Durably persist a batch of frames, deduplicate on (gateway_id, seq), and return ACK info."""
        rx_utc = float(time.time() if received_at_utc is None else received_at_utc)
        accepted = 0
        duplicates = 0
        published_events = []

        with self._lock:
            cur = self.conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                row = cur.execute(
                    "SELECT acked_seq FROM gateway_cursors WHERE gateway_id = ?",
                    (gateway_id,),
                ).fetchone()
                current_ack = int(row["acked_seq"]) if row else 0

                for fr in frames:
                    seq = int(fr["seq"])
                    created_at_utc = float(fr["created_at_utc"])
                    buffered = 1 if fr.get("buffered") else 0
                    snap = fr["snapshot"]
                    new_events = fr.get("new_events") or []

                    exists = cur.execute(
                        "SELECT id FROM frames WHERE gateway_id = ? AND seq = ?",
                        (gateway_id, seq),
                    ).fetchone()
                    if exists is not None:
                        duplicates += 1
                        continue

                    nodes = snap.get("nodes") if isinstance(snap.get("nodes"), dict) else {}
                    node_count = len(nodes)
                    snap_t = _opt_float(snap.get("t"))
                    b_status = snap.get("building_status")

                    cur.execute(
                        """
                        INSERT INTO frames (
                            gateway_id, seq, mode, created_at_utc, received_at_utc,
                            buffered, snapshot_t, building_status, node_count,
                            snapshot_json, new_events_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            gateway_id,
                            seq,
                            mode,
                            created_at_utc,
                            rx_utc,
                            buffered,
                            snap_t,
                            b_status,
                            node_count,
                            json.dumps(snap, separators=(",", ":")),
                            json.dumps(new_events, separators=(",", ":")),
                        ),
                    )
                    frame_id = cur.lastrowid

                    wq = snap.get("water_quality") if isinstance(snap.get("water_quality"), dict) else {}
                    lk = snap.get("leakage") if isinstance(snap.get("leakage"), dict) else {}
                    sup = snap.get("supply") if isinstance(snap.get("supply"), dict) else {}
                    nh = snap.get("network_health") if isinstance(snap.get("network_health"), dict) else {}
                    active_incs = snap.get("active_incidents") if isinstance(snap.get("active_incidents"), list) else []

                    cur.execute(
                        """
                        INSERT INTO telemetry_points (
                            frame_id, gateway_id, seq, mode, created_at_utc, snapshot_t,
                            building_status, node_count, balance_pct, pipe_leak_active,
                            quality_class, quality_compliant, quality_temporal_anomaly, quality_viol_mask,
                            ph, tds, turb, quality_score,
                            supply_state, tank_level_m, tank_inflow_lps,
                            active_incident_count, mean_delay_s, max_delay_s
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            frame_id,
                            gateway_id,
                            seq,
                            mode,
                            created_at_utc,
                            snap_t,
                            b_status,
                            node_count,
                            _opt_float(lk.get("last_balance_pct")),
                            _opt_int_bool(lk.get("pipe_leak_active")) if "pipe_leak_active" in lk else None,
                            wq.get("class"),
                            _opt_int_bool(wq.get("compliant")) if "compliant" in wq else None,
                            _opt_int_bool(wq.get("temporal_anomaly")) if "temporal_anomaly" in wq else None,
                            int(wq["viol_mask"]) if wq.get("viol_mask") is not None and not isinstance(wq.get("viol_mask"), bool) else None,
                            _opt_float(wq.get("ph")),
                            _opt_float(wq.get("tds")),
                            _opt_float(wq.get("turb")),
                            _opt_float(wq.get("score")),
                            sup.get("state"),
                            _opt_float(sup.get("level_m")),
                            _opt_float(sup.get("inflow_lps")),
                            len(active_incs) if isinstance(snap.get("active_incidents"), list) else None,
                            _opt_float(nh.get("mean_delay_s")),
                            _opt_float(nh.get("max_delay_s")),
                        ),
                    )

                    self._upsert_incidents(cur, gateway_id, mode, seq, snap)
                    accepted += 1
                    published_events.append(
                        {
                            "id": frame_id,
                            "event": "snapshot",
                            "data": {
                                "id": frame_id,
                                "gateway_id": gateway_id,
                                "seq": seq,
                                "mode": mode,
                                "created_at_utc": created_at_utc,
                                "received_at_utc": rx_utc,
                                "buffered": bool(buffered),
                                "snapshot": snap,
                                "new_events": new_events,
                            },
                        }
                    )

                new_ack = self._compute_contiguous_acked_seq(cur, gateway_id, current_ack)
                cur.execute(
                    """
                    INSERT INTO gateway_cursors (gateway_id, acked_seq, updated_at_utc)
                    VALUES (?, ?, ?)
                    ON CONFLICT(gateway_id) DO UPDATE SET
                        acked_seq = excluded.acked_seq,
                        updated_at_utc = excluded.updated_at_utc
                    """,
                    (gateway_id, new_ack, rx_utc),
                )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

        for ev in published_events:
            self.broker.publish(ev)

        return {
            "ok": True,
            "gateway_id": gateway_id,
            "mode": mode,
            "acked_seq": new_ack,
            "accepted": accepted,
            "duplicates": duplicates,
            "received_at_utc": rx_utc,
        }

    def _upsert_incidents(self, cur, gateway_id, mode, seq, snap):
        q_by_key = {}
        for item in snap.get("maintenance_queue") or []:
            if isinstance(item, dict) and "target" in item and "category" in item:
                q_by_key[(item["target"], item["category"])] = item

        for inc in snap.get("active_incidents") or []:
            if not isinstance(inc, dict) or "target" not in inc or "category" not in inc or "t_onset" not in inc:
                continue
            tgt, cat = inc["target"], inc["category"]
            q_item = q_by_key.get((tgt, cat), {})
            cur.execute(
                """
                INSERT INTO incidents (
                    gateway_id, target, category, t_onset, mode, status, t_last,
                    t_resolved, resolution_reason, repeat_count, affected_flats,
                    risk_score, severity, symptoms_json, evidence_json, recommended_action, last_seq
                ) VALUES (?, ?, ?, ?, ?, 'active', ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(gateway_id, target, category, t_onset) DO UPDATE SET
                    mode = excluded.mode,
                    status = CASE WHEN incidents.status = 'resolved' THEN 'resolved' ELSE 'active' END,
                    t_last = MAX(incidents.t_last, excluded.t_last),
                    repeat_count = MAX(incidents.repeat_count, excluded.repeat_count),
                    affected_flats = COALESCE(excluded.affected_flats, incidents.affected_flats),
                    risk_score = COALESCE(excluded.risk_score, incidents.risk_score),
                    severity = COALESCE(excluded.severity, incidents.severity),
                    symptoms_json = excluded.symptoms_json,
                    evidence_json = excluded.evidence_json,
                    recommended_action = COALESCE(excluded.recommended_action, incidents.recommended_action),
                    last_seq = MAX(incidents.last_seq, excluded.last_seq)
                """,
                (
                    gateway_id,
                    tgt,
                    cat,
                    float(inc["t_onset"]),
                    mode,
                    float(inc.get("t_last", inc["t_onset"])),
                    int(inc.get("repeat_count", 1)),
                    inc.get("affected_flats"),
                    _opt_float(q_item.get("risk_score")),
                    q_item.get("severity"),
                    json.dumps(inc.get("symptoms") or [], separators=(",", ":")),
                    json.dumps(inc.get("evidence") or {}, separators=(",", ":")),
                    q_item.get("recommended_action"),
                    seq,
                ),
            )

        for hist in snap.get("incident_history") or []:
            if not isinstance(hist, dict) or "target" not in hist or "category" not in hist or "t_onset" not in hist:
                continue
            tgt, cat = hist["target"], hist["category"]
            cur.execute(
                """
                INSERT INTO incidents (
                    gateway_id, target, category, t_onset, mode, status, t_last,
                    t_resolved, resolution_reason, repeat_count, affected_flats,
                    risk_score, severity, symptoms_json, evidence_json, recommended_action, last_seq
                ) VALUES (?, ?, ?, ?, ?, 'resolved', ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, NULL, ?)
                ON CONFLICT(gateway_id, target, category, t_onset) DO UPDATE SET
                    status = 'resolved',
                    t_last = MAX(incidents.t_last, excluded.t_last),
                    t_resolved = COALESCE(excluded.t_resolved, incidents.t_resolved),
                    resolution_reason = COALESCE(excluded.resolution_reason, incidents.resolution_reason),
                    repeat_count = MAX(incidents.repeat_count, excluded.repeat_count),
                    evidence_json = excluded.evidence_json,
                    last_seq = MAX(incidents.last_seq, excluded.last_seq)
                """,
                (
                    gateway_id,
                    tgt,
                    cat,
                    float(hist["t_onset"]),
                    mode,
                    float(hist.get("t_last", hist["t_onset"])),
                    _opt_float(hist.get("t_resolved")),
                    hist.get("resolution_reason"),
                    int(hist.get("repeat_count", 1)),
                    json.dumps(hist.get("symptoms") or [cat], separators=(",", ":")),
                    json.dumps(hist.get("evidence") or {}, separators=(",", ":")),
                    seq,
                ),
            )

    def _row_to_frame(self, row):
        return {
            "id": row["id"],
            "gateway_id": row["gateway_id"],
            "seq": row["seq"],
            "mode": row["mode"],
            "created_at_utc": row["created_at_utc"],
            "received_at_utc": row["received_at_utc"],
            "buffered": bool(row["buffered"]),
            "node_count": row["node_count"],
            "snapshot": json.loads(row["snapshot_json"]),
            "new_events": json.loads(row["new_events_json"]),
        }

    # --- manual valve reopen requests ---
    def _expire_requests(self, now):
        self.conn.execute(
            "UPDATE valve_requests SET status = 'expired', detail = 'not collected by the gateway in time', "
            "updated_at_utc = ? WHERE status = 'pending' AND requested_at_utc < ?", (now, now - REQUEST_TTL_S))
        self.conn.commit()

    @staticmethod
    def _request(row):
        return {**dict(row), "expires_at_utc": row["requested_at_utc"] + REQUEST_TTL_S}

    def request_reopen(self, gateway_id, node, now=None):
        """A new pending request, or the one already waiting for this valve."""
        now = float(time.time() if now is None else now)
        with self._lock:
            self._expire_requests(now)
            find = "SELECT * FROM valve_requests WHERE gateway_id = ? AND node = ? AND status = 'pending'"
            row = self.conn.execute(find, (gateway_id, node)).fetchone()
            if row is None:
                self.conn.execute("INSERT INTO valve_requests VALUES (?, ?, ?, ?, 'pending', NULL, ?)",
                                  (uuid.uuid4().hex, gateway_id, node, now, now))
                self.conn.commit()
                row = self.conn.execute(find, (gateway_id, node)).fetchone()
            return self._request(row)

    def pending_requests(self, gateway_id, now=None):
        now = float(time.time() if now is None else now)
        with self._lock:
            self._expire_requests(now)
            rows = self.conn.execute(
                "SELECT * FROM valve_requests WHERE gateway_id = ? AND status = 'pending' "
                "ORDER BY requested_at_utc LIMIT 50", (gateway_id,)).fetchall()
            return [self._request(r) for r in rows]

    def answer_request(self, gateway_id, request_id, status, detail=None, now=None):
        """What the gateway did with a request. It knows best, so this also replaces "expired"
        (its answer was late); an earlier answer stays. None: no such request."""
        now = float(time.time() if now is None else now)
        with self._lock:
            self.conn.execute(
                "UPDATE valve_requests SET status = ?, detail = ?, updated_at_utc = ? "
                "WHERE id = ? AND gateway_id = ? AND status IN ('pending', 'expired')",
                (status, detail, now, request_id, gateway_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM valve_requests WHERE id = ? AND gateway_id = ?",
                                    (request_id, gateway_id)).fetchone()
            return self._request(row) if row else None

    def latest_requests(self, gateway_id, now=None):
        """node -> its newest request."""
        now = float(time.time() if now is None else now)
        with self._lock:
            self._expire_requests(now)
            rows = self.conn.execute(
                "SELECT * FROM valve_requests WHERE gateway_id = ? ORDER BY requested_at_utc, rowid",
                (gateway_id,)).fetchall()
            return {r["node"]: self._request(r) for r in rows}

    def list_gateways(self):
        with self._lock:
            rows = self.conn.execute(
                """
                SELECT f.gateway_id,
                       MAX(f.seq) AS latest_seq,
                       COALESCE(c.acked_seq, 0) AS acked_seq,
                       (SELECT mode FROM frames f2 WHERE f2.gateway_id = f.gateway_id ORDER BY f2.seq DESC LIMIT 1) AS latest_mode,
                       MAX(f.received_at_utc) AS last_received_at_utc,
                       (SELECT node_count FROM frames f3 WHERE f3.gateway_id = f.gateway_id ORDER BY f3.seq DESC LIMIT 1) AS node_count
                FROM frames f
                LEFT JOIN gateway_cursors c ON c.gateway_id = f.gateway_id
                GROUP BY f.gateway_id
                ORDER BY f.gateway_id
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_snapshot(self, gateway_id=None, mode=None):
        clauses, params = [], []
        if gateway_id:
            clauses.append("gateway_id = ?")
            params.append(gateway_id)
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._lock:
            row = self.conn.execute(
                f"SELECT * FROM frames {where} ORDER BY id DESC LIMIT 1",
                params,
            ).fetchone()
        return self._row_to_frame(row) if row else None

    def get_telemetry(self, gateway_id=None, mode=None, start_utc=None, end_utc=None, limit=500):
        clauses, params = [], []
        if gateway_id:
            clauses.append("gateway_id = ?")
            params.append(gateway_id)
        if mode:
            clauses.append("mode = ?")
            params.append(mode)
        if start_utc is not None:
            clauses.append("created_at_utc >= ?")
            params.append(float(start_utc))
        if end_utc is not None:
            clauses.append("created_at_utc <= ?")
            params.append(float(end_utc))
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        lim = max(1, min(int(limit), 5000))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM telemetry_points {where} ORDER BY created_at_utc ASC, seq ASC LIMIT ?",
                params + [lim],
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d["pipe_leak_active"] is not None:
                d["pipe_leak_active"] = bool(d["pipe_leak_active"])
            if d["quality_compliant"] is not None:
                d["quality_compliant"] = bool(d["quality_compliant"])
            if d.get("quality_temporal_anomaly") is not None:
                d["quality_temporal_anomaly"] = bool(d["quality_temporal_anomaly"])
            out.append(d)
        return out

    def get_incidents(self, gateway_id=None, status=None, category=None, limit=200):
        clauses, params = [], []
        if gateway_id:
            clauses.append("gateway_id = ?")
            params.append(gateway_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if category:
            clauses.append("category = ?")
            params.append(category)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        lim = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM incidents {where} ORDER BY t_last DESC LIMIT ?",
                params + [lim],
            ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "gateway_id": r["gateway_id"],
                    "target": r["target"],
                    "category": r["category"],
                    "t_onset": r["t_onset"],
                    "mode": r["mode"],
                    "status": r["status"],
                    "t_last": r["t_last"],
                    "t_resolved": r["t_resolved"],
                    "resolution_reason": r["resolution_reason"],
                    "repeat_count": r["repeat_count"],
                    "affected_flats": r["affected_flats"],
                    "risk_score": r["risk_score"],
                    "severity": r["severity"],
                    "symptoms": json.loads(r["symptoms_json"]),
                    "evidence": json.loads(r["evidence_json"]),
                    "recommended_action": r["recommended_action"],
                    "last_seq": r["last_seq"],
                }
            )
        return out

    def get_frames_since(self, since_id=0, gateway_id=None, limit=500):
        clauses = ["id > ?"]
        params = [int(since_id)]
        if gateway_id:
            clauses.append("gateway_id = ?")
            params.append(gateway_id)
        where = "WHERE " + " AND ".join(clauses)
        lim = max(1, min(int(limit), 2000))
        with self._lock:
            rows = self.conn.execute(
                f"SELECT * FROM frames {where} ORDER BY id ASC LIMIT ?",
                params + [lim],
            ).fetchall()
        return [self._row_to_frame(r) for r in rows]
