"""FastAPI + SQLite backend for the JalRakshak dashboard (Phase 3).

Consumes the v1 ingest contract (dashboard/API_SPEC.md) without modifying any
teammate-owned files or recalculating upstream gateway analytics.
"""
import asyncio
import hmac
import json
import os
import queue
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from dashboard.shipper import MAX_BATCH_SIZE, MAX_PAYLOAD_BYTES, SCHEMA_VERSION, VALID_MODES
from dashboard.store import DashboardStore, OVERFLOW_SENTINEL
from gateway.topology import Topology


def _load_configured_topology(topology_path):
    """Load and validate configured Topology JSON without falling back to a hardcoded tree."""
    if not topology_path:
        return None, 404, "no topology_path configured"
    if not os.path.exists(topology_path):
        return None, 404, "configured topology file not found"
    try:
        topo = Topology.load(topology_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return None, 422, f"invalid topology configuration: {exc}"
    return topo.data, 200, None


def create_app(
    db_path=None,
    auth_token=None,
    topology_path=None,
    heartbeat_s=15.0,
    enable_outage_sim=None,
):
    """Create a configured FastAPI dashboard backend instance."""
    resolved_db = db_path or os.environ.get("AQUA_DASHBOARD_DB", os.path.join("dashboard", "run", "dashboard.db"))
    if resolved_db != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(resolved_db)), exist_ok=True)
    resolved_token = auth_token if auth_token is not None else os.environ.get("AQUA_DASHBOARD_TOKEN", "dev-token")
    resolved_topo = topology_path if topology_path is not None else os.environ.get("AQUA_TOPOLOGY_PATH")
    if enable_outage_sim is None:
        env_sim = os.environ.get("AQUA_ENABLE_OUTAGE_SIM", "1").strip().lower()
        resolved_outage_sim = env_sim in ("1", "true", "yes")
    else:
        resolved_outage_sim = bool(enable_outage_sim)

    store = DashboardStore(resolved_db)
    sim_lock = threading.Lock()
    state = {"internet_outage": False}

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            store.close()

    app = FastAPI(title="JalRakshak Dashboard API", version="1.0.0", lifespan=lifespan)
    app.state.store = store

    def _err(status_code, message):
        return JSONResponse(status_code=status_code, content={"ok": False, "error": message})

    def _check_auth(authorization):
        if not resolved_token:
            return True
        if not authorization:
            return False
        expected = f"Bearer {resolved_token}"
        return hmac.compare_digest(authorization, expected)

    @app.get("/api/v1/health")
    def health():
        topo_data, topo_code, topo_err = _load_configured_topology(resolved_topo)
        with sim_lock:
            outage = state["internet_outage"]
        return {
            "ok": True,
            "internet_outage_simulated": outage,
            "topology_configured": topo_code == 200,
            "topology_error": topo_err,
            "topology_building": topo_data.get("building") if topo_data else None,
            "gateways": store.list_gateways(),
        }

    @app.post("/api/v1/ingest")
    async def ingest(request: Request, authorization: str | None = Header(default=None)):
        if not _check_auth(authorization):
            return _err(401, "unauthorized")

        with sim_lock:
            if state["internet_outage"]:
                return _err(503, "simulated internet outage")

        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_PAYLOAD_BYTES:
                    return _err(413, "payload too large")
            except ValueError:
                return _err(400, "invalid Content-Length header")

        chunks = []
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > MAX_PAYLOAD_BYTES:
                return _err(413, "payload too large")
            chunks.append(chunk)
        raw = b"".join(chunks)

        try:
            env = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _err(400, "malformed JSON")

        if not isinstance(env, dict):
            return _err(400, "request body must be a JSON object")

        if env.get("schema_version") != SCHEMA_VERSION:
            return _err(422, f"unsupported schema_version: {env.get('schema_version')!r}")

        mode = env.get("mode")
        if mode not in VALID_MODES:
            return _err(422, f"invalid mode: {mode!r}")

        gateway_id = env.get("gateway_id")
        if not isinstance(gateway_id, str) or not gateway_id.strip():
            return _err(400, "gateway_id must be a non-empty string")

        frames = env.get("frames")
        if not isinstance(frames, list) or not (1 <= len(frames) <= MAX_BATCH_SIZE):
            return _err(400, f"frames must be a list of 1..{MAX_BATCH_SIZE} items")

        prev_seq = None
        for fr in frames:
            if not isinstance(fr, dict):
                return _err(400, "each frame must be a JSON object")
            seq = fr.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
                return _err(400, "frame.seq must be an integer >= 1")
            if prev_seq is not None and seq <= prev_seq:
                return _err(400, "frames must have strictly increasing seq within a batch")
            prev_seq = seq
            ts = fr.get("created_at_utc")
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                return _err(400, "frame.created_at_utc must be numeric")
            if not isinstance(fr.get("snapshot"), dict):
                return _err(400, "frame.snapshot must be a JSON object")

        return store.ingest_batch(gateway_id=gateway_id, mode=mode, frames=frames)

    @app.get("/api/v1/gateways")
    def list_gateways():
        return {"ok": True, "gateways": store.list_gateways()}

    @app.get("/api/v1/snapshots/latest")
    def latest_snapshot(gateway_id: str | None = Query(default=None), mode: str | None = Query(default=None)):
        if mode is not None and mode not in VALID_MODES:
            return _err(422, f"invalid mode: {mode!r}")
        frame = store.get_latest_snapshot(gateway_id=gateway_id, mode=mode)
        if frame is None:
            return _err(404, "no snapshots found")
        return {"ok": True, "frame": frame}

    @app.get("/api/v1/telemetry")
    def telemetry(
        gateway_id: str | None = Query(default=None),
        mode: str | None = Query(default=None),
        start_utc: float | None = Query(default=None),
        end_utc: float | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=5000),
    ):
        if mode is not None and mode not in VALID_MODES:
            return _err(422, f"invalid mode: {mode!r}")
        points = store.get_telemetry(
            gateway_id=gateway_id,
            mode=mode,
            start_utc=start_utc,
            end_utc=end_utc,
            limit=limit,
        )
        return {"ok": True, "count": len(points), "points": points}

    @app.get("/api/v1/incidents")
    def incidents(
        gateway_id: str | None = Query(default=None),
        status: str | None = Query(default=None),
        category: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=2000),
    ):
        if status is not None and status not in ("active", "resolved"):
            return _err(422, f"invalid status: {status!r}")
        rows = store.get_incidents(
            gateway_id=gateway_id,
            status=status,
            category=category,
            limit=limit,
        )
        return {"ok": True, "count": len(rows), "incidents": rows}

    @app.get("/api/v1/topology")
    def topology():
        topo_data, code, err_msg = _load_configured_topology(resolved_topo)
        if code != 200:
            return _err(code, err_msg)
        return {"ok": True, "topology": topo_data}

    @app.post("/api/v1/sim/outage")
    async def simulate_outage(request: Request, authorization: str | None = Header(default=None)):
        if not resolved_outage_sim:
            return _err(403, "outage simulation disabled")
        if not _check_auth(authorization):
            return _err(401, "unauthorized")
        try:
            body = await request.json()
        except Exception:
            return _err(400, "malformed JSON")
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return _err(400, "body must be {'enabled': true|false}")
        with sim_lock:
            state["internet_outage"] = body["enabled"]
            enabled = state["internet_outage"]
        return {"ok": True, "internet_outage_simulated": enabled}

    @app.get("/api/v1/stream")
    async def stream(
        request: Request,
        gateway_id: str | None = Query(default=None),
        since_id: int | None = Query(default=None),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
        max_events: int | None = Query(default=None, ge=1),
        once: bool = Query(default=False),
    ):
        cursor = 0
        if since_id is not None:
            cursor = max(0, int(since_id))
        elif last_event_id is not None:
            try:
                cursor = max(0, int(last_event_id))
            except ValueError:
                cursor = 0

        def _format_sse(ev_id, ev_name, payload_obj):
            return f"id: {ev_id}\nevent: {ev_name}\ndata: {json.dumps(payload_obj, separators=(',', ':'))}\n\n"

        async def event_generator():
            sub_q = store.broker.subscribe()
            sent_count = 0
            last_sent_id = cursor
            try:
                missed = store.get_frames_since(since_id=cursor, gateway_id=gateway_id)
                for fr in missed:
                    yield _format_sse(fr["id"], "snapshot", fr)
                    last_sent_id = max(last_sent_id, fr["id"])
                    sent_count += 1
                    if max_events is not None and sent_count >= max_events:
                        return
                if once:
                    return

                last_ping = time.monotonic()
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        ev = sub_q.get_nowait()
                        if ev is OVERFLOW_SENTINEL:
                            yield _format_sse(
                                last_sent_id,
                                "resync",
                                {"reason": "queue_overflow", "last_sent_id": last_sent_id},
                            )
                            return
                        ev_id = int(ev["id"])
                        ev_data = ev["data"]
                        if ev_id <= last_sent_id:
                            continue
                        if gateway_id and ev_data.get("gateway_id") != gateway_id:
                            continue
                        yield _format_sse(ev_id, ev["event"], ev_data)
                        last_sent_id = ev_id
                        sent_count += 1
                        if max_events is not None and sent_count >= max_events:
                            return
                    except queue.Empty:
                        now_m = time.monotonic()
                        if now_m - last_ping >= heartbeat_s:
                            yield ": ping\n\n"
                            last_ping = now_m
                        await asyncio.sleep(0.05)
            finally:
                store.broker.unsubscribe(sub_q)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def main():
    import uvicorn

    host = os.environ.get("AQUA_DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("AQUA_DASHBOARD_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()
