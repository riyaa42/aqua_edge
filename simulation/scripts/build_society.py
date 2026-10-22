"""Build a small Indian housing-society water network for EPANET.

Layout (like the housing complex in Alghamdi et al. [2], pressure-drop leak rule and
smart valve closure over LoRaWAN, but with rooftop tanks):
    underground sump -> one pump per building -> rooftop tank -> gravity downtake
    -> each floor -> 4 flats per floor.
4 buildings (A-D), ground + 4 floors, 4 flats per floor = 80 flats.

Height is handled the way EPANET always does it: every junction has an elevation,
so upper floors automatically get less pressure. The EPANET map is a 2D front view
(floors drawn stacked); results/society_3d.png shows the real 3D layout.

Writes epanet_gui/SOCIETY_normal.inp and epanet_gui/SOCIETY_leak.inp (leak in flat B-203).
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import wntr
from wntr.network.controls import ValueCondition, ControlAction, Control

# Assumptions (calibration knobs: replace with real numbers from your site)
BUILDINGS = ["A", "B", "C", "D"]
FLOORS = 5                     # ground + 4
FLATS_PER_FLOOR = 4
FLOOR_HEIGHT_M = 3.0
TAP_HEIGHT_M = 1.0             # taps sit ~1 m above floor level
LITRES_PER_FLAT_DAY = 540      # 4 people x 135 L/person/day (Indian CPHEEO guideline)
TANK_DIAMETER_M, TANK_MAX_M = 3.0, 2.0       # ~14 m3 rooftop tank
PUMP_ON_BELOW_M, PUMP_OFF_ABOVE_M = 0.5, 1.9
PUMP_FLOW_LPS, PUMP_HEAD_M = 2.0, 25.0
LEAK_FLAT, LEAK_LPS = "B-203", 0.05          # 0.05 L/s = 180 L/hour, e.g. a running toilet
BURST_AT, BURST_LPS, BURST_START_H = "C-D1", 1.5, 6   # pipe burst on building C's 1st-floor downtake at 6 am
# Hourly use pattern: morning and evening peaks, quiet at night (normalised to average 1)
PATTERN = np.array([0.2, 0.15, 0.1, 0.1, 0.3, 1.0, 2.4, 2.8, 2.5, 1.6, 1.0, 0.8,
                    0.9, 0.8, 0.6, 0.6, 0.8, 1.3, 1.9, 2.1, 1.6, 1.0, 0.6, 0.3])
PATTERN = PATTERN / PATTERN.mean()

SITE = {"A": (0, 0), "B": (60, 0), "C": (0, 60), "D": (60, 60)}   # real positions (m), for 3D plot
SUMP_SITE = (30, 30)
ROOF = FLOORS * FLOOR_HEIGHT_M


def flat_name(b, f, k):
    return f"{b}-{'G' if f == 0 else f}0{k}"


def build(leak=False):
    wn = wntr.network.WaterNetworkModel()
    wn.options.hydraulic.inpfile_units = "LPS"      # litres/second, pressure in metres
    wn.options.time.duration = 24 * 3600
    wn.options.time.hydraulic_timestep = 300
    wn.options.time.report_timestep = 900
    wn.options.time.pattern_timestep = 3600
    wn.add_pattern("household", PATTERN.tolist())
    wn.add_curve("pump", "HEAD", [(PUMP_FLOW_LPS / 1000, PUMP_HEAD_M)])
    base_demand = LITRES_PER_FLAT_DAY / 1000 / 86400          # m3/s

    # 2D map coordinates: front view, buildings side by side, floors stacked upward
    wn.add_reservoir("SUMP", base_head=-1.0, coordinates=(75, -25))      # underground sump
    for i, b in enumerate(BUILDINGS):
        x0 = i * 50
        wn.add_junction(f"{b}-PUMPOUT", elevation=0, coordinates=(75 + (i - 1.5) * 6, -18))
        wn.add_pump(f"{b}-PUMP", "SUMP", f"{b}-PUMPOUT", "HEAD", "pump")
        wn.add_junction(f"{b}-BASE", elevation=0, coordinates=(x0 - 12, 0))
        wn.add_pipe(f"{b}-MAIN", f"{b}-PUMPOUT", f"{b}-BASE", length=45, diameter=0.05, roughness=130)
        wn.add_tank(f"{b}-TANK", elevation=ROOF + 1, init_level=1.5, min_level=0.2, max_level=TANK_MAX_M,
                    diameter=TANK_DIAMETER_M, coordinates=(x0, (FLOORS + 1) * 10))
        wn.add_pipe(f"{b}-RISE", f"{b}-BASE", f"{b}-TANK", length=ROOF + 2, diameter=0.05, roughness=130)
        above = f"{b}-TANK"
        for f in reversed(range(FLOORS)):
            d = f"{b}-D{f}"
            wn.add_junction(d, elevation=f * FLOOR_HEIGHT_M + TAP_HEIGHT_M, coordinates=(x0, f * 10 + 5))
            wn.add_pipe(f"{b}-DT{f}", above, d, length=FLOOR_HEIGHT_M if f < FLOORS - 1 else 3,
                        diameter=0.05, roughness=130)
            for k in range(1, FLATS_PER_FLOOR + 1):
                n = flat_name(b, f, k)
                wn.add_junction(n, base_demand=base_demand, demand_pattern="household",
                                elevation=f * FLOOR_HEIGHT_M + TAP_HEIGHT_M,
                                coordinates=(x0 + (k - 2.5) * 8, f * 10 + 5 + (2 if k % 2 else -2)))
                wn.add_pipe(f"{n}-P", d, n, length=8, diameter=0.025, roughness=130)
            above = d
        tank, pump = wn.get_node(f"{b}-TANK"), wn.get_link(f"{b}-PUMP")
        wn.add_control(f"{b}-ON", Control(ValueCondition(tank, "level", "<", PUMP_ON_BELOW_M),
                                          ControlAction(pump, "status", 1)))
        wn.add_control(f"{b}-OFF", Control(ValueCondition(tank, "level", ">", PUMP_OFF_ABOVE_M),
                                           ControlAction(pump, "status", 0)))
    if leak:
        # EPANET leak = emitter: Q = C * sqrt(pressure); sized for ~LEAK_LPS at this flat's pressure
        f = int(LEAK_FLAT.split("-")[1][0]) if LEAK_FLAT.split("-")[1][0] != "G" else 0
        p_est = ROOF + 1 + 1.2 - (f * FLOOR_HEIGHT_M + TAP_HEIGHT_M)
        wn.get_node(LEAK_FLAT).emitter_coefficient = LEAK_LPS / 1000 / np.sqrt(p_est)
        # Timed burst: a closed pipe to a leaking node, opened by a control at BURST_START_H
        d = wn.get_node(BURST_AT)
        x, y = d.coordinates
        wn.add_junction(f"{BURST_AT}-BURST", elevation=d.elevation, coordinates=(x + 5, y - 4))
        wn.add_pipe(f"{BURST_AT}-BURSTPIPE", BURST_AT, f"{BURST_AT}-BURST", length=1, diameter=0.05,
                    roughness=130, initial_status="CLOSED")
        wn.get_node(f"{BURST_AT}-BURST").emitter_coefficient = BURST_LPS / 1000 / np.sqrt(ROOF + 2.2 - d.elevation)
        wn.add_control("BURST-START", Control._time_control(
            wn, BURST_START_H * 3600, "SIM_TIME", False, ControlAction(wn.get_link(f"{BURST_AT}-BURSTPIPE"), "status", 1)))
    return wn


def site_xyz(wn, name):
    """Real 3D position of a node (site x, y in metres, elevation z)."""
    b = name.split("-")[0]
    if name == "SUMP":
        return (*SUMP_SITE, -1)
    bx, by = SITE[b]
    if name.endswith("PUMPOUT"):
        return (*SUMP_SITE, 0)
    if name.endswith("TANK"):
        return (bx, by, ROOF + 1)
    if name.endswith("BASE"):
        return (bx, by, 0)
    z = wn.get_node(name).elevation
    if "-D" in name:
        return (bx, by, z)
    k = int(name[-1])
    return (bx + [-6, 6, 6, -6][k - 1], by + [-6, -6, 6, 6][k - 1], z)


if __name__ == "__main__":
    out = {}
    for tag, leak in [("normal", False), ("leak", True)]:
        wn = build(leak)
        wntr.network.write_inpfile(wn, f"epanet_gui/SOCIETY_{tag}.inp", units="LPS")
        wn = wntr.network.WaterNetworkModel(f"epanet_gui/SOCIETY_{tag}.inp")   # check the written file itself
        out[tag] = (wn, wntr.sim.EpanetSimulator(wn).run_sim())

    wn, r = out["normal"]
    flats = [n for n in wn.junction_name_list if n[-1].isdigit() and n[-2] == "0"]
    p = r.node["pressure"][flats]
    lvl = r.node["pressure"][[f"{b}-TANK" for b in BUILDINGS]]

    # Self-checks: physics must make sense
    assert (p > 0).all().all(), "a flat lost all pressure"
    assert (lvl.values > 0.21).all(), "a tank ran dry"
    peak = 8 * 3600
    by_floor = {f: p.loc[peak, [n for n in flats if n.split("-")[1][0] == ("G" if f == 0 else str(f))]].mean()
                for f in range(FLOORS)}
    assert all(by_floor[f] > by_floor[f + 1] for f in range(FLOORS - 1)), "upper floors should get less pressure"

    wl, rl = out["leak"]
    # Tank must never hit its minimum in the leak run either: EPANET flip-flops the outlet when a tank is empty
    assert (rl.node["pressure"][[f"{b}-TANK" for b in BUILDINGS]].values > 0.25).all(), "leak run emptied a tank"
    night = slice(1 * 3600, 4 * 3600)
    use = lambda res, b: 1000 * res.node["demand"].loc[night, [n for n in flats if n.startswith(b)]].sum(1).mean()
    lines = ["Pressure at 8 am (morning peak), average per floor (m):"]
    lines += [f"  {'Ground' if f == 0 else f'Floor {f}'}: {by_floor[f]:.1f} m ({by_floor[f] * 1.42:.1f} psi)" for f in range(FLOORS)]
    lines += [f"Tank levels stayed between {lvl.values.min():.2f} m and {lvl.values.max():.2f} m",
              f"Pump starts per building per day: "
              + ", ".join(f"{b} {int((r.link['status'][f'{b}-PUMP'].diff() > 0).sum())}" for b in BUILDINGS),
              f"Leak in {LEAK_FLAT}: night-time (1-4 am) water use in building B "
              f"{use(r, 'B'):.3f} L/s normal vs {use(rl, 'B'):.3f} L/s with leak",
              f"Pressure at {LEAK_FLAT} at 8 am: {r.node['pressure'].loc[peak, LEAK_FLAT]:.2f} m normal vs "
              f"{rl.node['pressure'].loc[peak, LEAK_FLAT]:.2f} m with leak",
              f"Pump starts for building B: normal {int((r.link['status']['B-PUMP'].diff() > 0).sum())}, "
              f"with leak {int((rl.link['status']['B-PUMP'].diff() > 0).sum())}",
              f"Burst at {BURST_AT} from {BURST_START_H}:00: flow {1000 * rl.node['demand'].loc[12 * 3600, BURST_AT + '-BURST']:.2f} L/s at noon; "
              f"building C pump starts {int((rl.link['status']['C-PUMP'].diff() > 0).sum())} vs {int((r.link['status']['C-PUMP'].diff() > 0).sum())} normal; "
              f"lowest tank C level {rl.node['pressure']['C-TANK'].min():.2f} m vs {r.node['pressure']['C-TANK'].min():.2f} m normal",
              f"Building C top-floor pressure, lowest of the day: {rl.node['pressure'][[n for n in flats if n.startswith('C-4')]].min().min():.2f} m "
              f"with burst vs {r.node['pressure'][[n for n in flats if n.startswith('C-4')]].min().min():.2f} m normal"]
    print("\n".join(lines))
    open("results/society_summary.txt", "w").write("\n".join(lines) + "\n")

    # 3D picture, nodes coloured by pressure at the morning peak
    fig = plt.figure(figsize=(10, 8))
    ax = fig.add_subplot(projection="3d")
    for l in wn.link_name_list:
        link = wn.get_link(l)
        a, c = site_xyz(wn, link.start_node_name), site_xyz(wn, link.end_node_name)
        ax.plot(*zip(a, c), color="0.6", lw=1.5 if "-P" not in l else 0.8)
    xyz = np.array([site_xyz(wn, n) for n in flats])
    sc = ax.scatter(*xyz.T, c=p.loc[peak].values, cmap="viridis", s=45, depthshade=False)
    for b in BUILDINGS:
        ax.scatter(*site_xyz(wn, f"{b}-TANK"), marker="s", s=150, color="tab:blue")
        ax.text(*np.add(site_xyz(wn, f"{b}-TANK"), (0, 0, 2)), f"Building {b}", ha="center", weight="bold")
    ax.scatter(*site_xyz(wn, "SUMP"), marker="s", s=150, color="tab:cyan")
    ax.text(*np.add(site_xyz(wn, "SUMP"), (0, 0, -4)), "Sump + pumps", ha="center")
    fig.colorbar(sc, ax=ax, shrink=0.6, label="Pressure at 8 am (m)")
    ax.set_xlabel("x (m)"); ax.set_ylabel("y (m)"); ax.set_zlabel("Height (m)")
    ax.set_title("Housing society: 4 buildings, rooftop tanks, 80 flats")
    ax.view_init(elev=22, azim=-60)
    fig.tight_layout(); fig.savefig("results/society_3d.png", dpi=140)
