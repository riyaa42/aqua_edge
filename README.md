# aqua_edge

Aqua Edge / JalRakshak (CSD457 Internet of Things): an offline-first IoT system that detects **unsafe water**, **pipeline leaks**, and **abnormal water consumption** directly on edge nodes, and keeps working during Internet outages.

## Quick access

Run `uv sync` once, then `uv run <script>` from anywhere in the repository. The scripts are defined in `pyproject.toml` (code in `tasks/`).

### Entry points

There are two solutions, and `edge/` and `gateway/` each have an entry point for both:

- **Edge-based:** alerts that need only one node's data are decided on the node. The gateway decides what needs several nodes' data.
- **Cloud-based:** the Pi only buffers and streams sensor data. All ML and every decision happen in the cloud.

| Solution | Node (Pi) | Gateway server |
|---|---|---|
| Edge-based | `uv run edge-based-node` (`edge/edge_based.py`) | `uv run edge-based-gateway` (`gateway/edge_based.py`) |
| Cloud-based | `uv run cloud-based-node` (`edge/cloud_based.py`) | `uv run cloud-based-gateway` (`gateway/cloud_based.py`) |

**The two edge-based programs run; the two cloud-based ones do not.** They are placeholders: each prints "not implemented yet" and exits with an error. `edge-based-node` replays a CSV in place of sensors and runs the node checks on the node. It needs a Raspberry Pi with an SX1278 (options: `uv run edge-based-node --help`); `uv run test-edge` tests it with a fake radio. On the Pi, first run `uv sync --extra pi`, then `uv run pi-radio-test` checks the LoRa module. Wiring and setup: [Running on the Pi](hal/README.md#running-on-the-pi).

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
| `edge/` | The Python code for the Raspberry Pi 3B nodes (`aqua_edge.py`). `edge_based.py` runs a node on the Pi, with its clock, LEDs and buzzer in `board.py`; `cloud_based.py` is a placeholder for the other node entry point |
| `gateway/` | The Python code for the laptop gateway (`core.py`, `analytics.py`). The gateway reads the building's pipes and valves as a tree of nodes from `gateway/building_B.json` (schema in `gateway/README.md`). `edge_based.py` is the edge-based gateway server, also run with Docker Compose (`compose.yaml`); `cloud_based.py` is a placeholder |
| `hal/` | Radio packets and SX1278 433 MHz settings (`packet.py`, `lora433.py`) and the hardware adapters (`pi_hal.py`) so the same detection code runs unchanged in simulation and on the Pi; placeholder for the gateway's ESP8266 passthrough |
| `compose.yaml`, `Dockerfile`, `.env.example` | Docker Compose setup for the building's computer: gateway server, MQTT broker and dashboard server ([how to run](gateway/README.md#run-with-docker-compose)) |
| `comms/` | How the Pi and gateway communicate: the specification (`comms/README.md`) and the code for its MQTT topics and publishing |
| `detection/` | The three detection subsystems: `quality/` (IS 10500 + `int8` TinyML), `leakage/` (flow/pressure rules + mass balance) and `consumption/` (statistical + Lempel–Ziv) |
| `detection/quality/` | Offline training, quantization, and testing code for the water-quality neural network (`train_quality.py`, `results.txt`). Holds `quality_model.py` and produces `quality_model.json` for `edge/` |
| `detection/leakage/` | Standalone leak-rule comparison scripts (`compare_leaks.py`) and documentation comparing flow-based and pressure-based leak detection |
| `detection/consumption/` | Abnormal water-consumption detection experiments (spikes, night bursts, prolonged zero flow, and Paper [3] Lempel–Ziv compression-rate analytics), scored separately from leaks |
| `dashboard/` | Buffered gateway-to-dashboard shipper and the ingest and read API server ([how to run](dashboard/README.md#running-it)). The web page is not written yet |
| `bogo/` | *(Planned, empty for now)* Streams parts of the datasets as real-time sensor data, in place of physical sensors |
| `tests/` | Gateway analytics tests and the unified runtime replay and benchmark |
| `tasks/` | The functions behind the `uv run` scripts above |
| `simulation/` | Everything that models the physical world: EPANET/WNTR pipe network (`scripts/`), real-time 21-node + LoRa radio simulator (`live/`), ns-3 LoRaWAN check (`ns3/`), and parked 3D simulator (`webots/`) |
| `planning/baselines.md` | *(Planned)* Cloud-dependent baseline (Ali et al. [6]) and single-task edge baselines kept separate from the Aqua Edge code for fair comparison |
| `planning/experiments.md` | *(Planned)* Evaluation scripts that feed identical scenarios and controlled outages (0 min–24 h) into the Aqua Edge code (`edge/`, `gateway/`) and the baselines to measure all 6 Slide 10 metrics |

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
