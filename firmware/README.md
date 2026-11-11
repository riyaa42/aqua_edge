# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card, buzzer/LED and valve. The simulation (`../simulation/`) plugs in a simulated hardware layer; the real hardware will get its own: one for the Raspberry Pi Zero 2 W nodes (mains powered), and one on the gateway laptop that exchanges packets with the ESP32 radio bridge over USB serial.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi Zero 2 W | Learns normal on day 1, leak checks with flow only (flat), water quality and tank outflow checks (tank), store-and-forward to SD card, sending in time slots, re-sending |
| `gateway.py` | Gateway laptop (LoRa module on an ESP32 bridges the radio) | Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes in each node's time slot (details and timings: `../leak_detection/README.md` section 7b).
Self-check: `python3 gateway.py`.

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Water-quality model plans: [`../tinyml/README.md`](../tinyml/README.md).
