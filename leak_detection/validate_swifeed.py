"""SWI-FEED NET_6 Validation Script (Evaluation Only).

1. Reproduces NET_6 (24h, no leak) in WNTR and compares against the published
   reference CSVs (NET_6_bd_pattern_h_24_leak_area_0_{nodes,links}.csv).
2. Verifies that published CSVs contain zero leak events (has_leak=False everywhere).
3. Runs 48h (24h learn + 24h test) EPANET/WNTR simulations at 60s timestep on NET_6
   with documented leak injections at 4 network locations x 2 severities (0.5, 1.5 L/s)
   plus 2 normal (no-leak) runs, feeding causal 1-minute readings into Aqua Edge's
   unmodified FlatNode + Gateway firmware.

Outputs JSON summary to leak_detection/results/validation_swifeed.json.
"""
import json
import os
import sys
import numpy as np
import pandas as pd
import wntr
from wntr.network.controls import Control, ControlAction

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [
    os.path.join(ROOT, "firmware", "edge_node"),
    os.path.join(ROOT, "firmware", "gateway"),
    os.path.join(ROOT, "firmware", "communication"),
    os.path.join(ROOT, "simulation", "scripts"),
]

from aqua_edge import FlatNode, TankNode, DAY, HOUR  # noqa: E402
from gateway import Gateway  # noqa: E402
from packet import sim_key  # noqa: E402

DATA = os.path.join(ROOT, "simulation", "data", "swifeed")
LEAK_ALERTS = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
CONSUMPTION_ALERTS = {"ABNORMAL_CONSUMPTION"}


class SimHAL:
    storage_path = None
    key = staticmethod(sim_key)

    def __init__(self, name, clock, readings, inboxes):
        self.name, self.clock, self.readings, self.inboxes = name, clock, readings, inboxes
        inboxes[name] = []

    def now(self):
        return self.clock[0]

    def read(self):
        return self.readings[0][self.name]

    def send(self, msg, at_s=None, cad=False):
        for n, box in self.inboxes.items():
            if n != self.name:
                box.append(msg)

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def led(self, colour):
        pass


def verify_reference():
    wn = wntr.network.WaterNetworkModel(os.path.join(DATA, "NET_6.inp"))
    wn.options.time.duration = 23 * 3600
    res = wntr.sim.WNTRSimulator(wn).run_sim()

    def to_long(df, id_col):
        d = df.copy()
        d.index = (d.index // 3600).astype(int)
        return d.stack().rename("ours").rename_axis(["h", id_col]).reset_index()

    def theirs(fname, id_col, col):
        d = pd.read_csv(os.path.join(DATA, fname), sep=";", dtype={id_col: str})
        d["h"] = d.hour.str.split(":").str[0].astype(int)
        return d[["h", id_col, col]].rename(columns={col: "theirs"})

    p = to_long(res.node["pressure"][wn.junction_name_list], "nodeID").merge(
        theirs("NET_6_bd_pattern_h_24_leak_area_0_nodes.csv", "nodeID", "pressure_value")
    )
    q = to_long(res.link["flowrate"][wn.pipe_name_list], "linkID").merge(
        theirs("NET_6_bd_pattern_h_24_leak_area_0_links.csv", "linkID", "flowrate")
    )
    ref_nodes = pd.read_csv(os.path.join(DATA, "NET_6_bd_pattern_h_24_leak_area_0_nodes.csv"), sep=";", usecols=["has_leak", "leak_demand_value"])
    ref_nodes2 = pd.read_csv(os.path.join(DATA, "NET_6_bd_0_005_h_24_nodes.csv"), sep=";", usecols=["has_leak", "leak_demand_value"])

    return {
        "pressure_values_compared": int(len(p)),
        "pressure_mean_abs_diff_m": float((p.ours - p.theirs).abs().mean()),
        "pressure_max_abs_diff_m": float((p.ours - p.theirs).abs().max()),
        "pressure_corr": float(np.corrcoef(p.ours, p.theirs)[0, 1]),
        "flow_values_compared": int(len(q)),
        "flow_mean_abs_diff_m3s": float((q.ours - q.theirs).abs().mean()),
        "flow_max_abs_diff_m3s": float((q.ours - q.theirs).abs().max()),
        "flow_corr": float(np.corrcoef(q.ours, q.theirs)[0, 1]),
        "csv_bd_pattern_has_leak_true": int(ref_nodes["has_leak"].sum()),
        "csv_bd_0_005_has_leak_true": int(ref_nodes2["has_leak"].sum()),
    }


def pick_monitored_junctions(wn):
    """Pick 10 non-zero-demand junctions spread across the network."""
    res0 = wntr.sim.EpanetSimulator(wn).run_sim()
    dem = res0.node["demand"][wn.junction_name_list].mean()
    # Select junctions with positive demand (>= 0.05 L/s) spread by index
    cands = [j for j in wn.junction_name_list if dem[j] * 1000 >= 0.05]
    if len(cands) < 10:
        cands = [j for j in wn.junction_name_list if dem[j] * 1000 > 0.01]
    step = max(len(cands) // 10, 1)
    return cands[: step * 10 : step][:10]


def run_net6_case(monitored, leak_node, leak_lps, start_h=30, seed=7):
    wn = wntr.network.WaterNetworkModel(os.path.join(DATA, "NET_6.inp"))
    wn.options.time.duration = 48 * 3600 - 60
    wn.options.time.hydraulic_timestep = 60
    wn.options.time.report_timestep = 60

    start_s = start_h * 3600
    if leak_node is not None and leak_lps > 0:
        j = wn.get_node(leak_node)
        x, y = j.coordinates
        wn.add_junction(f"{leak_node}-LEAK", elevation=j.elevation, coordinates=(x + 1, y - 1))
        wn.add_pipe(f"{leak_node}-LPIPE", leak_node, f"{leak_node}-LEAK", length=1, diameter=0.05, roughness=130, initial_status="CLOSED")
        # Size emitter for ~leak_lps at ~30 m nominal head
        wn.get_node(f"{leak_node}-LEAK").emitter_coefficient = leak_lps / 1000.0 / np.sqrt(30.0)
        wn.add_control(
            "SWI-LEAK-START",
            Control._time_control(wn, start_s, "SIM_TIME", False, ControlAction(wn.get_link(f"{leak_node}-LPIPE"), "status", 1)),
        )

    res = wntr.sim.EpanetSimulator(wn).run_sim()
    rng = np.random.default_rng(seed)

    # Reservoirs supply links
    res_links = []
    for rname in wn.reservoir_name_list:
        for lname in wn.get_links_for_node(rname):
            res_links.append((rname, lname))

    aliases = ["B-401", "B-402", "B-403", "B-404", "B-301", "B-302", "B-303", "B-304", "B-201", "B-202"]
    j_to_alias = dict(zip(monitored, aliases))
    leak_alias = j_to_alias.get(leak_node)

    clock, readings, inboxes = [0.0], [None], {}
    hal = lambda n: SimHAL(n, clock, readings, inboxes)
    nodes = [FlatNode(j_to_alias[j], hal(j_to_alias[j])) for j in monitored] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), aliases, "B-TANK")

    times = res.node["pressure"].index.values
    actual_leak_lps = 0.0
    if leak_node is not None and leak_lps > 0:
        lflow = res.link["flowrate"][f"{leak_node}-LPIPE"] * 1000.0
        actual_leak_lps = float(lflow[lflow.index >= start_s].mean())

    # Pre-extract arrays for speed
    p_df = res.node["pressure"][monitored]
    d_df = res.node["demand"][monitored] * 1000.0
    total_dem = res.node["demand"][wn.junction_name_list].sum(axis=1) * 1000.0
    if leak_node is not None and leak_lps > 0:
        l_arr = res.link["flowrate"][f"{leak_node}-LPIPE"].values * 1000.0
    else:
        l_arr = np.zeros(len(times))

    for idx, t in enumerate(times):
        clock[0] = float(t)
        r = {}
        for j in monitored:
            # At the leak junction, add leak outflow to the junction's local meter
            f_val = float(d_df[j].iloc[idx]) + (float(l_arr[idx]) if j == leak_node else 0.0)
            r[j_to_alias[j]] = {
                "flow_lps": max(f_val + float(rng.normal(0, 0.0003)), 0.0),
                "pressure_m": float(p_df[j].iloc[idx]) + float(rng.normal(0, 0.05)),
            }
        # Supply inflow = total network demand + leak
        sup_out = float(total_dem.iloc[idx]) + float(l_arr[idx])
        r["B-TANK"] = {
            "outflow_lps": max(sup_out + float(rng.normal(0, 0.001)), 0.0),
            "inflow_lps": sup_out,
            "level_m": 1.5 + float(rng.normal(0, 0.01)),
            "ph": 7.2,
            "tds_ppm": 250.0,
            "turbidity_ntu": 0.5,
            "temp_c": 25.0,
        }
        readings[0] = r
        for n in nodes:
            n.step()
        gw.step()

    target_alerts = [
        (m["t"], m["a"], round((m["t"] - start_s) / 60, 1))
        for m in gw.log
        if m["k"] == "ALERT" and leak_alias is not None and m["n"] == leak_alias and m["a"] in LEAK_ALERTS and m["t"] >= start_s
    ]
    other_flat_leaks = [
        (m["t"], m["n"], m["a"])
        for m in gw.log
        if m["k"] == "ALERT" and m["a"] in LEAK_ALERTS and not (leak_alias is not None and m["n"] == leak_alias and m["t"] >= start_s)
    ]
    cons_alerts = [
        (m["t"], m["n"], m["a"])
        for m in gw.log
        if m["k"] == "ALERT" and m["a"] in CONSUMPTION_ALERTS
    ]
    # Hour-matched baseline pressure drop localization among monitored nodes
    base_mask = (times >= start_s - 24 * 3600) & (times < start_s - 23 * 3600)
    post_mask = (times >= start_s) & (times < start_s + 3600)
    p_drops = {
        j: round(float(p_df[j].iloc[base_mask].mean() - p_df[j].iloc[post_mask].mean()), 4)
        for j in monitored
    }
    ranked = sorted(p_drops.items(), key=lambda kv: -kv[1])
    max_drop_node = ranked[0][0]
    top2_nodes = [k for k, _ in ranked[:2]]
    first = target_alerts[0] if target_alerts else None
    return {
        "leak_node": leak_node or "none",
        "requested_lps": leak_lps,
        "actual_mean_lps": round(actual_leak_lps, 4),
        "detected_by_node": first is not None,
        "first_mechanism": first[1] if first else None,
        "latency_min": first[2] if first else None,
        "other_flat_false_leaks": len(other_flat_leaks),
        "consumption_alerts": len(cons_alerts),
        "max_p_drop_node": max_drop_node,
        "max_p_drop_m": p_drops[max_drop_node],
        "leak_node_p_drop_m": p_drops.get(leak_node) if leak_node else None,
        "top1_pdrop_localized": max_drop_node == leak_node if leak_node else None,
        "top2_pdrop_localized": leak_node in top2_nodes if leak_node else None,
    }


def main():
    os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
    ref = verify_reference()
    print("Reference verification:", ref)
    wn0 = wntr.network.WaterNetworkModel(os.path.join(DATA, "NET_6.inp"))
    monitored = pick_monitored_junctions(wn0)
    print("Monitored junctions:", monitored)

    cases = [
        (monitored[1], 0.5, 10),
        (monitored[1], 1.5, 11),
        (monitored[4], 0.5, 12),
        (monitored[4], 1.5, 13),
        (monitored[7], 0.5, 14),
        (monitored[7], 1.5, 15),
        (monitored[9], 0.5, 16),
        (monitored[9], 1.5, 17),
        (monitored[2], 0.5, 20),  # held-out junction 2055
        (monitored[2], 1.5, 21),
        (monitored[5], 0.5, 22),  # held-out junction 5350
        (monitored[5], 1.5, 23),
        (None, 0.0, 18),
        (None, 0.0, 19),
    ]
    runs = []
    for node, lps, seed in cases:
        res = run_net6_case(monitored, node, lps, start_h=30, seed=seed)
        print(res)
        runs.append(res)

    out = {"reference": ref, "monitored_junctions": monitored, "runs": runs}
    out_path = os.path.join(HERE, "results", "validation_swifeed.json")
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Saved SWI-FEED validation to {out_path}")


if __name__ == "__main__":
    main()
