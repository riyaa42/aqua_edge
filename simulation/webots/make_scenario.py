"""Step 1: simulate building B for 48 h in EPANET and write what each sensor would read.

Day 1 is normal (the firmware learns its normal pattern). Day 2 has planted events:
  E1 02:00  leak inside flat B-203 (0.05 L/s, a running toilet), stays on
  E2 09:00-17:00  power cut: building pump cannot run
  E3 18:00  pipe burst on the 1st-floor downtake (1.5 L/s), stays on
  E4 06:00-07:00  rainwater-like water in the tank (low TDS, acidic)    } quality is made up:
  E5 20:00-20:45  chemical contamination (high TDS, high pH, bleach)    } EPANET cannot model it
Network outage O1 (day 2, 01:30-05:00): no node can reach the gateway.

Outputs (data/): sensors.csv (one row per minute, columns "<node>:<sensor>"),
events.csv (ground truth), outages.csv.
"""
import os
import sys
import numpy as np
import pandas as pd
import wntr
from wntr.network.controls import (Rule, Control, AndCondition, SimTimeCondition,
                                   ValueCondition, ControlAction)

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
import build_society as S  # noqa: E402

DAY = 86400
H = 3600
SEED = 7
# Sensor error model (calibration knobs: replace with numbers from your own sensor calibration)
FLOW_GAIN_ERR = 0.02        # each flow meter reads up to +-2% off, fixed per meter
FLOW_NOISE_LPS = 0.0003     # random noise per flow reading
PRESSURE_NOISE_M = 0.05     # random noise per pressure reading (~0.5 kPa)
LEVEL_NOISE_M = 0.01        # tank level sensor noise

EVENTS = [  # name, kind, start (s), end (s), where, which detector should catch it
    ("E1", "flat_leak", DAY + 2 * H, 2 * DAY, "B-203", "night flow at flat node"),
    ("E2", "power_cut", DAY + 9 * H, DAY + 17 * H, "B-PUMP", "gateway: supply event, no valve closures"),
    ("E3", "pipe_burst", DAY + 18 * H, 2 * DAY, "B-D1", "tank node supply anomaly + gateway mass balance"),
    ("E4", "quality_runoff", DAY + 6 * H, DAY + 7 * H, "B-TANK", "tank node quality check"),
    ("E5", "quality_chemical", DAY + 20 * H, DAY + 20 * H + 45 * 60, "B-TANK", "tank node quality check"),
]
OUTAGES = [("O1", "all", DAY + 1.5 * H, DAY + 5 * H)]


def base_network():
    """Building B with one-minute steps and pump rules, no planted events (also used by live/)."""
    S.BUILDINGS = ["B"]
    wn = S.build(leak=False)
    wn.options.time.duration = 2 * DAY - 60
    wn.options.time.hydraulic_timestep = 60
    wn.options.time.report_timestep = 60
    tank, pump = wn.get_node("B-TANK"), wn.get_link("B-PUMP")
    # Pump rules with priorities, so the power cut wins over "tank low -> pump on"
    wn.remove_control("B-ON"); wn.remove_control("B-OFF")
    wn.add_control("B-ON", Rule(ValueCondition(tank, "level", "<", S.PUMP_ON_BELOW_M),
                                [ControlAction(pump, "status", 1)], priority=1, name="B-ON"))
    wn.add_control("B-OFF", Rule(ValueCondition(tank, "level", ">", S.PUMP_OFF_ABOVE_M),
                                 [ControlAction(pump, "status", 0)], priority=1, name="B-OFF"))
    return wn


def build_network():
    wn = base_network()
    pump = wn.get_link("B-PUMP")
    _, _, cut_start, cut_end, _, _ = EVENTS[1]
    wn.add_control("POWER-CUT", Rule(AndCondition(SimTimeCondition(wn, ">=", cut_start),
                                                  SimTimeCondition(wn, "<", cut_end)),
                                     [ControlAction(pump, "status", 0)], priority=5, name="POWER-CUT"))
    # Timed leaks: a closed pipe to a leaking node, opened at the event start
    for name, _, start, _, at, _ in (EVENTS[0], EVENTS[2]):
        lps = S.LEAK_LPS if name == "E1" else S.BURST_LPS
        node = wn.get_node(at)
        x, y = node.coordinates
        wn.add_junction(f"{at}-LEAK", elevation=node.elevation, coordinates=(x + 3, y - 3))
        wn.add_pipe(f"{at}-LEAKPIPE", at, f"{at}-LEAK", length=1, diameter=0.05, roughness=130,
                    initial_status="CLOSED")
        wn.get_node(f"{at}-LEAK").emitter_coefficient = lps / 1000 / np.sqrt(S.ROOF + 2.2 - node.elevation)
        wn.add_control(f"{name}-START", Control._time_control(
            wn, start, "SIM_TIME", False, ControlAction(wn.get_link(f"{at}-LEAKPIPE"), "status", 1)))
    return wn


# Made-up water quality: (mean, noise std) per value; an event replaces the normal values it lists
QUALITY = {
    "normal": {"ph": (7.2, 0.05), "tds": (250, 5), "turb": (95, 1)},       # turbidity = clarity %, as in Sharma et al. [1]
    "runoff": {"ph": (6.4, 0.05), "tds": (60, 5), "turb": (88, 0)},        # rainwater-like: acidic, few dissolved solids
    "chemical": {"ph": (8.8, 0.05), "tds": (900, 20)},                     # bleach-like: alkaline, many dissolved solids
}


def quality(t, rng):
    """Made-up tank water quality with planted events E4 (runoff) and E5 (chemical)."""
    n = len(t)
    tod = (t % DAY) / H
    ph, tds, turb = (QUALITY["normal"][k][0] + rng.normal(0, QUALITY["normal"][k][1], n) for k in ("ph", "tds", "turb"))
    temp = 26 + 2 * np.sin((tod - 9) / 24 * 2 * np.pi) + rng.normal(0, 0.1, n)
    for _, kind, start, end, _, _ in EVENTS:
        m = (t >= start) & (t < end)
        if kind == "quality_runoff":
            q = QUALITY["runoff"]
            ph[m], tds[m], turb[m] = q["ph"][0] + rng.normal(0, q["ph"][1], m.sum()), q["tds"][0] + rng.normal(0, q["tds"][1], m.sum()), q["turb"][0]
        if kind == "quality_chemical":
            q = QUALITY["chemical"]
            ph[m], tds[m] = q["ph"][0] + rng.normal(0, q["ph"][1], m.sum()), q["tds"][0] + rng.normal(0, q["tds"][1], m.sum())
    return ph, tds, turb, temp


if __name__ == "__main__":
    rng = np.random.default_rng(SEED)
    wn = build_network()
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    wntr.network.write_inpfile(wn, os.path.join(HERE, "data", "building_B_scenario.inp"), units="LPS")
    r = wntr.sim.EpanetSimulator(wn).run_sim()
    t = r.node["pressure"].index.values
    level = r.node["pressure"]["B-TANK"].values
    assert level.min() > 0.25, "tank ran dry: EPANET results unreliable (outlet flip-flops)"

    flats = [n for n in wn.junction_name_list if n[-1].isdigit() and n[-2] == "0"]
    cols = {"t_s": t}

    def meter(lps):
        gain = 1 + rng.uniform(-FLOW_GAIN_ERR, FLOW_GAIN_ERR)
        return np.clip(lps * gain + rng.normal(0, FLOW_NOISE_LPS, len(lps)), 0, None)

    for f in flats:
        cols[f"{f}:flow_lps"] = meter(1000 * r.link["flowrate"][f"{f}-P"].values)
        cols[f"{f}:pressure_m"] = r.node["pressure"][f].values + rng.normal(0, PRESSURE_NOISE_M, len(t))
    cols["B-TANK:outflow_lps"] = meter(1000 * r.link["flowrate"]["B-DT4"].values)
    cols["B-TANK:inflow_lps"] = meter(1000 * r.link["flowrate"]["B-RISE"].values)
    cols["B-TANK:level_m"] = level + rng.normal(0, LEVEL_NOISE_M, len(t))
    ph, tds, turb, temp = quality(t, rng)
    cols.update({"B-TANK:ph": ph, "B-TANK:tds_ppm": tds, "B-TANK:turbidity_pct": turb, "B-TANK:temp_c": temp})

    pd.DataFrame(cols).round(5).to_csv(os.path.join(HERE, "data", "sensors.csv"), index=False)
    pd.DataFrame(EVENTS, columns=["event", "kind", "start_s", "end_s", "where", "expected_detector"]).to_csv(
        os.path.join(HERE, "data", "events.csv"), index=False)
    pd.DataFrame(OUTAGES, columns=["outage", "nodes", "start_s", "end_s"]).to_csv(
        os.path.join(HERE, "data", "outages.csv"), index=False)

    true_leak = 1000 * r.node["demand"]
    print(f"{len(t)} minutes, {len(flats)} flat nodes + 1 tank node")
    print(f"tank level {level.min():.2f}-{level.max():.2f} m; during power cut lowest {level[(t >= EVENTS[1][2]) & (t < EVENTS[1][3])].min():.2f} m")
    print(f"E1 leak flow {true_leak['B-203-LEAK'].iloc[-1]:.3f} L/s, E3 burst flow {true_leak['B-D1-LEAK'].iloc[-1]:.2f} L/s")
    print("pump on-minutes per day:", [int(r.link['status']['B-PUMP'].values[d * 1440:(d + 1) * 1440].sum()) for d in (0, 1)])
