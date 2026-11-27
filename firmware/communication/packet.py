"""Aqua Edge radio packets: compact binary, signed. Used by the nodes and the gateway.

Plain LoRa has no security, so we borrow two ideas from LoRaWAN (docs/IMPROVEMENT_PLAN.md
section 3): a short signature (tag) on every packet, and a counter that never repeats.

    header  type(1) node(1) counter(4) time(4)      10 bytes
    body    fields, each: field number(1) + value    (ALERT, FLAG, SUM; none for ACK)
    tag     first 4 bytes of HMAC-SHA256(key, header + body)

- Node packets: node = sender, counter = the node's sequence number, time = node clock.
- ACK: node = the node being answered, counter = the sequence number it acknowledges,
  time = gateway clock ("gt"). Signed with that node's key, so nobody else can fake it.
- Signed only, not encrypted: anyone can read a packet, nobody can forge or change one.
- Each node shares one secret 16-byte key with the gateway: hal.key(name).

    python3 packet.py      # self-check
"""
import hashlib
import hmac
import struct

TAG_BYTES = 4                                   # LoRaWAN also uses a 4-byte check
HEADER = struct.Struct(">BBII")
KINDS = ["", "ALERT", "FLAG", "SUM", "ACK"]
# ponytail: node table for building B; at installation it comes from the same file as the keys
NAMES = ["GATEWAY", "B-TANK"] + [f"B-{floor}{flat:02d}" for floor in "G1234" for flat in range(1, 5)]
ALERTS = ["LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP", "ABNORMAL_CONSUMPTION", "ABNORMAL_SUPPLY",
          "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "QUALITY_LIMIT_VIOLATION", "QUALITY_CLEARED",
          "QUALITY_TEMPORAL_ANOMALY", "QUALITY_TEMPORAL_CLEARED",
          "UNUSUAL_NIGHT_CONSUMPTION", "PROLONGED_ZERO_FLOW", "SUDDEN_CONSUMPTION_CHANGE",
          "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED", "ZERO_FLOW_CLEARED", "SUPPLY_FLOW_CLEARED"]   # append only
FLAGS = ["SUPPLY_LOW", "SUPPLY_RECOVERED"]                      # append only
VIOLATIONS = ["pH", "TDS", "TURBIDITY"]     # "viol" is a bit mask of broken limits
# field name -> how its value is packed; its number on air is its position here; "B" = index into ALERTS/FLAGS
FIELDS = {"a": "B", "f": "B", "i": ">I", "vol_l": ">f", "p_min": ">f", "in_l": ">f", "level_m": ">f",
          "flow": ">f", "ph": ">f", "tds": ">f", "level": ">f", "inflow": ">f", "score": ">f", "p_mean": ">f",
          "turb": ">f", "viol": ">B"}      # append only: a field's position is its number on air
FIELD_NAMES = list(FIELDS)
CODES = {"a": ALERTS, "f": FLAGS}
HEADER_KEYS = {"k", "n", "s", "t", "gt"}


def violation_mask(violations):
    return sum(1 << VIOLATIONS.index(v) for v in violations)


def sim_key(name):
    """Simulation only. A real node reads its key from a file on its SD card, never committed."""
    return hashlib.sha256(b"aqua-edge-simulation:" + name.encode()).digest()[:16]


def _tag(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:TAG_BYTES]


def pack(m, key):
    """Message dict -> signed bytes. Unknown fields or alert names fail loudly."""
    t = m["gt"] if m["k"] == "ACK" else m["t"]
    data = HEADER.pack(KINDS.index(m["k"]), NAMES.index(m["n"]), m["s"], round(t))
    for name in FIELD_NAMES:
        if name in m:
            v = CODES[name].index(m[name]) if name in CODES else m[name]
            data += bytes([FIELD_NAMES.index(name)]) + struct.pack(FIELDS[name], v)
    extra = set(m) - HEADER_KEYS - set(FIELDS)
    if extra:
        raise ValueError(f"no packet field for {sorted(extra)}")
    return data + _tag(key, data)


def peek(raw):
    """Read the header without checking the tag: who, what, which number. Only for routing
    and logs; never act on it."""
    kind, node, counter, t = HEADER.unpack_from(raw)
    return {"k": KINDS[kind], "n": NAMES[node], "s": counter, ("gt" if KINDS[kind] == "ACK" else "t"): t}


def unpack(raw, key_for):
    """Signed bytes -> message dict, or None if the packet is broken or the tag is wrong.
    key_for(name) gives the key for the node named in the header (None: not ours to read)."""
    try:
        m = peek(raw)
        key = key_for(m["n"])
        if key is None or not hmac.compare_digest(_tag(key, raw[:-TAG_BYTES]), raw[-TAG_BYTES:]):
            return None
        pos, end = HEADER.size, len(raw) - TAG_BYTES
        while pos < end:
            name = FIELD_NAMES[raw[pos]]
            (v,) = struct.unpack_from(FIELDS[name], raw, pos + 1)
            pos += 1 + struct.calcsize(FIELDS[name])
            m[name] = CODES[name][v] if name in CODES else round(v, 4) if isinstance(v, float) else v
        return m if pos == end else None
    except (IndexError, struct.error):
        return None


if __name__ == "__main__":
    import json
    k203, k101 = sim_key("B-203"), sim_key("B-101")
    keys = lambda n: sim_key(n)
    msgs = [{"n": "B-203", "s": 7, "t": 93600, "k": "SUM", "i": 104, "vol_l": 12.3456, "p_min": 10.4321},
            {"n": "B-203", "s": 8, "t": 93660, "k": "ALERT", "a": "LEAK_NIGHT_FLOW", "flow": 0.0512},
            {"n": "B-TANK", "s": 3, "t": 60, "k": "ALERT", "a": "QUALITY_CHEMICAL", "ph": 9.12, "tds": 700},
            {"n": "B-TANK", "s": 4, "t": 120, "k": "ALERT", "a": "QUALITY_CLEARED"},
            {"n": "B-TANK", "s": 9, "t": 130, "k": "ALERT", "a": "QUALITY_LIMIT_VIOLATION", "ph": 7.1, "tds": 300,
             "turb": 12.5, "viol": violation_mask(["TURBIDITY"])},
            {"n": "B-TANK", "s": 5, "t": 180, "k": "FLAG", "f": "SUPPLY_LOW", "level": 0.312, "inflow": 0.0},
            {"n": "B-TANK", "s": 6, "t": 240, "k": "SUM", "i": 5, "vol_l": 300.5, "in_l": 0.0, "level_m": 1.4991},
            {"k": "ACK", "n": "B-203", "s": 8, "gt": 93720}]
    msgs[-1:-1] = [{"n": "B-203", "s": 10, "t": 300, "k": "ALERT", "a": "FLAT_LEAK_CLEARED"},
                   {"n": "B-TANK", "s": 11, "t": 360, "k": "FLAG", "f": "SUPPLY_RECOVERED", "level": 1.5}]
    for m in msgs:
        raw = pack(m, keys(m["n"]))
        assert unpack(raw, keys) == m, (unpack(raw, keys), m)          # same message back
        bad = bytearray(raw)
        bad[5] ^= 1                                                      # one bit changed
        assert unpack(bytes(bad), keys) is None
        print(f"{m['k']:5} {len(raw):3} bytes (JSON {len(json.dumps(m, separators=(',', ':'))):3})")
    ack = pack(msgs[-1], k203)
    assert unpack(ack, lambda n: k101 if n == "B-203" else None) is None   # signed with the wrong key
    assert unpack(ack, lambda n: None) is None                           # not ours to read
    assert unpack(ack[:7], keys) is None                                 # cut short
    print("packet self-check passed")
