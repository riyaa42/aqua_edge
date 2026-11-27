# Hardware Abstraction Layers (HAL)

Adapters that let `firmware/edge_node/aqua_edge.py` and `firmware/gateway/gateway.py` run unchanged across environments:
- Simulation HAL (connects to `simulation/live/` or offline CSVs)
- Raspberry Pi 3B dataset-replay HAL (reads local datasets / EPANET outputs row-by-row from microSD and drives real LoRa, RTC, and buzzer hardware)
