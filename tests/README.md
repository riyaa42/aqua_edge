# Tests

| File | Runs on | What it does |
|---|---|---|
| `tests/` | Dev / CI | Unit & integration tests (`test_gateway_analytics.py`, `test_unified_runtime.py`) |
| `test_pi_node.py` | Dev / CI | The Pi node put together (`edge/edge_based.py`, `edge/board.py`, `edge/sensors/` and the HAL's LoRa link) with a fake radio chip |
| `test_edge_node.py` | Dev / CI | Clocks, saved state and restarts, queue limit, the alert, light and valve rules of `planning/valve_spec.md`, the valve and its failures, gateway orders, GPIO pins with fakes, the three-node tree, and the real node program with a gateway over the network on this computer: offline, delivery, orders, restart. No containers or hardware |
| `test_hal.py` | Dev / CI | The HAL's Python code beyond its self-checks: both openers, the Pi's hardware setup and DIO0 wake-up, the serial port, both radio tests, `common.py`, a node and a gateway talking through their openers, and the example in `hal/README.md`. `spidev`, `gpiozero` and `pyserial` are faked, so the real libraries are not tested; neither is the ESP sketch. Includes two encryption tests through both radios (the rest are in `hal/packet.py`'s self-check) |
| `test_comms.py` | Dev / CI | `comms/` with real nodes and a real gateway, a fake radio and fake MQTT objects, and the tables and examples in `comms/README.md`. No broker or `paho-mqtt` |
| `test_gateway_server.py` | Dev / CI | `gateway/edge_based.py` with a fake radio and fake MQTT connection, and checks on the Docker Compose files. No hardware or Docker |
| `test_small_buildings.py` | Dev / CI | One-node (tank or flat) and four-node trees with custom names: real nodes and gateway with encrypted packets over a fake radio, radio numbers, and both programs started with `--tree` |
| `test_gateway_edge.py` | Dev / CI | The gateway node: bad readings, levels and expected lights, the dashboard's valve reopen requests (tokens, expiry, lost answers, restarts) with the real server, and from reopen request to open valve with the real gateway and node programs |
| `test_cloud_baseline.py` | Dev / CI | The cloud-based control case: the same results as edge-based for the same data (planted dataset and a made-up pipe leak), no detection code on the thin node, lost link, lost packets, restarts and lost server state, valve failure, orders and reopening, forged packets, and the real programs with the dashboard server. Takes several minutes (`uv run test-cloud`). No containers or hardware |
| `cloud_world.py` | Tests | A building on a simulated clock that runs either solution, over a radio in memory that can be cut or can lose packets |
| `test_dashboard_link.py` | Dev / CI | `gateway/dashboard_link.py` with a real gateway, shipper and dashboard server on this computer: outage, restart, a server that never answers, a full disk, and the gateway program with `--dashboard-url`. The dashboard's own tests are in `../dashboard/tests/` |
| `offline_radio.py` | Tests | Lets a fake radio with no airtime deliver replies within one step. Also used by the `detection/` tests |

Run commands: [`../README.md`](../README.md#tests).
