"""Step 1: reproduce the SWI-FEED NET_6 hydraulic dataset (Pagano et al. [5]).

Runs NET_6.inp for 24 h in WNTR and compares pressure (every junction, every hour)
and flow (every pipe, every hour) against the CSVs the authors published.
"""
import sys
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import wntr

DATA = "data/swifeed/"
OUT = "results/"
SIM = sys.argv[1] if len(sys.argv) > 1 else "epanet"  # "epanet" or "wntr"


def run(wn):
    wn.options.time.duration = 23 * 3600  # hours 0..23, like the published CSVs
    sim = wntr.sim.EpanetSimulator(wn) if SIM == "epanet" else wntr.sim.WNTRSimulator(wn)
    return sim.run_sim()


def to_long(df, id_col):
    df = df.copy()
    df.index = (df.index // 3600).astype(int)
    return df.stack().rename("ours").rename_axis(["h", id_col]).reset_index()


def theirs(path, id_col, col):
    d = pd.read_csv(path, sep=";", dtype={id_col: str})
    d["h"] = d.hour.str.split(":").str[0].astype(int)
    return d[["h", id_col, col]].rename(columns={col: "theirs"})


if __name__ == "__main__":
    wn = wntr.network.WaterNetworkModel(DATA + "NET_6.inp")
    res = run(wn)

    p = to_long(res.node["pressure"][wn.junction_name_list], "nodeID").merge(
        theirs(DATA + "NET_6_bd_pattern_h_24_leak_area_0_nodes.csv", "nodeID", "pressure_value"))
    q = to_long(res.link["flowrate"][wn.pipe_name_list], "linkID").merge(
        theirs(DATA + "NET_6_bd_pattern_h_24_leak_area_0_links.csv", "linkID", "flowrate"))

    lines = [f"Simulator: {SIM}"]
    for name, d, unit in [("Pressure", p, "m"), ("Flow", q, "m3/s")]:
        err = (d.ours - d.theirs).abs()
        lines.append(f"{name}: {len(d)} values compared, mean |diff| = {err.mean():.6f} {unit}, "
                     f"max |diff| = {err.max():.6f} {unit}, correlation = {np.corrcoef(d.ours, d.theirs)[0, 1]:.6f}")
    print("\n".join(lines))
    with open(OUT + f"reproduction_{SIM}.txt", "w") as f:
        f.write("\n".join(lines) + "\n")

    fig, ax = plt.subplots(1, 2, figsize=(10, 4.5))
    for a, d, t in [(ax[0], p, "Junction pressure (m)"), (ax[1], q, "Pipe flow (m³/s)")]:
        a.scatter(d.theirs, d.ours, s=1, alpha=0.3)
        lo, hi = d.theirs.min(), d.theirs.max()
        a.plot([lo, hi], [lo, hi], "r--", lw=1, label="perfect match")
        a.set_xlabel("Published SWI-FEED value"); a.set_ylabel("Our WNTR value"); a.set_title(t); a.legend()
    fig.suptitle("Reproducing the SWI-FEED NET_6 dataset (24 h, no leak)")
    fig.tight_layout(); fig.savefig(OUT + f"reproduction_{SIM}.png", dpi=150)
