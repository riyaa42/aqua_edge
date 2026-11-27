"""Unit tests for Gateway analytics: incident lifecycle, root-cause correlation,
risk scoring, maintenance priority ranking, delivery latency, and JSON snapshot export.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.dirname(HERE)
for _sub in ("edge_node", "gateway", "communication"):
    _p = os.path.join(FIRMWARE_DIR, _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from aqua_edge import SUMMARY_S
from gateway import Gateway, SILENT_S
from packet import pack, sim_key


FLATS = ["B-401", "B-402", "B-301", "B-201", "B-101", "B-G01"]
TANK = "B-TANK"


class TestHAL:
    key = staticmethod(sim_key)

    def __init__(self, seen_path):
        self.t = 0
        self.inbox = []
        self.sent = []
        self.seen_path = seen_path

    def now(self):
        return self.t

    def utc_now(self):
        return (float(self.t), True)

    def receive(self):
        out = list(self.inbox)
        self.inbox.clear()
        return out

    def send(self, pkt):
        self.sent.append(pkt)

    def load_seen(self):
        return {}

    def save_seen(self, d):
        pass


def _make_gw():
    td = tempfile.mkdtemp(prefix="gw_analytics_")
    hal = TestHAL(os.path.join(td, "seen.json"))
    gw = Gateway(hal, FLATS, TANK)
    return gw, hal, td


def _send(gw, hal, msg, rx=None):
    if rx is not None:
        hal.t = rx
    hal.inbox.append(pack(msg, sim_key(msg["n"])))
    gw.step()


def test_lifecycle_and_autoclear():
    gw, hal, _ = _make_gw()
    # 1. Quality alerts open, then auto-clear on QUALITY_CLEARED & QUALITY_TEMPORAL_CLEARED
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1000, "s": 1, "a": "QUALITY_CHEMICAL",
                    "ph": 5.2, "tds": 820.0, "turb": 9.1}, rx=1002)
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1005, "s": 2, "a": "QUALITY_LIMIT_VIOLATION",
                    "viol": 7, "ph": 5.2, "tds": 820.0, "turb": 9.1}, rx=1006)
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1010, "s": 3, "a": "QUALITY_TEMPORAL_ANOMALY",
                    "score": 0.94}, rx=1012)

    snap = gw.snapshot(now=1020)
    assert snap["water_quality"]["class"] == "CHEMICAL"
    assert snap["water_quality"]["compliant"] is False
    assert snap["water_quality"]["temporal_anomaly"] is True
    assert len(snap["active_incidents"]) == 1  # grouped into 1 Tank Contamination incident
    assert sorted(snap["active_incidents"][0]["symptoms"]) == [
        "QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION", "QUALITY_TEMPORAL_ANOMALY"
    ]

    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1200, "s": 4, "a": "QUALITY_CLEARED"}, rx=1202)
    snap2 = gw.snapshot(now=1205)
    assert snap2["water_quality"]["class"] == "NORMAL"
    assert snap2["water_quality"]["compliant"] is True
    # Temporal anomaly still active until QUALITY_TEMPORAL_CLEARED
    assert len(snap2["active_incidents"]) == 1
    assert snap2["active_incidents"][0]["category"] == "QUALITY_TEMPORAL_ANOMALY"

    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": 1210, "s": 5, "a": "QUALITY_TEMPORAL_CLEARED"}, rx=1212)
    assert gw.snapshot(now=1215)["active_incidents"] == []

    # 2. NODE_SILENT -> NODE_BACK auto-clear
    _send(gw, hal, {"k": "SUM", "n": "B-401", "t": 2000, "s": 1, "i": 2, "vol_l": 10.0}, rx=2002)
    hal.t = 2000 + SILENT_S + 60
    gw.step()
    assert any(it["category"] == "NODE_SILENT" and it["target"] == "B-401"
               for it in gw.snapshot()["active_incidents"])
    _send(gw, hal, {"k": "SUM", "n": "B-401", "t": hal.t - 1, "s": 2, "i": 3, "vol_l": 5.0}, rx=hal.t)
    assert not any(it["category"] == "NODE_SILENT" and it["target"] == "B-401"
                   for it in gw.snapshot()["active_incidents"])


def test_mass_balance_rearm_and_pipe_correlation():
    gw, hal, _ = _make_gw()
    # Two 15-min intervals with high mass-balance loss + pressure drop on floor 4 & 3
    seq = {n: 1 for n in FLATS + [TANK]}
    for blk in (1, 2):
        t = blk * SUMMARY_S
        hal.t = t + 2
        _send(gw, hal, {"k": "SUM", "n": TANK, "t": t, "s": seq[TANK], "i": blk,
                        "vol_l": 1000.0, "in_l": 0.0, "level_m": 2.0})
        seq[TANK] += 1
        for f in FLATS:
            _send(gw, hal, {"k": "SUM", "n": f, "t": t, "s": seq[f], "i": blk, "vol_l": 50.0})
            seq[f] += 1
        if blk == 1:
            _send(gw, hal, {"k": "ALERT", "n": "B-301", "t": t + 15, "s": seq["B-301"],
                            "a": "ABNORMAL_SUPPLY", "flow": 0.0}, rx=t + 17)
            seq["B-301"] += 1

    # Pressure drop arriving while pipe_alerted is active is correlated as secondary symptom
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 2 * SUMMARY_S + 10, "s": seq["B-401"],
                    "a": "LEAK_PRESSURE_DROP", "p_min": 5.0}, rx=2 * SUMMARY_S + 12)
    seq["B-401"] += 1

    snap = gw.snapshot(now=2 * SUMMARY_S + 30)
    assert gw.pipe_alerted is True
    pipe_incs = [it for it in snap["active_incidents"] if it["category"] == "PIPE_LEAK"]
    assert len(pipe_incs) == 1
    assert pipe_incs[0]["target"] == "B-RISER"
    # Correlated secondary symptoms grouped into the single PIPE_LEAK incident
    assert "B-401:FLAT_LEAK" in pipe_incs[0]["symptoms"]
    assert "B-301:ABNORMAL_SUPPLY" in pipe_incs[0]["symptoms"]
    assert snap["leakage"]["last_balance_pct"] == 70.0

    # Two balanced intervals re-arm pipe_alerted and clear PIPE_LEAK
    for blk in (3, 4):
        t = blk * SUMMARY_S
        hal.t = t + 2
        _send(gw, hal, {"k": "SUM", "n": TANK, "t": t, "s": seq[TANK], "i": blk,
                        "vol_l": 300.0, "in_l": 300.0, "level_m": 2.1})
        seq[TANK] += 1
        for f in FLATS:
            _send(gw, hal, {"k": "SUM", "n": f, "t": t, "s": seq[f], "i": blk, "vol_l": 50.0})
            seq[f] += 1
    assert gw.pipe_alerted is False
    assert not any(it["category"] == "PIPE_LEAK" for it in gw.snapshot()["active_incidents"])


def test_risk_ranking_and_floor_impact():
    gw, hal, _ = _make_gw()
    t = 10000
    _send(gw, hal, {"k": "ALERT", "n": TANK, "t": t, "s": 1, "a": "QUALITY_CHEMICAL",
                    "ph": 4.9, "tds": 900.0, "turb": 12.0}, rx=t + 1)
    _send(gw, hal, {"k": "ALERT", "n": "B-201", "t": t, "s": 1, "a": "LEAK_CONTINUOUS_FLOW",
                    "flow": 0.45}, rx=t + 2)
    _send(gw, hal, {"k": "ALERT", "n": "B-101", "t": t, "s": 1, "a": "ABNORMAL_CONSUMPTION",
                    "flow": 0.6}, rx=t + 3)
    gw.decide(t + 5, "PIPE_LEAK", suspected_floor="4", error_pct=35.0)
    gw.decide(t + 6, "SUPPLY_EVENT", inflow_lps=0.0)
    gw.decide(t + 7, "NODE_SILENT", node="B-G01", last_heard_min=200.0)

    queue = gw.snapshot(now=t + 600)["maintenance_queue"]
    cats = [it["category"] for it in queue]
    assert cats.index("QUALITY_CHEMICAL") < cats.index("PIPE_LEAK")
    assert cats.index("PIPE_LEAK") < cats.index("SUPPLY_EVENT")
    assert cats.index("PIPE_LEAK") < cats.index("FLAT_LEAK")
    assert cats.index("FLAT_LEAK") < cats.index("ABNORMAL_CONSUMPTION")
    assert cats.index("ABNORMAL_CONSUMPTION") < cats.index("NODE_SILENT")
    assert [it["rank"] for it in queue] == list(range(1, len(queue) + 1))

    # Population impact check: B-D4 affects more downstream flats than B-D1
    gw_d4, hal4, _ = _make_gw()
    gw_d4.decide(1000, "PIPE_LEAK", suspected_floor="4", error_pct=30.0)
    gw_d1, hal1, _ = _make_gw()
    gw_d1.decide(1000, "PIPE_LEAK", suspected_floor="1", error_pct=30.0)
    q4 = gw_d4.snapshot(now=1600)["maintenance_queue"][0]
    q1 = gw_d1.snapshot(now=1600)["maintenance_queue"][0]
    assert q4["affected_flats"] > q1["affected_flats"]
    assert q4["risk_score"] > q1["risk_score"]


def test_latency_outage_recovery_and_json_snapshot():
    gw, hal, _ = _make_gw()
    # 1 online alert (2 s delay) + 1 buffered alert recovered after 3.5h (12600 s) outage
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 1000, "s": 1, "a": "ABNORMAL_CONSUMPTION",
                    "flow": 0.5}, rx=1002)
    _send(gw, hal, {"k": "ALERT", "n": "B-401", "t": 5000, "s": 2, "a": "LEAK_NIGHT_FLOW",
                    "flow": 0.25}, rx=5000 + 12600)

    snap = gw.snapshot(now=17610)
    nh = snap["network_health"]
    assert nh["online_alerts"] == 1
    assert nh["offline_alerts"] == 1
    assert nh["buffered_recovered"] == 1
    assert nh["max_delay_s"] == 12600.0
    assert nh["mean_delay_s"] == 6301.0

    # Verify full snapshot is cleanly JSON-serializable
    encoded = json.dumps(snap)
    decoded = json.loads(encoded)
    assert decoded["building_status"] in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "NORMAL")
    assert set(decoded["nodes"].keys()) == set(FLATS + [TANK])


def test_review_edge_cases():
    # 1. Correlation window: old unrelated ABNORMAL_SUPPLY (>30m ago) must NOT merge into new PIPE_LEAK
    gw, hal, _ = _make_gw()
    _send(gw, hal, {"k": "ALERT", "n": "B-301", "t": 1000, "s": 1, "a": "ABNORMAL_SUPPLY", "flow": 0.0}, rx=1002)
    gw.decide(5000, "PIPE_LEAK", suspected_floor="4", error_pct=25.0)
    incs = gw.snapshot(now=5010)["active_incidents"]
    pipe_inc = [it for it in incs if it["category"] == "PIPE_LEAK"][0]
    assert "B-301:ABNORMAL_SUPPLY" not in pipe_inc["symptoms"]
    assert any(it["category"] == "ABNORMAL_SUPPLY" and it["target"] == "B-301" for it in incs)

    # 2. Auto-resolution: NODE_BACK clears NODE_SILENT without clearing FLAT_LEAK on same node
    gw2, hal2, _ = _make_gw()
    _send(gw2, hal2, {"k": "ALERT", "n": "B-401", "t": 1000, "s": 1, "a": "LEAK_CONTINUOUS_FLOW", "flow": 0.3}, rx=1002)
    hal2.t = 1000 + SILENT_S + 60
    gw2.step()
    assert any(it["category"] == "NODE_SILENT" and it["target"] == "B-401" for it in gw2.snapshot()["active_incidents"])
    _send(gw2, hal2, {"k": "SUM", "n": "B-401", "t": hal2.t - 1, "s": 2, "i": 1, "vol_l": 5.0}, rx=hal2.t)
    active2 = gw2.snapshot()["active_incidents"]
    assert not any(it["category"] == "NODE_SILENT" and it["target"] == "B-401" for it in active2)
    assert any(it["category"] == "FLAT_LEAK" and it["target"] == "B-401" for it in active2)

    # 3. Risk calibration: long-lived low-severity events cannot outrank contamination
    gw3, hal3, _ = _make_gw()
    gw3.decide(1000, "NODE_SILENT", node=TANK, last_heard_min=1440.0)
    _send(gw3, hal3, {"k": "ALERT", "n": "B-101", "t": 1000, "s": 1, "a": "ABNORMAL_CONSUMPTION", "flow": 1.0}, rx=1002)
    _send(gw3, hal3, {"k": "ALERT", "n": TANK, "t": 86400, "s": 1, "a": "QUALITY_TEMPORAL_ANOMALY", "score": 0.9}, rx=86402)
    q3 = gw3.snapshot(now=87400)["maintenance_queue"]
    cats3 = [it["category"] for it in q3]
    assert cats3.index("QUALITY_TEMPORAL_ANOMALY") < cats3.index("ABNORMAL_CONSUMPTION")
    assert cats3.index("QUALITY_TEMPORAL_ANOMALY") < cats3.index("NODE_SILENT")

    # 4. Incident history: resolved incidents remain in incident_history after leaving active queue
    _send(gw3, hal3, {"k": "ALERT", "n": TANK, "t": 87500, "s": 2, "a": "QUALITY_TEMPORAL_CLEARED"}, rx=87502)
    snap3 = gw3.snapshot(now=87505)
    assert not any(it["category"] == "QUALITY_TEMPORAL_ANOMALY" for it in snap3["active_incidents"])
    assert any(it["category"] == "QUALITY_TEMPORAL_ANOMALY" and it["resolution_reason"] == "QUALITY_TEMPORAL_CLEARED"
               for it in snap3["incident_history"])

    # 5. Delivery latency: negative rx - t (clock skew) must NOT pollute delays_s or online_alerts
    gw4, hal4, _ = _make_gw()
    _send(gw4, hal4, {"k": "ALERT", "n": "B-401", "t": 5000, "s": 1, "a": "ABNORMAL_CONSUMPTION", "flow": 0.4}, rx=4000)
    nh4 = gw4.snapshot(now=5010)["network_health"]
    assert nh4["skewed_packets"] == 1
    assert nh4["online_alerts"] == 0
    assert nh4["mean_delay_s"] == 0.0
    assert nh4["max_delay_s"] == 0.0

    # 6. Empty snapshot JSON-serializable & non-mutating
    gw_empty, _, _ = _make_gw()
    s_a = gw_empty.snapshot()
    s_b = gw_empty.snapshot()
    assert s_a == s_b
    assert s_a["building_status"] == "NORMAL"
    assert s_a["incident_history"] == []
    assert json.loads(json.dumps(s_a)) == s_a


def main():
    test_lifecycle_and_autoclear()
    test_mass_balance_rearm_and_pipe_correlation()
    test_risk_ranking_and_floor_impact()
    test_latency_outage_recovery_and_json_snapshot()
    test_review_edge_cases()
    print("GATEWAY ANALYTICS TESTS PASSED")


if __name__ == "__main__":
    main()
