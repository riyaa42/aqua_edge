"""The message link between the two ends of the radio, shared by the node and the gateway: whole
messages go in and come out, packed, encrypted and checked (packet.py). Delivery (queues,
acknowledgements, retries) is not done here: that is the node's and the gateway's job.

    link = MessageLink(radio, key_for)
    link.send(message)              # encrypt and send one message
    messages, rejected = link.receive()

radio: anything with send(raw: bytes, cad=False) and receive() -> [raw bytes]. pi_hal.py and
gate_hal.py are the node's and the gateway's ways in.

key_for(name): the key for packets whose header names `name` (a node's name for everything to or
from that node, "GATEWAY" for the beacon). It decides what this end can read: give a node only
its own key and the beacon key, and packets to or from other nodes are rejected.

    python3 -m hal.link      # self-check
"""
from hal.lora433 import airtime_s
from hal.packet import pack, sim_key, unpack


class MessageLink:
    def __init__(self, radio, key_for):
        self.radio, self.key_for = radio, key_for

    def _known_key(self, name):
        try:
            return self.key_for(name)
        except KeyError:                    # a name we hold no key for
            return None

    def send(self, message, cad=True, admit=None):
        """Encrypt (packet.py) and send one message. admit(seconds_on_air) may refuse it first (the transmit-time
        limit): then nothing is sent and this returns False. True means handed to the radio, not
        that it arrived."""
        raw = pack(message, self.key_for(message["n"]))
        if admit is not None and not admit(airtime_s(len(raw))):
            return False
        self.radio.send(raw, cad=cad)
        return True

    def receive(self):
        """(messages received since the last call, number rejected). Rejected: broken, changed,
        made with another key, or not ours to read."""
        messages, rejected = [], 0
        for raw in self.radio.receive():
            message = unpack(raw, self._known_key)
            if message is None:
                rejected += 1
            else:
                messages.append(message)
        return messages, rejected


if __name__ == "__main__":
    class Air:
        def __init__(self): self.out, self.box = [], []
        def send(self, raw, cad=False): self.out.append((raw, cad))
        def receive(self): got, self.box = self.box, []; return got

    node_keys = {n: sim_key(n) for n in ("B-203", "GATEWAY")}
    a, b = Air(), Air()
    node, gateway = MessageLink(a, node_keys.get), MessageLink(b, sim_key)
    up = {"k": "ALERT", "n": "B-203", "s": 1, "t": 60, "a": "LEAK_NIGHT_FLOW", "flow": 0.05}
    assert node.send(up) and a.out[-1][1] is True
    b.box = [a.out[-1][0], b"junk", pack(up, b"attacker-guessed")]
    assert gateway.receive() == ([up], 2) and gateway.receive() == ([], 0)
    ack = {"k": "ACK", "n": "B-203", "s": 1, "gt": 61}
    other = {"k": "ACK", "n": "B-101", "s": 1, "gt": 61}
    assert gateway.send(ack) and gateway.send(other)
    a.box = [raw for raw, _ in b.out]
    assert node.receive() == ([ack], 1), "another node's packet is not ours to read"
    strict = MessageLink(b, {"B-203": sim_key("B-203")}.__getitem__)
    b.box = [pack(other, sim_key("B-101"))]
    assert strict.receive() == ([], 1), "a name without a key is rejected, not a crash"
    asked = []
    assert not node.send(up, admit=lambda s: asked.append(s)) and len(a.out) == 1
    assert 0.03 < asked[0] < 0.1, asked
    print("link self-check passed")
