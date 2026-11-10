"""Build a small Indian housing-society water network for EPANET.

Layout (like the housing complex in Alghamdi et al. [2], pressure-drop leak rule and
smart valve closure over LoRaWAN, but with rooftop tanks):
    underground sump -> one pump per building -> rooftop tank -> gravity downtake
    -> each floor -> 4 flats per floor.
4 buildings (A-D), ground + 4 floors, 4 flats per floor = 80 flats.

Height is handled the way EPANET always does it: every junction has an elevation,
so upper floors automatically get less pressure.

Used as a module: webots/make_scenario.py builds building B from it (live/water.py uses that).
"""
import numpy as np
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
