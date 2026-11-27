# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. There are no valves in this project: the response to a problem is the buzzer, the LED and an alert to the gateway (the buzzer is not implemented yet: the firmware only drives the LED). The simulation (`../simulation/`) plugs in a simulated hardware layer; the real hardware will get its own: one for the Raspberry Pi 4 nodes (USB powered), and one on the gateway laptop that exchanges packets with the ESP32 radio bridge over USB serial.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi 4 | Composable `EdgeRuntime` (`flat`, `tank`, `combined` modes) + `FlatNode`/`TankNode` presets. Learns Day-1 baselines (saved hourly to SD), runs leak + abnormal-consumption checks (`flat`), water quality + tank checks (`tank`), or all 3 pipelines jointly (`combined` for resource validation), with atomic `Store` buffering and TDMA/CSMA-CA LoRa scheduling |
| `quality_model.py` + `quality_model.json` + `temporal_model.json` | Edge node | Offline 3-module water-quality stack (pure Python, `int8`): **Module 1** (`check_standards`, authoritative IS 10500 / *TrinkwV* limits), **Module 2** (`TemporalModel`, 432 B `int8` Temporal AE for subtle temporal anomalies), and **Module 3** (`QualityModel`, 324 B `int8` 4-feature classifier: `NORMAL` / `RUNOFF` / `CHEMICAL`) |
| `packet.py` | Nodes and gateway | Radio packets: compact binary (about a third of the JSON size), signed with a 4-byte tag (HMAC-SHA256) using a key shared between each node and the gateway; a message counter that never repeats |
| `gateway.py` | Gateway laptop (LoRa module on an ESP32 bridges the radio) | Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes in each node's time slot (details and timings: `../leak_detection/README.md` section 7b).
Self-checks: `python3 aqua_edge.py` (power-cut-safe saves, baseline survives a restart, continuous-flow check, forged ACK ignored), `python3 quality_model.py` (normal, runoff and chemical readings classified right), `python3 packet.py` (packets come back unchanged, one changed bit or a wrong key is rejected), `python3 gateway.py` (includes: forged packet dropped, replay ignored even after a gateway restart).

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Abnormal consumption detection: [`../abnormal_consumption/README.md`](../abnormal_consumption/README.md).
Water-quality model, data and test results: [`../tinyml/README.md`](../tinyml/README.md).

## Deployment Topology vs. Combined Three-Pipeline Resource Validation

1. **Actual deployment topology:** `FlatNode` (`mode="flat"`, 20 nodes) runs flow + pressure leak detection (`LEAK_NIGHT_FLOW`, `LEAK_CONTINUOUS_FLOW`, `LEAK_PRESSURE_DROP`) and abnormal consumption (`StatisticalZeroFlowDetector`). `TankNode` (`mode="tank"`, 1 node) runs water-quality inference (`QualityMonitor` Modules 1–3) and tank outflow/level checks (`ABNORMAL_SUPPLY`, `SUPPLY_LOW`). Flat nodes without water-quality probes never run water-quality inference.
2. **Combined software resource-validation configuration:** `EdgeRuntime(..., mode="combined")` executes all three pipelines (leak + abnormal consumption + water quality/tank) concurrently in one process on replayed streams (`test_unified_runtime.py`).
3. **Host-machine resource measurements** (`Windows-11 AMD64`, Python `3.12.16`; **not** physical Raspberry Pi 4 measurements):
   - `flat` mode: `266.76 us/step`, `3748.7 steps/s`, `159.77 KB` peak `tracemalloc` RAM.
   - `tank` mode: `799.88 us/step`, `1250.2 steps/s`, `169.64 KB` peak `tracemalloc` RAM.
   - `combined` mode: `907.81 us/step` (`0.00133%` of 1-min budget), `1101.5 steps/s`, `185.51 KB` peak `tracemalloc` RAM (`+25.74 KB` over `flat`, `+15.87 KB` over `tank`), `9.42 us` per 34-byte `packet.pack`.
   - **Flash / storage footprint:** `78.67 KB` combined code + model JSONs (`quality_model.json` 1,450 B, `temporal_model.json` 3,029 B), `4,445 B` baseline JSON, `~11.6 KB` / 24h (`~81.1 KB` / 7d) `Store` retention vs. `505.02 GB` free host disk.
4. **Physical validation status & limitations:** Replayed streams (Building B WNTR hydraulics + synthetic `E4`/`E5` water quality) are not co-located physical sensor measurements; physical Raspberry Pi 4 hardware benchmarking remains pending.

Run the unified runtime replay & benchmark:
```bash
uv run python firmware/test_unified_runtime.py
```
