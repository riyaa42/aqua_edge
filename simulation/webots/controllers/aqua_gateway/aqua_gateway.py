"""Webots controller for the gateway: runs the gateway firmware and shows a live status overlay.

It is a Supervisor only so it can draw text on screen and pause the simulation at the end.
When the 48 h scenario finishes it writes results/webots_run/ and scores the run.
"""
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path[:0] = [ROOT, os.path.join(ROOT, "firmware")]

from controller import Supervisor  # noqa: E402
from gateway import Gateway  # noqa: E402
from simlink import STEP_S  # noqa: E402
from webots_hal import WebotsHAL, TIME_STEP_MS, RUN_DIR  # noqa: E402
from run_offline import score, write_csv, hhmm  # noqa: E402
from aqua_edge import DAY, HOUR  # noqa: E402

# Screenshots for slides: set AQUA_SNAPSHOTS=1 (needs rendering on). Leak during the outage, then the burst.
SNAPSHOTS = {DAY + 3 * HOUR: "leak_during_outage", DAY + 18.5 * HOUR: "pipe_burst"} if os.environ.get("AQUA_SNAPSHOTS") else {}

sup = Supervisor()
hal = WebotsHAL(sup)
flats = [n for n in hal.feed.nodes() if n != "B-TANK"]
gw = Gateway(hal, flats, "B-TANK")


def overlay():
    sup.setLabel(0, f"Aqua Edge  |  building B  |  {hhmm(hal.t)}", 0.01, 0.01, 0.09, 0x102030, 0, "Arial")
    net = "network OK" if hal.link_ok() else "NETWORK OUTAGE: nodes store data on SD"
    sup.setLabel(1, net, 0.01, 0.06, 0.07, 0x207020 if hal.link_ok() else 0xC02020, 0, "Arial")
    recent = [f"{hhmm(m['t'])}  {m['n']}  {m['a']}" for m in gw.log if m["k"] == "ALERT"][-6:]
    recent += [f"{hhmm(e['t'])}  GATEWAY  {e['event']}" for e in gw.events][-3:]
    sup.setLabel(2, "Alerts received:\n" + "\n".join(recent or ["none yet"]), 0.01, 0.11, 0.06, 0x202020, 0, "Arial")
    sup.setLabel(3, "LEDs: green normal, yellow waiting to send, red alert", 0.01, 0.95, 0.06, 0x404040, 0, "Arial")


def finish():
    valve_log = []
    for f in sorted(os.listdir(RUN_DIR)):
        if f.endswith("_valve.jsonl"):
            with open(os.path.join(RUN_DIR, f)) as fh:
                valve_log += [json.loads(line) for line in fh]
    write_csv(os.path.join(RUN_DIR, "gateway_log.csv"), gw.log)
    write_csv(os.path.join(RUN_DIR, "gateway_events.csv"), gw.events or [{"t": "", "event": ""}])
    write_csv(os.path.join(RUN_DIR, "valve_log.csv"), valve_log or [{"t": "", "node": ""}])
    lines, _, _ = score(gw, valve_log, [])
    with open(os.path.join(RUN_DIR, "summary.txt"), "w") as f:
        f.write("Webots run\n" + "\n".join(lines) + "\n")
    print("\n".join(lines))


while sup.step(TIME_STEP_MS) != -1:
    if hal.t >= hal.feed.duration_s:
        finish()
        sup.simulationSetMode(Supervisor.SIMULATION_MODE_PAUSE)
        break
    gw.step()
    hal.led("green" if hal.link_ok() else "yellow")
    overlay()
    if hal.t in SNAPSHOTS:
        sup.exportImage(os.path.join(RUN_DIR, f"snapshot_{SNAPSHOTS[hal.t]}.png"), 90)
    hal.t += STEP_S
