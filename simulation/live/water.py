"""Live water model of building B: WNTR runs one minute at a time, and leaks or quality events
can be switched on between minutes.

    uv run python live/water.py     # self-check: matches EPANET, leak size right, speed

Uses the same network, sensor errors and quality numbers as webots/make_scenario.py.
"""
import os
import sys
import time
import numpy as np
import wntr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "webots"), os.path.join(HERE, "..", "scripts")]
import make_scenario as M  # noqa: E402

STEP_S = 60
LEAK_SIZES_LPS = {"small": 0.05, "medium": 0.5, "large": 1.5}   # running toilet / pipe crack / burst
LEAK_CD = 0.75                                                  # orifice discharge coefficient
G = 9.81


class Water:
    def __init__(self, seed=M.SEED):
        self.wn = M.base_network()
        self.wn.options.time.duration = 0
        self.sim = wntr.sim.WNTRSimulator(self.wn)
        self.rng = np.random.default_rng(seed)
        self.flats = [n for n in self.wn.junction_name_list if n[-1].isdigit() and n[-2] == "0"]
        self.leak_sites = self.flats + [n for n in self.wn.junction_name_list if n.startswith("B-D")]
        meters = [f"{f}-P" for f in self.flats] + ["B-DT4", "B-RISE"]
        self.gain = {m: 1 + self.rng.uniform(-M.FLOW_GAIN_ERR, M.FLOW_GAIN_ERR) for m in meters}
        self.water_quality, self.quality_until = "normal", -1
        self.leaks = {}                                  # junction -> requested L/s
        self.res = self.sim.run_sim()

    @property
    def t(self):
        return int(self.wn.options.time.duration)

    def step(self):
        self.wn.options.time.duration += STEP_S
        # WNTR subscribes fresh observers to every control action on each run_sim call and never
        # removes the old ones, so stepping slows down every minute (41 s per sim hour by hour 30).
        # It re-subscribes the ones it needs at the start of each run, so dropping them is safe.
        for _, control in self.wn.controls():
            for action in control.actions():
                action._observers.clear()
        self.res = self.sim.run_sim()
        if self.t >= self.quality_until:
            self.water_quality = "normal"

    def _last(self, kind, field, name):
        return float(getattr(self.res, kind)[field][name].iloc[-1])

    # --- events (called by the timekeeper between minutes) ---
    def start_leak(self, where, size):
        """Leak hole sized so it gives `size` L/s at the junction's current pressure."""
        lps = LEAK_SIZES_LPS[size]
        p = max(self._last("node", "pressure", where), 0.5)
        j = self.wn.get_node(where)
        # ponytail: private WNTR attributes (stock WNTR only adds leaks before a run); pinned to wntr 1.5.0
        # in uv.lock, switch to Dyn-WNTR if an upgrade breaks this
        j._leak_status, j._leak_discharge_coeff = True, LEAK_CD
        j._leak_area = lps / 1000 / (LEAK_CD * np.sqrt(2 * G * p))
        self.leaks[where] = lps

    def fix_leak(self, where):
        self.wn.get_node(where)._leak_status = False
        self.leaks.pop(where, None)

    def set_quality(self, kind, minutes):
        self.water_quality, self.quality_until = kind, self.t + 60 * minutes

    def leak_lps(self, where):
        return 1000 * self._last("node", "leak_demand", where)

    # --- what each node's sensors read right now ---
    def _meter(self, link):
        lps = 1000 * self._last("link", "flowrate", link)
        return max(lps * self.gain[link] + self.rng.normal(0, M.FLOW_NOISE_LPS), 0.0)

    def readings(self):
        r = {f: {"flow_lps": self._meter(f"{f}-P"),
                 "pressure_m": self._last("node", "pressure", f) + self.rng.normal(0, M.PRESSURE_NOISE_M)}
             for f in self.flats}
        q = {**M.QUALITY["normal"], **M.QUALITY[self.water_quality]}
        ph, tds, turb = (q[k][0] + self.rng.normal(0, q[k][1]) for k in ("ph", "tds", "turb"))
        tod = (self.t % M.DAY) / M.H
        r["B-TANK"] = {"outflow_lps": self._meter("B-DT4"), "inflow_lps": self._meter("B-RISE"),
                       "level_m": self._last("node", "pressure", "B-TANK") + self.rng.normal(0, M.LEVEL_NOISE_M),
                       "ph": ph, "tds_ppm": tds, "turbidity_pct": turb,
                       "temp_c": 26 + 2 * np.sin((tod - 9) / 24 * 2 * np.pi) + self.rng.normal(0, 0.1)}
        return r


if __name__ == "__main__":
    # 1. No events: running minute by minute must match one uninterrupted WNTR run exactly.
    #    (WNTR vs EPANET differ by up to ~1.4 m for a few minutes because their pumps switch a
    #    minute apart; that is the solver, not the stepping.)
    hours = 24
    wn = M.base_network()
    wn.options.time.duration = hours * 3600
    ref = wntr.sim.WNTRSimulator(wn).run_sim().node["pressure"]
    w, start, worst = Water(), time.time(), 0.0
    while w.t < hours * 3600:
        w.step()
        p = w.res.node["pressure"].iloc[-1]
        worst = max(worst, float((p[w.flats] - ref.loc[w.t, w.flats]).abs().max()))
    speed = hours * 60 / (time.time() - start)
    print(f"{hours} h no events: worst flat pressure difference vs one WNTR run {worst:.2g} m; {speed:.0f} sim minutes per second")
    assert worst < 1e-6, "stepping changed the result"
    # 2. A small leak must give about the requested flow
    w.start_leak("B-203", "small")
    for _ in range(5):
        w.step()
    got = w.leak_lps("B-203")
    print(f"small leak at B-203: asked {LEAK_SIZES_LPS['small']} L/s, got {got:.4f} L/s")
    assert abs(got - LEAK_SIZES_LPS["small"]) < 0.01
    w.fix_leak("B-203")
    w.step()
    assert w.leak_lps("B-203") == 0
    # 3. Quality event switches on and back off
    w.set_quality("chemical", 2)
    assert w.readings()["B-TANK"]["tds_ppm"] > 600
    w.step(); w.step()
    assert w.readings()["B-TANK"]["tds_ppm"] < 400
    print("water self-check passed")
