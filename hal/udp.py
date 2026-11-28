"""A stand-in for the radio over a network, for nodes and a gateway without LoRa hardware (such as
containers). The datagrams are the same encrypted packets that go on air (packet.py), and the
methods are the radios' own: send(raw, cad) -> bool and receive() -> [(arrival time, raw)].

    node = UdpRadio("gateway", 1700)      # a node
    hub = UdpHub("0.0.0.0", 1700)         # the gateway

Like a radio, the hub sends every packet to every node, and each can read only its own. It knows a
node once it has heard from it, so nodes send an empty datagram every few seconds. Time on air,
collisions and loss are NOT simulated. Anyone who can reach the port can make the hub send to
them (never read or forge packets): keep it on a private network.

    python3 -m hal.udp      # self-check on this computer
"""
import socket
import time

from hal.common import MAX_PAYLOAD

PORT = 1700
HELLO_S = 5             # how often a node says it is there
PEER_TTL_S = 30         # a node not heard for this long is forgotten
MAX_PEERS = 64
MAX_PER_CALL = 256      # datagrams taken in one receive(), so a flood cannot hold the program


def _drain(sock):
    got = []
    for _ in range(MAX_PER_CALL):
        try:
            data, address = sock.recvfrom(MAX_PAYLOAD + 1)
        except BlockingIOError:
            break
        except OSError:
            break
        got.append((time.time(), data, address))
    return got


class UdpRadio:
    """A node's end."""

    def __init__(self, host, port=PORT, clock=time.monotonic, log=print):
        self.target, self.clock, self.log = (host, port), clock, log
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.address, self.hello_at, self.failing = None, None, False

    def _hello(self):
        now = self.clock()
        if self.hello_at is not None and now - self.hello_at < HELLO_S:
            return
        self.hello_at = now
        try:                    # looked up again each time: a restarted gateway may have moved
            self.address = (socket.gethostbyname(self.target[0]), self.target[1])
        except OSError as e:
            self._failed(e)
        self._send(b"")

    def _send(self, data):
        if self.address is None:
            return False
        try:
            self.sock.sendto(data, self.address)
        except OSError as e:
            self._failed(e)
            return False
        if self.failing:
            self.failing = False
            self.log("network radio: sending again")
        return True

    def _failed(self, error):
        if not self.failing:
            self.failing = True
            self.log(f"network radio: {error}")

    def send(self, raw, cad=True):
        self._hello()
        return self._send(bytes(raw))

    def receive(self):
        self._hello()
        # from the gateway's address only (every packet is also checked by its key)
        return [(t, data) for t, data, address in _drain(self.sock) if data and address == self.address]

    def close(self):
        self.sock.close()


class UdpHub:
    """The gateway's end."""

    def __init__(self, host="127.0.0.1", port=PORT, clock=time.monotonic):
        self.clock, self.peers = clock, {}        # address -> when last heard
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]

    def send(self, raw, cad=False):
        now, sent = self.clock(), False
        for address, heard in list(self.peers.items()):
            if now - heard > PEER_TTL_S:
                del self.peers[address]
                continue
            try:
                self.sock.sendto(bytes(raw), address)
                sent = True
            except OSError:
                del self.peers[address]
        return sent

    def receive(self):
        got = []
        for t, data, address in _drain(self.sock):
            if address not in self.peers and len(self.peers) >= MAX_PEERS:
                del self.peers[min(self.peers, key=self.peers.get)]
            self.peers[address] = self.clock()
            if data and len(data) <= MAX_PAYLOAD:       # an empty one only says "here"
                got.append((t, data))
        return got

    def close(self):
        self.sock.close()


class Radios:
    """Several radios as one, for a gateway that hears nodes in more than one way."""

    def __init__(self, radios):
        self.radios = list(radios)

    def send(self, raw, cad=False):
        return any([r.send(raw, cad) for r in self.radios])

    def receive(self):
        return [item for r in self.radios for item in r.receive()]

    def close(self):
        for r in self.radios:
            r.close()


def parse_address(text, default_port=PORT):
    """"host", "host:port" -> (host, port). ValueError if it is not one."""
    host, _, port = text.partition(":")
    if not host or (port and not port.isdigit()) or int(port or default_port) > 65535:
        raise ValueError(f"not a network address: {text!r}")
    return host, int(port or default_port)


if __name__ == "__main__":
    now = [0.0]
    hub = UdpHub(port=0, clock=lambda: now[0])
    a, b = (UdpRadio("127.0.0.1", hub.port, clock=lambda: now[0]) for _ in range(2))

    def got(radio, n=1):
        out, end = [], time.time() + 2
        while len(out) < n and time.time() < end:
            out += [raw for _, raw in radio.receive()]
        return out

    assert hub.send(b"nobody") is False
    a.receive(), b.receive()                              # only saying hello
    end = time.time() + 2
    while len(hub.peers) < 2 and time.time() < end:
        assert hub.receive() == []
    assert len(hub.peers) == 2
    assert a.send(b"up") and got(hub) == [b"up"]
    assert hub.send(b"down") and got(a) == [b"down"] and got(b) == [b"down"], "like a radio: to all"
    now[0] = PEER_TTL_S + 1
    assert hub.send(b"x") is False and not hub.peers, "silent nodes are forgotten"
    for i in range(MAX_PEERS + 5):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(b"", ("127.0.0.1", hub.port))
        s.close()
        now[0] += 0.001
        hub.receive()
    assert len(hub.peers) <= MAX_PEERS
    lines = []
    lost = UdpRadio("no-such-host.invalid", clock=lambda: now[0], log=lines.append)
    assert lost.send(b"x") is False and lost.receive() == [] and len(lines) == 1
    assert Radios([hub, hub]).receive() == []
    assert parse_address("gateway") == ("gateway", PORT) and parse_address("0.0.0.0:9") == ("0.0.0.0", 9)
    for bad in ("", ":1", "h:x", "h:70000"):
        try:
            parse_address(bad)
        except ValueError:
            continue
        raise AssertionError(bad)
    for r in (a, b, lost, hub):
        r.close()
    print("udp radio self-check passed")
