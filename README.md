# aqua_edge

Aqua Edge (CSD457 Internet of Things): an offline-first IoT node that detects unsafe water, leaks and abnormal consumption on the device itself.

| Folder | What is in it |
|---|---|
| `firmware/` | The real node and gateway code (runs in the simulation now; later on the Raspberry Pi Zero 2 W nodes and the gateway laptop) |
| `leak_detection/` | How flow-based leak detection works, its math, and the flow vs pressure test |
| `tinyml/` | Water-quality model: datasets, early model script |
| `simulation/` | Simulation and network design: water network, live simulation, LoRa radio model, ns-3, Webots |
| `docs/` | Components and connections list (`components.md`); presentations (`presentations/first/`: slides, speaking script, Q&A prep, reference list) |

What it detects: leaks inside a flat (flow checks on each flat's node), leaks on the shared down pipe, named by floor (flow meters above each floor and a per-floor water balance at the gateway), and unsafe tank water (runoff or chemical from a small model on the tank node, water above 35 °C from a plain limit). Everything is shown on a live dashboard (`python3 simulation/live/dashboard.py`, then localhost:8080).

**New to the project? Start with [`HANDOVER.md`](HANDOVER.md)**: what exists, why, current results, open decisions and next steps.

Run every check in one go: `sh simulation/check_all.sh` (about 4 minutes).
