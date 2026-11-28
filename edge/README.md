# Aqua Edge: Firmware

The real code for the nodes and the gateway (this folder for the nodes, [`../gateway/`](../gateway/README.md), [`../hal/`](../hal/README.md) and the water-quality model in [`../tinyml/quality/`](../tinyml/quality/README.md)). It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. There are no valves yet: the response to a problem is the buzzer, the LED and an alert. A node sounds `hal.buzzer` for its own alerts and for gateway alerts that concern it. There is a hook for valve control (`hal.on_gateway_alert`) that closes nothing. The simulation (`../simulation/`) plugs in a simulated hardware layer; real hardware has its own: `../hal/pi_hal.py` for the Raspberry Pi 3B nodes (USB powered; SX1278 and buzzer drivers, not verified on hardware in these tests). The laptop gateway's layer, which exchanges packets over USB serial with an ESP8266 (the interface to its LoRa module), and the ESP8266 sketch are not in the repository yet, so **no gateway message reaches a real node yet**.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi 3B | Composable `EdgeRuntime` (`flat`, `tank`, `combined` modes) + `FlatNode`/`TankNode` presets. Learns Day-1 baselines (saved hourly to SD), runs leak + abnormal-consumption checks (`flat`), water quality + tank checks (`tank`), or all 3 pipelines jointly (`combined` for resource validation), with atomic `Store` buffering and TDMA/CSMA-CA LoRa scheduling |
| `edge_based.py` | Every node | Entry point, edge-based solution: alerts that need only this node's data are decided on the node. `uv run edge-based-node`. Runs the node on the Pi in real time, calling `radio_poll()` between sensor steps. [Setup](../hal/README.md#running-on-the-pi). Tested with a fake radio only (`python3 -m tests.test_pi_node`) |
| `board.py` | Every node | The Pi's side of the `hal` that `aqua_edge.py` calls, apart from the radio: clock (set from genuine gateway acknowledgements, never from one older than the last used), time slot, keys, storage path, LEDs and buzzer. The buzzer sounds for a node's own alerts and for gateway alerts that concern it, and stops when the LED leaves red. It has no `on_gateway_alert` (the valve hook) |
| `cloud_based.py` | Every node | Entry point, cloud-based solution: the Pi only buffers and streams sensor data; all ML and every decision happen in the cloud. `uv run cloud-based-node`. **Placeholder, not implemented yet** |

The rest of this README describes the node logic that exists today, which belongs to the edge-based solution.

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes, first tried in each node's time slot.
Self-check, from the repository root: `python3 -m edge.aqua_edge`. The other modules list their own.

How the leak checks work, with the math: [`../tinyml/leakage/README.md`](../tinyml/leakage/README.md).
Abnormal consumption detection: [`../tinyml/consumption/README.md`](../tinyml/consumption/README.md).
Water-quality model, data and test results: [`../tinyml/quality/README.md`](../tinyml/quality/README.md).

## Hybrid Physical Pi 3B + Dockerized Multi-Node Setup

- **1 physical node** (`Raspberry Pi 3B` + `SX1278 433 MHz` + `RTC` + `microSD` + `buzzer`) streams one node's dataset/WNTR rows and transmits encrypted `packet.py` frames over LoRa to the **SX1278 → ESP8266 → USB serial** laptop gateway.
- **20 virtual nodes** run in **Docker containers** using the same `EdgeRuntime` and `packet.py` framing, feeding into the same `Gateway` instance so physical and virtual nodes are indistinguishable to `Gateway` and `GatewayAnalytics` (and from there to the dashboard).

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

What the gateway saves is described in [`../gateway/README.md`](../gateway/README.md#recovery-and-restarts).

## Deployment Topology vs. Combined Three-Pipeline Resource Validation

1. **Actual deployment topology:** `FlatNode` (`mode="flat"`, 20 nodes) runs flow + pressure leak detection (`LEAK_NIGHT_FLOW`, `LEAK_CONTINUOUS_FLOW`, `LEAK_PRESSURE_DROP`) and abnormal consumption (`StatisticalZeroFlowDetector`). `TankNode` (`mode="tank"`, 1 node) runs water-quality inference (`QualityMonitor` Modules 1–3) and tank outflow/level checks (`ABNORMAL_SUPPLY`, `SUPPLY_LOW`). Flat nodes without water-quality probes never run water-quality inference.
2. **Combined software resource-validation configuration:** `EdgeRuntime(..., mode="combined")` executes all three pipelines (leak + abnormal consumption + water quality/tank) concurrently in one process on replayed streams (`test_unified_runtime.py`).
3. **Host-machine resource measurements** (`Windows-11 AMD64`, Python `3.12.16`; **not** physical Raspberry Pi 3B measurements):
   - `flat` mode: `266.76 us/step`, `3748.7 steps/s`, `159.77 KB` peak `tracemalloc` RAM.
   - `tank` mode: `799.88 us/step`, `1250.2 steps/s`, `169.64 KB` peak `tracemalloc` RAM.
   - `combined` mode: `907.81 us/step` (`0.00133%` of 1-min budget), `1101.5 steps/s`, `185.51 KB` peak `tracemalloc` RAM (`+25.74 KB` over `flat`, `+15.87 KB` over `tank`), `9.42 us` per 34-byte `packet.pack` (measured before packets were encrypted).
   - **Flash / storage footprint:** `78.67 KB` combined code + model JSONs (`quality_model.json` 1,450 B, `temporal_model.json` 3,029 B), `4,445 B` baseline JSON, `~11.6 KB` / 24h (`~81.1 KB` / 7d) `Store` retention vs. `505.02 GB` free host disk.
4. **Physical validation status & limitations:** Replayed streams (Building B WNTR hydraulics + synthetic `E4`/`E5` water quality) are not co-located physical sensor measurements; physical Raspberry Pi 3B benchmarking remains pending.

Run the gateway analytics tests and unified runtime replay & benchmark:
```bash
uv run python -m tests.test_gateway_analytics
uv run python -m tests.test_unified_runtime
```
