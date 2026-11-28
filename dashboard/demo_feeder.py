"""Standalone dashboard demonstration feeder (Phase 5).

Completely separate from the real Pi -> LoRa -> ESP8266 -> Gateway -> DashboardFeed
pipeline. Generates clearly marked simulated Gateway.snapshot() payloads and ships
them through DashboardShipper -> FastAPI -> SQLite -> SSE -> React UI for previewing
1-node, 4-node, and Internet-outage recovery scenarios.
"""
import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import urllib.request

from dashboard.shipper import DashboardShipper
from gateway.topology import Topology

DEFAULT_4NODE_TOPOLOGY = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "demo_4node_topology.json"
)


def _load_demo_nodes(scenario, topology_path=None):
    """Return (root_id, all_node_ids, topology_obj_or_None) without hardcoding Building B."""
    if topology_path:
        topo = Topology.load(topology_path)
        devices = sorted(topo.devices)
        return topo.tank, devices, topo

    if scenario == "1node":
        return None, ["PI-NODE-1"], None

    return _load_demo_nodes("4node", DEFAULT_4NODE_TOPOLOGY)


def build_demo_snapshot(step_idx, now_utc, root_id, node_ids, scenario):
    """Build a representative simulated Gateway.snapshot() matching gateway/analytics.py.

    All values are explicitly simulated for dashboard demonstration; no detection
    algorithms, risk scoring, or correlation logic is implemented here.
    """
    phase = step_idx % 10
    is_single = scenario == "1node" or len(node_ids) <= 1
    primary = node_ids[0]
    secondary = node_ids[1] if len(node_ids) > 1 else primary
    tertiary = node_ids[2] if len(node_ids) > 2 else primary

    # Keep cross-node analytics unavailable in 1-node mode (never fabricate 0.0)
    balance_pct = None if is_single else round(3.2 + (step_idx % 3) * 0.4, 2)
    pipe_leak_active = False
    pipe_leak_info = None
    building_status = "NORMAL"
    queue = []
    active_incidents = []
    history = []
    silent_nodes = []
    abnormal_consumption = []
    flat_leaks = []

    ph = round(7.15 + ((step_idx % 4) - 1) * 0.05, 2)
    tds = round(240.0 + (step_idx % 5) * 6.0, 1)
    turb = round(0.8 + (step_idx % 3) * 0.1, 2)
    wq_class = "NORMAL"
    wq_compliant = True
    viol_mask = 0
    supply_state = "NORMAL"
    tank_level = None if (is_single and root_id is None) else round(1.85 - (step_idx % 4) * 0.03, 2)
    tank_inflow = None if (is_single and root_id is None) else 0.32

    if phase in (2, 3):
        # Simulated water-quality alert on primary/root node
        ph, tds, turb = 5.1, 860.0, 9.4
        wq_class, wq_compliant, viol_mask = "CHEMICAL", False, 7
        building_status = "CRITICAL"
        target = root_id or primary
        inc = {
            "target": target,
            "category": "QUALITY_CHEMICAL",
            "symptoms": ["QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION"],
            "t_onset": now_utc - 180.0,
            "t_last": now_utc,
            "repeat_count": 2,
            "affected_flats": len(node_ids),
            "evidence": {"simulated": True, "ph": ph, "tds": tds, "turb": turb, "viol": viol_mask},
        }
        active_incidents.append(inc)
        queue.append({
            "rank": 1,
            "target": target,
            "category": "QUALITY_CHEMICAL",
            "risk_score": 88.0,
            "severity": "CRITICAL",
            "affected_flats": len(node_ids),
            "duration_min": 3.0,
            "symptoms": inc["symptoms"],
            "evidence": inc["evidence"],
            "recommended_action": "[SIMULATED] Isolate water supply and inspect tank quality.",
        })
    elif phase in (4, 5) and not is_single:
        # Simulated 4-node main pipe leak + flat leak + node silence
        pipe_leak_active = True
        balance_pct = 38.5
        building_status = "HIGH"
        pipe_leak_info = {
            "target": root_id or primary,
            "suspected_floor": None,
            "suspected_range": [],
            "localization_confidence": "simulated",
            "error_pct": balance_pct,
        }
        inc_pipe = {
            "target": root_id or primary,
            "category": "PIPE_LEAK",
            "symptoms": ["PIPE_LEAK", f"{secondary}:FLAT_LEAK"],
            "t_onset": now_utc - 600.0,
            "t_last": now_utc,
            "repeat_count": 2,
            "affected_flats": len(node_ids),
            "evidence": {"simulated": True, "error_pct": balance_pct},
        }
        inc_silent = {
            "target": tertiary,
            "category": "NODE_SILENT",
            "symptoms": ["NODE_SILENT"],
            "t_onset": now_utc - 300.0,
            "t_last": now_utc,
            "repeat_count": 1,
            "affected_flats": 1,
            "evidence": {"simulated": True, "last_heard_min": 46.0},
        }
        active_incidents.extend([inc_pipe, inc_silent])
        silent_nodes.append(tertiary)
        flat_item = {
            "rank": 2,
            "target": secondary,
            "category": "FLAT_LEAK",
            "risk_score": 42.0,
            "severity": "MEDIUM",
            "affected_flats": 1,
            "duration_min": 5.0,
            "symptoms": ["FLAT_LEAK"],
            "evidence": {"simulated": True, "flow": 0.35},
            "recommended_action": "[SIMULATED] Inspect fixture for continuous flow.",
        }
        flat_leaks.append(flat_item)
        queue.extend([
            {
                "rank": 1,
                "target": root_id or primary,
                "category": "PIPE_LEAK",
                "risk_score": 68.0,
                "severity": "HIGH",
                "affected_flats": len(node_ids),
                "duration_min": 10.0,
                "symptoms": inc_pipe["symptoms"],
                "evidence": inc_pipe["evidence"],
                "recommended_action": "[SIMULATED] Inspect main distribution line.",
            },
            flat_item,
        ])
    elif phase in (6, 7):
        # Simulated abnormal consumption on primary (works in both 1-node and 4-node)
        building_status = "MEDIUM"
        item = {
            "rank": 1,
            "target": primary,
            "category": "ABNORMAL_CONSUMPTION",
            "risk_score": 32.0,
            "severity": "MEDIUM",
            "affected_flats": 1,
            "duration_min": 4.0,
            "symptoms": ["ABNORMAL_CONSUMPTION"],
            "evidence": {"simulated": True, "flow": 0.72},
            "recommended_action": "[SIMULATED] Check for unusual usage spike.",
        }
        abnormal_consumption.append(item)
        queue.append(item)
        active_incidents.append({
            "target": primary,
            "category": "ABNORMAL_CONSUMPTION",
            "symptoms": ["ABNORMAL_CONSUMPTION"],
            "t_onset": now_utc - 240.0,
            "t_last": now_utc,
            "repeat_count": 1,
            "affected_flats": 1,
            "evidence": {"simulated": True, "flow": 0.72},
        })

    if phase >= 4:
        history.append({
            "target": root_id or primary,
            "category": "QUALITY_CHEMICAL",
            "t_onset": now_utc - 1200.0,
            "t_last": now_utc - 900.0,
            "rx_last": now_utc - 898.0,
            "repeat_count": 2,
            "evidence": {"simulated": True},
            "t_resolved": now_utc - 840.0,
            "resolution_reason": "QUALITY_CLEARED",
        })

    nodes = {}
    for idx, nid in enumerate(node_ids):
        is_sil = nid in silent_nodes
        nodes[nid] = {
            "status": "silent" if is_sil else "ok",
            "last_seen": (now_utc - 2800.0) if is_sil else now_utc,
            "last_sum": {"t": now_utc, "vol_l": round(40.0 + idx * 5.0, 1), "simulated": True},
        }

    return {
        "t": now_utc,
        "building_status": building_status,
        "maintenance_queue": queue,
        "active_incidents": active_incidents,
        "incident_history": history,
        "water_quality": {
            "class": wq_class,
            "compliant": wq_compliant,
            "viol_mask": viol_mask,
            "temporal_anomaly": False,
            "ph": ph,
            "tds": tds,
            "turb": turb,
            "score": None,
        },
        "leakage": {
            "pipe_leak_active": pipe_leak_active,
            "last_balance_pct": balance_pct,
            "pipe_leak": pipe_leak_info,
            "flat_leaks": flat_leaks,
        },
        "supply": {
            "state": supply_state,
            "level_m": tank_level,
            "inflow_lps": tank_inflow,
            "t": now_utc,
        },
        "abnormal_consumption": abnormal_consumption,
        "network_health": {
            "silent_nodes": silent_nodes,
            "balance_paused": is_single,
            "bad_packets": 0,
            "skewed_packets": 0,
            "clock_skew_nodes": [],
            "online_alerts": len(active_incidents),
            "offline_alerts": 0,
            "buffered_recovered": 0,
            "mean_delay_s": 0.4,
            "max_delay_s": 1.2,
        },
        "nodes": nodes,
    }


def _toggle_server_outage(ingest_url, token, enabled):
    parsed = urllib.parse.urlparse(ingest_url)
    outage_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/api/v1/sim/outage", "", "", ""))
    body = json.dumps({"enabled": enabled}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(outage_url, data=body, headers=headers, method="POST")
    if (parsed.hostname or "").lower() in ("127.0.0.1", "localhost", "::1"):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=5.0) as resp:
            return resp.status
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        return resp.status


def run_demo(
    scenario="4node",
    mode="edge",
    interval_s=3.0,
    gateway_id=None,
    topology_path=None,
    endpoint="http://127.0.0.1:8000/api/v1/ingest",
    auth_token=None,
    storage_dir=None,
    steps=0,
    outage_at=None,
    outage_steps=3,
    stop_event=None,
    log=print,
):
    """Run the standalone demo feeder loop."""
    gw_id = gateway_id or f"demo-{scenario}"
    s_dir = storage_dir or os.path.join("dashboard", "run", f"demo_{gw_id}")
    token = auth_token if auth_token is not None else os.environ.get("AQUA_DASHBOARD_TOKEN", "dev-token")
    root_id, node_ids, _ = _load_demo_nodes(scenario, topology_path)
    shipper = DashboardShipper(
        endpoint=endpoint,
        storage_dir=s_dir,
        gateway_id=gw_id,
        mode=mode,
        auth_token=token,
    )

    stop = stop_event or threading.Event()
    step_idx = 0
    in_outage = False
    log(f"[SIMULATED DEMO] gateway={gw_id} scenario={scenario} mode={mode} nodes={node_ids}")

    try:
        while not stop.is_set():
            if outage_at is not None:
                if step_idx == outage_at and not in_outage:
                    try:
                        _toggle_server_outage(endpoint, token, True)
                        in_outage = True
                        log(f"[SIMULATED DEMO] Internet outage ON at step {step_idx}")
                    except Exception as exc:
                        log(f"[SIMULATED DEMO] Could not toggle outage ON: {exc}")
                elif in_outage and step_idx >= outage_at + outage_steps:
                    try:
                        _toggle_server_outage(endpoint, token, False)
                        in_outage = False
                        log(f"[SIMULATED DEMO] Internet outage OFF at step {step_idx}")
                    except Exception as exc:
                        log(f"[SIMULATED DEMO] Could not toggle outage OFF: {exc}")

            now_utc = time.time()
            snap = build_demo_snapshot(step_idx, now_utc, root_id, node_ids, scenario)
            seq = shipper.publish(snap, created_at_utc=now_utc, force=(not in_outage and bool(shipper.queue)))
            m = shipper.metrics()
            log(
                f"[SIMULATED DEMO] seq={seq} status={snap['building_status']} "
                f"pending={m['pending_frames']} delivered={m['delivered_frames']}"
            )
            step_idx += 1
            if steps > 0 and step_idx >= steps:
                break
            if interval_s > 0:
                stop.wait(interval_s)
    finally:
        if in_outage:
            try:
                _toggle_server_outage(endpoint, token, False)
            except Exception:
                pass
        shipper.flush_all(force=True)
    return shipper


def main(argv=None):
    ap = argparse.ArgumentParser(description="Standalone JalRakshak dashboard demo feeder (simulated data only).")
    ap.add_argument("--scenario", choices=("1node", "4node"), default="4node")
    ap.add_argument("--mode", choices=("edge", "cloud"), default="edge")
    ap.add_argument("--interval", type=float, default=3.0, help="seconds between simulated snapshots")
    ap.add_argument("--gateway-id", default=None, help="dedicated demo gateway id (default: demo-<scenario>)")
    ap.add_argument("--topology", default=None, help="optional Topology JSON path")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8000/api/v1/ingest")
    ap.add_argument("--token", default=None, help="Bearer token (default: AQUA_DASHBOARD_TOKEN or dev-token)")
    ap.add_argument("--storage-dir", default=None, help="isolated demo outbox directory")
    ap.add_argument("--steps", type=int, default=0, help="number of frames to emit (0 = run until Ctrl-C)")
    ap.add_argument("--outage-at", type=int, default=None, help="step index to start simulated Internet outage")
    ap.add_argument("--outage-steps", type=int, default=3, help="number of steps the simulated outage lasts")
    args = ap.parse_args(argv)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    run_demo(
        scenario=args.scenario,
        mode=args.mode,
        interval_s=args.interval,
        gateway_id=args.gateway_id,
        topology_path=args.topology,
        endpoint=args.endpoint,
        auth_token=args.token,
        storage_dir=args.storage_dir,
        steps=args.steps,
        outage_at=args.outage_at,
        outage_steps=args.outage_steps,
        stop_event=stop,
    )


if __name__ == "__main__":
    main()
