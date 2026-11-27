# Hardware Abstraction Layers (HAL)

Adapters that let `firmware/edge_node/aqua_edge.py` and `firmware/gateway/gateway.py` run unchanged across environments:
- Simulation HAL (connects to `simulation/live/` or offline CSVs)
- Raspberry Pi 3B dataset-replay HAL (reads local datasets / EPANET outputs row-by-row from microSD and drives real LoRa, RTC, and buzzer hardware)

## Radio contract (SX1278 driver not written yet)
The method list is at the top of `edge_node/aqua_edge.py`. A real driver must:
- use the settings in `communication/lora433.py` (check the legal notes there first);
- stay in receive mode whenever it is not sending;
- call `Node.radio_poll()` when a packet arrives and at the time it returns, as well as `Node.step()` once a minute;
- call `Gateway.step()` when a packet arrives and at the time it returns, at least every 0.5 s (through the ESP8266 on USB serial);
- for `send(packet, cad=True)`, run channel activity detection first; if the channel is busy, wait a short random time and try again;
- call these from one thread (the classes are not thread-safe);
- optionally provide `buzzer(code)` and `on_gateway_alert(code, active)`.
