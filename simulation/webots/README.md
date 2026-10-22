# Aqua Edge in Webots: firmware test bench

EPANET (through WNTR) simulates the water. Webots runs the **real node firmware** on 20 flat nodes, 1 tank node and 1 gateway in a 3D model of building B. The firmware never talks to Webots directly, only to a small hardware layer, so the same `firmware/` files are meant to run on the Raspberry Pi Zero 2 W.

## How the pieces fit

| File | Job |
|---|---|
| `make_scenario.py` | Simulates building B for 48 h in EPANET and writes what every sensor would read (`data/sensors.csv`), the planted events (`data/events.csv`) and network outages (`data/outages.csv`) |
| `firmware/aqua_edge.py` | **Node firmware**: detection rules, valve decisions, store-and-forward queue |
| `firmware/gateway.py` | **Gateway firmware**: acknowledgements, supply-event rule, mass balance |
| `simlink.py` | Serves sensor readings and outage times to a simulated node |
| `run_offline.py` | Runs all firmware without Webots in about 1 second and scores it. Use it after every firmware change |
| `webots_hal.py` | Hardware layer for Webots (radio = Emitter/Receiver, light = LED, SD card = file) |
| `controllers/` | Thin Webots controllers that start the firmware |
| `make_world.py` | Writes `worlds/aqua_edge.wbt` (building, nodes at real heights, gateway) |

## Planted events (day 2) and what catches them

| Event | What happens | Caught by |
|---|---|---|
| E1 02:00 | Leak in flat B-203 (0.05 L/s) | B-203 node: continuous night flow, valve closed |
| E2 09:00 to 17:00 | Power cut, pump cannot run | Tank node reports low tank, gateway declares a supply event, no valve closures |
| E3 18:00 | Pipe burst on the 1st-floor downtake (1.5 L/s) | Tank node: abnormal supply (14 min); gateway mass balance from Ali et al. [6] (mass balance of supply vs branch flows for leaks) (30 min) |
| E4 06:00 | Rainwater-like water in the tank | Tank node quality check (2 min), stand-in for the model of Sharma et al. [1] (8-bit neural network on the device that classifies water quality) |
| E5 20:00 | Chemical contamination | Tank node quality check (2 min) |
| O1 01:30 to 05:00 | No node can reach the gateway | E1 alert stored on the node's SD card and delivered at 05:01, following Mariadhason et al. [4] (timestamped store-and-forward to SD card) |

Current result (`results/offline/summary.txt`, same in Webots): all 5 events detected, 0 false valve closures, 0 unexplained alerts, nothing left unsent.

## What the first test taught us
The first firmware version closed a flat's valve whenever its pressure dropped and the gateway had not declared a supply event, following the pressure-drop rule of Alghamdi et al. [2] (pressure-drop leak rule and smart valve closure over LoRaWAN). During the power cut the tank drained slowly, so flats saw their pressure drop hours apart instead of all at once, the "many nodes at once" rule never fired, and **19 valves closed falsely**. Two changes fixed it:
1. A flat closes its valve on a pressure drop only if its own flow is also abnormally high (a real leak raises flow; a supply drop does not).
2. The tank node reports an abnormally low tank, which tells the gateway directly that it is a supply problem.

## Run it

From this `webots/` folder (the node programs only use the Python standard library):

```
python3 run_offline.py              # node program test, with the outage
python3 run_offline.py --no-outage  # same, perfect network
webots worlds/aqua_edge.wbt         # Webots R2025a
```

In Webots, press play. One step is one minute of water time, so 48 h takes about 6 minutes at normal speed; use fast-forward to skip ahead. LEDs: green normal, yellow messages stuck waiting to send, red alert. Results are written to `results/webots_run/` when the 48 h end. `AQUA_SNAPSHOTS=1` before the command saves two screenshots for slides.

After changing the scenario: `uv run python make_scenario.py` (needs the uv project in `simulation/`), then `python3 make_world.py` if the node list changed.

## Limits
- Webots radio is range based; it does not model LoRa airtime, spreading factors or collisions (that is the NS-3 part of the plan).
- Valve decisions are logged but do not change the water simulation yet.
- Water quality readings are made up, with events planted on purpose; EPANET cannot model pH or TDS.
- The quality check is threshold rules, to be replaced by a trained quantized model.
