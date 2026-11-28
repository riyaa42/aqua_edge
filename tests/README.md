# Tests

| File | Runs on | What it does |
|---|---|---|
| `tests/` | Dev / CI | Unit & integration tests (`test_gateway_analytics.py`, `test_unified_runtime.py`) |
| `test_pi_node.py` | Dev / CI | The Pi node put together (`edge/edge_based.py`, `edge/board.py`, `bogo/csv_replay.py` and the HAL's LoRa link) with a fake radio chip |
| `test_hal.py` | Dev / CI | The HAL's Python code beyond its self-checks: both openers, the Pi's hardware setup and DIO0 wake-up, the serial port, both radio tests, `common.py`, a node and a gateway talking through their openers, and the example in `hal/README.md`. `spidev`, `gpiozero` and `pyserial` are faked, so the real libraries are not tested; neither is the ESP sketch. Includes two encryption tests through both radios (the rest are in `hal/packet.py`'s self-check) |
| `test_comms.py` | Dev / CI | `comms/` with real nodes and a real gateway, a fake radio and fake MQTT objects, and the tables and examples in `comms/README.md`. No broker or `paho-mqtt` |
| `test_gateway_server.py` | Dev / CI | `gateway/edge_based.py` with a fake radio and fake MQTT connection, and checks on the Docker Compose files. No hardware or Docker |
| `test_small_buildings.py` | Dev / CI | One-node (tank or flat) and four-node trees with custom names: real nodes and gateway with encrypted packets over a fake radio, radio numbers, and both programs started with `--tree` |
| `offline_radio.py` | Tests | Lets a fake radio with no airtime deliver replies within one step. Also used by the `tinyml/` tests |

Run commands: [`../edge/README.md`](../edge/README.md#deployment-topology-vs-combined-three-pipeline-resource-validation).
