# JalRakshak Gateway-to-Dashboard API Specification (`v1`)

> **Boundary Note:** Both `edge`-based and `cloud`-based gateways emit the **exact same `Gateway.snapshot()` payload**. `mode` (`"edge"` or `"cloud"`) is presentation/provenance metadata only.

---

## 1. Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/v1/ingest` | Ingest 1 live frame or a bounded batch of up to 50 buffered frames |
| `GET` | `/api/v1/health` | Lightweight health & backhaul reachability check (`200 OK`) |
| `POST` | `/api/v1/valves/reopen` | The dashboard's **reopen valve button** ([section 7](#7-manual-valve-reopen)) |
| `GET` | `/api/v1/valves` | Each valve's reported state and latest reopen request ([section 7](#7-manual-valve-reopen)) |
| `GET`, `POST` | `/api/v1/gateway/commands`, `.../ack` | The gateway collects and answers reopen requests ([section 7](#7-manual-valve-reopen)) |

---

## 2. Authentication

Every `POST /api/v1/ingest` request must include a Bearer token:

```http
Authorization: Bearer <AQUA_DASHBOARD_TOKEN>
Content-Type: application/json
```

- Missing or invalid tokens return `401 Unauthorized`.
- Tokens are compared in constant time (`hmac.compare_digest`).

---

## 3. Request Payload (`POST /api/v1/ingest`)

### Limits
- **Maximum batch size:** `50` frames per request (`1 <= len(frames) <= 50`).
- **Maximum request body:** `1 MiB` (`1,048,576` bytes).

### Envelope Schema (`schema_version = 1`)

```json
{
  "schema_version": 1,
  "gateway_id": "building-B",
  "mode": "edge",
  "frames": [
    {
      "seq": 104,
      "created_at_utc": 1764289200.5,
      "buffered": false,
      "snapshot": { "...exact Gateway.snapshot() dict..." },
      "new_events": []
    }
  ]
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `schema_version` | `int` | Yes | Must be `1`. Unsupported versions return `422`. |
| `gateway_id` | `string` | Yes | Non-empty gateway identifier (e.g. `"building-B"`). |
| `mode` | `string` | Yes | `"edge"` or `"cloud"`. Metadata only; never alters `snapshot` schema. |
| `frames` | `list[object]` | Yes | Ordered list (`1..50`) of frame objects sorted by ascending `seq`. |
| `frames[].seq` | `int` | Yes | Monotonically increasing sequence number (`>= 1`), persisted across gateway restarts. |
| `frames[].created_at_utc` | `float` | Yes | UTC Unix timestamp (seconds) when the snapshot was created at the gateway. Kept separate from delivery time. |
| `frames[].buffered` | `bool` | Yes | `false` if delivered on first attempt; `true` if held in the disk outbox before delivery. |
| `frames[].snapshot` | `object` | Yes | Exact output of `Gateway.snapshot()` (`gateway/analytics.py`). |
| `frames[].new_events` | `list[object]` | No | Optional list of `gw.events` dicts produced since the previous `seq`. |

---

## 4. Exact `Gateway.snapshot()` Schema (Read-Only from `gateway/analytics.py`)

The dashboard backend and frontend **tolerate optional/missing fields and ignore unknown future fields**. Fields produced by `GatewayAnalytics.snapshot(gw, now)`:

```json
{
  "t": 1764289200.0,
  "building_status": "NORMAL",
  "maintenance_queue": [
    {
      "rank": 1,
      "target": "B-TANK",
      "category": "QUALITY_CHEMICAL",
      "risk_score": 85.0,
      "severity": "CRITICAL",
      "affected_flats": 20,
      "duration_min": 12.0,
      "symptoms": ["QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION"],
      "evidence": { "ph": 5.1, "tds": 840.0, "turb": 9.2, "viol": 7 },
      "recommended_action": "Isolate tank distribution and flush/test water immediately (chemical hazard)."
    }
  ],
  "active_incidents": [
    {
      "target": "B-TANK",
      "category": "QUALITY_CHEMICAL",
      "symptoms": ["QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION"],
      "t_onset": 1764288480.0,
      "t_last": 1764289200.0,
      "repeat_count": 3,
      "affected_flats": 20,
      "evidence": {}
    }
  ],
  "incident_history": [
    {
      "target": "B-401",
      "category": "NODE_SILENT",
      "t_onset": 1764280000.0,
      "t_last": 1764282700.0,
      "rx_last": 1764282700.0,
      "repeat_count": 1,
      "evidence": {},
      "t_resolved": 1764283000.0,
      "resolution_reason": "NODE_BACK"
    }
  ],
  "water_quality": {
    "class": "NORMAL",
    "compliant": true,
    "viol_mask": 0,
    "temporal_anomaly": false,
    "ph": null,
    "tds": null,
    "turb": null,
    "score": null
  },
  "leakage": {
    "pipe_leak_active": false,
    "last_balance_pct": null,
    "pipe_leak": null,
    "flat_leaks": []
  },
  "supply": {
    "state": "NORMAL",
    "level_m": null,
    "inflow_lps": null,
    "t": null
  },
  "abnormal_consumption": [],
  "network_health": {
    "silent_nodes": [],
    "balance_paused": false,
    "bad_packets": 0,
    "skewed_packets": 0,
    "clock_skew_nodes": [],
    "online_alerts": 0,
    "offline_alerts": 0,
    "buffered_recovered": 0,
    "mean_delay_s": 0.0,
    "max_delay_s": 0.0
  },
  "nodes": {
    "B-101": { "status": "ok", "last_seen": null, "last_sum": null }
  }
}
```

*(Note: `leakage.pipe_leak`, when active, includes `target`, `suspected_floor`, `suspected_range`, `localization_confidence`, `isolation_valve`, `error_pct`, and `floor_drops`.)*

---

## 5. Acknowledgement, Idempotency & Error Responses

### Success (`200 OK`)

```json
{
  "ok": true,
  "gateway_id": "building-B",
  "mode": "edge",
  "acked_seq": 104,
  "accepted": 1,
  "duplicates": 0,
  "received_at_utc": 1764289201.15
}
```

- **Deduplication key:** `(gateway_id, seq)`.
- **Contiguous ACK semantics:** `acked_seq` is the **highest contiguous durably ingested sequence number** (never jumping past a rejected or failed frame in a batch). The shipper only deletes frames with `seq <= acked_seq`.
- **Mode preservation:** Each frame retains the `mode` (`"edge"` or `"cloud"`) active when it was generated; if a gateway switches modes while offline frames are queued, the shipper flushes earlier-mode frames in their original `mode` batch before sending newer-mode frames, while keeping `seq` monotonic for `gateway_id`.
- **Idempotency:** If a frame with an already-stored `(gateway_id, seq)` is re-sent (for example after a lost HTTP response), the server skips re-inserting it, counts it under `duplicates`, and still returns `200 OK` with the highest contiguous `acked_seq` so the shipper can safely prune its outbox.
- **Out-of-order frames:** Accepted and stored idempotently by `(gateway_id, seq)`. `acked_seq` reflects the highest sequence processed in the request (and the shipper only deletes `seq <= acked_seq` in its ordered queue).

### Error Responses

| Status | Condition | Body |
|---|---|---|
| `400 Bad Request` | Malformed JSON, empty `frames`, batch `> 50`, missing required fields, or non-increasing `seq` within batch | `{"ok": false, "error": "..."}` |
| `401 Unauthorized` | Missing or invalid `Authorization: Bearer` token | `{"ok": false, "error": "unauthorized"}` |
| `413 Payload Too Large` | Request body exceeds `1,048,576` bytes | `{"ok": false, "error": "payload too large"}` |
| `422 Unprocessable Entity` | Unsupported `schema_version` (`!= 1`) or invalid `mode` (`not in {"edge", "cloud"}`) | `{"ok": false, "error": "..."}` |
| `503 Service Unavailable` | Simulated Internet outage active on test backend | `{"ok": false, "error": "simulated internet outage"}` |

---

## 6. Minimal Integration Interface for Gateway Entry Points (`gateway/edge_based.py`, `gateway/cloud_based.py`)

```python
from dashboard.shipper import DashboardShipper

shipper = DashboardShipper(
    endpoint="http://127.0.0.1:8000/api/v1/ingest",
    storage_dir="dashboard/run/edge",
    gateway_id="building-B",
    mode="edge",  # or "cloud"
    auth_token="dev-token",
)

# After gw.step():
shipper.publish(gw.snapshot(), new_events=new_events)
```

---

## 7. Manual valve reopen

The button calls the dashboard server. The server never connects to the gateway: the gateway asks it for waiting requests about every 3 s.

```
button -> POST /valves/reopen          (status "pending")
gateway -> GET /gateway/commands       collects it, sends the node a signed VALVE_OPEN order
gateway -> POST /gateway/commands/ack  (status "sent" or "rejected")
node -> gateway -> next snapshot       nodes[name].valve becomes "open"
```

**A reopen opens the valve even while an alert is still active.** The node's light stays red, and a new alert, or one confirmed again, can close the valve again. Ask the operator to confirm, and show the node's `light` and active incidents beside the button.

### For the button

`POST /api/v1/valves/reopen` with `Authorization: Bearer <AQUA_OPERATOR_TOKEN>`:

```json
{"gateway_id": "building-B", "node": "B-203"}
```

`202 Accepted`:

```json
{"ok": true, "request": {"id": "9f1c...", "gateway_id": "building-B", "node": "B-203", "status": "pending",
  "detail": null, "requested_at_utc": 1764289200.5, "updated_at_utc": 1764289200.5, "expires_at_utc": 1764289320.5}}
```

Asking again while a request for that valve is `pending` returns the same request.

| Status | Condition |
|---|---|
| `400` | Body is not `{"gateway_id": "...", "node": "..."}` |
| `401` | Missing or wrong operator token |
| `403` | Reopening is off: `AQUA_OPERATOR_TOKEN` is unset, or the same as the gateway's token |
| `404` | The gateway has sent nothing, or its newest snapshot has no such node |

**The operator token is separate from the gateway's**, which must never be put in a web page. Do not build it into the page's files: ask the operator for it.

`GET /api/v1/valves?gateway_id=building-B` (no token, like other reads; without `gateway_id`, the gateway that sent last):

```json
{"ok": true, "gateway_id": "building-B", "reopen_enabled": true, "snapshot_received_at_utc": 1764289201.1,
 "valves": {"B-203": {"state": "closed", "request": null}}}
```

`state` is what the node last reported: `"closed"`, `"open"`, `"fault"` or `null` (never reported).

| Request status | Meaning |
|---|---|
| `pending` | Waiting for the gateway. It expires after 120 s |
| `sent` | The gateway queued the order for the node. **Not proof the valve opened:** wait for `state` to become `"open"` |
| `rejected` | The gateway refused it; `detail` says why |
| `expired` | The gateway did not collect it in time. If its answer arrives late, the status becomes that answer |

A `sent` order the node has not confirmed within 15 minutes of the click is dropped: the next snapshots carry an `ORDER_EXPIRED` event in `new_events`. A node ignores an order older than its last valve change, so a closing after the click wins. There is no "close" endpoint.

### For the gateway

Both use the gateway's token (`AQUA_DASHBOARD_TOKEN`), and answer `503` during a simulated outage.

- `GET /api/v1/gateway/commands?gateway_id=` → `{"ok": true, "commands": [{"id": "9f1c...", "node": "B-203", "command": "VALVE_OPEN", "age_s": 1.4}]}`. `age_s` is how long ago it was asked for, so the two computers need not share a clock.
- `POST /api/v1/gateway/commands/ack` with `{"gateway_id", "id", "result": "sent" | "rejected", "detail"?}`. The first answer stays. `404`: no such request.

**Trust:** the gateway opens valves on this server's say, so point it only at a server you control, and use `https` across a network.

---

## 8. Flags added to the snapshot

These follow [`../planning/valve_spec.md`](../planning/valve_spec.md). They are extra fields; the others are unchanged.

| Field | Meaning |
|---|---|
| `level` on `maintenance_queue`, `active_incidents`, `incident_history` items and on events in `new_events` | `"ALERT"` (red), `"WARNING"` (yellow) or `"INFO"`. A silent node and a wrong clock are warnings |
| `nodes[name].light` | `{"colour": "red", "blinks": 2, "pattern": "R2"}`; patterns `R1`, `R2`, `Y1`, `Y2`, `G`. `null` for a silent node. It is the light **expected** from what the gateway knows: it cannot see a node's own delivery or sensor warnings (so `G` may really be yellow, and `Y1` is never reported) |

New events: `BAD_READING` (`node`, `fields`: readings left out because they could not be true) and `ORDER_EXPIRED` (`node`, `order`).
