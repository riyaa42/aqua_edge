# Aqua Edge: Firmware

The code for the nodes (the gateway's is in [`../gateway/`](../gateway/README.md), radio and packets in [`../hal/`](../hal/README.md), detection models in [`../detection/`](../detection/leakage/README.md)). The node logic never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, storage, buzzer, LEDs and valve. The simulation (`../simulation/`) plugs in a simulated layer; `board.py` is the node program's, with real pins on a Raspberry Pi or simulated ones in a container. **The Pi, radio, LEDs and valve relay have not been tested on real hardware.**

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi 3B | Composable `EdgeRuntime` (`flat`, `tank`, `combined` modes) + `FlatNode`/`TankNode` presets. Learns Day-1 baselines (saved hourly to SD), runs leak + abnormal-consumption checks (`flat`), water quality + tank checks (`tank`), or all 3 pipelines jointly (`combined` for resource validation), with atomic `Store` buffering and TDMA/CSMA-CA LoRa scheduling |
| `edge_based.py` | Every node | Entry point, edge-based solution: alerts that need only this node's data are decided on the node. `uv run prod-edge-edge` (same as `edge-based-node`). [On a Pi](../hal/README.md#running-on-the-pi) or [as a virtual node](#run-virtual-nodes). Tested with a fake radio (`tests/test_pi_node.py`) and over the network (`tests/test_edge_node.py`) |
| `policy.py` | Every node | Which result is an alert, a warning or information ([levels](#notification-levels-and-the-valve)). The one place to change the criteria |
| `actuators.py` | Every node | LEDs, buzzer and valve: real GPIO pins, or a simulated stand-in that only logs |
| `valve.py` | Every node | Command to [reopen the valve](#notification-levels-and-the-valve) at the node |
| `sensors/` | Every node | The node's [sensor readings](#sensor-readings), replayed from a recorded dataset |
| `board.py` | Every node | The `hal` that `aqua_edge.py` calls, apart from the radio: two [clocks](#clocks-and-what-the-node-stores), time slot, keys, storage path and the actuators |
| `cloud_based.py` | Every node | Entry point, cloud-based solution: the Pi only buffers and streams sensor data; all ML and every decision happen in the cloud. `uv run cloud-based-node`. **Placeholder, not implemented yet** |

The rest of this README describes the node logic that exists today, which belongs to the edge-based solution.

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes, first tried in each node's time slot.
Self-check, from the repository root: `python3 -m edge.aqua_edge`. The other modules list their own.

How the leak checks work, with the math: [`../detection/leakage/README.md`](../detection/leakage/README.md).
Abnormal consumption detection: [`../detection/consumption/README.md`](../detection/consumption/README.md).
Water-quality model, data and test results: [`../detection/quality/README.md`](../detection/quality/README.md).

## Notification levels and the valve

The rules come from [`../planning/valve_spec.md`](../planning/valve_spec.md), with a [tolerance period](#when-the-valve-reopens) added before a valve reopens. The code's one table of them is `policy.py`.

| Light | Pattern (2 s cycle) | When | Valve and buzzer |
|---|---|---|---|
| **Red, one blink (R1)** | on 200 ms, off 1800 ms | Water quality: `QUALITY_LIMIT_VIOLATION`, `QUALITY_RUNOFF`, `QUALITY_CHEMICAL`, `QUALITY_TEMPORAL_ANOMALY` | Valve closes; intermittent buzzer |
| **Red, two blinks (R2)** | on 200, off 200, on 200, off 1400 ms | `LEAK_NIGHT_FLOW`, `LEAK_CONTINUOUS_FLOW`, `LEAK_PRESSURE_DROP`, `ABNORMAL_SUPPLY`, `ABNORMAL_CONSUMPTION`, `UNUSUAL_NIGHT_CONSUMPTION`, `SUDDEN_CONSUMPTION_CHANGE`, `PROLONGED_ZERO_FLOW`. From the gateway: `PIPE_LEAK`, `PIPE_LEAK_CAUSE`, `SUPPLY_EVENT`, `LEAK_TANK_DRAIN`, `SUPPLY_UNCERTAIN`, and the operator's `VALVE_CLOSE` and `ALERT_LED_ON` | Valve closes; intermittent buzzer |
| **Yellow, one blink (Y1)** | as R1 | A message unacknowledged for more than 30 s (a summary's time slot is not counted), or a radio fault | No change; silent |
| **Yellow, two blinks (Y2)** | as R2 | A reading missing, no new reading for 3 minutes (also after the replay data ends), a clock warning (more than 60 s from the gateway's), `SUPPLY_LOW`, `VALVE_FAULT` | No change; silent |
| **Green, solid** | | Nothing active | No change; silent |

Only one is shown: red before yellow before green, R1 before R2, Y1 before Y2. A condition counts until it is really over, so clearing one never hides another. **The light shows conditions, not the valve:** once they are all cleared it goes back to yellow or green and the buzzer stops, even though the valve stays shut. It also stays red while an operator's `VALVE_CLOSE` or `ALERT_LED_ON` order is in force. The buzzer sounds exactly while the light is red, also after a restart.

**The valve** closes on every red condition, found by the node or told by the gateway.


#### When the valve reopens

**By itself,** once all of these hold:

1. Every red condition that held it shut has been **explicitly ended**: the node's own recovery message, the gateway's "cleared" for its problem, or `led-off` for the alert-LED order. The node saves this list with the valve.
2. After that, the readings stay normal for the **tolerance period**: by default **30 minutes** of new readings in a row (`--reopen-after-s`, or `NODE_REOPEN_AFTER_S` with Docker Compose; in the data's time, `0` for the first normal reading). Normal means every expected reading is there and nothing red is in force. Yellow conditions do not count against it.

A red condition or a missing reading starts the period again, and so does a restart. Silence, an acknowledgement or the same reading twice count for nothing. A recovery message itself already needs the problem to look over for a while (10 minutes for flows), so the wait comes on top of that.

**By hand,** at any time, even while a condition is still active (the light then stays red):
- the gateway's reopen signal, the [`valve-open` order](../gateway/README.md#orders-to-a-node);
- at the node:
  ```sh
  python3 -m edge.valve open --sd <the node's --sd folder>
  ```

**A `valve-close` order is not a condition:** a valve under that order only reopens by hand.

- After a reopening by hand, a local problem must be confirmed again before it closes the valve again. `QUALITY_TEMPORAL_ANOMALY` raises only when it starts, so one still active does not close it again until it has cleared and returned.
- A gateway problem or close order dated before the last reopening **by hand** is ignored. One heard late after the valve reopened **by itself** still closes it. A reopen signal older than the last time the valve was closed or opened, or marked by the gateway as no longer meant, is ignored.
- While a flat is shut, its flow, pressure and consumption checks pause, nothing is learned, and the zero flow does not count as a recovery, so its light stays red. While the tank is shut, only its outflow checks pause.
- **So a valve shut for a leak, consumption or outflow alert of its own does not reopen by itself:** with the water off, nothing can show the problem is over, and zero flow proves nothing. It needs a reopening by hand, or [trial reopening](#trial-reopening-flats) switched on for a flat. What can reopen by itself without trials: a tank shut for water quality, and any valve shut only for gateway problems or the alert-LED order.
- The node saves what the valve should be before moving it, and drives it again after a restart. It reports `VALVE_CLOSED`, `VALVE_OPENED` or `VALVE_FAULT` (the valve could not be driven, or the node has none).
- Nothing reads back whether the valve really moved. **Closing the tank valve cuts water to the whole building.**

#### Trial reopening (flats)

A shut flat can only show whether its problem is over by letting water through. **This is off by default.** Turn it on with `--trial-reopen`, or `NODE_TRIAL_REOPEN=1` with Docker Compose.

| Point | Rule |
|---|---|
| Which valves | Flats shut only by their own leak or usage alerts. Never the tank. No trial with a gateway problem or LED order in force, an operator's close order, a valve fault or a missing reading |
| First wait | 6 hours shut |
| Pass | The water stops (0.005 L/s or less) for 10 readings in a row within 60 readings. The valve stays open |
| Fail | No such stop, a new red condition, a missing reading, no readings for 3 minutes, the valve not opening, or the node stopping or restarting. The valve shuts |
| Retries | One more trial 12 hours after a failure. After two failures, only a reopening by hand. A pass or a reopening by hand resets the count |

The light stays red until the alert clears by its usual recovery rule, even while the valve is open. The gateway gets the usual `VALVE_OPENED` and `VALVE_CLOSED` reports. If power is lost during a trial, the valve is shut as soon as the node starts again; until then it is wherever the unpowered valve rests. The times are constants in `aqua_edge.py` (`TRIAL_*`).

**Risks:**
- **A pause is not proof there is no leak.** A leak that stops and starts can pass. The usual checks go on, but they take time to confirm (up to 3 hours for continuous flow).
- **Water runs through a leak** for up to about an hour in each failed trial.
- **An empty flat** shut for `PROLONGED_ZERO_FLOW` can pass, because it uses no water. Its light stays red until water is used.
- **No overall limit:** every new closing starts a new count, so a flat that passes and closes again can repeat this for as long as it runs.

**Where this differs from the spec:**

- **Reopening.** Section 7 reopens the valve when the red conditions clear. Here it also waits for the tolerance period, and an operator can reopen it earlier. With trial reopening on, a flat's valve also opens **while still red**, with nothing yet saying the problem is over. The spec has not been edited.
- `SUPPLY_LOW` and `VALVE_FAULT` are not in the spec. They show Y2 and close nothing. The gateway may answer `SUPPLY_LOW` with a supply problem, which does close the tank's valve.
- The drinking-water limit check also runs on the first, learning day, because it needs nothing learned.

**Effect on the 48 h replay,** each node on its own: all 21 close their valves, and **none reopens by itself.** 19 flats close on `LEAK_CONTINUOUS_FLOW`, although only one flat has a planted leak; `B-203` closes on `LEAK_NIGHT_FLOW` (flats between hours 26.5 and 33). The tank closes at about hour 24.3 on `QUALITY_TEMPORAL_ANOMALY`, which gets no clearing message in this data. With a gateway, its problems close valves too. The thresholds are unchanged.

**With trial reopening on:** 21 trials start. `B-203`, the flat with the planted leak, fails both of its two and stays shut. The other 19 flats pass, and **all 19 shut again 5 to 6 hours later on `LEAK_CONTINUOUS_FLOW`.** The replay ends before they could be tried again, so no valve is open at the end. Trials do not fix that rule's frequent alarms. The recorded data does not react to a shut valve, so this does not show how real homes would behave.

## Clocks and what the node stores

- **Sample time** is when a reading was taken (replay: the dataset's time). All detection uses it. **Wall time** stamps messages and drives retries; gateway acknowledgements correct it.
- **Raw readings are never saved after the first day.** `<node>_state.json` holds the learned trends and detector counts in a fixed size (about 7 KB for a flat, 3 KB for the tank in the tests). It is saved every 15 minutes and on any alert or valve change, so a power cut loses at most 15 minutes.
- Until the gateway acknowledges them, messages wait in `<node>.jsonl`. Alerts and flags are never dropped; at most 7 days of 15-minute summaries are kept (672, oldest dropped and counted).
- `--prime-s 86400` makes a first start learn from the first day of the data at once, sending and switching nothing.

## Run virtual nodes

`Dockerfile.edge` and the root `compose.yaml` run one or three virtual nodes next to the [gateway, broker and dashboard](../gateway/README.md#run-with-docker-compose). They replay the dataset, send the same encrypted packets over the Compose network (`../hal/udp.py`, port not published), and **only log** what their LEDs and valve would do. This image is not for a Raspberry Pi: [run the node directly there](../hal/README.md#running-on-the-pi).

| Profile | Nodes | `GATEWAY_TREE` |
|---|---|---|
| `one-node` | `B-203` (the flat with the planted night leak) | `./gateway/virtual_1node.json` |
| `three-nodes` | `B-203`, `B-101` (no planted leak; closes on a continuous-flow alert) and `B-TANK` | `./gateway/virtual_3node.json` |

1. [Make the gateway's key file](../hal/README.md#setting-up-keys), then one file per node:
   ```sh
   for n in B-203 B-101 B-TANK; do (umask 077; uv run python -c 'import json, sys; k = json.load(open(sys.argv[1])); print(json.dumps({n: k[n] for n in (sys.argv[2], "GATEWAY")}))' ~/gateway_keys.json $n > ~/${n}_keys.json); done
   ```
2. In `.env`, set the [gateway's settings](../gateway/README.md#run-with-docker-compose), `GATEWAY_TREE` as above, and `NODE_B203_KEYS`, `NODE_B101_KEYS` and `NODE_TANK_KEYS` (full paths; one node needs only the first).
3. Start, watch, order and stop:
   ```sh
   docker compose --profile three-nodes up -d --build
   docker compose logs -f node-b203
   docker compose exec gateway python -m gateway.order B-203 valve-open --data /data
   docker compose exec node-b203 python -m edge.valve open --sd /data
   docker compose --profile three-nodes down
   ```

- A first start learns day 1 at once (`NODE_PRIME_S`, default 86400), then plays day 2 at one row a minute: the tank closes its valve after about 20 minutes, `B-203` after about 2.5 hours and `B-101` after about 8. For a quick demo set `NODE_STEP_S`, for example `0.4` (about 10 seconds, 1 minute and 3 minutes).
- After the last row a node keeps running: it sends what is left and takes orders. Each node's volume keeps its place, valve and learning across restarts. `down -v` deletes them and starts over.
- **Use separate volumes for each tree**, with `docker compose -p <name> ...` or `down -v` first: the gateway and dashboard keep saved state. In the commands above, add the same `-p` and `--profile` each time.
- The three-node tree says its tank also feeds homes that have no node, so the gateway does not compare tank and flats: **no pipe-leak detection** in these demos.
- Replayed data cannot react to a valve, so a shut node reads zero flow. Radio airtime, range, collisions and packet loss are not simulated.
- Tested with Podman and `podman-compose`; not with Docker itself.


## Sensor readings

There are no physical sensors yet. `sensors/` replays a recorded dataset instead:

```python
from edge.sensors import sensor_reader

get_readings = sensor_reader("B-203")     # or sensor_reader("B-203", "other.csv")
sample = get_readings()                   # (time_s, {"flow_lps": ..., "pressure_m": ...}), or None at the end
```

`board.py` calls `get_readings()` once per step. A flat gets `flow_lps` and `pressure_m`; the tank gets `level_m`, `inflow_lps`, `outflow_lps`, `ph`, `tds_ppm`, `turbidity_ntu` and `temp_c`. A missing or unreadable value is left out, and a failed read does not stop the node.

- **Default dataset:** [`../simulation/webots/data/sensors.csv`](../simulation/webots/README.md): 48 hours of building B, one row per minute, **simulated, not measured**. Day 1 is normal; day 2 has planted leaks, a power cut and water-quality events.
- **Another dataset:** the same layout (time in seconds first, then one `<node>:<sensor>` column each). The node program takes it with `--csv`. A node with its own name, not one of building B's, needs this.
- **It plays once.** After the last row the node stops reading but keeps sending and taking orders (`--exit-at-end` stops it). After a restart it carries on from its last saved row. `--step-s` only changes the pace; the readings keep the dataset's time of day.
- To see a node's first readings: `python3 -m edge.sensors B-203`.

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

The gateway closes the pipe leak (2 tight blocks in a row), silence (`NODE_BACK`) and clock errors (`CLOCK_OK`) itself. A flat leak that never lets the water stop stays open. A node saves its open problems and valve (`<node>_open.json`) and its learned trends, consumption detector, tank minimum level and water-quality history (`<node>_state.json`), so it can report recovery after a restart. A recovery lets the valve [reopen after the tolerance period](#when-the-valve-reopens). An old `<node>_baseline.json` is read once.

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
