"""Step 2: add leaks to NET_6 and test two leak rules from the literature.

Rule A, pressure drop (Alghamdi et al. [2], pressure-drop leak rule):
    a pressure sensor at every junction (one per house, as in [2]); a zone raises an
    alarm when any sensor reads more than PRESSURE_DROP_M below its normal value
    for that hour of the day.
Rule B, mass balance (Ali et al. [6], supply vs branch flow balance):
    flow meters on every pipe entering/leaving a zone plus a meter at every house;
    alarm when (water in - water out) / water in > MASS_BALANCE_PCT.

Network and hydraulics: SWI-FEED NET_6 (Pagano et al. [5], EPANET co-simulation),
same WNTR solver and settings that reproduced their dataset exactly.
"""
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import wntr

DATA, OUT = "data/swifeed/", "results/"

# Calibration knobs (real sensors are never ideal; tune these against your own hardware)
PRESSURE_NOISE_SD_M = 0.5    # random error per pressure reading (~5 kPa)
PRESSURE_DROP_M = 2.0        # Rule A threshold (~20 kPa below normal)
FLOW_METER_BIAS = 0.05       # each flow meter is off by a fixed amount within +-5% (Ali [6] measured < 5%)
MASS_BALANCE_PCT = 10.0      # Rule B threshold, Ali [6]'s value (+5% supply and -5% consumption)
LEAK_AREAS_M2 = [1e-5, 5e-5, 2e-4, 5e-4]   # hole size, small to large
N_LOCATIONS = 8
LEAK_START_H = 6
ZONE_GRID = 4                # 4 x 4 = 16 zones
SEED = 1


def simulate(leak_node=None, area=0.0):
    wn = wntr.network.WaterNetworkModel(DATA + "NET_6.inp")
    wn.options.time.duration = 23 * 3600
    if leak_node:
        wn.get_node(leak_node).add_leak(wn, area=area, discharge_coeff=0.75,
                                        start_time=LEAK_START_H * 3600, end_time=24 * 3600)
    r = wntr.sim.WNTRSimulator(wn).run_sim()
    return wn, r


def build_zones(wn):
    """Split all nodes into a 4x4 grid by coordinates (roughly equal junctions per zone)."""
    names = wn.node_name_list
    xy = np.array([wn.get_node(n).coordinates for n in names])
    qx = np.quantile(xy[:, 0], np.linspace(0, 1, ZONE_GRID + 1)[1:-1])
    qy = np.quantile(xy[:, 1], np.linspace(0, 1, ZONE_GRID + 1)[1:-1])
    zone = np.searchsorted(qx, xy[:, 0]) * ZONE_GRID + np.searchsorted(qy, xy[:, 1])
    return pd.Series(zone, index=names)


def zone_mass_balance(wn, r, zone, link_bias, node_bias):
    """Hourly (water in, water out) per zone, as the meters would measure them."""
    Z = ZONE_GRID ** 2
    links = wn.link_name_list
    zs = zone[[wn.get_link(l).start_node_name for l in links]].values
    ze = zone[[wn.get_link(l).end_node_name for l in links]].values
    S = np.zeros((len(links), Z))                      # +1 flow enters zone, -1 flow leaves
    b = zs != ze
    S[np.where(b)[0], ze[b]] = 1
    S[np.where(b)[0], zs[b]] = -1
    Q = r.link["flowrate"][links].values * (1 + link_bias)        # hours x links
    C = Q[:, :, None] * S[None, :, :]                              # hours x links x zones
    water_in = np.clip(C, 0, None).sum(1)
    water_out = np.clip(-C, 0, None).sum(1)

    nodes = wn.node_name_list
    D = r.node["demand"][nodes].values * (1 + node_bias)          # house use (+), reservoir supply (-)
    M = np.eye(Z)[zone[nodes].values]                              # nodes x zones
    water_in += np.clip(-D, 0, None) @ M
    water_out += np.clip(D, 0, None) @ M
    return water_in, water_out


def pressure_alarms(wn, r, base, zone, rng):
    j = wn.junction_name_list
    measured = r.node["pressure"][j].values + rng.normal(0, PRESSURE_NOISE_SD_M, (24, len(j)))
    drop = base.node["pressure"][j].values - measured          # vs normal pattern for that hour
    M = np.eye(ZONE_GRID ** 2)[zone[j].values]
    return ((drop > PRESSURE_DROP_M).astype(int) @ M) > 0      # hours x zones


def mass_alarms(wn, r, zone, link_bias, node_bias):
    w_in, w_out = zone_mass_balance(wn, r, zone, link_bias, node_bias)
    err = 100 * (w_in - w_out) / w_in
    return err > MASS_BALANCE_PCT, err


if __name__ == "__main__":
    rng = np.random.default_rng(SEED)
    wn, base = simulate()
    zone = build_zones(wn)
    link_bias = rng.uniform(-FLOW_METER_BIAS, FLOW_METER_BIAS, wn.num_links)
    node_bias = rng.uniform(-FLOW_METER_BIAS, FLOW_METER_BIAS, wn.num_nodes)

    # Self-check: with perfect meters, zone (in - out) must equal the leak flow. Proves the zone accounting.
    test_node = wn.junction_name_list[100]
    wn_t, r_t = simulate(test_node, 2e-4)
    w_in, w_out = zone_mass_balance(wn_t, r_t, zone, 0, 0)
    leak = r_t.node["leak_demand"][test_node].values
    assert np.allclose((w_in - w_out)[:, zone[test_node]], leak, atol=1e-6), "zone mass balance broken"
    others = [z for z in range(ZONE_GRID ** 2) if z != zone[test_node]]
    assert np.allclose((w_in - w_out)[:, others], 0, atol=1e-6), "water appearing in leak-free zones"

    # False alarms with no leak at all
    fa_p = pressure_alarms(wn, base, base, zone, rng).mean()
    ma, base_err = mass_alarms(wn, base, zone, link_bias, node_bias)
    fa_m = ma.mean()

    locations = rng.choice(wn.junction_name_list, N_LOCATIONS, replace=False)
    rows, kept = [], {}  # 2e-4 m2 runs, kept for the example plots
    for area in LEAK_AREAS_M2:
        for node in locations:
            wn_l, r = simulate(node, area)
            z = zone[node]
            after = slice(LEAK_START_H, 24)
            pa = pressure_alarms(wn_l, r, base, zone, rng)
            mb, err = mass_alarms(wn_l, r, zone, link_bias, node_bias)
            w_in, _ = zone_mass_balance(wn_l, r, zone, 0, 0)
            leak_ls = 1000 * r.node["leak_demand"][node].values[after].mean()
            other = [k for k in range(ZONE_GRID ** 2) if k != z]
            zj = [n for n in wn_l.junction_name_list if zone[n] == z]
            true_drop = (base.node["pressure"][zj] - r.node["pressure"][zj]).values[after]
            rows.append(dict(
                area_m2=area, node=node, zone=z, leak_L_per_s=leak_ls,
                true_drop_at_leak_m=true_drop[:, zj.index(node)].max(),
                true_max_drop_in_zone_m=true_drop.max(),
                leak_pct_of_zone_inflow=100 * leak_ls / 1000 / w_in[after, z].mean(),
                pressure_detected=pa[after, z].any(), pressure_hours_hit=pa[after, z].mean(),
                pressure_false_alarm_rate=pa[:, other].mean(),
                massbal_detected=mb[after, z].any(), massbal_hours_hit=mb[after, z].mean(),
                massbal_false_alarm_rate=mb[:, other].mean()))
            if area == 2e-4:
                kept[node] = (z, r, err)
            print(f"area {area:.0e} node {node:>5} zone {z:2d} leak {leak_ls:6.2f} L/s  "
                  f"pressure {'HIT ' if rows[-1]['pressure_detected'] else 'miss'}  "
                  f"massbal {'HIT ' if rows[-1]['massbal_detected'] else 'miss'}", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT + "leak_rules_runs.csv", index=False)
    summ = df.groupby("area_m2").agg(
        leak_L_per_s=("leak_L_per_s", "mean"),
        leak_pct_of_zone_inflow=("leak_pct_of_zone_inflow", "mean"),
        true_drop_at_leak_m=("true_drop_at_leak_m", "mean"),
        true_drop_over_threshold=("true_max_drop_in_zone_m", lambda s: (s > PRESSURE_DROP_M).mean()),
        pressure_detection_rate=("pressure_detected", "mean"),
        pressure_false_alarm_rate=("pressure_false_alarm_rate", "mean"),
        massbal_detection_rate=("massbal_detected", "mean"),
        massbal_false_alarm_rate=("massbal_false_alarm_rate", "mean")).round(3)
    summ.to_csv(OUT + "leak_rules_summary.csv")
    text = (summ.to_string() + f"\n\nNo-leak run: pressure-rule false alarm rate {fa_p:.3f}, "
            f"mass-balance false alarm rate {fa_m:.3f} (fraction of zone-hours with an alarm)\n"
            f"No-leak mass-balance error range across zones: {base_err.min():.1f}% to {base_err.max():.1f}%\n")
    print(text)
    open(OUT + "leak_rules_summary.txt", "w").write(text)

    # Plot 1: detection rate vs leak size
    fig, ax = plt.subplots(figsize=(6.5, 4.5))
    ax.plot(summ.leak_L_per_s, summ.pressure_detection_rate * 100, "o-", label="Pressure drop (Alghamdi [2])")
    ax.plot(summ.leak_L_per_s, summ.true_drop_over_threshold * 100, "o:", color="C0", alpha=0.6,
            label="Pressure drop really above threshold (no noise)")
    ax.plot(summ.leak_L_per_s, summ.massbal_detection_rate * 100, "s-", label="Mass balance (Ali [6])")
    ax.set_xscale("log"); ax.set_xlabel("Leak flow (L/s, log scale)"); ax.set_ylabel("Leaks detected (%)")
    ax.set_title(f"Leak detection on SWI-FEED NET_6 ({N_LOCATIONS} locations per size)")
    ax.set_ylim(-5, 105); ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(OUT + "detection_vs_leak_size.png", dpi=150)

    # Plot 2: Ali-style system error over time for one example leak
    ex = df[df.area_m2 == 2e-4].set_index("node")
    node = ex.leak_pct_of_zone_inflow.idxmax()
    z, r, err = kept[node]
    fig, ax = plt.subplots(figsize=(7, 4.5))
    ax.plot(range(24), base_err[:, z], "o-", label="No leak")
    ax.plot(range(24), err[:, z], "s-", label=f"Leak at junction {node} from hour {LEAK_START_H}")
    ax.axhline(MASS_BALANCE_PCT, color="r", ls="--", label=f"{MASS_BALANCE_PCT:.0f}% threshold (Ali [6])")
    ax.set_xlabel("Hour of day"); ax.set_ylabel("System error % = (in - out) / in")
    ax.set_title(f"Zone {z} mass balance, leak hole {2e-4:.0e} m²"); ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(OUT + "mass_balance_example.png", dpi=150)

    # Plot 3: pressure at the leaking junction, biggest vs smallest pressure response, same leak size
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for ax, node in zip(axes, [ex.true_drop_at_leak_m.idxmax(), ex.true_drop_at_leak_m.idxmin()]):
        r = kept[node][1]
        ax.plot(range(24), base.node["pressure"][node].values, "o-", label="No leak")
        ax.plot(range(24), r.node["pressure"][node].values, "s-", label="With leak")
        ax.axvline(LEAK_START_H, color="gray", ls=":", label="Leak starts")
        ax.set_xlabel("Hour of day"); ax.set_ylabel("Pressure (m)"); ax.grid(alpha=0.3); ax.legend()
        ax.set_title(f"Junction {node}: leak {ex.leak_L_per_s[node]:.1f} L/s, "
                     f"max drop {ex.true_drop_at_leak_m[node]:.2f} m")
    fig.suptitle(f"Same leak hole ({2e-4:.0e} m²), very different pressure response")
    fig.tight_layout(); fig.savefig(OUT + "pressure_example.png", dpi=150)
