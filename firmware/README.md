# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. There are no valves in this project: the response to a problem is the buzzer, the LED and an alert to the gateway. The simulation (`../simulation/`) plugs in a simulated hardware layer; the real hardware will get its own: one for the Raspberry Pi Zero 2 W nodes (mains powered), and one on the gateway laptop that exchanges packets with the ESP32 radio bridge over USB serial.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi Zero 2 W | Learns normal on day 1 (saved to SD hourly, so a power cut keeps it), leak checks with flow only (flat: night, continuous flow, too much), water quality and tank outflow checks (tank), store-and-forward to SD card (saved safely, so a power cut never leaves half a file), sending in time slots, re-sending |
| `quality_model.py` + `quality_model.json` | Tank node | Water-quality model: 8-bit neural network (308 bytes), pH + TDS + turbidity in, NORMAL / RUNOFF / CHEMICAL out; integer-only, plain Python. Trained by `../tinyml/train_quality.py` |
| `packet.py` | Nodes and gateway | Radio packets: compact binary (about a third of the JSON size), signed with a 4-byte tag (HMAC-SHA256) using a key shared between each node and the gateway; a message counter that never repeats |
| `gateway.py` | Gateway laptop (LoRa module on an ESP32 bridges the radio) | Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes in each node's time slot (details and timings: `../leak_detection/README.md` section 7b).
Self-checks: `python3 aqua_edge.py` (power-cut-safe saves, baseline survives a restart, continuous-flow check, forged ACK ignored), `python3 quality_model.py` (normal, runoff and chemical readings classified right), `python3 packet.py` (packets come back unchanged, one changed bit or a wrong key is rejected), `python3 gateway.py` (includes: forged packet dropped, replay ignored even after a gateway restart).

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Water-quality model, data and test results: [`../tinyml/README.md`](../tinyml/README.md).
