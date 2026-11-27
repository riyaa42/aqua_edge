# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. There are no valves yet: the response to a problem is the buzzer, the LED and an alert. A node sounds `hal.buzzer` for its own alerts and for gateway alerts that concern it. There is a hook for valve control (`hal.on_gateway_alert`) that closes nothing. The simulation (`../simulation/`) plugs in a simulated hardware layer; real hardware has its own: `communication/hal/pi_hal.py` for the Raspberry Pi 3B nodes (USB powered; SX1278 and buzzer drivers, not verified on hardware in these tests). The laptop gateway's layer, which exchanges packets over USB serial with an ESP8266 (the interface to its LoRa module), and the ESP8266 sketch are not in the repository yet, so **no gateway message reaches a real node yet**.

| File | Runs on | What it does |
|---|---|---|
| `edge_node/aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi 3B | Composable `EdgeRuntime` (`flat`, `tank`, `combined` modes) + `FlatNode`/`TankNode` presets. Learns Day-1 baselines (saved hourly to SD), runs leak + abnormal-consumption checks (`flat`), water quality + tank checks (`tank`), or all 3 pipelines jointly (`combined` for resource validation), with atomic `Store` buffering and TDMA/CSMA-CA LoRa scheduling |
| `edge_node/quality_model.py` + `quality_model.json` + `temporal_model.json` | Edge node | Offline 3-module water-quality stack (pure Python, `int8`): **Module 1** (`check_standards`, authoritative IS 10500 / *TrinkwV* limits), **Module 2** (`TemporalModel`, 432 B `int8` Temporal AE for subtle temporal anomalies), and **Module 3** (`QualityModel`, 324 B `int8` 4-feature classifier: `NORMAL` / `RUNOFF` / `CHEMICAL`) |
| `communication/packet.py` + `communication/hal/` | Nodes and gateway | Radio packets: compact binary (about a third of the JSON size), signed with a 4-byte tag (HMAC-SHA256) using a key shared between each node and the gateway; a message counter that never repeats, plus hardware abstraction (`hal/`) |
| `communication/lora433.py` | Nodes and gateway | SX1278 433 MHz radio settings, time on air, transmit-time limit, retry timing |
| `communication/offline_radio.py` | Tests | Lets a fake radio with no airtime deliver replies within one step |
| `gateway/gateway.py` | Gateway: laptop (ESP8266 over USB serial to an SX1278 433 MHz LoRa module) | Combines data from all nodes. Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check, saved state, and delegates analytics to `GatewayAnalytics` |
| `gateway/building_B.json` + `gateway/topology.py` | Gateway | The building's pipes as a tree of nodes, and its loader and checks ([Building tree](#building-tree)) |
| `gateway/gateway_analytics.py` | Gateway | Open incidents, closed by explicit recovery messages, cross-node root-cause correlation (tank contamination & main riser leak groups), composite risk scoring (0–100) & ranked maintenance queue, online vs. offline delivery-latency accounting, and `Gateway.snapshot()` JSON export |
| `tests/` | Dev / CI | Unit & integration tests (`tests/test_gateway_analytics.py`, `tests/test_unified_runtime.py`) |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes, first tried in each node's time slot.
Self-checks: `python3 firmware/edge_node/aqua_edge.py`, `python3 firmware/edge_node/quality_model.py`, `python3 firmware/communication/packet.py`, `python3 firmware/gateway/topology.py`, `python3 firmware/gateway/gateway.py`.

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Abnormal consumption detection: [`../abnormal_consumption/README.md`](../abnormal_consumption/README.md).
Water-quality model, data and test results: [`../tinyml/README.md`](../tinyml/README.md).

## Radio and instant delivery

**One SX1278 per device.** It listens on one channel and one spreading factor at a time and cannot receive while sending, so the whole building shares 433.175 MHz, SF7, 125 kHz, CR 4/5, 8-symbol preamble (`lora433.py`). Power is set to 10 dBm, and each device limits itself to 10% of an hour on air (summaries pause at 8%), matching India's WPC **G.S.R. 347(E) (May 9, 2022)** licence exemption for 433.05–434.79 MHz (`10 mW ERP` max, `10%` duty cycle, no 10 kHz bandwidth cap, integral antenna). A 10 dBm module setting is not automatically 10 mW radiated power (antenna gain and cable loss count), and WPC Equipment Type Approval (ETA) applies to commercial/imported hardware.

**Timing.** A node has two entry points: `step()` once a minute (sensors and checks) and `radio_poll()`, to be called when a packet arrives and at the time it returns. The gateway's `step()` works the same way.

| Case | What happens |
|---|---|
| A node raises an alert or tank flag | Local buzzer and LED at once (no gateway needed); the message is sent in the same call |
| No reply, gateway heard in the last 15 s | Sent again after 1-2 s, then 2-4 s, then every 4-8 s |
| No reply, gateway away | Kept on the SD card; tried about every 30 s (summaries every 5 min) |
| Gateway comes back (beacon after silence, or after a restart) | Stored alerts go at once, each node 0.15 s after the one before; summaries follow from 3.5 s, one per acknowledgement, oldest first |
| Gateway running | Beacon every 5 s and at startup (about 0.9% of its time on air) |

These cover delivery after a decision. Detection windows (for example 3 h of continuous flow, or two 15-minute blocks for a pipe leak) are unchanged, and repeated packet loss can still delay a message. **None of this timing has been measured on real radios or in a simulation with airtime and collisions:** the tests use a fake radio with no airtime, and the live simulation has not been updated for it.

## Hybrid Physical Pi 3B + Dockerized Multi-Node Setup

- **1 physical node** (`Raspberry Pi 3B` + `SX1278 433 MHz` + `RTC` + `microSD` + `buzzer`) streams one node's dataset/WNTR rows and transmits signed `packet.py` frames over LoRa to the **SX1278 → ESP8266 → USB serial** laptop gateway.
- **20 virtual nodes** run in **Docker containers** using the same `EdgeRuntime` and `packet.py` framing, feeding into the same `Gateway` instance so physical and virtual nodes are indistinguishable to `Gateway` and `GatewayAnalytics` (and from there to the dashboard).

## Building tree

The gateway is told the building's layout as a tree: `Gateway(hal, Topology.load(path))`. The default file is [`gateway/building_B.json`](gateway/building_B.json). **A parent is upstream of its children:** water passes the parent first. It is JSON because JSON nests naturally and Python reads it with no extra library (TOML is awkward for deep trees; YAML needs a dependency).

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
| `root` | The tank entry; everything else hangs under it |

| Entry key | Needed | Meaning |
|---|---|---|
| `id` | Yes | Unique name. For `tank` and `flat` it is the node's radio name |
| `type` | Yes | `tank` (the root, exactly one), `junction` (a point in the pipes with no device) or `flat` |
| `valve` | Yes | `true` if a valve sits on the pipe **just before** this entry. It controls the water to this entry and everything under it |
| `children` | Junctions | The entries fed through this one. Optional for the tank (the tree needs at least one flat); not allowed on a flat, whose meter would then count their water twice |
| `label` | No, junctions only | Short unique name used in gateway events (`"4"` .. `"G"`); defaults to the `id` |

Other keys are rejected, and so is a junction that uses a radio name. The loader stops with a message naming the problem.

**Valve flags in `building_B.json` are an assumption:** `true` for the tank and every flat, `false` for every junction, because no junction valve is documented (the water model has no valves). Set a junction to `true` where one is really fitted. Valve flags are only used to record `isolation_valve` on a pipe leak: the nearest valve at or above the suspected junction. Nothing opens or closes.

**What the gateway takes from the tree:** the list of nodes; which junction feeds each flat (the old "floor", no longer read from the flat's name); what is upstream of what, for locating pipe leaks; who is fed through a suspected junction; and the affected-flat count in the maintenance ranking. Pipe lengths, diameters and heights are not in it.

**Adding a node:** add its entry to the JSON file. If the radio name is new, also append it to the end of `NAMES` in `communication/packet.py` (packets carry a node's position in that list, so never reorder it), give it a key, and update every device. Restart the gateway to load a changed tree. Saved silence timers and unconfirmed alerts for removed nodes are dropped.

**Limits:** one tank and one tree per gateway. Branching pipes are allowed, and the self-check covers a two-branch example, but the pressure thresholds have only been tested on building B. The building check compares the tank with all flats together, not branch by branch. No program yet starts the gateway on the real laptop.

## Gateway alerts sent to nodes

Nodes act by themselves on local anomalies (triggering their own buzzer/LED immediately without LoRa or Internet) and buffer messages for the gateway. What needs several nodes' data, the gateway also sends back to the affected nodes, as a signed command (`CMD`) repeated until the node confirms it (`GOT`) and kept across gateway restarts. The same results stay available to the dashboard through `Gateway.snapshot()`. Sending is on by default; `Gateway(..., enable_commands=False)` keeps results on the gateway only.

| Gateway result | Sent to |
|---|---|
| Pipe leak, and its end | The tank and every flat fed through the suspected junction (its floor and all below it: a leak at `B-D1` alerts floors 1 and G). **Only the tank** if the spot cannot be told |
| Supply event, leak draining the tank, uncertain supply, and recovery | The tank |
| A flat's pressure alert during a pipe leak | That flat (its leak alert may not be its own leak) |

The spot is decided once, when the pipe leak is first raised. It is an estimate: if the real leak is just above the suspected junction, the floor above is not alerted, and isolating it may need the next valve up. When the spot is unknown, which in tests includes medium (0.5 L/s) pipe leaks, no flat gets the pipe leak alert; a flat with its own pressure alert is still told the likely cause (last row).

Silent nodes, clock errors and new/repeat labels are only recorded. A node acts on each command once, even after a reboot. A new problem sounds the buzzer, turns the LED red and calls `hal.on_gateway_alert(code, active)`. **Nothing closes a valve**; that hook is where it would go. A problem that started and ended before it could be delivered still arrives, marked over, and raises no continuing alarm. A node that is off cannot be reached until it returns.

The beacon is signed with a building-wide key (`hal.key("GATEWAY")`). It only makes nodes send and never deletes data.

## Recovery and restarts

A problem is closed only by an explicit recovery, never by silence. Each rule needs 10 minutes of readings in a row (a missing reading restarts the count):

| Problem | Node says it is over when | Message |
|---|---|---|
| Flat leak (night, continuous, pressure) | flow is at most 0.005 L/s (water off). 05:00 passing or the pressure recovering is not enough | `FLAT_LEAK_CLEARED` |
| High, night or sudden-change use | none of the reported conditions still holds (the 6-hour one needs 6 hours of readings to judge) | `CONSUMPTION_CLEARED` |
| No water use for 24 h | water has been used again | `ZERO_FLOW_CLEARED` |
| Abnormal tank outflow | outflow is back under this hour's limit | `SUPPLY_FLOW_CLEARED` |
| Low tank | level is no longer low | `SUPPLY_RECOVERED` flag |
| Water quality | existing quality rules | `QUALITY_CLEARED`, `QUALITY_TEMPORAL_CLEARED` |

The gateway closes the pipe leak (2 tight blocks in a row), silence (`NODE_BACK`) and clock errors (`CLOCK_OK`) itself. A flat leak that never lets the water stop stays open. A node saves its open problems (`<node>_open.json`) and can report recovery after a reboot, with two limits: the consumption detector's baseline and the tank's learned `min_level` are not saved (a rebooted tank uses its fallback limit), and the water-quality monitor's state is not saved, so a quality problem open across a tank reboot may get no clearing message.

The gateway saves `<storage>_state.json` before sending ACKs (an older `_seen.json` is still read). It keeps 7 days of per-block data. Its in-memory message log and event list are not saved.

## Deployment Topology vs. Combined Three-Pipeline Resource Validation

1. **Actual deployment topology:** `FlatNode` (`mode="flat"`, 20 nodes) runs flow + pressure leak detection (`LEAK_NIGHT_FLOW`, `LEAK_CONTINUOUS_FLOW`, `LEAK_PRESSURE_DROP`) and abnormal consumption (`StatisticalZeroFlowDetector`). `TankNode` (`mode="tank"`, 1 node) runs water-quality inference (`QualityMonitor` Modules 1–3) and tank outflow/level checks (`ABNORMAL_SUPPLY`, `SUPPLY_LOW`). Flat nodes without water-quality probes never run water-quality inference.
2. **Combined software resource-validation configuration:** `EdgeRuntime(..., mode="combined")` executes all three pipelines (leak + abnormal consumption + water quality/tank) concurrently in one process on replayed streams (`test_unified_runtime.py`).
3. **Host-machine resource measurements** (`Windows-11 AMD64`, Python `3.12.16`; **not** physical Raspberry Pi 3B measurements):
   - `flat` mode: `266.76 us/step`, `3748.7 steps/s`, `159.77 KB` peak `tracemalloc` RAM.
   - `tank` mode: `799.88 us/step`, `1250.2 steps/s`, `169.64 KB` peak `tracemalloc` RAM.
   - `combined` mode: `907.81 us/step` (`0.00133%` of 1-min budget), `1101.5 steps/s`, `185.51 KB` peak `tracemalloc` RAM (`+25.74 KB` over `flat`, `+15.87 KB` over `tank`), `9.42 us` per 34-byte `packet.pack`.
   - **Flash / storage footprint:** `78.67 KB` combined code + model JSONs (`quality_model.json` 1,450 B, `temporal_model.json` 3,029 B), `4,445 B` baseline JSON, `~11.6 KB` / 24h (`~81.1 KB` / 7d) `Store` retention vs. `505.02 GB` free host disk.
4. **Physical validation status & limitations:** Replayed streams (Building B WNTR hydraulics + synthetic `E4`/`E5` water quality) are not co-located physical sensor measurements; physical Raspberry Pi 3B benchmarking remains pending.

Run the gateway analytics tests and unified runtime replay & benchmark:
```bash
uv run python firmware/tests/test_gateway_analytics.py
uv run python firmware/tests/test_unified_runtime.py
```
