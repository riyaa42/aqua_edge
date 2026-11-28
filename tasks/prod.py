"""The production scripts (README.md, "Deploy"):

    uv run prod-up [--no-modem]     the building computer: gateway, MQTT broker and dashboard, in containers
    uv run prod-status / prod-logs / prod-down
    uv run prod-node-service ...    a Pi: prints a systemd service that keeps its node running

On this branch they deploy the cloud-based solution (the control case): the building computer does
all the deciding, and a Pi only streams its readings.

prod-up checks the settings first, starts compose.yaml with the ESP8266's file (never the virtual
nodes), and waits until the dashboard has heard from the gateway. No script prints a secret, and
none deletes data. Standard library and the project's own modules only.
"""
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECRETS = ("MQTT_GATEWAY_PASSWORD", "MQTT_VIEWER_PASSWORD", "AQUA_DASHBOARD_TOKEN")
READY_S = 120       # how long after starting the dashboard may take to hear from the gateway


def settings(env_file=os.path.join(ROOT, ".env"), environ=os.environ):
    """What Compose will see: .env, overridden by the environment."""
    values = {}
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                name, eq, value = line.strip().partition("=")
                if eq and not name.startswith("#"):
                    value = value.strip()
                    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
                        value = value[1:-1]
                    else:
                        value = value.split(" #")[0].rstrip()
                    values[name.strip()] = value
    return {**values, **environ}


def problems(env, modem=True):
    """Why this setup must not be started: a list, empty if it may be."""
    found = [f"{name} is not set (a long random secret, for example: openssl rand -hex 32)"
             for name in SECRETS if not env.get(name)]
    if env.get("AQUA_DASHBOARD_TOKEN") == "dev-token":
        found.append("AQUA_DASHBOARD_TOKEN is the development default, which anyone can guess")
    if env.get("AQUA_OPERATOR_TOKEN") and env.get("AQUA_OPERATOR_TOKEN") == env.get("AQUA_DASHBOARD_TOKEN"):
        found.append("AQUA_OPERATOR_TOKEN must differ from AQUA_DASHBOARD_TOKEN")
    if env.get("AQUA_ENABLE_OUTAGE_SIM", "0") not in ("", "0", "false", "no"):
        found.append("AQUA_ENABLE_OUTAGE_SIM lets the dashboard refuse data for demos: remove it")
    if env.get("COMPOSE_PROFILES"):
        found.append("COMPOSE_PROFILES would start virtual nodes with made-up readings: remove it")
    path = lambda p: os.path.join(ROOT, os.path.expanduser(p))
    tree = path(env.get("GATEWAY_TREE") or os.path.join("gateway", "building_B.json"))
    keys = env.get("GATEWAY_KEYS")
    if not keys:
        found.append("GATEWAY_KEYS is not set: the full path of the gateway's key file (hal/README.md)")
    elif not os.path.isabs(keys) or not os.path.isfile(keys):
        found.append(f"GATEWAY_KEYS must be the full path of an existing file: {keys}")
    if not os.path.isfile(tree):
        found.append(f"no building tree at {tree}")
    elif keys and os.path.isfile(keys):
        from gateway.edge_based import load_gateway_keys
        from gateway.topology import Topology
        try:
            load_gateway_keys(keys, Topology.load(tree))
        except (OSError, ValueError, SystemExit) as e:
            found.append(str(e))
    if modem and not os.path.exists(env.get("GATEWAY_SERIAL") or "/dev/ttyUSB0"):
        found.append(f"no ESP8266 at {env.get('GATEWAY_SERIAL') or '/dev/ttyUSB0'}: plug it in or set "
                     "GATEWAY_SERIAL (--no-modem starts without radio, to rehearse)")
    return found


def compose_command():
    if shutil.which("docker") and subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("podman-compose"):
        return ["podman-compose"]
    sys.exit("neither `docker compose` nor `podman-compose` was found")


def compose(*args, modem=False):
    files = ["-f", "compose.yaml"] + (["-f", "compose.esp8266.yaml"] if modem else [])
    return subprocess.call([*compose_command(), *files, *args], cwd=ROOT)


def heard_from_gateway(port, since):
    """Whether the dashboard has had data from the gateway since `since`."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/api/v1/gateways", timeout=3) as r:
            return any(g["last_received_at_utc"] >= since for g in json.load(r)["gateways"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def up():
    ap = argparse.ArgumentParser(prog="prod-up", description="Deploy the gateway, MQTT broker and dashboard.")
    ap.add_argument("--no-modem", action="store_true", help="without the ESP8266: no node can be heard")
    ap.add_argument("--check", action="store_true", help="only check the settings")
    a = ap.parse_args()
    env = settings()
    found = problems(env, modem=not a.no_modem)
    if found:
        sys.exit("not deploying. Fix these in .env (see .env.example):\n  - " + "\n  - ".join(found))
    if not env.get("AQUA_OPERATOR_TOKEN"):
        print("note: AQUA_OPERATOR_TOKEN is not set, so the dashboard's valve reopen button is off")
    if a.check:
        print("settings are complete")
        return
    started = time.time()
    if compose("up", "-d", "--build", modem=not a.no_modem):
        sys.exit("compose could not start the services")
    port = env.get("DASHBOARD_PORT") or "8000"
    print("waiting for the dashboard to hear from the gateway ...", flush=True)
    while not heard_from_gateway(port, started):
        if time.time() - started > READY_S:
            sys.exit(f"the dashboard has heard nothing from the gateway after {READY_S} s: run `uv run prod-logs`")
        time.sleep(2)
    print(f"deployed. Dashboard: http://127.0.0.1:{port}/  (this computer only)")
    if a.no_modem:
        print("started WITHOUT the radio: no node can be heard")


def down():
    if any(arg in ("-v", "--volumes") or arg.startswith("--volumes=") for arg in sys.argv[1:]):
        sys.exit("prod-down keeps the data: it will not remove volumes")
    sys.exit(compose("down", *sys.argv[1:]))


def status():
    sys.exit(compose("ps", *sys.argv[1:]))


def logs():
    sys.exit(compose("logs", *(sys.argv[1:] or ["-f"])))


# --- a Pi ---
def unit(node, keys, csv, sd, slot, tree=None, user="pi", root=ROOT):
    """A systemd service running one node on a Pi, with the real radio and GPIO."""
    command = [os.path.join(root, ".venv", "bin", "python"), "-m", "edge.cloud_based", node,
               "--keys", keys, "--csv", csv, "--sd", sd, "--slot", str(slot[0]), str(slot[1]),
               "--radio", "lora", "--actuators", "gpio", *(["--tree", tree] if tree else [])]
    quoted = " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
                      for part in command)
    return f"""[Unit]
Description=Aqua Edge node {node} (cloud-based: streams readings)
After=local-fs.target

[Service]
User={user}
WorkingDirectory={root}
Environment=PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
ExecStart={quoted}
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


def node_service():
    ap = argparse.ArgumentParser(
        prog="prod-node-service",
        description="Print a systemd service for this Pi's node. Nothing is installed. First, on the Pi: "
                    "uv sync --no-default-groups --extra pi")
    ap.add_argument("name", help="node name, e.g. B-203 or B-TANK")
    ap.add_argument("--keys", required=True, help="this node's key file")
    ap.add_argument("--csv", required=True,
                    help="the sensor data. There are no sensor drivers yet: the node REPLAYS this file, "
                         "and the real valve is moved by what is in it")
    ap.add_argument("--sd", required=True, help="folder for the node's queue and saved state")
    ap.add_argument("--slot", nargs=2, type=int, required=True, metavar=("INDEX", "COUNT"))
    ap.add_argument("--tree", help="the building tree, if not building B")
    ap.add_argument("--user", default="pi", help="the account the node runs as (needs GPIO and SPI)")
    a = ap.parse_args()
    from edge.sensors import sensor_reader
    from gateway.topology import BUILDING_B, Topology
    from hal.packet import add_names, load_keys
    files = {k: os.path.abspath(v) for k, v in (("keys", a.keys), ("csv", a.csv), ("sd", a.sd))}
    tree = a.tree and os.path.abspath(a.tree)
    try:
        topology = Topology.load(tree or BUILDING_B)
        add_names(topology.radio_ids)
        if a.name not in topology.devices:
            raise ValueError(f"{a.name} is not a tank or flat in the building tree")
        if not {a.name, "GATEWAY"} <= set(load_keys(files["keys"])):
            raise ValueError(f"{files['keys']} needs a key for {a.name} and for GATEWAY")
        sensor_reader(a.name, files["csv"])
    except (OSError, ValueError) as e:
        sys.exit(f"no service written: {e}")
    if not 0 <= a.slot[0] < a.slot[1]:
        sys.exit("no service written: --slot needs 0 <= INDEX < COUNT")
    if any("\n" in v for v in (*files.values(), a.user, tree or "")):
        sys.exit("no service written: a value has a line break")
    print(unit(a.name, files["keys"], files["csv"], files["sd"], a.slot, tree, a.user), end="")
    service = f"aqua-edge-{a.name}.service"
    print(f"""
The service is above, on standard output. To install it, save that output to a file, then:
  sudo install -m 644 <that file> {shlex.quote('/etc/systemd/system/' + service)}
  sudo systemctl daemon-reload && sudo systemctl enable --now {shlex.quote(service)}
Sensor data is REPLAYED from {files['csv']}: there are no sensor drivers yet.""", file=sys.stderr)
