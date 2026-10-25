# Aqua Edge: Simulation

Water simulations for Aqua Edge in EPANET (through WNTR), plus a Webots test bench for the node programs. The 2-page progress report is `report/Aqua_Edge_Simulation_Report.pdf`.

| Folder | What is in it |
|---|---|
| `scripts/` | Reproduction of SWI-FEED, leak-rule experiments, the housing-society network builder, data download |
| `data/swifeed/` | SWI-FEED network and published results (downloaded, not stored in git) |
| `epanet_gui/` | Networks to open in the EPANET program: the housing society (stored) and NET_6 variants (generated) |
| `results/` | Plots and tables from the experiments |
| `webots/` | Webots world, node and gateway programs, scenario data (see `webots/README.md`) |
| `report/`, `report_figures/`, `screenshots/` | Progress report and its figures |

## 1. Reproducing the SWI-FEED dataset

**Goal:** check that we can regenerate the hydraulic dataset published by Pagano et al. [5] (EPANET and NS-3 co-simulation of a water network) before building our own experiments on it.

**Setup**
- Network: NET_6 from the authors' GitHub repository (WITS-Restart/WDN-IoT-Dataset-Workbench): 4,419 junctions, 5,066 pipes, 3 reservoirs
- 24 hours, one result per hour, no leaks (the authors' "leak_area_0" scenario)
- Tool: WNTR 1.5.0 (Python interface to EPANET)

**Result**

| Solver | Values compared | Mean difference | Largest difference |
|---|---|---|---|
| EPANET solver | 105,816 pressures, 121,200 flows | 0.04 m pressure | 0.37 m pressure |
| WNTR solver | 105,816 pressures, 121,200 flows | **0.000000** | **0.000001** (CSV rounding) |

With the WNTR solver our output matches the published dataset exactly, so the authors used the WNTR solver. We use the same solver for everything below. Plot: `results/reproduction_wntr.png`.

## 2. Testing two leak rules from the literature

**Goal:** see how well the two leak rules in our approach work on a realistic network with imperfect sensors.

**Rules tested**
- **Rule A, pressure drop**, from Alghamdi et al. [2] (pressure-drop leak rule and smart valve closure over LoRaWAN): a pressure sensor at every junction (one per house, as in [2]). A zone raises an alarm when any sensor reads more than 2 m (about 20 kPa) below its normal value for that hour of the day.
- **Rule B, mass balance**, from Ali et al. [6] (mass balance of supply vs branch flows for leaks, plus quality sensing): flow meters on every pipe entering or leaving a zone and at every house. Alarm when (water in minus water out) divided by water in is above 10%, the threshold used in [6].

**Setup**
- The network is split into 16 zones (4 x 4 grid, about 276 junctions each)
- Leaks of 4 hole sizes at 8 random junctions (32 runs), each starting at hour 6
- Sensor errors: pressure readings have random noise (standard deviation 0.5 m); each flow meter has a fixed error within plus or minus 5% (Ali et al. [6] measured under 5% for their sensors)
- Self-check built into the script: with perfect meters, the missing water in each zone equals the leak flow exactly

**Results** (`results/leak_rules_summary.csv`, one row per run in `results/leak_rules_runs.csv`)

| Leak flow | Leak as % of water entering zone | Pressure rule detected | Pressure drop really above 2 m | Mass balance detected |
|---|---|---|---|---|
| 0.2 L/s | 0.2% | 1 of 8 | 0 of 8 | 0 of 8 |
| 1.1 L/s | 1.2% | 0 of 8 | 0 of 8 | 0 of 8 |
| 4.3 L/s | 4.6% | 4 of 8 | 1 of 8 | 2 of 8 |
| 10.5 L/s | 10.6% | 3 of 8 | 2 of 8 | 5 of 8 |

False alarms with no leak: pressure rule about 1% of zone-hours; mass balance 0% (its no-leak error stayed between -3.9% and +3.3%).

**What we learned**
1. **Neither rule sees small leaks at zone level.** Leaks of 0.2 and 1 L/s were missed by both.
2. **The pressure rule depends heavily on location.** The same 4 L/s leak dropped pressure by 6.45 m at one junction and by 0.02 m at another (`results/pressure_example.png`). Some pressure "detections" were caused by sensor noise, not the leak: compare the "detected" and "really above 2 m" columns. A fixed threshold copied from Alghamdi et al. [2] (pressure-drop leak rule and smart valve closure over LoRaWAN), which uses 2.7 kPa, would not work here; each node needs a threshold based on its own normal pattern.
3. **Mass balance gives no false alarms but only catches leaks of about 10% or more of the zone's inflow** (`results/mass_balance_example.png`). The no-leak error never left plus or minus 4%, so a lower threshold may be possible after calibrating the meters.
4. **The two rules catch different leaks.** For example, junction 6562 was caught by pressure but not by mass balance, and junction 5985 the other way round. This supports using both rules together on the Aqua Edge node.
5. **Smaller metered areas help mass balance.** Inside one building with our three flow sensors, the same leak is a much larger share of the flow than inside a 276-junction zone, so the 10% rule from Ali et al. [6] (mass balance of supply vs branch flows for leaks, plus quality sensing) should work far better at node level.

**Limits of this test**
- Hourly time steps, so detection delay cannot be measured yet
- Only 8 leak locations per size
- Sensor error sizes are assumptions, to be replaced with values from our own calibration
- Zones are a simple grid, not real district meter areas

## 3. Housing-society network (our own, building level)

NET_6 is a whole town, too large for a building-level node. We built a small network that matches where an Aqua Edge node would sit, similar in idea to the housing complex in Alghamdi et al. [2] (pressure-drop leak rule and smart valve closure over LoRaWAN), but with the rooftop tanks common in Indian societies.

**Layout:** underground sump, one pump per building, rooftop tank, gravity downtake to each floor. 4 buildings (A to D), ground + 4 floors, 4 flats per floor = 80 flats. Water use is 540 L per flat per day (4 people x 135 L, Indian CPHEEO guideline) with morning and evening peaks. Building height is modelled through each junction's elevation; `results/society_3d.png` shows the 3D layout.

**Files:** `epanet_gui/SOCIETY_normal.inp`, `epanet_gui/SOCIETY_leak.inp` (180 L/hour leak in flat B-203), built by `scripts/build_society.py`. Units are litres per second, pressure in metres.

**Results** (`results/society_summary.txt`)
- Pressure falls about 3 m per floor: 16.5 m on the ground floor, 4.5 m on the top floor at the morning peak
- The leak raised building B's night-time (1 to 4 am) water use from 0.015 L/s to 0.067 L/s, more than 4 times normal
- The same leak lowered the flat's own pressure by only 0.22 m

- The leak file also has a 1.5 L/s pipe burst on building C's first-floor downtake, starting at 6 am. It made building C's pump start 4 times instead of once, yet the lowest top-floor pressure moved only from 3.51 m to 3.46 m. In a rooftop-tank building, pressure is set by height and tank level, so even a burst barely changes it while the pump keeps up.

So inside a building, **a leak shows up clearly as flow (night-time flow, pump starts) but hardly at all in pressure**. This supports flow-based checks (the mass balance from Ali et al. [6], mass balance of supply vs branch flows for leaks, plus quality sensing, and our continuous night-flow check) over a pressure-only rule at building level.

## Next steps
- Shorter time steps (15 minutes or less) to measure detection delay
- Building-sized zones that match our node design
- Supply-drop events, to test our gateway rule that tells a supply drop from a leak
- Network outages in NS-3, following Pagano et al. [5] (EPANET and NS-3 co-simulation of a water network)

## How to run

Uses [uv](https://docs.astral.sh/uv/), like the rest of the repo. From this `simulation/` folder:

```
sh scripts/get_swifeed_data.sh                          # download SWI-FEED NET_6 (about 48 MB)
uv run python scripts/reproduce_swifeed.py wntr         # section 1, about 10 seconds
uv run python scripts/leak_rules.py                     # section 2, about 4 minutes
uv run python scripts/build_society.py                  # section 3, writes epanet_gui/SOCIETY_*.inp
uv run python scripts/make_epanet_files.py              # NET_6 files for the EPANET program
```

Outputs go to `results/`. Sensor error sizes and thresholds are set at the top of each script.

## Live simulation and the LoRa network (ns-3)

Every node runs the real firmware in its own process (or Docker container), fed one minute at a time by a live WNTR water model. Leaks, water-quality changes and radio cuts are triggered from a control panel. From this `simulation/` folder:

```
sh live/run_local.sh --scenario live/scenarios/standard.jsonl   # no Docker: one process per node, 48 h in ~1 min
docker compose -f live/compose.yaml up                          # same, one container per node; panel at localhost:8501
python3 live/check_comms.py                                     # did communication work? PASS/FAIL per check
```

ns-3 (with the LoRaWAN module, inside Docker) runs once on building B to give each node's spreading factor, signal strength and delivery rate (`ns3/link_table.csv`, used by `live/radio.py`). It can then replay every packet of a live run, so `check_comms.py` compares the Python radio model with ns-3:

```
sh ns3/run_ns3.sh            # link table (first run builds ns-3: ~20-30 min)
sh ns3/run_ns3.sh replay     # replay live/run/tx_log.csv
```

Standard scenario result: 100% of uplink attempts got through while the radio was up (2 collisions out of 4066), 0 messages lost, and ns-3 and the Python model agree on every node (gap 0.0 points). All nodes reach the gateway on SF7 with at least 26 dB to spare.
