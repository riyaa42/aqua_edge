# aqua_edge

Aqua Edge (CSD457 Internet of Things): an offline-first IoT node that detects unsafe water, leaks and abnormal consumption on the device itself.

| Folder | What is in it |
|---|---|
| `firmware/` | The real node and gateway code (runs in the simulation now, on the Raspberry Pi later) |
| `leak_detection/` | How flow-based leak detection works, its math, and the flow vs pressure test |
| `tinyml/` | Water-quality model: datasets, early model script |
| `simulation/` | Simulation and network design: water network, live simulation, LoRa radio model, ns-3, Webots |
| `prep_work/` | Components and connections list |
| `first_presentation/` | Slides, speaking script and Q&A prep for the first review, reference list |
