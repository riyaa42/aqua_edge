"""Combined Three-Pipeline EdgeRuntime Replay, Resource & Storage Benchmark.

Exercises EdgeRuntime in 'flat', 'tank', and 'combined' modes simultaneously:
  - Building B EPANET/WNTR flat (flow_lps, pressure_m) + tank (level_m, inflow/outflow)
  - Synthetic E4 (runoff) & E5 (chemical) water-quality streams (make_scenario.QUALITY)
  - Abnormal-consumption spike, flat leak, Store SD persistence, Baseline, packet.pack,
    Gateway integration, NaN/missing-stream safety, and state isolation.

Measures actual combined CPU time, tracemalloc RAM, throughput, packet serialization
overhead, and flash/storage footprint (explicitly labeled with host hardware/OS).
"""
import json
import math
import os
import platform
import shutil
import sys
import tempfile
import time
import tracemalloc
import numpy as np
import wntr

HERE = os.path.dirname(os.path.abspath(__file__))
FIRMWARE_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(FIRMWARE_DIR)
sys.path[:0] = [
    os.path.join(FIRMWARE_DIR, "edge_node"),
    os.path.join(FIRMWARE_DIR, "gateway"),
    os.path.join(FIRMWARE_DIR, "communication"),
    os.path.join(ROOT, "leak_detection"),
    os.path.join(ROOT, "simulation", "webots"),
    os.path.join(ROOT, "simulation", "scripts"),
]

from aqua_edge import EdgeRuntime, FlatNode, TankNode, DAY  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import pack, sim_key  # noqa: E402
import compare_leaks as CL  # noqa: E402
import make_scenario as M  # noqa: E402
import build_society as S  # noqa: E402


class ReplayHAL:
    key = staticmethod(sim_key)

    def __init__(self, name, clock, readings, inboxes, outage_windows=(), storage_path=None):
        self.name, self.clock, self.readings, self.inboxes = name, clock, readings, inboxes
        self.outage_windows = outage_windows
        self.storage_path = storage_path
        inboxes[name] = []

    def now(self):
        return self.clock[0]

    def read(self):
        return self.readings[0][self.name]

    def send(self, msg, at_s=None, cad=False):
        t = self.clock[0]
        if any(s <= t < e for s, e in self.outage_windows):
            return
        for n, box in self.inboxes.items():
            if n != self.name:
                box.append(msg)

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def led(self, colour):
        pass


def build_replay_streams(hours=54):
    """Generate causal 1-min streams from Building B WNTR + synthetic E4/E5 quality."""
    S.BUILDINGS = ["B"]
    wn = S.build(leak=False, bursty=True, seed=101)
    wn.options.time.duration = hours * 3600 - 60
    wn.options.time.hydraulic_timestep = 60
    wn.options.time.report_timestep = 60
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        res = wntr.sim.EpanetSimulator(wn).run_sim(file_prefix=os.path.join(td, "sim"))

    rng = np.random.default_rng(7)
    times = res.node["pressure"].index.values
    f_b203 = res.link["flowrate"]["B-203-P"].values * 1000.0
    p_b203 = res.node["pressure"]["B-203"].values
    dt4 = res.link["flowrate"]["B-DT4"].values * 1000.0
    rise = res.link["flowrate"]["B-RISE"].values * 1000.0
    lvl = res.node["pressure"]["B-TANK"].values

    ph, tds, turb, temp = M.quality(times, rng)
    stream = []
    for idx, t in enumerate(times):
        t_f = float(t)
        fl = max(float(f_b203[idx]) + float(rng.normal(0, M.FLOW_NOISE_LPS)), 0.0)
        pr = float(p_b203[idx]) + float(rng.normal(0, M.PRESSURE_NOISE_M))
        # Inject simultaneous abnormal consumption + medium flat leak at 34h..36h
        if 34 * 3600 <= t_f < 36 * 3600:
            fl += 0.50
            pr -= 0.70
        stream.append({
            "t": t_f,
            "flow_lps": fl,
            "pressure_m": pr,
            "outflow_lps": max(float(dt4[idx]), 0.0),
            "inflow_lps": max(float(rise[idx]), 0.0),
            "level_m": float(lvl[idx]),
            "ph": float(ph[idx]),
            "tds_ppm": float(tds[idx]),
            "turbidity_ntu": float(turb[idx]),
            "temp_c": float(temp[idx]),
        })
    return stream


def test_combined_replay_and_isolation(stream):
    # 1. Combined 3-pipeline replay on one EdgeRuntime with Store, Gateway, and 1h outage
    clock, readings, inboxes = [0.0], [{}], {}
    td = tempfile.mkdtemp(prefix="unified_rt_")
    hal_rt = ReplayHAL(
        "B-203", clock, readings, inboxes,
        outage_windows=[(34 * 3600, 35 * 3600)],
        storage_path=os.path.join(td, "B-203_q.jsonl"),
    )
    rt = EdgeRuntime("B-203", hal_rt, mode="combined")
    gw = Gateway(ReplayHAL("GATEWAY", clock, readings, inboxes), ["B-203"], "B-TANK")

    for row in stream:
        clock[0] = row["t"]
        readings[0] = {"B-203": row}
        rt.step()
        gw.step()

    alerts = {(m["a"], round(m["t"] / 3600, 2)) for m in gw.log if m["k"] == "ALERT"}
    alert_names = {a for a, _ in alerts}
    assert "QUALITY_RUNOFF" in alert_names, alert_names       # E4 at 30h
    assert "QUALITY_CHEMICAL" in alert_names, alert_names     # E5 at 44h
    assert "LEAK_PRESSURE_DROP" in alert_names, alert_names   # flat leak at 34h
    assert "ABNORMAL_CONSUMPTION" in alert_names, alert_names # high flow at 34h
    assert len(rt.store.queue) == 0, f"Queue did not drain: {len(rt.store.queue)}"
    assert os.path.exists(rt.base_path), "Baseline not persisted"

    # Verify leak scoring excludes consumption & quality alerts
    for non_leak in ("ABNORMAL_CONSUMPTION", "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "PROLONGED_ZERO_FLOW"):
        assert CL.score((None, None, 100, 0), [(500, "B-203", non_leak)]) == {"flow": (None, 0), "pressure": (None, 0)}

    # 2. Missing / NaN sensor streams must not fabricate readings or crash
    clock2, readings2, inboxes2 = [0.0], [{}], {}
    rt_nan = EdgeRuntime("B-203", ReplayHAL("B-203", clock2, readings2, inboxes2), mode="combined")
    for m in range(25 * 60):
        clock2[0] = m * 60.0
        if 24 * 60 <= m < 24 * 60 + 20:
            readings2[0] = {"B-203": {"flow_lps": float("nan"), "pressure_m": None, "ph": float("nan")}}
        else:
            readings2[0] = {"B-203": stream[m]}
        rt_nan.step()

    # 3. State isolation between households (B-203 vs B-401)
    c3, r3, i3 = [0.0], [{}], {}
    n1 = EdgeRuntime("B-203", ReplayHAL("B-203", c3, r3, i3), mode="flat")
    n2 = EdgeRuntime("B-401", ReplayHAL("B-401", c3, r3, i3), mode="flat")
    for m in range(25 * 60):
        c3[0] = m * 60.0
        r3[0] = {
            "B-203": {"flow_lps": 0.01, "pressure_m": 10.0},
            "B-401": {"flow_lps": 0.20, "pressure_m": 10.0},
        }
        n1.step(); n2.step()
    assert abs(n1.base["flow_lps"].mean(0) - 0.01) < 1e-6
    assert abs(n2.base["flow_lps"].mean(0) - 0.20) < 1e-6
    return alert_names, td


def measure_resources(stream, td_sample):
    results = {}
    for mode in ("flat", "tank", "combined"):
        clock, readings, inboxes = [0.0], [{}], {}
        hal = ReplayHAL("B-203", clock, readings, inboxes)
        tracemalloc.start()
        rt = EdgeRuntime("B-203", hal, mode=mode)
        t_wall0 = time.perf_counter()
        t_cpu0 = time.process_time()
        for row in stream:
            clock[0] = row["t"]
            readings[0] = {"B-203": row}
            rt.step()
        wall_s = time.perf_counter() - t_wall0
        cpu_s = time.process_time() - t_cpu0
        _, peak_ram = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        n = len(stream)
        results[mode] = {
            "steps": n,
            "wall_ms_total": round(wall_s * 1000, 2),
            "us_per_step": round(wall_s / n * 1e6, 2),
            "cpu_pct_of_1min_budget": round((cpu_s / (n * 60.0)) * 100, 5),
            "throughput_steps_per_s": round(n / wall_s, 1),
            "peak_ram_kb": round(peak_ram / 1024, 2),
        }

    # Packet serialization benchmark
    sample_msg = {"k": "SUM", "n": "B-203", "s": 1, "t": 900, "i": 1, "vol_l": 12.5, "p_min": 9.8, "p_mean": 10.1}
    k = sim_key("B-203")
    t0 = time.perf_counter()
    for _ in range(10000):
        raw = pack(sample_msg, k)
    pack_us = (time.perf_counter() - t0) / 10000 * 1e6

    # Flash / storage footprint
    artifacts = [
        "firmware/edge_node/aqua_edge.py",
        "firmware/gateway/gateway.py",
        "firmware/gateway/gateway_analytics.py",
        "firmware/communication/packet.py",
        "firmware/edge_node/quality_model.py",
        "firmware/edge_node/quality_model.json",
        "firmware/edge_node/temporal_model.json",
        "abnormal_consumption/detectors.py",
    ]
    artifact_bytes = {p: os.path.getsize(os.path.join(ROOT, p)) for p in artifacts}
    disk = shutil.disk_usage(ROOT)
    # 24h & 7d Store retention estimate (~96 SUM/day * ~110 bytes JSONL + ~5 alerts)
    store_24h_bytes = 96 * 115 + 5 * 110
    store_7d_bytes = store_24h_bytes * 7
    base_bytes = os.path.getsize(os.path.join(td_sample, "B-203_q_baseline.json")) if os.path.exists(os.path.join(td_sample, "B-203_q_baseline.json")) else 0

    return {
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "is_raspberry_pi": False,
        },
        "modes": results,
        "incremental_ram_kb": {
            "combined_minus_flat": round(results["combined"]["peak_ram_kb"] - results["flat"]["peak_ram_kb"], 2),
            "combined_minus_tank": round(results["combined"]["peak_ram_kb"] - results["tank"]["peak_ram_kb"], 2),
        },
        "packet_pack_us": round(pack_us, 2),
        "packet_sum_bytes": len(raw),
        "storage_bytes": {
            "artifacts": artifact_bytes,
            "total_code_and_models_kb": round(sum(artifact_bytes.values()) / 1024, 2),
            "baseline_json_bytes": base_bytes,
            "store_24h_est_bytes": store_24h_bytes,
            "store_7d_est_bytes": store_7d_bytes,
            "host_disk_free_gb": round(disk.free / (1024 ** 3), 2),
        },
    }


def main():
    stream = build_replay_streams(hours=54)
    alerts, td = test_combined_replay_and_isolation(stream)
    print("Combined 3-pipeline replay PASS; alerts:", sorted(alerts))
    metrics = measure_resources(stream, td)
    print(json.dumps(metrics, indent=2))
    out_path = os.path.join(FIRMWARE_DIR, "unified_runtime_metrics.json")
    with open(out_path, "w") as f:
        json.dump(metrics, f, indent=2)
    print("UNIFIED EDGE RUNTIME TESTS PASSED")


if __name__ == "__main__":
    main()
