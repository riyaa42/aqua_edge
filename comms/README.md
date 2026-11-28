# comms: how the Pi and the gateway communicate

The specification for subscribing to alerts, sensor data and status on both the Raspberry Pi nodes and the gateway computer, and the code that implements its MQTT side. It covers the **Pi–gateway link only**; the dashboard and Internet link are not part of it.

**Draft, version 1.** Each part is marked:

| Mark | Meaning |
|---|---|
| **Existing** | The radio protocol as `edge/`, `gateway/` and `hal/` already run it |
| **New** | MQTT, written in this module. Tested without hardware; see [Status](#status) |
| **Proposed** | Designed here, not written: the cloud-based solution |

## Overview

```
Raspberry Pi (one per node)                       Gateway computer (one per building)

 other programs on the Pi                          other programs on the computer
        ▲ subscribe                                        ▲ subscribe
 ┌──────┴───────┐                                  ┌───────┴──────┐
 │ MQTT broker  │                                  │ MQTT broker  │
 └──────▲───────┘                                  └───────▲──────┘
        │ publish copies (comms.edge)                      │ publish copies (comms.gateway)
 ┌──────┴────────────────────┐    LoRa, 433 MHz     ┌──────┴───────────────────┐
 │ node program              │ ◄──────────────────► │ gateway program          │
 │ checks, buzzer, SD queue  │  encrypted binary    │ cross-node checks, state │
 └───────────────────────────┘  packets             └──────────────────────────┘
```

MQTT needs TCP/IP, which the LoRa link does not have, so **MQTT never goes over the air**:

- Each end has its own MQTT 5 broker (Mosquitto). The two brokers are not connected to each other.
- Both use the same topic names. The radio carries only the encrypted packets of [`hal/packet.py`](../hal/packet.py); a [fixed table](#what-crosses-the-radio-existing) says what crosses.
- A subscription is local. Subscribing on one end never makes the other end send anything.

Three rules shape the design:

1. **The node and gateway programs are the bridge between MQTT and LoRa.** The SD queue (`Store` in `edge/aqua_edge.py`) and "save state, then acknowledge" (`Gateway.step`) are unchanged. A broker confirming receipt would not mean that data reached the SD card or was processed, so delivery does not depend on a broker.
2. **MQTT gets a copy, afterwards.** Publishing happens after the program's own call returns, never waits for the broker and never raises into the program. A stopped broker cannot delay a buzzer, a queued alert or an acknowledgement.
3. **MQTT is read-only in v1.** Neither program subscribes to anything, so nothing published can change what they do. In particular, nothing sent over MQTT can acknowledge a message and make a node delete it.

## Files

| File | Used by | What is in it |
|---|---|---|
| `common.py` | Both ends and subscribers | Topic names and filters, delivery settings, payloads and their mapping to radio messages, `Publisher` (never raises), `dispatcher` for subscribers |
| `edge.py` | Node program | `EdgeComms`: publishes a node's readings, messages, gateway commands, state and link |
| `gateway.py` | Gateway program | `GatewayComms`: publishes node messages, gateway decisions, commands, node links and building state |
| `client.py` | Anything that connects | `MqttLink`: the only code that uses an MQTT library (`paho-mqtt` 2) |

`edge.py` and `gateway.py` do not change `Node` or `Gateway`; they read them after each call.

## Topics (New)

All topics start with `aqua/v1/{building}/`. `{node}` is a node name from the [building tree](../gateway/README.md#building-tree), such as `B-203` or `B-TANK`; gateway-wide topics use `gateway` in its place.

| Topic | What it carries | QoS | Retained | Expires | Pi broker | Gateway broker |
|---|---|---|---|---|---|---|
| `{node}/sensor` | Latest readings, one per step | 0 | yes | 3 min | yes | cloud-based only (Proposed) |
| `{node}/alert` | A node's alert or its recovery | 1 | no | 7 days | yes | yes |
| `{node}/flag` | Tank supply flags | 1 | no | 7 days | yes | yes |
| `{node}/summary` | 15-minute summary | 1 | no | 7 days | yes | yes |
| `{node}/cmd` | Cross-node alert from the gateway, or its end | 1 | no | 7 days | when received | when issued |
| `{node}/state` | Active problems and gateway alerts | 1 | yes | never | yes | no |
| `{node}/link` | State of the radio link (differs per end) | 1 | yes | never | yes | yes |
| `{node}/status` | Node program online or offline | 1 | yes | never | yes | no |
| `gateway/status` | Gateway program online or offline | 1 | yes | never | no | yes |
| `gateway/event/{EVENT}` | A gateway decision | 1 | no | 7 days | no | yes |
| `gateway/state/{name}` | `building` and `unconfirmed` | 1 | yes | never | no | yes |

A Pi's broker holds **only that node's topics**: its own data and what the gateway sent it. It never gets another flat's data.

### Subscribing

| To get | Filter | Helper |
|---|---|---|
| Every alarm, on either end | `aqua/v1/B/+/alert`, `.../+/flag`, `.../+/cmd` | `alert_filters()` |
| Readings and summaries | `aqua/v1/B/+/sensor`, `.../+/summary` | `sensor_filters()` |
| One flat, everything | `aqua/v1/B/B-203/+` | `topic_filter("B-203")` |
| All gateway decisions | `aqua/v1/B/gateway/event/+` | `topic_filter("gateway", "event", "+")` |
| Only pipe leaks | `aqua/v1/B/gateway/event/PIPE_LEAK` | `topic_filter("gateway", "event", "PIPE_LEAK")` |

`gateway/event/+` is separate from the node alerts: a building-wide problem such as `PIPE_LEAK` or `NODE_SILENT` appears only there (and in `cmd` for the nodes told about it).

## Payloads (New)

UTF-8 JSON, one object per message. Every payload has `v` (spec version, 1). The node and the kind of message come from the topic and are not repeated inside.

| Field | Meaning |
|---|---|
| `s` | Message number. Node messages: the node's counter. Commands: the gateway's command number |
| `t` | Time in seconds. UTC Unix time on real hardware. For node messages and `cmd`, when it happened; for `state` and `link`, when published |
| `rx` | Gateway copies only: when the gateway received it |
| `a`, `f` | Alert or flag name, from `ALERTS` and `FLAGS` in `hal/packet.py` |
| `over` | `cmd`: 1 if the problem had already ended when sent |

Message fields keep their radio names (`FIELDS` in `hal/packet.py`), so a payload maps one-to-one to a packet. A value that is missing or not a number is left out; inside lists it is `null`. Subscribers must ignore fields they do not know.

```json
aqua/v1/B/B-203/alert {"v":1,"s":41,"t":1767225600,"a":"LEAK_NIGHT_FLOW","flow":0.0512}
aqua/v1/B/B-TANK/flag {"v":1,"s":12,"t":1767225660,"f":"SUPPLY_LOW","level":0.312,"inflow":0.0}
aqua/v1/B/B-203/summary {"v":1,"s":42,"t":1767226500,"i":1963584,"vol_l":12.3456,"p_min":10.4321,"p_mean":10.61}
aqua/v1/B/B-203/cmd {"v":1,"s":1767227000,"t":1767226500,"a":"PIPE_LEAK","over":0}
```

### Readings

`sensor` uses these names. A flat has the first two; the tank has the others.

| `sensor` field | Reading in the code | Unit |
|---|---|---|
| `flow` | `flow_lps` | L/s |
| `pressure` | `pressure_m` | metres of water |
| `level_m` | `level_m` | m |
| `inflow` | `inflow_lps` | L/s |
| `outflow` | `outflow_lps` | L/s |
| `ph` | `ph` | pH |
| `tds` | `tds_ppm` | ppm |
| `turb` | `turbidity_ntu` | NTU |
| `temp` | `temp_c` | °C |

```json
aqua/v1/B/B-203/sensor {"v":1,"t":1767225600,"flow":0.05,"pressure":10.4}
```

### Alert, flag and command names

- **Node alerts** (`alert`): `LEAK_NIGHT_FLOW`, `LEAK_CONTINUOUS_FLOW`, `LEAK_PRESSURE_DROP`, `ABNORMAL_CONSUMPTION`, `UNUSUAL_NIGHT_CONSUMPTION`, `SUDDEN_CONSUMPTION_CHANGE`, `PROLONGED_ZERO_FLOW`, `ABNORMAL_SUPPLY`, `QUALITY_RUNOFF`, `QUALITY_CHEMICAL`, `QUALITY_LIMIT_VIOLATION`, `QUALITY_TEMPORAL_ANOMALY`.
- **Valve reports** (`alert`): `VALVE_CLOSED`, `VALVE_OPENED`, `VALVE_FAULT`. What each result makes a node do: [notification levels](../edge/README.md#notification-levels-and-the-valve).
- **Recoveries** (`alert`): `FLAT_LEAK_CLEARED`, `CONSUMPTION_CLEARED`, `ZERO_FLOW_CLEARED`, `SUPPLY_FLOW_CLEARED`, `QUALITY_CLEARED`, `QUALITY_TEMPORAL_CLEARED`. What closes each: [recovery rules](../edge/README.md#recovery-and-restarts). Silence never closes a problem.
- **Flags** (`flag`): `SUPPLY_LOW`, `SUPPLY_RECOVERED`.
- **Gateway commands** (`cmd`): `PIPE_LEAK`, `PIPE_LEAK_CAUSE`, `SUPPLY_EVENT`, `LEAK_TANK_DRAIN`, `SUPPLY_UNCERTAIN`, ended by `PIPE_LEAK_CLEARED`, `PIPE_LEAK_CAUSE_CLEARED` or `SUPPLY_CLEARED`. Which nodes get what: [gateway alerts](../gateway/README.md#gateway-alerts-sent-to-nodes).
- **Operator orders** (`cmd`): `VALVE_CLOSE`, `VALVE_OPEN`, `ALERT_LED_ON`, `ALERT_LED_OFF` ([orders](../gateway/README.md#orders-to-a-node)).
- **Gateway events** (`gateway/event/{EVENT}`): `ORDER`, `VALVE_CLOSED`, `VALVE_OPENED`, `VALVE_FAULT`, `NEW_ALERT`, `REPEAT_ALERT`, `PIPE_LEAK`, `PIPE_LEAK_PAST`, `BALANCE_PAUSED`, `SUPPLY_EVENT`, `LEAK_TANK_DRAIN`, `SUPPLY_UNCERTAIN`, `SUPPLY_RECOVERED`, `NODE_SILENT`, `NODE_BACK`, `CLOCK_SKEW`, `CLOCK_OK`, `BAD_PACKET`, `BAD_READING`, `ORDER_EXPIRED`. The payload is the gateway's event without its name; it has a `level` (`ALERT`, `WARNING` or `INFO`), and its other fields depend on the event.

### State, link and status

| Topic | Fields |
|---|---|
| `{node}/state` (Pi) | `problems`: alerts reported and not yet reported recovered. `gateway_alerts`: active cross-node problems, name to start time |
| `{node}/link` (Pi) | `gateway_heard`: a beacon or reply within 15 s. `waiting`: messages not yet acknowledged |
| `{node}/link` (gateway) | `silent`: nothing heard for 45 min. `last_message`: when the latest new message arrived, `null` if none since the gateway started |
| `{node}/status`, `gateway/status` | `online`. Set to `false` by the broker (MQTT last will) if the program vanishes |
| `gateway/state/building` | `Gateway.snapshot()` without `t`: building status, ranked maintenance queue, incidents, water quality, leakage, supply, network health, nodes. Rebuilt on new data and at least every 60 s |
| `gateway/state/unconfirmed` | `commands`: those no node has confirmed yet, each with `s`, `node`, `a`, `t`, `over` |

`state`, `link` and `gateway/state/…` are published only when their content changes. `status` describes the program's connection to **its own broker**, not the radio: use `link` for that.

```json
aqua/v1/B/B-203/state {"v":1,"problems":["LEAK_NIGHT_FLOW"],"gateway_alerts":{"PIPE_LEAK":1767226500},"t":1767227001}
aqua/v1/B/B-203/link {"v":1,"gateway_heard":false,"waiting":3,"t":1767227001}
aqua/v1/B/gateway/status {"v":1,"online":true}
```

## What crosses the radio (Existing)

| Direction | Packet | MQTT topic | Bytes | Time on air |
|---|---|---|---|---|
| Node → gateway | `ALERT` | `{node}/alert` | 29–46 | 67–92 ms |
| Node → gateway | `FLAG` | `{node}/flag` | 39 | 82 ms |
| Node → gateway | `SUM` | `{node}/summary` | up to 47 | up to 92 ms |
| Node → gateway | `GOT` (confirms a command) | none: the command leaves `gateway/state/unconfirmed` | 27 | 67 ms |
| Gateway → node | `CMD` | `{node}/cmd` | 31 | 72 ms |
| Gateway → node | `ACK` | none: `waiting` drops in `{node}/link` | 27 | 67 ms |
| Gateway → all | `BEACON`, every 5 s | none: shows as `gateway_heard` in `{node}/link` | 14 | 46 ms |

`sensor`, `state`, `status` and `gateway/…` never cross the radio. Packet format and encryption: [`hal/README.md`](../hal/README.md#encryption). Radio settings and limits: `hal/lora433.py`.

## Delivery

### Over the radio (Existing)

This is the **end-to-end** guarantee.

- A node saves each `ALERT`, `FLAG` and `SUM` to its SD queue and deletes it only on the gateway's `ACK`.
- The gateway saves its state **before** sending the ACK. It tells duplicates and replays apart by node and `s`, acknowledges them again, and does not use them twice.
- A command is repeated, and kept across gateway restarts, until the node sends `GOT`. A node acts on each command number once, even after a reboot.
- Retries: alerts, flags and commands after 1–2 s, doubling to 4–8 s; summaries after 10–20 s, rising to 30–60 s. With the other end silent, alerts are retried about every 30 s and summaries about every 5 min.
- Order: alerts first, then flags. Summaries go only when no alert or flag is waiting, one at a time, oldest first.
- Duty cycle: 10% of an hour for alerts, flags, commands, ACKs and beacons; 8% for a node's summaries.

An alert:

```
node                                          gateway
 check fires → local action → saved to SD
 publish {node}/alert (Pi broker)
 ──────────────── ALERT ────────────────────► check, then save state
 ◄─────────────── ACK ─────────────────────── send ACK
 delete from the SD queue                     publish {node}/alert and gateway/event/NEW_ALERT
```

A cross-node command:

```
node                                          gateway
                                              decide → save state → send
 ◄─────────────── CMD ─────────────────────── publish {node}/cmd, state/unconfirmed
 act once → save → send GOT
 ───────────────── GOT ─────────────────────► remove from outbox
 publish {node}/cmd and {node}/state          publish state/unconfirmed
```

### Over MQTT (New)

QoS covers only the hop between one program and its broker. **MQTT is a live view, not the record**: the record is the SD queue, the gateway's saved state and its log.

- **Subscribers must accept duplicates.** For node messages use (node, `s`); for commands, `s`. QoS 1 can repeat a message, and a restarted node or gateway publishes its still-waiting messages or commands again.
- **To receive what you missed**, connect with the same client ID every time. `MqttLink` asks the broker to keep the session for 7 days; the broker then holds QoS 1 messages while you are away.
- **A new subscriber** gets the retained topics at once (state, link, status, and a reading under 3 min old), not past alerts.
- **Broker away:** the program carries on and reconnects by itself. Up to 1,000 QoS 1 messages wait in memory and newer ones are dropped; readings (QoS 0) are lost. After reconnecting, the current `state`, `link`, `status` and `gateway/state` are sent again.
- **What can be lost to subscribers:** messages waiting for the broker when the program stops, a gateway message acknowledged on the radio just before a crash and not yet published, and what Mosquitto had not yet written to disk when it died (the Docker setup writes every 60 s).

## Edge-based solution

Nodes decide their own alerts; the gateway decides what needs several nodes' data. This is the **Existing** radio behaviour plus the **New** MQTT copies. Readings stay on the Pi: the gateway receives alerts, flags and summaries, never raw readings. Local alarms and valve closing need neither the gateway nor either broker.

## Cloud-based solution (Proposed)

**None of this is written.** "Cloud" here means the gateway side, as in [`gateway/README.md`](../gateway/README.md): the Pi buffers and sends its readings, and the gateway runs all checks.

| Change | Detail |
|---|---|
| New packet `READ` | One per minute, appended to `KINDS` in `hal/packet.py`. Goes through the SD queue and is acknowledged like the others |
| New packet fields | `pressure`, `outflow` and `temp`, appended to `FIELDS`; the others exist. These are the `sensor` field names. A flat's packet: 37 bytes, 82 ms. The tank's: 62 bytes, 118 ms |
| Topics | The gateway publishes each accepted `READ` on `{node}/sensor`. Subscribers see the same topics as in the edge-based solution |
| Order | Oldest first, one at a time, because the checks count consecutive minutes |
| Airtime limits | `READ` is routine traffic (8%). The gateway must hold its ACKs for readings to the 8% limit too (today every ACK may use 10%), leaving room for alerts and commands |
| Decisions | The gateway runs the node checks and sends results back as `CMD` with the existing alert names. `COMMAND_ENDS` in `edge/aqua_edge.py` needs an entry for each node recovery |

Estimated load, before collisions and retries: the shared channel about 6% busy, the gateway transmitting about 3.3% of the time, a node 0.1–0.2%.

**Limits of this solution:**

- **No local alarm while the radio link or gateway is down.** This gives up the offline-first property of the edge-based solution.
- **Slow recovery.** After a 24 h outage, 21 nodes hold about 30,000 readings. At the gateway's ACK limit, catching up takes about **8 h at 10% and 12 h at 8%**, before collisions. Because readings go oldest first, decisions about the present wait that long. Sending several minutes in one packet is the likely fix; it is not designed in v1.

## Security

- **Radio (Existing):** every packet is encrypted and authenticated, except the beacon, which is only signed. What this does not hide or prevent: [`hal/README.md`](../hal/README.md#encryption).
- **MQTT (New):** the module sets no TLS. Keep each broker reachable only from its own computer (listen on, or publish to, `127.0.0.1`), turn anonymous access off, and give each program its own login with access rules. Opening a broker to a network needs TLS first, and is outside this version.
- **Access rules:** the publishing program may only write; viewers may only read. Whoever holds a publishing login can mislead subscribers with false messages, but cannot change what a node or gateway does or make one delete data.

## Why not CoAP

CoAP is not used in v1.

- **The radio link has no IP.** CoAP over raw LoRa needs an IP adaptation layer (such as SCHC) on both ends.
- **Its main benefits already exist here.** The packets are confirmed, retried and de-duplicated, and are smaller than CoAP with IP headers and security would be.
- **MQTT covers subscribing** on both ends, where IP is available.

Reconsider it if a constrained IP link is added (for example 6LoWPAN or NB-IoT), or if a device too small to run MQTT over TCP must be read directly. The topic path and JSON payload could then be offered as CoAP resources with Observe.

## Time and versions

- Real nodes use UTC Unix time. A node sets its clock only from a genuine `ACK` newer than the last one it used. The gateway's clock must be right and must not jump back across restarts.
- Packet lists (`KINDS`, `ALERTS`, `FLAGS`, `FIELDS`, `NAMES`) are append-only: a value's position is its number on air. Custom nodes instead use the `radio_id` (128–255) in the building tree.
- Adding a topic or payload field keeps `v1`. Removing or changing the meaning of one needs `aqua/v2`.

## Using the module

Install `paho-mqtt` with `uv sync --extra mqtt` (the `gateway` extra includes it). On the Pi, together with the hardware libraries: `uv sync --extra pi --extra mqtt`.

**Gateway:** the server does this itself when given `--mqtt-host`. [Run it with Docker Compose](../gateway/README.md#run-with-docker-compose), which also starts the broker.

**Node:** `edge-based-node` does not connect to MQTT yet. In code:

```python
from comms.edge import EdgeComms, connect

link = connect("B-203", username="node", password=password)   # the Pi's own broker
EdgeComms(node, link).attach()                                # node: a FlatNode or TankNode
```

Call `link.close()` on exit. A Mosquitto setup for a Pi (not tried on a Pi): in the configuration, `listener 1883 127.0.0.1`, `allow_anonymous false`, `password_file` (made with `mosquitto_passwd`) and `acl_file`, with this access file:

```
user node
topic write aqua/v1/B/B-203/#

user viewer
topic read aqua/v1/#
```

**Subscribe**, on either end:

```python
from comms.client import MqttLink
from comms.common import alert_filters, dispatcher

show = dispatcher(lambda topic, payload: print(topic.node, topic.channel, payload))
link = MqttLink("my-screen", username="viewer", password=password,
                subscriptions={f: show for f in alert_filters()})
```

Or with the Mosquitto tools: `mosquitto_sub -V 5 -u viewer -P <password> -t 'aqua/v1/B/#' -v`.

Give the filters in `subscriptions=` rather than adding them later, so messages the broker kept for you are not delivered before anything listens. Each running program needs its own client ID.

## Status

- `uv run test-comms` runs the module with real `Node` and `Gateway` objects, a fake radio, and fake MQTT objects. It checks that the radio traffic is identical with MQTT attached, broken or absent, and that this file's topic table and examples agree with the code.
- The gateway server, `paho-mqtt` and Mosquitto 2.1 were also run once by hand in containers with Podman, with a simulated radio and node: delivery, access rules, wrong password, a subscriber returning, a broker outage and restart, last will, and shutdown.
- **Not tested:** a broker on a Pi, the ESP8266 or any real radio, Docker itself, and long runs.

## Open questions

1. How should several readings share a packet in the cloud-based solution, and should live readings overtake a backlog?
2. Should cloud-based nodes keep a minimal local alarm for when the link is down?
3. `Gateway.log` and `Gateway.events` grow in memory for as long as the gateway runs. A long-running server needs them trimmed, and `GatewayComms` reads them by position.
4. Will anything need to read MQTT from another computer (for example a dashboard)? That needs TLS and new access rules.
