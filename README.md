# aqua_edge

Aqua Edge / JalRakshak (CSD457 Internet of Things): an offline-first IoT system that detects **unsafe water**, **pipeline leaks**, and **abnormal water consumption** directly on edge nodes, and keeps working during Internet outages.

## What Each Folder & File Contains

| Folder / File | What is inside it (in simple terms) |
|---|---|
| `presentation/` | `initial/` (first review slides, speaking script, Q&A prep, reference list) and `final/` (reserved for final review materials) |
| `circuit_plan.md` | Updated hardware components and connections: Raspberry Pi 4 on 5 V USB power, LoRa + ESP32 + laptop gateway, and the dataset-replay layer (replacing physical sensors, ADC, and battery HAT) |
| `branch_reference.md` | Checklist of technical alignments for porting `live-simulation` and the full experimentation plan |
| `planning_archive/` | Older planning and handover notes (`IMPROVEMENT_PLAN.md`, `HANDOVER.md`) kept for history |
| `firmware/` | The actual Python code that runs on the Raspberry Pi 4 nodes and the laptop gateway (`aqua_edge.py`, `gateway.py`, `packet.py`, `quality_model.py`). `firmware/hal/` holds the hardware adapters so the same detection code runs unchanged in simulation and on the Pi |
| `tinyml/` | Offline training, quantization, and testing code for the water-quality neural network (`train_quality.py`, `results.txt`). Produces `quality_model.json` for `firmware/` |
| `leak_detection/` | Standalone leak-rule comparison scripts (`compare_leaks.py`) and documentation comparing flow-based and pressure-based leak detection |
| `abnormal_consumption/` | Abnormal water-consumption detection experiments (spikes, night bursts, prolonged zero flow, and Paper [3] Lempel–Ziv compression-rate analytics), scored separately from leaks |
| `simulation/` | Everything that models the physical world: EPANET/WNTR pipe network (`scripts/`), real-time 21-node + LoRa radio simulator (`live/`), ns-3 LoRaWAN check (`ns3/`), and parked 3D simulator (`webots/`) |
| `baselines/` | *(Planned)* Cloud-dependent baseline (Ali et al. [6]) and single-task edge baselines kept separate from `firmware/` for fair comparison |
| `experiments/` | *(Planned)* Evaluation scripts that feed identical scenarios and controlled outages (0 min–24 h) into `firmware/` and `baselines/` to measure all 6 Slide 10 metrics |

## Python setup

This repository uses one `uv` project at the repository root (`pyproject.toml`, `uv.lock`, `.python-version`). From here or any subdirectory:

```sh
uv sync
```

Run every existing simulation check in one go:

```sh
sh simulation/check_all.sh
```
