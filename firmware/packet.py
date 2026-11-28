"""Aqua Edge radio packets: compact binary, encrypted and authenticated. Used by the nodes and the gateway.

Plain LoRa has no security, so every packet is protected here (the AES-SIV scheme from the team's
communication branch, hal/packet.py):

    every packet but BEACON
        header  type(1) node(1) counter(4) time(4)      10 bytes, readable, authenticated
        sealed  AES-SIV: tag(16) + encrypted(format version(1) + fields)
                fields, each: field number(1) + value   (ALERT, FLAG, SUM, CMD; none for ACK, GOT)
    BEACON
        header  as above                                10 bytes, readable
        tag     first 4 bytes of HMAC-SHA256(building key, header)

- Node packets: node = sender, counter = the node's sequence number, time = node clock.
- ACK: node = the node being answered, counter = the sequence number it acknowledges,
  time = gateway clock ("gt"). Made with that node's key, so nobody else can fake it.
- CMD (gateway to node): node = the node told, counter = the gateway's command number. The node
  answers GOT with the same number; the gateway repeats the CMD until it gets one.
- BEACON: once a minute from the gateway to everyone, only its clock. Signed, not encrypted, with
  the building key key("GATEWAY"). It only tells nodes the gateway can hear them, so a leaked
  building key could cause extra sends, no more.
- Each node shares one secret 16-byte key with the gateway: hal.key(name). The AES-SIV key is
  derived from it with HKDF-SHA256.
- AES-SIV (RFC 5297) needs no nonce: a node whose counter restarts (SD card wiped) gives away at
  most that two packets were exactly the same. AES counter mode, used before, would leak the
  contents in that case. The price is 12 more bytes per packet than the old 4-byte tag.
- NOT hidden: which node, the kind of packet, the counter, the time and the length.
- NOT prevented here: sending an old packet again. The node and the gateway check the counter.

    python3 packet.py      # self-check
"""
import functools
import hashlib
import hmac
import json
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

BEACON_TAG_BYTES = 4                            # LoRaWAN also uses a 4-byte check
SIV_BYTES = 16                                  # AES-SIV's tag, in front of the encrypted bytes
SIV_INFO = b"aqua-edge packet encryption v1"    # HKDF label: both ends must use the same
FORMAT = b"\x01"                                # first encrypted byte: format version
HEADER = struct.Struct(">BBII")
KINDS = ["", "ALERT", "FLAG", "SUM", "ACK", "BEACON", "CMD", "GOT"]       # append only
GATEWAY_CLOCK = ("ACK", "BEACON")               # kinds whose time field is the gateway's clock, "gt"
# ponytail: node table for building B; at installation it comes from the same file as the keys
NAMES = ["GATEWAY", "B-TANK"] + [f"B-{floor}{flat:02d}" for floor in "G1234" for flat in range(1, 5)]
ALERTS = ["LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "ABNORMAL_CONSUMPTION", "ABNORMAL_SUPPLY",
          "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "QUALITY_CLEARED", "QUALITY_HOT",
          "QUALITY_LIMIT_VIOLATION", "LEAK_PRESSURE_DROP",
          "FLAT_LEAK_CLEARED", "CONSUMPTION_CLEARED", "SUPPLY_FLOW_CLEARED",     # recoveries
          "VALVE_CLOSED", "VALVE_OPENED",                                      # tank valve
          "PIPE_LEAK", "PIPE_LEAK_CLEARED"]          # CMD only: what only the gateway knows. Append only
FLAGS = ["SUPPLY_LOW"]
VIOLATIONS = ["pH", "TDS", "TURBIDITY"]         # "viol" is a bit mask of broken IS 10500 limits
# field name -> how its value is packed; its number on air is its position here; "B" = index into ALERTS/FLAGS
FIELDS = {"a": "B", "f": "B", "i": ">I", "vol_l": ">f", "p_min": ">f", "in_l": ">f", "level_m": ">f",
          "flow": ">f", "ph": ">f", "tds": ">f", "level": ">f", "inflow": ">f",
          "pipe_l": ">f", "temp": ">f", "turb": ">f", "viol": ">B", "p_mean": ">f",
          "valve": ">B", "floor": ">B"}             # new fields go at the end, so old numbers keep their meaning
FIELD_NAMES = list(FIELDS)
CODES = {"a": ALERTS, "f": FLAGS}
HEADER_KEYS = {"k", "n", "s", "t", "gt"}


def violation_mask(violations):
    return sum(1 << VIOLATIONS.index(v) for v in violations)


def violations_of(mask):
    return [v for i, v in enumerate(VIOLATIONS) if mask >> i & 1]


def sim_key(name):
    """Simulation only: anyone who knows a node's name can work out its key. Real hardware
    uses load_keys()."""
    return hashlib.sha256(b"aqua-edge-simulation:" + name.encode()).digest()[:16]


def load_keys(path):
    """Real nodes and gateway: secret keys from a JSON file on the SD card,
    {"GATEWAY": "<32 hex>", "B-101": "<32 hex>", ...} -> {name: 16 bytes}. Never committed
    (*keys.json is in .gitignore). Make the file once at installation:
        python3 -c "import json,secrets,packet; print(json.dumps({n: secrets.token_hex(16) for n in packet.NAMES}))" > aqua_keys.json
    The gateway gets the whole file; each node gets its own line and the GATEWAY line (the
    building key that checks beacons), so one stolen node does not give away the others' keys."""
    with open(path) as f:
        keys = {n: bytes.fromhex(h) for n, h in json.load(f).items()}
    bad = sorted(n for n, k in keys.items() if n not in NAMES or len(k) != 16)
    if bad:
        raise ValueError(f"bad entries in {path}: {bad} (unknown node or key not 16 bytes)")
    return keys


def _beacon_tag(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:BEACON_TAG_BYTES]


@functools.lru_cache(maxsize=64)
def _cipher(key):
    return AESSIV(HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=SIV_INFO).derive(key))


def pack(m, key):
    """Message dict -> the bytes to send: encrypted, except a BEACON, which is only signed.
    Unknown fields or alert names fail loudly."""
    beacon = m["k"] == "BEACON"
    if beacon != (m["n"] == "GATEWAY"):
        raise ValueError("a BEACON is from GATEWAY, and GATEWAY sends nothing else")
    t = m["gt"] if m["k"] in GATEWAY_CLOCK else m["t"]
    header = HEADER.pack(KINDS.index(m["k"]), NAMES.index(m["n"]), m["s"], round(t))
    body = b""
    for name in FIELD_NAMES:
        if name in m:
            v = CODES[name].index(m[name]) if name in CODES else m[name]
            body += bytes([FIELD_NAMES.index(name)]) + struct.pack(FIELDS[name], v)
    extra = set(m) - HEADER_KEYS - set(FIELDS)
    if extra:
        raise ValueError(f"no packet field for {sorted(extra)}")
    if beacon:
        if body:
            raise ValueError("a BEACON is not encrypted, so it carries no fields")
        return header + _beacon_tag(key, header)
    return header + _cipher(bytes(key)).encrypt(FORMAT + body, [header])


def peek(raw):
    """Read the header without checking the tag: who, what, which number. Only for routing
    and logs; never act on it."""
    kind, node, counter, t = HEADER.unpack_from(raw)
    return {"k": KINDS[kind], "n": NAMES[node], "s": counter, ("gt" if KINDS[kind] in GATEWAY_CLOCK else "t"): t}


def _open(raw, kind, node, key):
    """The fields' bytes if the packet is genuine and unchanged, else None."""
    header, rest = raw[:HEADER.size], raw[HEADER.size:]
    if (kind == "BEACON") != (node == "GATEWAY"):
        return None
    if kind == "BEACON":                        # exactly header and tag
        return b"" if hmac.compare_digest(_beacon_tag(key, header), rest) else None
    if len(rest) <= SIV_BYTES:
        return None
    try:
        plain = _cipher(bytes(key)).decrypt(rest, [header])
    except InvalidTag:
        return None
    return plain[len(FORMAT):] if plain.startswith(FORMAT) else None


def unpack(raw, key_for):
    """Received bytes -> message dict, or None if the packet is broken, changed, made with another
    key, or in another format. key_for(name) gives the key for the node named in the header
    (None: not ours to read); for a BEACON the name is GATEWAY."""
    try:
        m = peek(raw)
        key = key_for(m["n"])
        body = None if key is None else _open(bytes(raw), m["k"], m["n"], key)
        if body is None:
            return None
        pos = 0
        while pos < len(body):
            name = FIELD_NAMES[body[pos]]
            (v,) = struct.unpack_from(FIELDS[name], body, pos + 1)
            pos += 1 + struct.calcsize(FIELDS[name])
            m[name] = CODES[name][v] if name in CODES else round(v, 4) if isinstance(v, float) else v
        return m
    except (IndexError, struct.error):
        return None


if __name__ == "__main__":
    import os
    import secrets
    import tempfile
    k203, k101, gk = sim_key("B-203"), sim_key("B-101"), sim_key("GATEWAY")
    keys = lambda n: sim_key(n)
    msgs = [{"n": "B-203", "s": 7, "t": 93600, "k": "SUM", "i": 104, "vol_l": 12.3456, "p_min": 10.4321, "p_mean": 10.6},
            {"n": "B-301", "s": 9, "t": 93600, "k": "SUM", "i": 104, "vol_l": 8.25, "p_min": 7.9, "pipe_l": 40.5},
            {"n": "B-203", "s": 8, "t": 93660, "k": "ALERT", "a": "LEAK_NIGHT_FLOW", "flow": 0.0512},
            {"n": "B-TANK", "s": 3, "t": 60, "k": "ALERT", "a": "QUALITY_CHEMICAL", "ph": 9.12, "tds": 700},
            {"n": "B-TANK", "s": 4, "t": 120, "k": "ALERT", "a": "QUALITY_CLEARED"},
            {"n": "B-TANK", "s": 10, "t": 300, "k": "ALERT", "a": "QUALITY_HOT", "ph": 7.6, "tds": 250, "temp": 38.5},
            {"n": "B-TANK", "s": 11, "t": 360, "k": "ALERT", "a": "QUALITY_LIMIT_VIOLATION", "ph": 7.1, "tds": 300,
             "turb": 12.5, "viol": violation_mask(["TURBIDITY"])},
            {"n": "B-TANK", "s": 12, "t": 420, "k": "ALERT", "a": "VALVE_CLOSED", "valve": 0},
            {"n": "B-TANK", "s": 5, "t": 180, "k": "FLAG", "f": "SUPPLY_LOW", "level": 0.312, "inflow": 0.0},
            {"n": "B-TANK", "s": 6, "t": 240, "k": "SUM", "i": 5, "vol_l": 300.5, "in_l": 0.0, "level_m": 1.4991,
             "ph": 7.4, "tds": 210, "turb": 0.8, "temp": 26.1, "valve": 1},
            {"n": "B-203", "s": 13, "t": 480, "k": "ALERT", "a": "FLAT_LEAK_CLEARED"},
            {"n": "B-203", "s": 1701, "t": 93000, "k": "CMD", "a": "PIPE_LEAK", "floor": 2},
            {"n": "B-203", "s": 1701, "t": 93721, "k": "GOT"},
            {"n": "GATEWAY", "s": 1700, "k": "BEACON", "gt": 93720},
            {"k": "ACK", "n": "B-203", "s": 8, "gt": 93720}]
    for m in msgs:
        raw = pack(m, keys(m["n"]))
        assert unpack(raw, keys) == m, (unpack(raw, keys), m)          # same message back
        for i in range(len(raw) * 8):                                   # any one bit changed
            bad = bytearray(raw)
            bad[i // 8] ^= 1 << (i % 8)
            assert unpack(bytes(bad), keys) is None, (m["k"], i)
        for n in range(len(raw)):                                       # cut short anywhere
            assert unpack(raw[:n], keys) is None, (m["k"], n)
        assert unpack(raw + b"\0", keys) is None
        print(f"{m['k']:6} {len(raw):3} bytes (JSON {len(json.dumps(m, separators=(',', ':'))):3})")
    ack = pack(msgs[-1], k203)
    assert unpack(ack, lambda n: k101 if n == "B-203" else None) is None   # made with the wrong key
    assert unpack(ack, lambda n: None) is None                           # not ours to read
    assert len(ack) == HEADER.size + SIV_BYTES + 1                       # nothing to say: only the format byte
    # Nobody without the key can read the body; the same message gives the same bytes, any change others
    alert = msgs[2]
    sent = pack(alert, k203)
    assert struct.pack(">f", alert["flow"]) not in sent and sent == pack(alert, k203)
    for other in ({**alert, "s": 99}, {**alert, "flow": 0.06}, {**alert, "k": "FLAG"}):
        assert pack(other, k203)[HEADER.size:] != sent[HEADER.size:], other
    assert pack(alert, k203)[HEADER.size:] != pack(alert, k101)[HEADER.size:]
    # Forged kinds: an ACK "from" the gateway, a beacon "from" a node, wrong things packed
    for kind, node, key in (("ACK", "GATEWAY", gk), ("BEACON", "B-203", k203)):
        head = HEADER.pack(KINDS.index(kind), NAMES.index(node), 1, 1)
        for forged in (head + _beacon_tag(key, head), head + _cipher(key).encrypt(FORMAT, [head])):
            assert unpack(forged, keys) is None, (kind, node)
    for wrong in ({"k": "BEACON", "n": "GATEWAY", "s": 1, "gt": 1, "flow": 1.0},
                  {"k": "BEACON", "n": "B-203", "s": 1, "gt": 1}, {"k": "SUM", "n": "GATEWAY", "s": 1, "t": 1}):
        try:
            pack(wrong, gk)
            raise AssertionError(f"packed {wrong}")
        except ValueError:
            pass
    rng = __import__("random").Random(1)                                # noise never crashes it
    for _ in range(3000):
        assert unpack(rng.randbytes(rng.randrange(0, 80)), keys) is None
    assert violations_of(violation_mask(["pH", "TURBIDITY"])) == ["pH", "TURBIDITY"]
    # Key file: a good one loads; a short key or an unknown node is refused
    path = os.path.join(tempfile.mkdtemp(), "aqua_keys.json")
    good = {n: secrets.token_hex(16) for n in NAMES}
    for content, ok in ((good, True), ({**good, "B-101": "abcd"}, False), ({"B-999": "00" * 16}, False)):
        with open(path, "w") as f:
            json.dump(content, f)
        try:
            assert load_keys(path)["B-101"] == bytes.fromhex(good["B-101"]) and ok
        except ValueError:
            assert not ok
    print("packet self-check passed")
