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
| `Dockerfile.edge` | Image of a virtual node: `compose.yaml` runs [one or three](edge/README.md#run-virtual-nodes) |
| `comms/` | How the Pi and gateway communicate: the specification (`comms/README.md`) and the code for its MQTT topics and publishing |
| `detection/` | The three detection subsystems: `quality/` (IS 10500 + `int8` TinyML), `leakage/` (flow/pressure rules + mass balance) and `consumption/` (statistical + Lempel–Ziv) |
| `detection/quality/` | Offline training, quantization, and testing code for the water-quality neural network (`train_quality.py`, `results.txt`). Holds `quality_model.py` and produces `quality_model.json` for `edge/` |
| `detection/leakage/` | Standalone leak-rule comparison scripts (`compare_leaks.py`) and documentation comparing flow-based and pressure-based leak detection |
| `detection/consumption/` | Abnormal water-consumption detection experiments (spikes, night bursts, prolonged zero flow, and Paper [3] Lempel–Ziv compression-rate analytics), scored separately from leaks |
| `dashboard/` | Buffered gateway-to-dashboard shipper and the ingest and read API server ([how to run](dashboard/README.md#running-it)). The web page is not written yet |
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

Without containers, `uv run prod-edge-gateway` and `uv run dashboard-server` run the two programs in the foreground; `uv run prod-edge-edge` does the same for a node. The two `prod-edge-*` scripts take their settings from `.env` (see `.env.example`): the gateway uses `GATEWAY_KEYS`, `GATEWAY_TREE` and `GATEWAY_SERIAL`; a node needs `NODE_NAME`, `NODE_KEYS` and `NODE_CSV`, and can set `NODE_SD` and `NODE_SLOT`. Options typed on the command line win; a node name goes first. The `edge-based-*` scripts do not read `.env`.

For this branch's solution without containers, use `uv run prod-cloud-gateway` and `uv run prod-cloud-edge`. The cloud-based gateway keeps its state in its own folder (`cloud-gateway-data`) and Compose volumes, apart from an edge-based deployment's: deploying here neither reads nor deletes the edge-based data.

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
