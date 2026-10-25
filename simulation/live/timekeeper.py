"""Timekeeper: runs the water model one minute at a time, applies commands from the control
panel (or a scenario file), and keeps every node in step.

    uv run python live/timekeeper.py [--scenario live/scenarios/standard.jsonl] [--hours 48]

Each minute: send every node only its own readings and its radio inbox, wait until all of them
have run their firmware, pass what they sent through the radio model, then step the water.
Commands are lines of JSON appended to run/commands.jsonl, e.g.
    {"do": "leak", "where": "B-203", "size": "small"}           size: small | medium | large
    {"do": "fix", "where": "B-203"}
    {"do": "quality", "kind": "runoff", "minutes": 60}          kind: runoff | chemical
    {"do": "radio_cut", "who": "all", "minutes": 210}           who: a node name or "all"
    {"do": "speed", "delay_s": 0.5}     {"do": "pause"}     {"do": "resume"}     {"do": "stop"}
Add "at_s": <simulation seconds> to schedule a command instead of applying it straight away.
"""
import argparse
import json
import os
import shutil
import time
from multiprocessing.connection import Listener

from water import Water, LEAK_SIZES_LPS
from radio import Radio, GATEWAY
from node import RUN, ADDRESS, AUTHKEY

DAY = 86400


class Commands:
    def __init__(self, path):
        self.path, self.offset, self.pending = path, 0, []
        open(path, "a").close()

    def due(self, t):
        with open(self.path) as f:
            f.seek(self.offset)
            self.pending += [json.loads(line) for line in f if line.strip()]
            self.offset = f.tell()
        now = [c for c in self.pending if c.get("at_s", t) <= t]
        self.pending = [c for c in self.pending if c.get("at_s", t) > t]
        return now


def apply(c, t, water, radio, applied):
    do = c["do"]
    if do == "leak":
        if c["where"] not in water.leak_sites or c["size"] not in LEAK_SIZES_LPS:
            print(f"ignored bad command {c}")
            return
        water.start_leak(c["where"], c["size"])
    elif do == "fix":
        water.fix_leak(c["where"])
    elif do == "quality":
        water.set_quality(c["kind"], c["minutes"])
    elif do == "radio_cut":
        radio.cut(c["who"], t, t + 60 * c["minutes"])
    if do in ("leak", "fix", "quality", "radio_cut"):
        applied.write(json.dumps({"t": t, **c}) + "\n")
        applied.flush()
        print(f"{t / 3600:6.2f} h  {c}")


def main(args):
    shutil.rmtree(RUN, ignore_errors=True)
    os.makedirs(os.path.join(RUN, "sd"))
    if args.scenario:
        shutil.copy(args.scenario, os.path.join(RUN, "commands.jsonl"))
    water = Water()
    names = water.flats + ["B-TANK"]
    with open(os.path.join(RUN, "nodes.txt"), "w") as f:
        f.write("\n".join(names) + "\n")
    with open(os.path.join(RUN, "leak_sites.txt"), "w") as f:
        f.write("\n".join(water.leak_sites) + "\n")
    radio = Radio(os.path.join(RUN, "tx_log.csv"))
    commands = Commands(os.path.join(RUN, "commands.jsonl"))
    applied = open(os.path.join(RUN, "events_applied.jsonl"), "w")

    conns = {}
    with Listener(ADDRESS, authkey=AUTHKEY, backlog=64) as listener:
        print(f"waiting for {len(names)} nodes + gateway ...")
        while len(conns) < len(names) + 1:
            c = listener.accept()
            conns[c.recv()] = c
    for c in conns.values():
        c.send({"flats": water.flats, "tank": "B-TANK", "slots": {n: (i, len(names)) for i, n in enumerate(names)}})
    print("all connected, running")

    readings_of = lambda name, r: r.get(name, {}) if name != GATEWAY else {}
    inbox, delay, paused, end = {}, args.delay, False, args.hours * 3600
    start = time.time()
    while water.t < end:
        t = water.t
        for c in commands.due(t):
            if c["do"] == "speed":
                delay = c["delay_s"]
            elif c["do"] in ("pause", "resume"):
                paused = c["do"] == "pause"
            elif c["do"] == "stop":
                end = t
            else:
                apply(c, t, water, radio, applied)
        with open(os.path.join(RUN, "clock.json"), "w") as f:      # for the control panel
            json.dump({"t": t, "end": end, "paused": paused, "delay_s": delay, "leaks": water.leaks,
                       "quality": water.water_quality}, f)
        if paused:
            time.sleep(0.2)
            continue
        r = water.readings()
        for name, c in conns.items():
            c.send({"t": t, "r": readings_of(name, r), "inbox": inbox.get(name, [])})
        outs = {name: c.recv() for name, c in conns.items()}
        inbox = radio.transmit(t, outs)
        water.step()
        if t % 3600 == 0:
            print(f"day {t // DAY + 1} {t % DAY // 3600:02d}:00  ({time.time() - start:.0f} s)", flush=True)
        time.sleep(delay)
    for c in conns.values():
        c.send(None)
    print(f"done: {end / 3600:.0f} h simulated in {time.time() - start:.0f} s; results in {RUN}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", help="commands to run, copied to run/commands.jsonl")
    p.add_argument("--hours", type=float, default=48)
    p.add_argument("--delay", type=float, default=0.0, help="real seconds to wait per simulated minute")
    main(p.parse_args())
