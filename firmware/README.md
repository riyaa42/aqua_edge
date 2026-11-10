# Aqua Edge: Firmware

The real code for the nodes and the gateway. It only uses the Python standard library, and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card, buzzer/LED and valve. The simulation (`../simulation/`) plugs in a simulated hardware layer; the Raspberry Pi will get its own.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank) | Learns normal on day 1, leak and usage checks (flat), water quality and tank outflow checks (tank), store-and-forward to SD card, sending in time slots, re-sending |
| `gateway.py` | Gateway | Replies to every message (with the time), building check (water in = water out), supply-event rule, new vs repeat alerts |

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Water-quality model plans: [`../tinyml/README.md`](../tinyml/README.md).
