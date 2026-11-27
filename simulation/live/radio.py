"""LoRa radio model for the live simulation.

Per node link quality (spreading factor, chance a lone message gets through) comes from the
ns-3 link table (ns3/link_table.csv) when it exists, otherwise from the defaults below.
Collisions are worked out here, because they depend on who sends when.

    python3 live/radio.py       # self-check
"""
import csv
import math
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LINK_TABLE = os.path.join(HERE, "..", "ns3", "link_table.csv")
GATEWAY = "GATEWAY"
sys.path.insert(0, os.path.join(HERE, "..", ".."))
from hal.packet import NAMES, pack, peek, sim_key  # noqa: E402

BW_HZ, CODING_RATE, PREAMBLE = 125_000, 1, 8   # LoRa defaults: 125 kHz, 4/5, 8 symbols
DEFAULT_SF, DEFAULT_PDR = 7, 1.0               # used until ns-3 gives real numbers
UPLINK_CHANNELS = 3                            # India IN865: 3 standard channels (865.0625, 865.4025, 865.985 MHz)
CAD_TRIES, CAD_BACKOFF_S = 5, (0.1, 1.0)       # listen-before-talk: retries, random wait between them
TURNAROUND_S = 0.01                            # a radio's gap between its own back-to-back packets
CAD_SYMBOLS = 2                                # a CAD listen lasts ~2 symbols; packets newer than that go unheard
LOG_FIELDS = ["t_s", "src", "dst", "kind", "seq", "bytes", "sf", "ch", "airtime_ms", "backoffs", "result"]


def airtime_s(nbytes, sf, bw=BW_HZ):
    """Time on air of one LoRa packet (Semtech AN1200.13): explicit header, CRC on."""
    t_sym = 2 ** sf / bw
    de = 1 if sf >= 11 and bw == 125_000 else 0          # low data rate optimisation
    n = 8 + max(math.ceil((8 * nbytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (CODING_RATE + 4), 0)
    return (PREAMBLE + 4.25) * t_sym + n * t_sym


def load_links(path=LINK_TABLE):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {r["node"]: {"sf": int(r["sf"]), "pdr": float(r["pdr"])} for r in csv.DictReader(f)}


class Radio:
    def __init__(self, log_path, links=None, seed=1, channels=UPLINK_CHANNELS):
        self.links = load_links() if links is None else links
        self.channels = channels
        self.airtime_by_node = {}
        self.cuts = []                                    # (who, start_s, end_s); who = node or "all"
        self.rng = random.Random(seed)
        self.log = open(log_path, "w", newline="") if log_path else None
        self.writer = csv.DictWriter(self.log, LOG_FIELDS) if self.log else None
        if self.writer:
            self.writer.writeheader()

    def duty_cycle_pct(self, node, elapsed_s):
        return 100.0 * self.airtime_by_node.get(node, 0.0) / max(elapsed_s, 1.0)

    def link(self, node):
        return self.links.get(node, {"sf": DEFAULT_SF, "pdr": DEFAULT_PDR})

    def cut(self, who, start, end):
        self.cuts.append((who, start, end))

    def is_cut(self, node, t):
        return any(s <= t < e and who in ("all", node) for who, s, e in self.cuts)

    def transmit(self, t, outs):
        """outs: {sender: [(offset_s, msg[, cad]), ...]} sent during minute t. Returns {receiver: [msg]}
        for delivery at the next minute. Star network: nodes talk to the gateway only.
        Timed packets (time slots) go first; cad=True packets listen before talking."""
        inbox, tx, cad_wait = {}, [], []
        for src, sends in outs.items():
            sf = self.link(src)["sf"] if src != GATEWAY else None
            free_at = t
            for offset, msg, cad in sorted(((*s, False)[:3] for s in sends), key=lambda s: s[0]):
                h = peek(msg)                                   # packets are signed binary (hal/packet.py)
                nbytes = len(msg)
                dsts = [h["n"]] if src == GATEWAY and h["k"] == "ACK" else \
                    [n for n in outs if n != GATEWAY] if src == GATEWAY else [GATEWAY]
                air = airtime_s(nbytes, sf or max(self.link(d)["sf"] for d in dsts))
                x = {"src": src, "dsts": dsts, "msg": msg, "h": h, "bytes": nbytes, "air": air, "sf": sf, "backoffs": 0,
                     "ch": self.rng.randrange(self.channels) if src != GATEWAY else "down"}
                self.airtime_by_node[src] = self.airtime_by_node.get(src, 0.0) + air
                if cad and src != GATEWAY:
                    x["want"] = t + offset
                    cad_wait.append(x)
                    continue
                x["start"] = max(t + offset, free_at)        # one radio cannot send two packets at once
                x["end"] = x["start"] + air
                free_at = x["end"] + TURNAROUND_S
                tx.append(x)
        ups = [x for x in tx if x["src"] != GATEWAY]
        # Listen before talk (LoRa CAD): busy if another packet on our channel and SF is on air right
        # now, or our own radio is sending then; back off and listen again, then send anyway.
        # ponytail: every node hears every other node (one building); hidden nodes need node-to-node links from ns-3
        busy = lambda x, at: any((y["ch"] == x["ch"] and y["sf"] == x["sf"] and y["start"] + CAD_SYMBOLS * 2 ** x["sf"] / BW_HZ <= at < y["end"])
                                 or (y["src"] == x["src"] and y["start"] < at + x["air"] and at < y["end"]) for y in ups)
        for x in sorted(cad_wait, key=lambda x: x["want"]):
            at = x["want"]
            while x["backoffs"] < CAD_TRIES and busy(x, at):
                x["backoffs"] += 1
                at += self.rng.uniform(*CAD_BACKOFF_S)
            x["start"], x["end"] = at, at + x["air"]
            ups.append(x)
            tx.append(x)
        for x in tx:
            for d in x["dsts"]:
                node = d if x["src"] == GATEWAY else x["src"]
                # ponytail: no capture effect (overlap = both lost), downlinks on their own channel and
                # gateway half-duplex ignored, overlap across the minute boundary not checked
                if self.is_cut(node, x["start"]):
                    result = "radio_cut"
                elif x["src"] != GATEWAY and any(y is not x and y["ch"] == x["ch"] and y["sf"] == x["sf"]
                                                 and y["start"] < x["end"] and x["start"] < y["end"] for y in ups):
                    result = "collision"
                elif self.rng.random() >= self.link(node)["pdr"]:
                    result = "weak_link"
                else:
                    result = "ok"
                    inbox.setdefault(d, []).append(x["msg"])
                if self.writer:
                    self.writer.writerow({"t_s": round(x["start"], 3), "src": x["src"], "dst": d, "kind": x["h"]["k"],
                                          "seq": x["h"]["s"], "bytes": x["bytes"], "sf": self.link(node)["sf"],
                                          "ch": x["ch"], "airtime_ms": round(1000 * x["air"], 1),
                                          "backoffs": x["backoffs"], "result": result})
        if self.log:
            self.log.flush()
        return inbox


if __name__ == "__main__":
    a = airtime_s(75, 7)
    print(f"airtime 75 bytes: SF7 {1000 * a:.1f} ms, SF12 {1000 * airtime_s(75, 12):.0f} ms")
    assert abs(a - 0.1334) < 0.001
    r = Radio(None, channels=1)  # single-channel test to force deterministic collision checks
    pk = lambda m: pack({"t": 0, **m}, sim_key(m["n"]))
    m = lambda n, s: pk({"n": n, "s": s, "k": "SUM"})
    A, B = "B-101", "B-102"
    # two nodes at the same second on the same channel and SF: both lost
    got = r.transmit(0, {A: [(10, m(A, 1))], B: [(10.01, m(B, 1))], GATEWAY: []})   # short packets: 10 ms apart
    assert GATEWAY not in got
    # far apart: both arrive
    got = r.transmit(60, {A: [(10, m(A, 2))], B: [(40, m(B, 2))], GATEWAY: []})
    assert len(got[GATEWAY]) == 2
    # one node's two packets never overlap each other
    got = r.transmit(120, {A: [(5, m(A, 3)), (5, m(A, 4))], GATEWAY: []})
    assert len(got[GATEWAY]) == 2
    # ACK reaches only its node; a radio cut blocks it
    r.cut(B, 180, 240)
    ack = lambda n: pack({"k": "ACK", "n": n, "s": 1, "gt": 0}, sim_key(n))
    got = r.transmit(180, {GATEWAY: [(1, ack(A)), (2, ack(B))], A: [], B: []})
    assert list(got) == [A]
    # time slots: 21 nodes, each in its own slot of the minute, all arrive
    nodes = NAMES[1:]
    slots = {n: [(i * 60 / len(nodes) + 0.2, m(n, 1))] for i, n in enumerate(nodes)}
    got = r.transmit(240, {**slots, GATEWAY: []})
    assert len(got[GATEWAY]) == len(nodes)
    # listen before talk: an alert that hears a packet on air waits, and both arrive
    a = pk({"n": A, "s": 9, "k": "ALERT", "a": "LEAK_NIGHT_FLOW"})
    got = r.transmit(300, {B: [(10, m(B, 5))], A: [(10.05, a, True)], GATEWAY: []})
    assert len(got[GATEWAY]) == 2
    # two alerts starting at the very same instant both hear silence and collide: CAD cannot stop that
    got = r.transmit(360, {A: [(10, a, True)], B: [(10, pk({"n": B, "s": 9, "k": "ALERT", "a": "LEAK_NIGHT_FLOW"}), True)], GATEWAY: []})
    assert GATEWAY not in got
    print("radio self-check passed")
