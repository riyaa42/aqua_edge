# aqua_edge

Aqua Edge / JalRakshak (CSD457 Internet of Things): an offline-first IoT node that detects unsafe water, leaks and abnormal consumption on the device itself.

| Folder / File | What is in it |
|---|---|
| `presentation/` | `initial/` (first review slides, speaking script, Q&A prep, reference list) and `final/` |
| `circuit_plan.md` | Updated hardware components, USB power, and dataset-replay connections |
| `branch_reference.md` | Alignment checklist for porting `live-simulation` and full experimentation plan |
| `planning_archive/` | Archived planning and handover notes (`IMPROVEMENT_PLAN.md`, `HANDOVER.md`) |
| `firmware/` | Node and gateway code (runs in the simulation now; later on the Raspberry Pi 4 nodes and the gateway laptop) |
| `leak_detection/` | Flow-based leak detection documentation, math, and flow vs. pressure experiments |
| `tinyml/` | Water-quality model: datasets, training/quantization script, and evaluation results |
| `simulation/` | Simulation and network design: water network, live simulation, LoRa radio model, ns-3, Webots |

## Python setup

This repository uses one `uv` project at the repository root (`pyproject.toml`, `uv.lock`, `.python-version`). From here or any subdirectory:

```sh
uv sync
```

Run every check in one go: `sh simulation/check_all.sh` (about 4 minutes).
