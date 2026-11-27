# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. There are no valves yet: the response to a problem is the buzzer, the LED and an alert. The firmware calls an optional `hal.buzzer` and has a hook for valve control (`hal.on_gateway_alert`) that closes nothing; no hardware layer drives a real buzzer yet. The simulation (`../simulation/`) plugs in a simulated hardware layer; the real hardware will get its own: one for the Raspberry Pi 3B nodes (USB powered), and one on the gateway laptop that exchanges packets over USB serial with an ESP8266, the interface to the gateway's LoRa module.

| File | Runs on | What it does |
|---|---|---|
| `edge_node/aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi 3B | Composable `EdgeRuntime` (`flat`, `tank`, `combined` modes) + `FlatNode`/`TankNode` presets. Learns Day-1 baselines (saved hourly to SD), runs leak + abnormal-consumption checks (`flat`), water quality + tank checks (`tank`), or all 3 pipelines jointly (`combined` for resource validation), with atomic `Store` buffering and TDMA/CSMA-CA LoRa scheduling |
| `edge_node/quality_model.py` + `quality_model.json` + `temporal_model.json` | Edge node | Offline 3-module water-quality stack (pure Python, `int8`): **Module 1** (`check_standards`, authoritative IS 10500 / *TrinkwV* limits), **Module 2** (`TemporalModel`, 432 B `int8` Temporal AE for subtle temporal anomalies), and **Module 3** (`QualityModel`, 324 B `int8` 4-feature classifier: `NORMAL` / `RUNOFF` / `CHEMICAL`) |
| `communication/packet.py` + `communication/hal/` | Nodes and gateway | Radio packets: compact binary (about a third of the JSON size), signed with a 4-byte tag (HMAC-SHA256) using a key shared between each node and the gateway; a message counter that never repeats, plus hardware abstraction (`hal/`) |
| `communication/lora433.py` | Nodes and gateway | SX1278 433 MHz radio settings, time on air, transmit-time limit, retry timing |
| `communication/offline_radio.py` | Tests | Lets a fake radio with no airtime deliver replies within one step |
| `gateway/gateway.py` | Gateway: laptop (ESP8266 over USB serial to an SX1278 433 MHz LoRa module) | Combines data from all nodes. Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check, saved state, and delegates analytics to `GatewayAnalytics` |
| `gateway/gateway_analytics.py` | Gateway | Open incidents, closed by explicit recovery messages, cross-node root-cause correlation (tank contamination & main riser leak groups), composite risk scoring (0–100) & ranked maintenance queue, online vs. offline delivery-latency accounting, and `Gateway.snapshot()` JSON export |
| `tests/` | Dev / CI | Unit & integration tests (`tests/test_gateway_analytics.py`, `tests/test_unified_runtime.py`) |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes, first tried in each node's time slot.
Self-checks: `python3 firmware/edge_node/aqua_edge.py`, `python3 firmware/edge_node/quality_model.py`, `python3 firmware/communication/packet.py`, `python3 firmware/gateway/gateway.py`.

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Abnormal consumption detection: [`../abnormal_consumption/README.md`](../abnormal_consumption/README.md).
Water-quality model, data and test results: [`../tinyml/README.md`](../tinyml/README.md).

## Radio and instant delivery

**One SX1278 per device.** It listens on one channel and one spreading factor at a time and cannot receive while sending, so the whole building shares 433.175 MHz, SF7, 125 kHz, CR 4/5, 8-symbol preamble (`lora433.py`). Power is set to 10 dBm, and each device limits itself to 10% of an hour on air (summaries pause at 8%). **These two limits come from secondary summaries of India's 2022 licence exemption for 433.05-434.79 MHz and are not checked against the official gazette.** A 10 dBm module setting is not automatically 10 mW radiated power (antenna gain and cable loss count), and equipment approval may apply.

**Timing.** A node has two entry points: `step()` once a minute (sensors and checks) and `radio_poll()`, to be called when a packet arrives and at the time it returns. The gateway's `step()` works the same way.

| Case | What happens |
|---|---|
| A node raises an alert or tank flag | Local buzzer and LED at once (no gateway needed); the message is sent in the same call |
| No reply, gateway heard in the last 15 s | Sent again after 1-2 s, then 2-4 s, then every 4-8 s |
| No reply, gateway away | Kept on the SD card; tried about every 30 s (summaries every 5 min) |
| Gateway comes back (beacon after silence, or after a restart) | Stored alerts go at once, each node 0.15 s after the one before; summaries follow from 3.5 s, one per acknowledgement, oldest first |
| Gateway running | Beacon every 5 s and at startup (about 0.9% of its time on air) |

These cover delivery after a decision. Detection windows (for example 3 h of continuous flow, or two 15-minute blocks for a pipe leak) are unchanged, and repeated packet loss can still delay a message. **None of this timing has been measured on real radios or in a simulation with airtime and collisions:** the tests use a fake radio with no airtime, and the live simulation has not been updated for it.

## Gateway alerts sent to nodes

Nodes act by themselves on what they can detect. What needs several nodes' data, the gateway sends back as a signed command (`CMD`), repeated until the node confirms it (`GOT`) and kept across gateway restarts.

| Gateway result | Sent to |
|---|---|
| Pipe leak, and its end | The tank and the flats on the suspected floors; every node if the floor cannot be told |
| Supply event, leak draining the tank, uncertain supply, and recovery | The tank |
| A flat's pressure alert during a pipe leak | That flat (its leak alert may not be its own leak) |

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
