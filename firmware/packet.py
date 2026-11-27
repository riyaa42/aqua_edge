"""Aqua Edge radio packets: compact binary, encrypted and signed. Used by the nodes and the gateway.

Plain LoRa has no security, so we borrow three ideas from LoRaWAN (docs/IMPROVEMENT_PLAN.md
section 3): the body is encrypted with AES-128 in counter mode, a short signature (tag) covers
the whole packet, and a counter never repeats.

    header  type(1) node(1) counter(4) time(4)      10 bytes, readable
    body    fields, each: field number(1) + value    encrypted (ALERT, FLAG, SUM; none for ACK)
    tag     first 4 bytes of HMAC-SHA256(key, header + encrypted body)

- Node packets: node = sender, counter = the node's sequence number, time = node clock.
- ACK: node = the node being answered, counter = the sequence number it acknowledges,
  time = gateway clock ("gt"). Signed with that node's key, so nobody else can fake it.
- Nobody can read the body (flow, volumes, pH, which alert) or forge or change any packet.
  The header stays readable, as in LoRaWAN, so the gateway knows whose key to use: a listener
  can still see that B-203 sent an ALERT at 02:30, but not what it says.
- Counter mode adds no bytes, so encryption does not change packet sizes or airtime. Its
  starting block is the header, which never repeats for a key (the node's counter never
  repeats; the type byte tells a message from its ACK). A resend is the identical packet.
- Encrypt, then sign: the tag is checked before anything is decrypted.
- Each node shares one secret 16-byte key with the gateway: hal.key(name). The encryption key
  is derived from it, so encrypting and signing use different keys (like LoRaWAN's two
  session keys) without a second key file.

    python3 packet.py      # self-check
"""
import hashlib
import hmac
import json
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

TAG_BYTES = 4                                   # LoRaWAN also uses a 4-byte check
HEADER = struct.Struct(">BBII")
KINDS = ["", "ALERT", "FLAG", "SUM", "ACK"]
# ponytail: node table for building B; at installation it comes from the same file as the keys
NAMES = ["GATEWAY", "B-TANK"] + [f"B-{floor}{flat:02d}" for floor in "G1234" for flat in range(1, 5)]
ALERTS = ["LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "ABNORMAL_CONSUMPTION", "ABNORMAL_SUPPLY",
          "QUALITY_RUNOFF", "QUALITY_CHEMICAL", "QUALITY_CLEARED"]
FLAGS = ["SUPPLY_LOW"]
# field name -> how its value is packed; its number on air is its position here; "B" = index into ALERTS/FLAGS
FIELDS = {"a": "B", "f": "B", "i": ">I", "vol_l": ">f", "p_min": ">f", "in_l": ">f", "level_m": ">f",
          "flow": ">f", "ph": ">f", "tds": ">f", "level": ">f", "inflow": ">f"}
FIELD_NAMES = list(FIELDS)
CODES = {"a": ALERTS, "f": FLAGS}
HEADER_KEYS = {"k", "n", "s", "t", "gt"}


def sim_key(name):
    """Simulation only: anyone who knows a node's name can work out its key. Real hardware
    uses load_keys()."""
    return hashlib.sha256(b"aqua-edge-simulation:" + name.encode()).digest()[:16]


def load_keys(path):
    """Real nodes and gateway: secret keys from a JSON file on the SD card,
    {"B-101": "<32 hex digits>", ...} -> {name: 16 bytes}. Never committed (*keys.json is in
    .gitignore). Make the file once at installation:
        python3 -c "import json,secrets,packet; print(json.dumps({n: secrets.token_hex(16) for n in packet.NAMES[1:]}))" > aqua_keys.json
    The gateway gets the whole file; each node gets a file with only its own line, so one
    stolen node does not give away the others' keys."""
    with open(path) as f:
        keys = {n: bytes.fromhex(h) for n, h in json.load(f).items()}
    bad = sorted(n for n, k in keys.items() if n not in NAMES or len(k) != 16)
    if bad:
        raise ValueError(f"bad entries in {path}: {bad} (unknown node or key not 16 bytes)")
    return keys


def _tag(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()[:TAG_BYTES]


def _crypt(key, header, body):
    """AES-128 in counter mode; the same call encrypts and decrypts."""
    enc_key = hmac.new(key, b"aqua-edge-encrypt", hashlib.sha256).digest()[:16]
    c = Cipher(algorithms.AES(enc_key), modes.CTR(header + bytes(16 - len(header)))).encryptor()
    return c.update(body) + c.finalize()


def pack(m, key):
    """Message dict -> encrypted, signed bytes. Unknown fields or alert names fail loudly."""
    t = m["gt"] if m["k"] == "ACK" else m["t"]
    header = HEADER.pack(KINDS.index(m["k"]), NAMES.index(m["n"]), m["s"], round(t))
    body = b""
    for name in FIELD_NAMES:
        if name in m:
            v = CODES[name].index(m[name]) if name in CODES else m[name]
            body += bytes([FIELD_NAMES.index(name)]) + struct.pack(FIELDS[name], v)
    extra = set(m) - HEADER_KEYS - set(FIELDS)
    if extra:
        raise ValueError(f"no packet field for {sorted(extra)}")
    data = header + _crypt(key, header, body)
    return data + _tag(key, data)


def peek(raw):
    """Read the header without checking the tag: who, what, which number. Only for routing
    and logs; never act on it."""
    kind, node, counter, t = HEADER.unpack_from(raw)
    return {"k": KINDS[kind], "n": NAMES[node], "s": counter, ("gt" if KINDS[kind] == "ACK" else "t"): t}


def unpack(raw, key_for):
    """Encrypted, signed bytes -> message dict, or None if the packet is broken or the tag is
    wrong. key_for(name) gives the key for the node named in the header (None: not ours to read)."""
    try:
        m = peek(raw)
        key = key_for(m["n"])
        if key is None or not hmac.compare_digest(_tag(key, raw[:-TAG_BYTES]), raw[-TAG_BYTES:]):
            return None                                 # checked before anything is decrypted
        body = _crypt(key, raw[:HEADER.size], raw[HEADER.size:-TAG_BYTES])
        pos = 0
        while pos < len(body):
            name = FIELD_NAMES[body[pos]]
            (v,) = struct.unpack_from(FIELDS[name], body, pos + 1)
            pos += 1 + struct.calcsize(FIELDS[name])
            m[name] = CODES[name][v] if name in CODES else round(v, 4) if isinstance(v, float) else v
        return m if pos == len(body) else None
    except (IndexError, struct.error):
        return None


if __name__ == "__main__":
    import os
    import secrets
    import tempfile
    k203, k101 = sim_key("B-203"), sim_key("B-101")
    keys = lambda n: sim_key(n)
    msgs = [{"n": "B-203", "s": 7, "t": 93600, "k": "SUM", "i": 104, "vol_l": 12.3456, "p_min": 10.4321},
            {"n": "B-203", "s": 8, "t": 93660, "k": "ALERT", "a": "LEAK_NIGHT_FLOW", "flow": 0.0512},
            {"n": "B-TANK", "s": 3, "t": 60, "k": "ALERT", "a": "QUALITY_CHEMICAL", "ph": 9.12, "tds": 700},
            {"n": "B-TANK", "s": 4, "t": 120, "k": "ALERT", "a": "QUALITY_CLEARED"},
            {"n": "B-TANK", "s": 5, "t": 180, "k": "FLAG", "f": "SUPPLY_LOW", "level": 0.312, "inflow": 0.0},
            {"n": "B-TANK", "s": 6, "t": 240, "k": "SUM", "i": 5, "vol_l": 300.5, "in_l": 0.0, "level_m": 1.4991},
            {"k": "ACK", "n": "B-203", "s": 8, "gt": 93720}]
    for m in msgs:
        raw = pack(m, keys(m["n"]))
        assert unpack(raw, keys) == m, (unpack(raw, keys), m)          # same message back
        fields = sum(1 + struct.calcsize(FIELDS[f]) for f in m if f in FIELDS)
        assert len(raw) == HEADER.size + fields + TAG_BYTES              # encryption adds no bytes
        for bit_at in (5, HEADER.size) if fields else (5,):
            bad = bytearray(raw)
            bad[bit_at] ^= 1                                             # one bit changed: header or encrypted body
            assert unpack(bytes(bad), keys) is None
        print(f"{m['k']:5} {len(raw):3} bytes (JSON {len(json.dumps(m, separators=(',', ':'))):3})")
    # Nobody without the key can read the body: the packed flow value does not appear in the packet,
    # and the same message under another node's key gives different bytes
    alert = msgs[1]
    assert struct.pack(">f", alert["flow"]) not in pack(alert, k203)
    assert pack(alert, k203)[HEADER.size:] != pack(alert, k101)[HEADER.size:]
    ack = pack(msgs[-1], k203)
    assert unpack(ack, lambda n: k101 if n == "B-203" else None) is None   # signed with the wrong key
    assert unpack(ack, lambda n: None) is None                           # not ours to read
    assert unpack(ack[:7], keys) is None                                 # cut short
    # Key file: a good one loads; a short key or an unknown node is refused
    path = os.path.join(tempfile.mkdtemp(), "aqua_keys.json")
    good = {n: secrets.token_hex(16) for n in NAMES[1:]}
    for content, ok in ((good, True), ({**good, "B-101": "abcd"}, False), ({"B-999": "00" * 16}, False)):
        with open(path, "w") as f:
            json.dump(content, f)
        try:
            assert load_keys(path)["B-101"] == bytes.fromhex(good["B-101"]) and ok
        except ValueError:
            assert not ok
    print("packet self-check passed")
