# aqua_edge

Aqua Edge / JalRakshak (CSD457 Internet of Things): an offline-first IoT system that detects **unsafe water**, **pipeline leaks**, and **abnormal water consumption** directly on edge nodes, and keeps working during Internet outages.

## Quick access

Run `uv sync` once, then `uv run <script>` from anywhere in the repository. The scripts are defined in `pyproject.toml` (code in `tasks/`).

### Entry points

There are two solutions, and `edge/` and `gateway/` each have an entry point for both:

- **Edge-based:** alerts that need only one node's data are decided on the node. The gateway decides what needs several nodes' data.
- **Cloud-based:** the Pi only buffers and streams sensor data, and moves its valve and lights as told. All ML and every decision happen on the server: here, the gateway computer. This is the control case, [Baseline A](planning/baselines.md).

| Solution | Node (Pi) | Gateway server |
|---|---|---|
| Edge-based | `uv run prod-edge-edge`, or `uv run edge-based-node` (`edge/edge_based.py`) | `uv run prod-edge-gateway`, or `uv run edge-based-gateway` (`gateway/edge_based.py`) |
| Cloud-based | `uv run prod-cloud-edge`, or `uv run cloud-based-node` (`edge/cloud_based.py`) | `uv run prod-cloud-gateway`, or `uv run cloud-based-gateway` (`gateway/cloud_based.py`) |

**All four programs run; none has been tried on real hardware.** Both kinds of node replay a CSV in place of sensors, on a Raspberry Pi with an SX1278 or as a virtual node. `edge-based-node` runs the node checks on the node ([virtual nodes on this branch](edge/README.md#run-virtual-nodes) are cloud-based ones). The cloud-based node [only streams](edge/README.md#the-cloud-based-node), and the cloud-based gateway [runs those same checks for it](gateway/README.md#the-cloud-based-gateway), so the two solutions differ in where the deciding happens. A node's two kinds are not interchangeable: run the same solution at both ends. `uv run test-edge` tests the edge-based node with a fake radio, and `uv run test-cloud` compares the two solutions. On the Pi, first run `uv sync --extra pi`, then `uv run pi-radio-test` checks the LoRa module. Wiring and setup: [Running on the Pi](hal/README.md#running-on-the-pi).

The gateway computer reaches the nodes through an ESP8266 on USB. After flashing it, run `uv sync --extra gateway`, then `uv run gate-radio-test` checks the modem (`--port` if it is not `/dev/ttyUSB0`). Wiring and flashing: [Gateway modem (ESP8266)](hal/README.md#gateway-modem-esp8266). `uv run dashboard-server` runs the [dashboard API](dashboard/README.md#running-it). To run the gateway, with or without Docker Compose: [Running the server](gateway/README.md#running-the-server). What the two ends publish over MQTT: [`comms/README.md`](comms/README.md).

### Tests

Latest results: [Test results](#test-results).

A group runs all its checks even if one fails, then lists the failures.

| Script | What it runs |
|---|---|
| `uv run test-all` | Every group below except `test-parity` |
| `uv run test-hal` | Packet, radio-settings and Pi hardware-layer self-checks |
| `uv run test-edge` | Node self-check |
| `uv run test-gateway` | Building tree and gateway self-checks, gateway analytics, server and small-building tests |
| `uv run test-comms` | MQTT communication self-check and tests |
| `uv run test-dashboard` | Dashboard sender and server tests, and the gateway-to-dashboard link |
| `uv run test-quality` | Water-quality model self-check |
| `uv run test-consumption` | Abnormal-consumption integration tests |
| `uv run test-leakage` | Leak regression, LoRa-outage and supply-robustness tests, and the comparison's scoring self-check |
| `uv run test-cloud` | The cloud-based control case against the edge-based solution, its failure cases, and the real programs with the dashboard server. Takes several minutes |
| `uv run test-runtime` | Unified runtime replay and benchmark. Also part of `test-all`; both rewrite `tests/unified_runtime_metrics.json` |
| `uv run test-parity` | Temporal model parity check. Needs the GECCO data, and **currently fails** (difference 7.84e-06, limit 1e-6) |

### Experiments

Datasets are not in the repository: see the linked README for each area.

| Script | What it does | Needs | Writes |
|---|---|---|---|
| `uv run exp-compare-leaks` | Flow vs pressure leak detection, 35 runs ([leakage](tinyml/leakage/README.md)) | | `tinyml/leakage/results/` |
| `uv run exp-validate-campaign` | Building B leak campaign | | `tinyml/leakage/results/` |
| `uv run exp-validate-swifeed` | Leak validation on SWI-FEED | SWI-FEED data | `tinyml/leakage/results/` |
| `uv run exp-validate-cache-county` | Leak validation on household data | Cache County data | `tinyml/leakage/results/` |
| `uv run exp-consumption-building-b` | Consumption detectors on Building B ([consumption](tinyml/consumption/README.md)) | | `tinyml/consumption/results/` |
| `uv run exp-consumption-cache-county` | Consumption detectors on household data | Cache County data | `tinyml/consumption/results/` |
| `uv run exp-static-anomaly` | Experiment 1, static anomaly detection ([quality](tinyml/quality/README.md)) | GECCO, Kenya data | **Overwrites** `tinyml/quality/exp1_results.txt` |
| `uv run exp-temporal-anomaly` | Experiment 2, temporal anomaly detection | GECCO, Loddon data | **Overwrites** `temporal_model.json` and `exp2_results.txt` in `tinyml/quality/` |
| `uv run exp-train-quality` | Retrains the water-quality classifier | Kenya, GECCO, CSU ROSS, Loddon data | **Overwrites** `quality_model.json` and `results.txt` in `tinyml/quality/` |

The comparisons against the baselines planned in [`planning/experiments.md`](planning/experiments.md) are not written yet, so they have no script.

## Test results

Run on 28 November 2026 on a Linux development computer, with simulated or replayed sensor data. **The software works from end to end, from sensor replay to dashboard and back to the valve. The detectors also raise false alarms on this replay** ([below](#detection-on-the-replayed-day)).

**Not verified on hardware.** The Raspberry Pi has GPIO problems that we could not resolve, so nothing that depends on its pins is verified: the SX1278 radio on the Pi, the LEDs, buzzer and relay/valve. Tests of the software above that layer use fake or network radios, and never move a real valve. The ESP8266 modem, a real radio link, real sensors (there are no drivers yet) and Docker itself were not tested either.

### Automated tests

| Run | Result |
|---|---|
| `uv run test-all` | **30 of 30 checks passed** |
| `uv run python -m tests.test_comms_reliability` (not part of `test-all`) | Passed |

`test-all` covers packets and encryption, the radio code with fake chips, node and gateway rules, saved state and restarts, radio outages, MQTT messages (fake broker), the dashboard server, the reopen button through to the node's valve, and the detectors. `test-parity` is not included ([see above](#tests)).

### Four virtual nodes, one gateway and dashboard

Podman 6.2.1 and `podman-compose`, `--profile virtual-nodes`, `NODE_STEP_S=0.4`, with new random keys. Times are from when `up` returned. The times include streaming day 1 to the server to learn from.

| Node | Dashboard showed its valve closed | Cause on this data |
|---|---|---|
| `B-TANK` | Yes, after 242 s | `QUALITY_TEMPORAL_ANOMALY`, before any planted event |
| `B-101` | Yes, after 252 s | `LEAK_CONTINUOUS_FLOW`, **false alarm** |
| `B-203` | Yes, after 240 s | `LEAK_NIGHT_FLOW`, planted leak |
| `B-301` | Yes, after 254 s | `LEAK_CONTINUOUS_FLOW`, **false alarm** |

- The dashboard received all four nodes in frames marked `cloud`; the broker, gateway, dashboard and all four node containers stayed up.
- Reopening `B-203` through the dashboard API was accepted (HTTP 202), and the node reported its valve open 5 s later.
- The web page and health endpoint answered with HTTP 200 (the page's display was not checked in a browser).
- The gateway logged no bad packets or crashes. At startup it logged refused connections until the dashboard was up.
- The node image contains no detection code.

The causes were not read from the container run. They come from replaying the same data through the same node and gateway code on a simulated clock, with a one-off script that is not in the repository.

### Leak and consumption tests

From `test-all`, on simulated water-network data:

| Case | Result |
|---|---|
| Leak in flat B-203, small | Detected after 180 min (continuous flow) |
| Leak in flat B-203, medium and large | Detected after 10 min (pressure drop) |
| Other flats during those three leaks | No leak alerts |
| 48 hours without a leak | No flat or pipe leak alerts |
| Pipe leaks: 5 places, medium and large | All 10 detected after 30 min |
| Finding the leaking floor | The test's score: first choice right in 6 of 10, in the first two in 7 of 10. It named a floor only for the 5 large leaks |
| Regression scenarios, and offline and supply scenarios | 8 of 8 and 8 of 8 passed |
| Abnormal-consumption integration tests | 5 of 5 passed |

### Detection on the replayed day

From the same kind of one-off replay: all 21 nodes of building B, on day 2 of `simulation/webots/data/sensors.csv` (a different data set from the table above). The replay cannot react to a closed valve.

| Planted event | Result |
|---|---|
| 02:00 leak in flat B-203 | Detected at 02:29 |
| 06:00 runoff in the tank | Detected at 06:02 |
| 09:00 power cut to the pump | Low supply reported at 13:49, recovery at 17:14 |
| 18:00 pipe burst | **No pipe-leak alert.** The tank valve had been shut since 00:17, so this replay does not show whether the burst would be found |
| 20:00 chemical contamination | Detected at 20:03 |

**All 21 valves closed.** The 19 flats without a planted leak all closed between 07:59 and 08:59 on `LEAK_CONTINUOUS_FLOW`, and the tank closed at 00:17 on `QUALITY_TEMPORAL_ANOMALY`, almost six hours before the first quality event. The no-leak result above does not carry over to this data. The alert rules or this data need work before the valves can be trusted.

### Cloud-based compared with edge-based

`tests/test_cloud_baseline.py` (15 checks, part of `test-all`) compares the two on a simulated clock for 5 nodes, including a made-up pipe leak, and tests link outages, lost packets, restarts, old and forged messages, valve failures and the real programs with the dashboard.

A separate one-off run compared all 21 nodes over the replayed day, with no packet loss:

| | Edge-based | Cloud-based |
|---|---|---|
| Node alerts and flags | 50 | The same 50 |
| Gateway decisions / valve moves / light changes | 48 / 21 / all | The same |
| 15-minute summaries | 1,995 | 1,974 identical. In the other 21, one per valve closing, cloud also counts the reading from the minute the valve closed |
| Longest alert delay in the gateway's network figures | 0 s | 5 s (one simulation step) |
| Data sent by the nodes / by the gateway | 83 kB / 297 kB | 1,368 kB / 1,060 kB |
| Shared radio channel busy (estimate) | 1.3% | 6.6% |
| Airtime per flat / gateway, seconds per hour (limit 360 each) | 0.33-0.35 / 39 | 5.6 / 118 |

Matching results mean the false alarms above happen in both. Radio figures are estimates from packet sizes: collisions, range and loss are not simulated. A cloud-based node that cannot reach the gateway changes no valve.

## What Each Folder & File Contains

| Folder / File | What is inside it (in simple terms) |
|---|---|
| `presentation/` | `initial/` (first review slides, speaking script, Q&A prep, reference list) and `final/` (reserved for final review materials) |
| `planning/circuit_plan.md` | Updated hardware components and connections: Raspberry Pi 3B nodes on 5 V USB power, a laptop gateway with an ESP8266 interface to its LoRa module, and the dataset-replay layer (replacing physical sensors, ADC, and battery HAT) |
| `planning/archive/branch_reference.md` | Checklist of technical alignments for porting `live-simulation` and the full experimentation plan |
| `planning/archive/` | Older planning and handover notes (`IMPROVEMENT_PLAN.md`, `HANDOVER.md`) kept for history |
| `edge/` | The Python code for the Raspberry Pi 3B nodes (`aqua_edge.py`). `edge_based.py` runs a node on the Pi, with its clocks, LEDs, buzzer and valve in `board.py` and `actuators.py`, its alert levels in `policy.py` and its sensor readings replayed from a recorded dataset by [`sensors/`](edge/README.md#sensor-readings), in place of physical sensors; `cloud_based.py` is the cloud-based node, which only streams and does as told (it uses `loop.py` and `store.py`, and no detection code) |
| `gateway/` | The Python code for the laptop gateway (`core.py`, `analytics.py`). The gateway reads the building's pipes and valves as a tree of nodes from `gateway/building_B.json` (schema in `gateway/README.md`). `edge_based.py` is the edge-based gateway server; `cloud_based.py`, which Docker Compose (`compose.yaml`) runs on this branch, is the same server with every node's checks run on it (`cloud_host.py`) |
| `hal/` | Radio packets and SX1278 433 MHz settings (`packet.py`, `lora433.py`) and the hardware adapters (`pi_hal.py`) so the same detection code runs unchanged in simulation and on the Pi; placeholder for the gateway's ESP8266 passthrough |
| `compose.yaml`, `Dockerfile`, `.env.example` | Docker Compose setup for the building's computer: cloud-based gateway server, MQTT broker and dashboard server ([how to run](gateway/README.md#run-with-docker-compose)). `compose.esp8266.yaml` adds the radio modem |
| `Dockerfile.edge` | Image of a virtual node: `compose.yaml` runs [four](edge/README.md#run-virtual-nodes) |
| `comms/` | How the Pi and gateway communicate: the specification (`comms/README.md`) and the code for its MQTT topics and publishing |
| `detection/` | The three detection subsystems: `quality/` (IS 10500 + `int8` TinyML), `leakage/` (flow/pressure rules + mass balance) and `consumption/` (statistical + Lempel–Ziv) |
| `detection/quality/` | Offline training, quantization, and testing code for the water-quality neural network (`train_quality.py`, `results.txt`). Holds `quality_model.py` and produces `quality_model.json` for `edge/` |
| `detection/leakage/` | Standalone leak-rule comparison scripts (`compare_leaks.py`) and documentation comparing flow-based and pressure-based leak detection |
| `detection/consumption/` | Abnormal water-consumption detection experiments (spikes, night bursts, prolonged zero flow, and Paper [3] Lempel–Ziv compression-rate analytics), scored separately from leaks |
| `dashboard/` | Buffered gateway-to-dashboard shipper and the ingest and read API server ([how to run](dashboard/README.md#running-it)). The web page is in `dashboard/frontend/` |
| `tests/` | Gateway analytics tests and the unified runtime replay and benchmark |
| `tasks/` | The functions behind the `uv run` scripts above |
| `simulation/` | Everything that models the physical world: EPANET/WNTR pipe network (`scripts/`), real-time 21-node + LoRa radio simulator (`live/`), ns-3 LoRaWAN check (`ns3/`), and parked 3D simulator (`webots/`) |
| `planning/baselines.md` | *(Planned)* Cloud-dependent baseline (Ali et al. [6]) and single-task edge baselines kept separate from the Aqua Edge code for fair comparison |
| `planning/experiments.md` | *(Planned)* Evaluation scripts that feed identical scenarios and controlled outages (0 min–24 h) into the Aqua Edge code (`edge/`, `gateway/`) and the baselines to measure all 6 Slide 10 metrics |

## Deploy

On this branch these scripts deploy the **cloud-based** solution, the control case. **Not tested on real hardware** (no ESP8266, SX1278, Pi or valve), and the building setup has been tried only with Podman, not Docker.

> **The nodes have no sensor drivers yet.** A node replays a CSV file, and on a Pi its real valve moves by what is in that file. Do not connect a real water supply until real sensor reading exists.
>
> **A cloud-based node protects nothing on its own.** While it cannot reach the gateway computer, no leak or bad water closes any valve.

**1. Building computer** (cloud-based gateway, MQTT broker and dashboard, in containers). It needs Docker or Podman with Compose, the [flashed ESP8266](hal/README.md#gateway-modem-esp8266), the [key file](hal/README.md#setting-up-keys) and the right time.

```sh
cp .env.example .env                    # fill it in: gateway/README.md, "Run with Docker Compose"
uv run --no-default-groups prod-up      # checks the settings, builds, starts, waits for the gateway
```

| Script | What it does |
|---|---|
| `uv run prod-up` | Deploys, or updates after a code change. It refuses missing or unsafe settings, always uses the ESP8266 and never starts virtual nodes. `--check` only checks; `--no-modem` rehearses without the radio |
| `uv run prod-status` | The services |
| `uv run prod-logs` | Follows their logs (or `uv run prod-logs --tail 50 gateway`) |
| `uv run prod-down` | Stops them. The data is kept; it refuses `-v` |

The services restart if they fail. Whether they start again after a reboot depends on Docker or Podman being set up to start them, which these scripts do not do. The dashboard is reachable from that computer only; before opening it to others, put HTTPS and a login in front.

**2. Each Pi** (after [its setup](hal/README.md#running-on-the-pi)):

```sh
uv sync --no-default-groups --extra pi
uv run --no-default-groups --extra pi prod-node-service B-203 --keys /home/pi/keys.json \
    --csv /home/pi/sensors.csv --sd /home/pi/aqua --slot 5 20 > aqua-edge-B-203.service
```

`prod-node-service` checks the node, key and data files, then **only prints** a systemd service and the commands that install it, which need `sudo`. `--csv` has no default, so the simulated data is never used by accident. Add `--tree` for another building and `--user` if the account is not `pi`.

**In the foreground**, without containers or a service, use `uv run prod-cloud-gateway`, `uv run dashboard-server` and, on a Pi, `uv run prod-cloud-edge`. They take the same options as `cloud-based-gateway` and `cloud-based-node` (`--help`). `uv run prod-edge-gateway` and `uv run prod-edge-edge` run the edge-based programs the same way. None of these reads `.env`. The cloud-based gateway keeps its state in its own folder (`cloud-gateway-data`) and Compose volumes, apart from an edge-based deployment's: deploying here neither reads nor deletes the edge-based data.

## Python setup

This repository uses one `uv` project at the repository root (`pyproject.toml`, `uv.lock`, `.python-version`). From here or any subdirectory:

```sh
uv sync
```

The libraries for the simulation, the experiments and some tests (`numpy`, `pandas`, `wntr`, `scikit-learn`, `matplotlib`, `streamlit`) are in the `research` dependency group in `pyproject.toml`. `uv sync` and `uv run` install it by default. The nodes and the gateway do not need it: `--no-default-groups` leaves it out, as the gateway's Docker image does. The dashboard server's libraries are in the `dashboard` group, which its Docker image includes, and the test-only ones in `test`; both are installed by default too. Add a library to the project's `dependencies` only if a node or the gateway needs it to run.

Run every existing simulation check in one go:

```sh
sh simulation/check_all.sh
```
