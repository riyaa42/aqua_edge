# Aqua Edge: Gateway

| File | Runs on | What it does |
|---|---|---|
| `core.py` | Gateway: laptop (ESP8266 over USB serial to an SX1278 433 MHz LoRa module) | Combines data from all nodes. Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check, saved state, and delegates analytics to `GatewayAnalytics` |
| `building_B.json` + `topology.py` | Gateway | The building's pipes as a tree of nodes, and its loader and checks ([Building tree](#building-tree)) |
| `analytics.py` | Gateway | Open incidents, closed by explicit recovery messages, cross-node root-cause correlation (tank contamination & main riser leak groups), composite risk scoring (0–100) & ranked maintenance queue, online vs. offline delivery-latency accounting, and `Gateway.snapshot()` JSON export |
| `edge_based.py` | Gateway | Server entry point, edge-based solution: nodes decide their own alerts, and the gateway decides what needs several nodes' data. It reaches the LoRa antenna through an ESP8266 (`../hal/gate_hal.py`), runs `core.py` and publishes over MQTT (`../comms/`). `uv run prod-edge-gateway` (same as `edge-based-gateway`), or [with Docker Compose](#run-with-docker-compose). Not tested on real hardware |
| `dashboard_link.py` | Gateway | Sends snapshots to the [dashboard](../dashboard/README.md) on its own thread, through `dashboard/shipper.py`, and collects its [valve reopen requests](../dashboard/API_SPEC.md#7-manual-valve-reopen) |
| `readings.py` | Gateway | Leaves out [readings](#the-gateway-node) that cannot be true |
| `flags.py` | Gateway | [Levels and expected lights](#the-gateway-node) for the dashboard, by the valve spec |
| `order.py` | Gateway | Command that sends a node a [valve or alert-LED order](#orders-to-a-node) |
| `virtual_1node.json`, `virtual_3node.json` | Gateway | Trees for the [virtual-node demos](../edge/README.md#run-virtual-nodes) |
| `mosquitto/` | Gateway | The MQTT broker's settings and access rules, used by [Docker Compose](#run-with-docker-compose) |
| `cloud_based.py` | Gateway | Server entry point, cloud-based solution: nodes only buffer and stream sensor data, and all ML and every decision happen on this side. Will reach the LoRa antenna through an ESP8266. `uv run cloud-based-gateway`. **Placeholder, not implemented yet** |

The rest of this README describes the gateway logic that exists today, which belongs to the edge-based solution.

Self-checks, from the repository root: `python3 -m gateway.topology`, `python3 -m gateway.core`.

## The gateway node

| Job | How |
|---|---|
| Sensor readings | The gateway has no sensors. It works from what nodes send: alerts, flags and 15-minute summaries, never raw samples. A reading that is not a number, or a negative litres or flow value, is left out as if the sensor gave nothing, and logged as `BAD_READING` |
| Cross-node checks | The building check, leak location and supply rule are in [`../detection/leakage/`](../detection/leakage/README.md) (`mass_balance.py`, `localize.py`, `supply.py`). `core.py` feeds them and sends their results to the nodes concerned |
| Flags | By [`../planning/valve_spec.md`](../planning/valve_spec.md): each incident, queue item and event has a `level`, and each node an expected `light` ([fields](../dashboard/API_SPEC.md#8-flags-added-to-the-snapshot)). They are labels; only the [commands below](#gateway-alerts-sent-to-nodes) close valves |
| Dashboard | Sends snapshots, and collects [reopen requests](../dashboard/API_SPEC.md#7-manual-valve-reopen) from the dashboard's button. No port is opened on the gateway |

This is a draft: it has run only on one computer, with network nodes.

## Building tree

The gateway is told the building's layout as a tree: `Gateway(hal, Topology.load(path))`. The default file is [`building_B.json`](building_B.json). **A parent is upstream of its children:** water passes the parent first. It is JSON because JSON nests naturally and Python reads it with no extra library (TOML is awkward for deep trees; YAML needs a dependency).

```json
{"schema_version": 1, "building": "B",
 "root": {"id": "B-TANK", "type": "tank", "valve": true, "children": [
   {"id": "B-D4", "type": "junction", "label": "4", "valve": false, "children": [
     {"id": "B-401", "type": "flat", "valve": true},
     {"id": "B-D3", "type": "junction", "label": "3", "valve": false, "children": [
       {"id": "B-301", "type": "flat", "valve": true}]}]}]}}
```

The example is cut short. Building B has this shape:

```
B-TANK (tank, valve)
└─ B-D4 "4"        ── B-401 .. B-404
   └─ B-D3 "3"     ── B-301 .. B-304
      └─ B-D2 "2"  ── B-201 .. B-204
         └─ B-D1 "1"     ── B-101 .. B-104
            └─ B-D0 "G"  ── B-G01 .. B-G04
```

| Top-level key | Meaning |
|---|---|
| `schema_version` | `1` |
| `building` | The building's name |
| `root` | The tank entry, with everything else under it. A one-node building may instead be a [lone flat](#one-node-and-custom-names) |

| Entry key | Needed | Meaning |
|---|---|---|
| `id` | Yes | Unique name. For `tank` and `flat` it is the node's radio name |
| `radio_id` | Custom names only | `tank` and `flat`: the number the node's packets carry, 128 to 255 ([custom names](#one-node-and-custom-names)) |
| `type` | Yes | `tank` (only as the root; none in a lone-flat tree), `junction` (a point in the pipes with no device) or `flat` |
| `valve` | Yes | `true` if a valve sits on the pipe **just before** this entry. It controls the water to this entry and everything under it |
| `children` | Junctions | The entries fed through this one. Optional for the tank (a tank alone is a one-node building); not allowed on a flat, whose meter would then count their water twice |
| `unmetered_homes` | No, tank only | `true` if the tank also feeds homes that are not in the tree. The gateway then **skips the building check and pipe-leak detection**: that water would look like a leak |
| `label` | No, junctions only | Short unique name used in gateway events (`"4"` .. `"G"`); defaults to the `id` |

Other keys are rejected, and so is a junction that uses a radio name. The loader stops with a message naming the problem.

**Valve flags in `building_B.json` are an assumption:** `true` for the tank and every flat, `false` for every junction, because no junction valve is documented (the water model has no valves). Set a junction to `true` where one is really fitted. On the gateway, valve flags are only used to record `isolation_valve` on a pipe leak: the nearest valve at or above the suspected junction. They do not decide which valves close: each node told of a [gateway problem](#gateway-alerts-sent-to-nodes) closes its own.

**What the gateway takes from the tree:** the list of nodes; which junction feeds each flat (the old "floor", no longer read from the flat's name); what is upstream of what, for locating pipe leaks; who is fed through a suspected junction; and the affected-flat count in the maintenance ranking. Pipe lengths, diameters and heights are not in it.

**Adding a node:** add its entry to the JSON file, with a `radio_id` if the name is not built in, give it a key, and give every device that needs it the new tree. Restart the gateway to load a changed tree. Saved silence timers and unconfirmed alerts for removed nodes are dropped.

**Limits:** at most one tank, and one tree per gateway. Branching pipes are allowed, and the self-check covers a two-branch example, but the pressure thresholds have only been tested on building B. The building check compares the tank with all flats together, not branch by branch.

### One node and custom names

Building B's names (`B-TANK`, `B-G01` to `B-404`) are built in: their radio numbers are their positions in `NAMES` in `../hal/packet.py`. **Any other `tank` or `flat` needs a `radio_id`**, a whole number from 128 to 255, used once in the tree. A custom name is 1 to 32 letters, digits, `_`, `.` or `-`, starts with a letter or digit, and is not `GATEWAY`.

Packets carry the number, not the name. **Every device must have the same number for the same node.** If they differ, the receiver rejects the packet (`BAD_PACKET` on the gateway). Changing a node's number means updating both ends.

A four-node demo, one of them the physical Pi:

```json
{"schema_version": 1, "building": "lab",
 "root": {"id": "LAB-TANK", "type": "tank", "valve": true, "radio_id": 128, "children": [
   {"id": "pi-flat", "type": "flat", "valve": true, "radio_id": 129},
   {"id": "virtual-1", "type": "flat", "valve": true, "radio_id": 130},
   {"id": "virtual-2", "type": "flat", "valve": true, "radio_id": 131}]}}
```

A one-node demo is the root alone, either a tank or a flat:

```json
{"schema_version": 1, "building": "demo",
 "root": {"id": "pi-flat", "type": "flat", "valve": true, "radio_id": 129}}
```

| Tree | What the gateway can check |
|---|---|
| Tank and flats | Everything. The pipe-leak thresholds were tested only on building B |
| Tank alone | What the tank reports, silent nodes, clock errors, repeats. **No building check or pipe leak** (there are no flats to compare with). Its incidents show `affected_flats: 0` but are ranked as affecting the whole building |
| Flat alone | What the flat reports, silent nodes, clock errors, repeats. No building check, pipe leak or supply event |

To use a tree:

1. **Keys.** [Make a key file](../hal/README.md#setting-up-keys) holding `GATEWAY` and every node in the tree.
2. **Gateway.** `--tree path.json` or `AQUA_TOPOLOGY_PATH` (the dashboard server reads the same variable). With Docker Compose, set `GATEWAY_TREE`. MQTT topics use the tree's `building` unless `--building` is given.
3. **Pi.** Copy the same tree to it: `uv run edge-based-node pi-flat --keys pi-flat_keys.json --tree lab.json --csv sensors.csv`. The tree decides whether the node runs the tank or the flat checks. The CSV needs columns named for this node, such as `pi-flat:flow_lps`.
4. **Virtual nodes** started the same way need nothing more. Code that builds a `FlatNode` or `TankNode` itself must first call `hal.packet.add_names(topology.radio_ids)`. The live simulation (`../simulation/live/`) still runs building B only.

**Use a new data folder (`--data`) for a different building.** Saved state remembers which message numbers were seen for each name, so a node reusing a name with a fresh counter would be treated as a replay. With Docker Compose the state is in the `gateway-data` volume, which stays when you change `GATEWAY_TREE`; `docker compose -p <another name> up` gives a building its own volumes. The dashboard also keeps what it was sent under the gateway's name.

## Running the server

The server needs a key file holding `GATEWAY` and every node in the building tree ([key setup](../hal/README.md#encryption)) and the [flashed ESP8266](../hal/README.md#gateway-modem-esp8266). It stops with a message if either is missing. The computer's clock must be right: nodes set theirs from it.

Without Docker, from the repository root:

```sh
uv run --no-default-groups --extra gateway prod-edge-gateway
```

It takes `GATEWAY_KEYS` (default `~/gateway_keys.json`), `GATEWAY_TREE` and `GATEWAY_SERIAL` from the environment or `.env`, keeps its state in `GATEWAY_DATA` (default `gateway-data/` in the repository) and installs the `gateway` libraries if they are missing. Options given on the command line win: `--keys`, `--data`, `--port` and the rest, as for `edge-based-gateway`.

`--udp-listen 127.0.0.1:1700` also takes nodes over the network (`../hal/udp.py`), and `--port none` runs without the ESP8266.

This runs without MQTT. To publish to a broker you run yourself, add `--mqtt-host 127.0.0.1 --mqtt-user gateway` and set `MQTT_PASSWORD`. It prints each gateway decision.

### Run with Docker Compose

This starts the gateway server, a Mosquitto broker and the [dashboard server](../dashboard/README.md) together, using `compose.yaml` and `Dockerfile` at the repository root. The gateway and dashboard share one image, in which `uv` installs the locked libraries: the project's required ones, the `gateway` extra and the `dashboard` group. It leaves out the `research` and `test` groups ([Python setup](../README.md#python-setup)). From the repository root:

```sh
cp .env.example .env      # set both passwords, the dashboard token and the full path of the key file
docker compose -f compose.yaml -f compose.esp8266.yaml up -d --build     # with the ESP8266
docker compose logs -f gateway
```

**`compose.yaml` alone uses no radio hardware.** For real nodes you must add `compose.esp8266.yaml` as above, every time (or set `COMPOSE_FILE=compose.yaml:compose.esp8266.yaml` in `.env`; not tried with Podman). To run with virtual nodes instead, leave it out and [pick a profile](../edge/README.md#run-virtual-nodes).

| In `.env` | Meaning |
|---|---|
| `MQTT_GATEWAY_PASSWORD` | The gateway's broker login `gateway`: may only publish |
| `MQTT_VIEWER_PASSWORD` | The login `viewer` for subscribers: may only read |
| `AQUA_DASHBOARD_TOKEN` | A long random secret, which the gateway sends with its data. For example `openssl rand -hex 32` |
| `AQUA_OPERATOR_TOKEN` | Optional: another secret, which turns on the dashboard's [valve reopen](../dashboard/API_SPEC.md#7-manual-valve-reopen) |
| `DASHBOARD_PORT` | The dashboard's port on this computer, default 8000 |
| `DASHBOARD_URL` | Send to a dashboard elsewhere instead of this setup's own (its full ingest address; use `https` over a network) |
| `DASHBOARD_GATEWAY_ID` | This gateway's name on the dashboard, default `building-<building>` |
| `AQUA_ENABLE_OUTAGE_SIM` | `1` allows the simulated Internet outage, off by default |
| `GATEWAY_KEYS` | Full path of the key file. Mounted read-only, never copied into the image |
| `GATEWAY_TREE` | The [building tree](#building-tree), default `./gateway/building_B.json`. Start a relative path with `./` |
| `GATEWAY_SERIAL` | With `compose.esp8266.yaml`: the ESP8266's port, default `/dev/ttyUSB0` |
| `MQTT_PORT` | Port on this computer, default 1883 |

- **Subscribe** from this computer at `127.0.0.1:1883` as `viewer`, or: `docker compose exec mqtt mosquitto_sub -V 5 -u viewer -P <password> -t 'aqua/v1/#' -v`. Topics and payloads: [`../comms/README.md`](../comms/README.md). The broker is not reachable from other computers.
- **Dashboard:** read it from this computer at `http://127.0.0.1:8000/api/v1/health` ([all addresses](../dashboard/README.md#running-it)). The image also builds the web page, served at `http://127.0.0.1:8000/`. It is not reachable from other computers.
- **State** is in the `gateway-data` and `dashboard-data` volumes. `docker compose down` keeps them; **`down -v` deletes them**, including the record of which packets were already seen, the commands not yet confirmed and the dashboard's history. Do not delete `gateway-data` alone: [the dashboard would ignore new data](../dashboard/README.md#how-the-gateway-sends). If you do, also set a new `DASHBOARD_GATEWAY_ID`.
- **With `compose.esp8266.yaml`**, the gateway container cannot start if the serial port does not exist.
- **After unplugging the ESP8266**, the container may not see the device again, or it may return as another port: check the port, then run `docker compose up -d --force-recreate gateway`.
- If the broker or dashboard stops, the gateway keeps working over the radio. It reconnects to the broker, and sends the dashboard what it kept.

**Tested** with Podman and `podman-compose`, using a copy of this setup with a simulated radio and no serial device. **Not tested:** Docker itself, and passing a real serial port into the container. With rootless Podman your user needs access to the port (the `uucp` or `dialout` group), and the container may also need that group kept (`group_add: [keep-groups]`).

## Gateway alerts sent to nodes

Nodes act by themselves on local anomalies (their own valve, buzzer and LED at once, without LoRa or Internet) and buffer messages for the gateway. What needs several nodes' data, the gateway also sends back to the affected nodes, as a signed command (`CMD`) repeated until the node confirms it (`GOT`) and kept across gateway restarts. The same results stay available to the dashboard through `Gateway.snapshot()`. Sending is on by default; `Gateway(..., enable_commands=False)` keeps results on the gateway only.

| Gateway result | Sent to |
|---|---|
| Pipe leak, and its end | The tank and every flat fed through the suspected junction (its floor and all below it: a leak at `B-D1` alerts floors 1 and G). **Only the tank** if the spot cannot be told |
| Supply event, leak draining the tank, uncertain supply, and recovery | The tank |
| A flat's pressure alert during a pipe leak | That flat (its leak alert may not be its own leak) |

The spot is decided once, when the pipe leak is first raised. It is an estimate: if the real leak is just above the suspected junction, the floor above is not alerted, and isolating it may need the next valve up. When the spot is unknown, which in tests includes medium (0.5 L/s) pipe leaks, no flat gets the pipe leak alert; a flat with its own pressure alert is still told the likely cause (last row).

Silent nodes, clock errors and new/repeat labels are only recorded. A node acts on each command once, even after a reboot. **These cross-node problems are red:** the node blinks R2, sounds the buzzer, **closes its valve** and calls `hal.on_gateway_alert(code, active)` ([rules](../edge/README.md#notification-levels-and-the-valve)). When the gateway ends a problem the light and buzzer follow, and the valve [reopens after the node's tolerance period](../edge/README.md#when-the-valve-reopens) if nothing else holds it, or at once by an [operator's order](#orders-to-a-node). A flat with [trial reopening](../edge/README.md#trial-reopening-flats) on may also open while red for its own alerts, but not while a gateway problem is in force. A pipe leak that cannot be placed therefore shuts the tank valve, and so the whole building's water. A problem that started and ended before it could be delivered still arrives, marked over, and raises no continuing alarm. A node that is off cannot be reached until it returns.

The beacon is signed with a building-wide key (`hal.key("GATEWAY")`). It only makes nodes send and never deletes data.

## Orders to a node

An operator can close or open a node's valve, or switch its alert LED, through the running gateway:

```sh
python -m gateway.order B-203 valve-close --data gateway-data
docker compose exec gateway python -m gateway.order B-203 valve-open --data /data
```

| Order | The node |
|---|---|
| `valve-close` | Closes its valve, red LED, buzzer. It stays shut until reopened by hand |
| `valve-open` | The reopen signal: opens its valve at once, whatever the conditions |
| `led-on` / `led-off` | Turns the ordered red LED and buzzer on or off. Red closes the valve, so **`led-on` closes it too**; after `led-off` it reopens once the [tolerance period](../edge/README.md#when-the-valve-reopens) has passed. `led-off` does not hide any other red condition |

- The command only leaves a request in the gateway's data folder, so only someone who can write there can use it. The one other source of orders is the [dashboard's reopen button](../dashboard/API_SPEC.md#7-manual-valve-reopen) (`valve-open` only), which the gateway collects from the dashboard server it is told to send to, so **whoever controls that server can open valves**. No port on the gateway takes orders, and MQTT stays read-only. **The gateway sends these orders only when an operator asks.** The problems it finds are [sent separately](#gateway-alerts-sent-to-nodes) and do close valves.
- Orders are signed with the node's key, repeated until confirmed and kept across restarts. A newer order replaces an undelivered opposite one. **A `valve-open` the node has not confirmed within 15 minutes of being asked for is dropped** (`ORDER_EXPIRED`), so it cannot open a valve much later. A node ignores an order older than its last valve or LED change, including a reopening at the node.
- Confirmation only means the node received it. What the valve did comes back as `VALVE_CLOSED`, `VALVE_OPENED` or `VALVE_FAULT` (an incident). `Gateway.snapshot()` shows each node's `valve`; the web page does not display it yet.
- **Update the gateway before the nodes:** an older gateway rejects the new packet names.

## Recovery and restarts

The gateway saves `<storage>_state.json` before sending ACKs (an older `_seen.json` is still read). It keeps 7 days of per-block data. Its in-memory message log and event list are not saved.

The recovery rules are in [`../edge/README.md`](../edge/README.md#recovery-and-restarts).
