"""One Aqua Edge node (or the gateway) in the live simulation. One process, later one container, each.

    NODE_ID=B-203 python3 live/node.py
    NODE_ID=GATEWAY python3 live/node.py

Runs the real firmware (webots/firmware) through NetHAL, a hardware layer that gets this node's
sensor readings and radio messages from the timekeeper, one minute at a time. Standard library only.
"""
import json
import os
import random
import sys
import time
import zlib
from multiprocessing.connection import Client

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "webots", "firmware"))  # ponytail: move firmware/ to a shared folder once Webots is retired
from aqua_edge import FlatNode, TankNode  # noqa: E402
from gateway import Gateway  # noqa: E402

RUN = os.environ.get("AQUA_RUN", os.path.join(HERE, "run"))
ADDRESS = (os.environ.get("AQUA_TIMEKEEPER", "localhost"), 6000)
AUTHKEY = b"aqua-edge"
SEND_SPREAD_S = 59        # each packet goes out at a random second of its minute, like unsynchronised real nodes


class NetHAL:
    def __init__(self, name):
        self.name, self.t, self.r, self.inbox, self.outbox = name, 0.0, {}, [], []
        self.storage_path = os.path.join(RUN, "sd", f"{name}.jsonl")
        self.rng = random.Random(zlib.crc32(name.encode()))
        self.colour = "green"

    def now(self):
        return self.t

    def read(self):
        return self.r

    def send(self, msg):
        self.outbox.append((self.rng.uniform(0, SEND_SPREAD_S), msg))

    def receive(self):
        msgs, self.inbox = self.inbox, []
        return msgs

    def set_valve(self, closed, reason):     # no valves for now: log only
        with open(os.path.join(RUN, "valves.jsonl"), "a") as f:
            f.write(json.dumps({"t": self.t, "node": self.name, "closed": closed, "reason": reason}) + "\n")

    def led(self, colour):
        self.colour = colour


def connect():
    for _ in range(100):                     # the timekeeper may still be starting
        try:
            return Client(ADDRESS, authkey=AUTHKEY)
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.2)
    raise SystemExit(f"cannot reach the timekeeper at {ADDRESS}")


def main(name):
    os.makedirs(os.path.join(RUN, "sd"), exist_ok=True)
    conn = connect()
    conn.send(name)
    setup = conn.recv()                      # {"flats": [...], "tank": "B-TANK"}
    hal = NetHAL(name)
    if name == "GATEWAY":
        fw = Gateway(hal, setup["flats"], setup["tank"])
    else:
        fw = TankNode(name, hal) if name == setup["tank"] else FlatNode(name, hal)
    logged = events_logged = 0
    while (tick := conn.recv()) is not None:
        hal.t, hal.r, hal.inbox = tick["t"], tick["r"], tick["inbox"]
        fw.step()
        conn.send(hal.outbox)
        hal.outbox = []
        if name == "GATEWAY":                # what the gateway knows, as it learns it
            with open(os.path.join(RUN, "gateway_log.jsonl"), "a") as f:
                f.writelines(json.dumps(m) + "\n" for m in fw.log[logged:])
            with open(os.path.join(RUN, "gateway_events.jsonl"), "a") as f:
                f.writelines(json.dumps(e) + "\n" for e in fw.events[events_logged:])
            logged, events_logged = len(fw.log), len(fw.events)


if __name__ == "__main__":
    main(os.environ.get("NODE_ID") or sys.argv[1])
