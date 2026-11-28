"""Timekeeper: runs the water model one minute at a time, applies commands from the control
panel (or a scenario file), and keeps every node in step.

    uv run python live/timekeeper.py [--scenario live/scenarios/standard.jsonl] [--hours 48]

Each minute: send every node only its own readings and its radio inbox, wait until all of them
have run their firmware, pass what they sent through the radio model, then step the water.
Commands are lines of JSON appended to run/commands.jsonl, e.g.
    {"do": "leak", "where": "B-203", "size": "small"}           size: small | medium | large | L/s
    {"do": "fix", "where": "B-203"}
    {"do": "quality", "kind": "runoff", "minutes": 60}          kind: runoff | chemical | hot
    {"do": "radio_cut", "who": "all", "minutes": 210}           who: a node name or "all"
    {"do": "valve_reopen"}      someone tested the water and pressed the tank node's reopen button
    {"do": "speed", "delay_s": 0.5}     {"do": "pause"}     {"do": "resume"}     {"do": "stop"}
    {"do": "until_alert", "node": "B-203", "from_s": 93600}
        run as fast as possible until an alert from B-203 made at or after from_s reaches the
        gateway, then pause one minute later (once the gateway has logged it); gives up and
        pauses after WATCH_MAX_S. The timekeeper only reads the packet's header (readable).
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
from node import RUN, ADDRESS, AUTHKEY, peek

DAY = 86400
WATCH_MAX_S = 2 * DAY          # until_alert gives up after this long (a pipe leak waits for the next night check)
MAX_LEAK_LPS = 5.0


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


def gateway_decided(event, since):
    """The gateway's own decisions (PIPE_LEAK) are not radio packets, so read its log."""
    try:
        with open(os.path.join(RUN, "gateway_events.jsonl")) as f:
            return any(e["event"] == event and e["t"] >= since for e in map(json.loads, f))
    except (OSError, ValueError):
        return False


def apply(c, t, water, radio, applied):
    do = c["do"]
    if do == "leak":
        size = c.get("size")
        ok_size = size in LEAK_SIZES_LPS or (isinstance(size, (int, float)) and 0 < size <= MAX_LEAK_LPS)
        if c["where"] not in water.leak_sites or not ok_size:
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
    bind = (os.environ.get("AQUA_BIND", "localhost"), ADDRESS[1])     # 0.0.0.0 in Docker
    with Listener(bind, authkey=AUTHKEY, backlog=64) as listener:
        print(f"waiting for {len(names)} nodes + gateway ...")
        while len(conns) < len(names) + 1:
            c = listener.accept()
            conns[c.recv()] = c
    for c in conns.values():
        # one time slot per node, and the last one for the gateway's beacon and commands
        c.send({"flats": water.flats, "tank": "B-TANK",
                "slots": {n: (i, len(names) + 1) for i, n in enumerate(names + [GATEWAY])}})
    print("all connected, running")

    readings_of = lambda name, r: r.get(name, {}) if name != GATEWAY else {}
    inbox, delay, paused, end = {}, args.delay, False, args.hours * 3600
    watch = pause_at = None              # until_alert: {"node", "from_s", "delay"}; minute to pause at
    reopen = False
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
            elif c["do"] == "valve_reopen":
                reopen = True
                applied.write(json.dumps({"t": t, **c}) + "\n")
                applied.flush()
            elif c["do"] == "until_alert":
                watch = {"node": c["node"], "from_s": c.get("from_s", t), "delay": delay}
                delay, paused, pause_at = 0, False, None
            else:
                apply(c, t, water, radio, applied)
        with open(os.path.join(RUN, "clock.json"), "w") as f:      # for the dashboard
            json.dump({"t": t, "end": end, "paused": paused, "delay_s": delay, "leaks": water.leaks,
                       "quality": water.water_quality, "valve_open": water.valve_open, "watching": watch and {k: watch[k] for k in ("node", "from_s")}}, f)
        if paused:
            time.sleep(0.2)
            continue
        r = water.readings()
        for name, c in conns.items():
            c.send({"t": t, "r": readings_of(name, r), "inbox": inbox.get(name, []),
                    **({"reopen": True} if reopen and name == "B-TANK" else {})})
        reopen = False
        replies = {name: c.recv() for name, c in conns.items()}
        if replies["B-TANK"]["valve"] != water.valve_open:    # the tank node opened or shut its valve
            water.set_valve(replies["B-TANK"]["valve"])
        inbox = radio.transmit(t, {name: x["out"] for name, x in replies.items()})
        if watch:
            heard = any(h["k"] == "ALERT" and h["n"] == watch["node"] and h["t"] >= watch["from_s"]
                        for h in map(peek, inbox.get(GATEWAY, [])))
            if watch["node"] == "B-TANK" and not heard:          # down pipe: the gateway's night check
                heard = gateway_decided("PIPE_LEAK", watch["from_s"])
            if heard or t >= watch["from_s"] + WATCH_MAX_S:
                pause_at, delay, watch = t + 60, watch["delay"], None    # the gateway logs it next minute
        if pause_at is not None and t >= pause_at:
            paused, pause_at = True, None
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
