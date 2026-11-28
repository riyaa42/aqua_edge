"""The gateway server (gateway/edge_based.py) with a fake radio and a fake MQTT connection, and the
Docker Compose files. No hardware, broker or Docker.

    python3 -m tests.test_gateway_server
"""
import json
import os
import re
import signal
import tempfile
import threading

import comms.gateway
import gateway.edge_based as server
from gateway.topology import Topology
from hal.packet import NAMES, pack, sim_key, unpack

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
F1 = "B-101"


class Radio:
    """As gate_hal.open_radio() returns it: receive() gives (arrival time, packet)."""

    def __init__(self, stop_after=None):
        self.air, self.sent, self.closed, self.polls, self.stop_after = [], [], False, 0, stop_after

    def send(self, raw, cad=False):
        self.sent.append(raw)
        return True

    def receive(self):
        self.polls += 1
        if self.stop_after and self.polls == self.stop_after:
            signal.raise_signal(signal.SIGTERM)         # as `docker stop` does
        got, self.air = self.air, []
        return [(0.0, raw) for raw in got]

    def close(self):
        self.closed = True


class Mqtt:
    def __init__(self):
        self.published, self.closed = [], False

    def publish(self, where, data, qos, retain, expiry_s):
        self.published.append((where, json.loads(data)))

    def close(self):
        self.closed = True


def key_file(folder, names):
    path = os.path.join(folder, "gateway_keys.json")
    with open(path, "w") as f:
        json.dump({n: sim_key(n).hex() for n in names}, f)
    return path


def check_run():
    import time
    radio, stop, lines = Radio(), threading.Event(), []
    keys = {n: sim_key(n) for n in NAMES}
    folder = tempfile.mkdtemp()
    board = server.GatewayBoard(radio, keys, os.path.join(folder, "gateway.jsonl"))
    gw = server.Gateway(board, Topology.load())
    thread = threading.Thread(target=server.run, args=(gw, stop, lines.append))
    thread.start()
    alert = {"k": "ALERT", "n": F1, "s": 1, "t": time.time(), "a": "LEAK_NIGHT_FLOW"}
    radio.air.append(pack(alert, sim_key(F1)))
    sent_at = time.monotonic()
    acked = lambda: [m for m in (unpack(r, sim_key) for r in list(radio.sent)) if m["k"] == "ACK"]
    while not acked() and time.monotonic() - sent_at < 3:
        time.sleep(0.005)
    took = time.monotonic() - sent_at
    before = radio.polls
    time.sleep(0.5)                 # idle: it should wake about every POLL_S, not spin
    idle_polls = radio.polls - before
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive() and took < 0.5, took
    assert [(m["n"], m["s"]) for m in acked()] == [(F1, 1)] and abs(acked()[0]["gt"] - time.time()) < 5
    assert any("NEW_ALERT" in line for line in lines) and os.path.exists(os.path.join(folder, "gateway_state.json"))
    assert 5 < idle_polls < 0.5 / server.MIN_WAIT_S / 2, f"{idle_polls} steps in 0.5 idle seconds"


def check_main():
    folder = tempfile.mkdtemp()
    everyone = ["GATEWAY", "B-TANK", *Topology.load().flats]
    keys = key_file(folder, everyone)
    saved = server.open_radio, comms.gateway.connect, os.environ.get("MQTT_PASSWORD"), os.environ.pop("MQTT_HOST", None)
    os.environ.pop("DASHBOARD_URL", None)
    handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        for with_mqtt in (False, True):
            radio, mqtt, asked = Radio(stop_after=4), Mqtt(), []
            server.open_radio = lambda port: asked.append(port) or radio
            comms.gateway.connect = lambda building, **options: asked.append((building, options)) or mqtt
            os.environ["MQTT_PASSWORD"] = "from-the-environment"
            extra = ["--mqtt-host", "broker", "--mqtt-user", "gateway"] if with_mqtt else []
            server.main(["--keys", keys, "--data", os.path.join(folder, "data"), "--port", "/dev/x", *extra])
            assert radio.closed and asked[0] == "/dev/x" and radio.sent, "beacon sent, radio closed on SIGTERM"
            if with_mqtt:
                assert asked[1] == ("B", {"host": "broker", "port": 1883, "username": "gateway",
                                          "password": "from-the-environment"})
                assert mqtt.closed and any(w == "aqua/v1/B/gateway/state/building" for w, _ in mqtt.published)
            else:
                assert len(asked) == 1 and not mqtt.published

        def no_modem(port):
            raise OSError("no answer from the ESP")

        server.open_radio = no_modem
        for args, words in ((["--keys", keys, "--data", folder], "no answer"),
                            (["--keys", key_file(tempfile.mkdtemp(), everyone[:-1]), "--data", folder], "no key for")):
            try:
                server.main(args)
            except SystemExit as e:
                assert words in str(e), e
            else:
                raise AssertionError("started without " + words)
    finally:
        server.open_radio, comms.gateway.connect = saved[0], saved[1]
        for name, value in (("MQTT_PASSWORD", saved[2]), ("MQTT_HOST", saved[3])):
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value
        for s, handler in handlers.items():
            signal.signal(s, handler)


def check_compose_files():
    """The image installs the locked libraries, gets all the code it copies, and secrets stay out of
    the image and Git."""
    read = lambda *path: open(os.path.join(ROOT, *path)).read()
    dockerfile = read("Dockerfile")
    assert "uv sync --locked" in dockerfile and "--extra gateway" in dockerfile
    assert "--no-default-groups" in dockerfile, "the image needs none of the research libraries"
    ignored = [line for line in read(".dockerignore").splitlines() if not line.startswith("#")]
    assert ignored[0] == "*" and {"**/*keys.json", "**/.env", "**/data"} <= set(ignored)
    copied = [source for line in re.findall(r"^COPY (?!--from)(.+)$", dockerfile, re.M) for source in line.split()[:-1]]
    for source in copied:
        assert os.path.exists(os.path.join(ROOT, source)) and "!" + source in ignored, source
    assert {"pyproject.toml", "uv.lock", "comms", "detection", "edge", "gateway", "hal"} <= set(copied)
    assert ".env" in read(".gitignore").split()
    compose = read("compose.yaml")
    assert '"127.0.0.1:${MQTT_PORT:-1883}:1883"' in compose, "the broker is published to this computer only"
    assert "./gateway/mosquitto:/config:ro" in compose and '"gateway.edge_based"' in compose
    assert '"127.0.0.1:${DASHBOARD_PORT:-8000}:8000"' in compose, "the dashboard too: reading it needs no token"
    assert "--group dashboard" in dockerfile and "dashboard.server:create_app" in compose and "dashboard" in copied
    assert compose.count("AQUA_DASHBOARD_TOKEN: ${AQUA_DASHBOARD_TOKEN:?") == 2, "no default token in this setup"
    assert "AQUA_ENABLE_OUTAGE_SIM:-0" in compose
    assert "devices:" not in compose, "the base setup needs no radio hardware"
    assert "/dev/ttyUSB0" in read("compose.esp8266.yaml") and "AQUA_RADIO_PORT: /dev/ttyUSB0" in read("compose.esp8266.yaml")
    assert "1700:1700" not in compose and ":1700\"" not in compose.replace("gateway:1700\"", ""), "never published"
    # the virtual nodes' image: only the required library, and every file it copies is sent
    edge = read("Dockerfile.edge")
    assert "uv sync --locked" in edge and "--no-default-groups" in edge and "--extra" not in edge
    sources = [x for line in re.findall(r"^COPY (?!--from)(.+)$", edge, re.M) for x in line.split()[:-1]]
    for source in sources:
        assert os.path.exists(os.path.join(ROOT, source)) and any(
            source == i[1:] or source.startswith(i[1:] + "/") for i in ignored if i.startswith("!")), source
    assert ignored.index("!simulation/webots/data/sensors.csv") > ignored.index("**/data")
    nodes = compose[compose.index("x-node:"):]
    assert "devices:" not in nodes and '"gpio"' not in nodes and '"lora"' not in nodes
    # one node or three, and a tree for each with exactly those nodes
    services = dict(re.findall(r"^  (node-[a-z0-9]+):.*?\n(.*?)(?=^  \S|^\S)", compose, re.M | re.S))
    for profile, tree in (("one-node", "virtual_1node.json"), ("three-nodes", "virtual_3node.json")):
        started = {re.search(r'"edge\.edge_based", "([^"]+)"', body).group(1) for body in services.values()
                   if profile in (re.search(r"profiles: \[(.+)\]", body) or
                                  re.search(r"x-node:.*?profiles: \[(.+?)\]", compose, re.S)).group(1)}
        assert started == Topology.load(os.path.join(ROOT, "gateway", tree)).devices, (profile, started)
    assert "allow_anonymous false" in read("gateway", "mosquitto", "mosquitto.conf")
    start = read("gateway", "mosquitto", "start.sh")        # or the broker cannot start a second time
    assert start.index('rm -rf "$dir"') < start.index("mosquitto_passwd -c")


if __name__ == "__main__":
    for check in (check_run, check_main, check_compose_files):
        check()
        print(f"{check.__name__}: ok")
    print("gateway server tests passed")
