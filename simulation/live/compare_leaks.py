"""Which leak signal works better: flow or pressure? Runs the real firmware on the live water
model (perfect radio, no valves) for many leaks, and scores each signal on its own.

    uv run python live/compare_leaks.py            # ~35 runs of 54 h, in parallel
    uv run python live/compare_leaks.py --check    # self-check of the scoring only

Each run: day 1 the nodes learn normal, a leak starts at a random time on day 2, the run goes
on to 06:00 on day 3 so every leak sees one night (needed by the night-flow rule).
Signals compared:
  flat leak      flow:     LEAK_NIGHT_FLOW / ABNORMAL_CONSUMPTION from that flat
                 pressure: PRESSURE_LOW flag from that flat (pressure alone, no flow condition)
  downtake leak  flow:     gateway mass balance PIPE_LEAK or tank ABNORMAL_SUPPLY
                 pressure: PRESSURE_LOW flag from any flat
A signal from the wrong flat, before the leak, or in a no-leak run is a false alarm.
"""
import csv
import os
import random
import sys
from multiprocessing import Pool

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "webots", "firmware")]
from water import Water, LEAK_SIZES_LPS  # noqa: E402
from aqua_edge import FlatNode, TankNode, DAY  # noqa: E402
from gateway import Gateway  # noqa: E402

END_S = 2 * DAY + 6 * 3600
FLATS = ["B-401", "B-302", "B-203", "B-104", "B-G01"]       # one per floor, different corners
DOWNTAKES = ["B-D4", "B-D3", "B-D2", "B-D1", "B-D0"]
NO_LEAK_RUNS = 5
FLOW_ALERTS = {"LEAK_NIGHT_FLOW", "ABNORMAL_CONSUMPTION"}


class HAL:
    """Perfect radio: every message reaches everyone else straight away. Valves do nothing."""
    storage_path = None

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
                box.append(dict(msg))

    def receive(self):
        msgs, self.inboxes[self.name] = self.inboxes[self.name], []
        return msgs

    def set_valve(self, closed, reason):
        pass

    def led(self, colour):
        pass


def run(job):
    """job: (site or None, size or None, start_s, seed). Returns the signals seen: (t, node, signal)."""
    site, size, start, seed = job
    w, clock, readings, inboxes = Water(seed=seed), [0.0], [None], {}
    hal = lambda n: HAL(n, clock, readings, inboxes)
    nodes = [FlatNode(f, hal(f)) for f in w.flats] + [TankNode("B-TANK", hal("B-TANK"))]
    gw = Gateway(hal("GATEWAY"), w.flats, "B-TANK")
    while w.t < END_S:
        clock[0] = w.t
        if site and w.t == start:
            w.start_leak(site, size)
        readings[0] = w.readings()
        for n in nodes:
            n.step()
        gw.step()
        w.step()
    sig = [(m["t"], m["n"], m.get("a") or m.get("f")) for m in gw.log if m["k"] in ("ALERT", "FLAG")]
    sig += [(e["t"], "GATEWAY", e["event"]) for e in gw.events if e["event"] in ("PIPE_LEAK", "SUPPLY_EVENT")]
    return job, sig


def method_of(signal):
    if signal in FLOW_ALERTS or signal in ("PIPE_LEAK", "ABNORMAL_SUPPLY"):
        return "flow"
    if signal == "PRESSURE_LOW":
        return "pressure"
    return None                       # quality, supply events, the mixed pressure+flow alert


def score(job, sig):
    """-> {method: (detected_after_s or None, false_alarms)} for one run."""
    site, _, start, _ = job
    out = {}
    for method in ("flow", "pressure"):
        hit, false = None, 0
        for t, node, s in sorted(sig):
            if method_of(s) != method:
                continue
            if site and not site.startswith("B-D") and s == "ABNORMAL_SUPPLY" and t >= start:
                continue              # flat leak: the tank sees the extra outflow; true, just not located
            downtake_hit = site and site.startswith("B-D") and (node in ("GATEWAY", "B-TANK") or method == "pressure")
            right_place = site and (node == site or downtake_hit)
            if right_place and t >= start:
                hit = t - start if hit is None else hit
            else:
                false += 1
        out[method] = (hit, false)
    return out


def jobs():
    rng = random.Random(1)
    leaks = [(s, z) for s in FLATS + DOWNTAKES for z in LEAK_SIZES_LPS] + [(None, None)] * NO_LEAK_RUNS
    return [(s, z, DAY + 60 * rng.randrange(24 * 60), 100 + i) for i, (s, z) in enumerate(leaks)]


def main():
    with Pool(max(os.cpu_count() - 2, 1)) as pool:
        results = [r for r in pool.imap_unordered(run, jobs()) if print(".", end="", flush=True) or True]
    print()
    rows = []
    for job, sig in sorted(results, key=lambda r: (r[0][0] or "~", list(LEAK_SIZES_LPS).index(r[0][1]) if r[0][1] else 0)):
        sc = score(job, sig)
        rows.append({"site": job[0] or "none", "size": job[1] or "-", "start_h": round(job[2] / 3600, 2),
                     **{f"{m}_min": "" if sc[m][0] is None else round(sc[m][0] / 60) for m in sc},
                     **{f"{m}_false": sc[m][1] for m in sc}})
    os.makedirs(os.path.join(HERE, "run"), exist_ok=True)
    path = os.path.join(HERE, "run", "compare_leaks.csv")
    with open(path, "w", newline="") as f:
        wr = csv.DictWriter(f, rows[0])
        wr.writeheader()
        wr.writerows(rows)

    print(f"{'leak':10} {'size':7} {'flow':>12} {'pressure':>12}")
    fmt = lambda r, m: (f"{r[m + '_min']} min" if r[m + "_min"] != "" else "missed") + (f" +{r[m + '_false']}F" if r[m + "_false"] else "")
    for r in rows:
        print(f"{r['site']:10} {r['size']:7} {fmt(r, 'flow'):>12} {fmt(r, 'pressure'):>12}")
    print("\n(+nF = n false alarms in that run)")
    for kind, test in (("flat", lambda r: r["site"] in FLATS), ("downtake", lambda r: r["site"] in DOWNTAKES)):
        for size in LEAK_SIZES_LPS:
            rs = [r for r in rows if test(r) and r["size"] == size]
            line = []
            for m in ("flow", "pressure"):
                hits = [r[m + "_min"] for r in rs if r[m + "_min"] != ""]
                med = sorted(hits)[len(hits) // 2] if hits else "-"
                line.append(f"{m} {len(hits)}/{len(rs)} caught, median {med} min")
            print(f"{kind:8} {size:6}: " + " | ".join(line))
    for m in ("flow", "pressure"):
        print(f"false alarms, {m}: {sum(r[m + '_false'] for r in rows)} in total, "
              f"{sum(r[m + '_false'] for r in rows if r['site'] == 'none')} in the {NO_LEAK_RUNS} no-leak runs")
    print(f"details: {path}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        # flat leak at B-203 from t=100: flow hit after 50 s, pressure from another flat = false alarm
        sc = score(("B-203", "small", 100, 0), [(150, "B-203", "LEAK_NIGHT_FLOW"), (120, "B-104", "PRESSURE_LOW")])
        assert sc == {"flow": (50, 0), "pressure": (None, 1)}, sc
        # downtake leak: any flat's low pressure counts; an alert before the leak is false
        sc = score(("B-D2", "large", 100, 0), [(160, "B-G01", "PRESSURE_LOW"), (50, "GATEWAY", "PIPE_LEAK")])
        assert sc == {"flow": (None, 1), "pressure": (60, 0)}, sc
        # no-leak run: everything is false
        assert score((None, None, 100, 0), [(500, "B-203", "ABNORMAL_CONSUMPTION")])["flow"] == (None, 1)
        print("scoring self-check passed")
    else:
        main()
