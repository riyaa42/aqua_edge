"""The production scripts (README.md, "Deploy"):

    uv run prod-edge-edge          a Pi: its node, in the foreground
    uv run prod-edge-gateway       the building computer: the gateway alone, without containers

    uv run prod-up [--no-modem]     the building computer: gateway, MQTT broker and dashboard, in containers
    uv run prod-status / prod-logs / prod-down
    uv run prod-node-service ...    a Pi: prints a systemd service that keeps its node running

prod-up checks the settings first, starts compose.yaml with the ESP8266's file (never the virtual
nodes), and waits until the dashboard has heard from the gateway. No script prints a secret, and
none deletes data. Standard library and the project's own modules only.
"""
import argparse
import glob
import importlib.util
import json
import os
import posixpath
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


# --- the two programs in the foreground ---
# Both take the same options as the developer scripts. What is not given comes from the
# environment or .env, then from the defaults here.
def load_env():
    """Put .env into the environment. What is already there wins; empty values count as unset."""
    for name, value in settings().items():
        if value:
            os.environ.setdefault(name, value)


def configured_path(value):
    """A path from the environment or .env: relative ones start at the repository, as for Compose."""
    return os.path.join(ROOT, os.path.expanduser(value))


def need(script, extra, modules):
    """Start `script` again with the optional libraries of `extra`, if any are missing."""
    missing = [m for m in modules if importlib.util.find_spec(m) is None]
    if not missing:
        return
    uv = os.environ.get("UV") or shutil.which("uv")
    if not uv or os.environ.get("AQUA_PROD_RESTARTED"):
        sys.exit(f"{script}: not installed: {', '.join(missing)}. Run: uv sync --no-default-groups --extra {extra}")
    print(f"{script}: installing the '{extra}' libraries ({', '.join(missing)} missing)", flush=True)
    os.environ["AQUA_PROD_RESTARTED"] = "1"
    os.execv(uv, [uv, "run", "--project", ROOT, "--no-default-groups", "--extra", extra, script, *sys.argv[1:]])


def key_names(path):
    """The names in a key file, or an empty set if it cannot be read as one."""
    try:
        with open(path) as f:
            return set(json.load(f))
    except (OSError, ValueError, TypeError):
        return set()


def find_node_keys(name=None, home="~"):
    """This Pi's node and key file, from the key files in the home folder: a node's own file holds
    its key and GATEWAY's only (hal/README.md). ValueError unless exactly one fits."""
    home = os.path.expanduser(home)
    found = []
    for path in sorted(glob.glob(os.path.join(glob.escape(home), "*keys.json"))):
        names = key_names(path)
        nodes = names - {"GATEWAY"}
        if "GATEWAY" in names and len(nodes) == 1 and (name is None or name in nodes):
            found.append((nodes.pop(), path))
    if len(found) == 1:
        return found[0]
    what = f"for {name} " if name else ""
    if not found:
        raise ValueError(f"no node key file {what}in {home} (a file ending in keys.json with this node's key "
                         "and GATEWAY: hal/README.md, \"Setting up keys\"). Or set AQUA_NODE and AQUA_KEYS in .env")
    raise ValueError(f"several node key files {what}in {home}: {', '.join(p for _, p in found)}. "
                     "Set AQUA_NODE and AQUA_KEYS in .env")


def complete_node(a):
    """Fill in what prod-edge-edge was not told: the node's name and its key file."""
    a.name = a.name or os.environ.get("AQUA_NODE")
    if not a.keys and os.environ.get("AQUA_KEYS"):
        a.keys = configured_path(os.environ["AQUA_KEYS"])
    try:
        if not a.keys:
            name, a.keys = find_node_keys(a.name)
            a.name = a.name or name
        elif not a.name:
            nodes = key_names(a.keys) - {"GATEWAY"}
            if len(nodes) != 1:
                raise ValueError(f"cannot tell which node this is from {a.keys}: give its name, or set AQUA_NODE")
            a.name = nodes.pop()
    except ValueError as e:
        sys.exit(f"prod-edge-edge: {e}")
    modules = ["spidev"] if a.radio == "lora" else []
    if a.radio == "lora" or a.actuators == "gpio":     # the radio's reset pin is GPIO too
        modules += ["gpiozero"] + (["lgpio"] if sys.platform == "linux" else [])
    need("prod-edge-edge", "pi", modules)
    from edge.sensors import DATASET
    if a.actuators == "gpio" and os.path.abspath(a.csv) == os.path.abspath(DATASET):
        print("WARNING: no sensor drivers yet. This REPLAYS the simulated data and moves the real valve "
              "by it: do not connect a water supply", flush=True)
    print(f"node {a.name}, keys {a.keys}, saved state in {a.sd}", flush=True)


def edge_node():
    from edge import edge_based
    load_env()
    defaults = ["--sd", configured_path(os.environ.get("AQUA_SD") or os.path.join("~", "aqua"))]
    if os.environ.get("AQUA_CSV"):
        defaults += ["--csv", configured_path(os.environ["AQUA_CSV"])]
    if os.environ.get("AQUA_SLOT"):
        defaults += ["--slot", *os.environ["AQUA_SLOT"].split()]
    edge_based.main(defaults + sys.argv[1:], complete=complete_node)   # given options come last, so win


def gateway_defaults():
    """The gateway's options from the same settings prod-up uses (.env.example)."""
    env = os.environ.get
    keys = env("GATEWAY_KEYS") or os.path.join("~", "gateway_keys.json")
    defaults = ["--keys", configured_path(keys), "--data", configured_path(env("GATEWAY_DATA") or "gateway-data"),
                "--port", env("AQUA_RADIO_PORT") or env("GATEWAY_SERIAL") or "/dev/ttyUSB0"]
    tree = env("AQUA_TOPOLOGY_PATH") or env("GATEWAY_TREE")
    return defaults + (["--tree", configured_path(tree)] if tree else [])


def edge_gateway():
    from gateway import edge_based
    load_env()
    argv = gateway_defaults() + sys.argv[1:]
    a = edge_based.parse_args(argv)
    if not os.path.isfile(a.keys):
        sys.exit(f"prod-edge-gateway: no key file at {a.keys}: set GATEWAY_KEYS in .env or give --keys "
                 "(hal/README.md, \"Setting up keys\")")
    modules = (["serial"] if a.port != "none" else []) + (["paho"] if a.mqtt_host else [])
    need("prod-edge-gateway", "gateway", modules)
    edge_based.main(argv)


# --- a Pi ---
def unit(node, keys, csv, sd, slot, tree=None, user="pi", root=ROOT):
    """A systemd service running one node on a Pi, with the real radio and GPIO."""
    command = [posixpath.join(root, ".venv", "bin", "python"), "-m", "edge.edge_based", node,
               "--keys", keys, "--csv", csv, "--sd", sd, "--slot", str(slot[0]), str(slot[1]),
               "--radio", "lora", "--actuators", "gpio", *(["--tree", tree] if tree else [])]
    quoted = " ".join('"' + part.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'
                      for part in command)
    return f"""[Unit]
Description=Aqua Edge node {node}
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
                         "and moves the real valve by what is in it")
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
