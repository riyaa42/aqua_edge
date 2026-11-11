# Aqua Edge: Handover

Status as of 11 November 2026, branch `live-simulation`. This file is the single place to catch up on the project: what exists, why it was built that way, how to check it, and what is still open. Read it first, then follow the links into the folder READMEs for detail.

**Suggested reading order:** this file, then `leak_detection/README.md` (detection method and math), `simulation/LIVE_SIMULATION.md` (simulation and radio), `firmware/README.md`, `tinyml/README.md`, `docs/components.md` (hardware).

---

## 1. The project in one paragraph

Aqua Edge (CSD457 Internet of Things; earlier names: JalRakshak, HydroDrive) is an **offline-first** water monitoring system for one residential building, "building B": 20 flats on 5 floors (G, 1 to 4; 4 flats per floor) fed by a rooftop tank through a shared downtake pipe. Every flat and the tank get a small node (Raspberry Pi Zero 2 W on mains power) with sensors. Nodes detect **leaks, abnormal consumption and unsafe water on the device itself**, warn locally (buzzer, LED, valve), and send alerts and 15-minute summaries over **LoRa** (long-range, low-power radio; no internet needed) to a gateway in the building: a LoRa module on an ESP32 board, connected by USB to a laptop that runs the gateway program and does building-wide checks. Nothing depends on the internet or a cloud.

---

## 2. Repository map

| Path | What it is |
|---|---|
| `firmware/aqua_edge.py` | **The real node code.** Flat node and tank node. Standard library only. Talks to hardware only through a small hardware layer object (`hal`). |
| `firmware/gateway.py` | **The real gateway code.** ACKs, building check, silent nodes, supply events, repeat alerts, clock check. Has a self-check (`python3 firmware/gateway.py`). |
| `leak_detection/README.md` | How leak detection works: checks, math, timing, results, known problems. |
| `leak_detection/compare_leaks.py` | Flow vs pressure experiment (35 runs). Run from `simulation/`. Results go to `leak_detection/results/` (not in git). |
| `tinyml/` | Water-quality model work: dataset survey, open decision, a teammate's early quantized-network script (`pre-quality-check/quality.py`, trained on made-up data). |
| `simulation/live/` | **Main test system.** Live simulation: water engine, timekeeper, one process or Docker container per node, LoRa radio model, control panel, checker, scenarios. |
| `simulation/ns3/` | ns-3 (research network simulator) with the LoRaWAN module, in Docker. Produces the radio link table and replays live runs to cross-check the radio model. |
| `simulation/webots/` | Older 3D test bench (Webots), the pre-made 48 h sensor scenario, and `run_offline.py` (fast firmware test without Webots). Parked, see section 6. |
| `simulation/scripts/build_society.py` | Builds the building B water network used everywhere. |
| `simulation/check_all.sh` | **Runs every check in one go** (about 4 minutes). |
| `docs/components.md` | Hardware components and connections. |
| `docs/presentations/first/` | First review: slides, speaking script, Q&A prep, reference list. |

Python environment: `simulation/` is a `uv` project (uv is the Python package manager used here). The firmware itself needs no libraries.

---

## 3. How the system works

### 3.1 Nodes (`firmware/aqua_edge.py`)

Every node runs one step **once a minute**: read sensors, learn or check, queue messages, send.

- **Day 1 (first 24 h): learning only.** Each node records, for each hour of the day, the average and spread of every sensor (`Baseline` class). No alarms on day 1. Each flat learns its own pattern.
- **Flat node, from day 2: flow only.**
  - *Night check:* flow above 0.02 L/s for 30 minutes in a row between 1 and 5 am gives `LEAK_NIGHT_FLOW` and **closes the valve** (nobody runs water steadily at night).
  - *Too-much check:* flow above max(3 x this hour's learned average, 0.05 L/s) for 60 minutes gives `ABNORMAL_CONSUMPTION` (alert only, no valve: it could be guests, not a leak).
  - Pressure is still read, learned and sent in summaries, but raises no alarm (section 5, decision D1).
- **Tank node:**
  - *Outflow check:* outflow above average + max(4 x spread, 0.5 L/s) for 15 minutes gives `ABNORMAL_SUPPLY` (pipe burst).
  - *Level low:* level below average - max(4 x spread, 0.3 m) for 10 minutes sends a `SUPPLY_LOW` flag (pump or power problem).
  - *Water quality:* pH, TDS, turbidity, temperature; **threshold rules for now** (`classify_quality`: TDS > 600 or pH > 8.5 is chemical; TDS < 120 and pH < 6.8 is runoff), to be replaced by the TinyML model. Fires after 3 bad readings in a row (about 2 to 3 minutes); `QUALITY_CLEARED` when it is normal again.
- **One alarm per problem:** an alert re-arms after 10 quiet minutes (`ALERT_CLEAR_S`).
- **Messages:** `ALERT` (urgent), `FLAG`, `SUM` (15-minute summary: litres used, lowest pressure). Every message carries node id `n`, sequence `s`, node time `t`.

### 3.2 Gateway (`firmware/gateway.py`)

Hardware: LoRa module + ESP32 (radio bridge: passes packets between LoRa and USB serial) + laptop. `gateway.py` runs **on the laptop**; it needs a small laptop-side hardware layer that reads and writes packets over the USB serial link.

1. **ACK** every message; the ACK carries the gateway time `gt`, and nodes reset their clocks from it. Duplicates are dropped; each logged message gets the receive time `rx`.
2. **Building check (mass balance)** [Ali et al. [6], for comparing tank supply with the flats' use to find pipe leaks]: per 15-minute block, (water out of tank - sum of all flats) / water out of tank. Above 10% for 2 blocks gives `PIPE_LEAK`. Blocks under 5 L are skipped. Re-arms after 2 normal blocks. Cannot see leaks inside a flat (those pass the flat's meter; the flat's own checks catch them).
3. **Silent nodes:** nothing heard for 45 minutes gives `NODE_SILENT`; `BALANCE_PAUSED` names the silent nodes (the building check waits rather than guess); `NODE_BACK` when heard again, and the late blocks are then checked from the stored summaries.
4. **Supply event:** the tank's `SUPPLY_LOW` flag gives `SUPPLY_EVENT` (our own rule, not from a paper).
5. **Repeat check:** each alert is marked `NEW_ALERT` or `REPEAT_ALERT` (count, minutes since last).
6. **Clock check:** a message stamped more than 1 minute in the future gives `CLOCK_SKEW`.

### 3.3 Communication

| Method | Used for |
|---|---|
| Own time slot per node (TDMA) | Summaries: node k sends at second k x 60/21 + 0.2 of the minute (2.86 s slots, 0.2 s guard) |
| Listen before talk (LoRa CAD) + random backoff 0.1 to 1 s, up to 5 tries | Alerts: sent the same minute they fire |
| Re-send if no ACK within 2 minutes | Everything |
| Store-and-forward to SD card | Messages wait during radio cuts [Mariadhason et al. [4], for timestamped store-and-forward to SD card] |
| At most 4 messages per node per minute; 10 ms gap between own packets | Keeps traffic inside the slot |

Airtime is about 133 ms per 75-byte packet at SF7 (SF = spreading factor, LoRa's speed vs range setting). ns-3 says every node in building B reaches the gateway on SF7 with about 26 dB to spare.

### 3.4 Time

- Every message has `t` (when the node measured or decided) and, at the gateway, `rx` (when it arrived). Stored messages keep their original `t`.
- Simulation: seconds since the start of the run. Real nodes: DS3231 real-time clock (rated +-2 ppm), reset from every ACK.
- **Planned, to be discussed first:** UTC Unix seconds on the nodes, ISO 8601 UTC times in log files, local time (IST) only for display.

### 3.5 Simulation and testing

| Setup | Water | Radio | Use |
|---|---|---|---|
| **Live simulation** (`simulation/live/`) | EPANET through WNTR (Python library driving the EPANET pipe simulator), stepped minute by minute, so leaks can be added mid-run | `live/radio.py`: airtime, collisions, cuts, weak links; checked against ns-3 replay | **Main test.** Real firmware in 22 processes, or 24 Docker containers (nodes, gateway, timekeeper, control panel) |
| **Offline test** (`simulation/webots/run_offline.py`) | Pre-made 48 h file `webots/data/sensors.csv` | Outage times only | 1-second firmware regression test |
| **Webots** (`simulation/webots/`) | Same pre-made file | Simple in range / out of range | 3D view only; parked |

Docker: `simulation/live/Dockerfile` builds **one** image (Python 3.12 + libraries, no code); `simulation/live/compose.yaml` starts one container per node from it, mounts `simulation/` and `firmware/` into each, and gives each its `NODE_ID`. Editing the firmware needs no rebuild.

Water quality values in the simulation are made up (EPANET cannot model pH or TDS). Valve actions are logged but do not change the simulated water.

---

## 4. Current results

From `sh simulation/check_all.sh` on 11 November 2026: **all checks pass.**

| Scenario | Result |
|---|---|
| Standard (48 h): 3.5 h radio cut, small flat leak, runoff, downtake burst, chemical | All events caught; stored alarms delivered 4 min after the radio returned; 100% of attempts through while radio up; 0 lost; 0 false alarms; radio model vs ns-3 gap 0.0 points |
| Flow leaks: small leak in B-203 at 02:00, medium leak in B-402 at 10:00, large burst on downtake B-D1 at 18:00 | Night check 30 min; too-much check 60 min; tank outflow 15 min and building check 31 min (67% of water missing); 0 false alarms |
| Flow vs pressure comparison (35 runs, firmware of commit 4f7867a) | Flow: 30/30 leaks caught, 0 false alarms. Pressure: 748 false alarms (mostly the wrong flat), small leaks missed, faster only on medium/large flat leaks (10 min) |
| Laptop measurement of one flat node | About 0.2 ms of work per simulated minute, about 13 MB for the whole Python process. To be confirmed on a real Pi Zero 2 W |

---

## 5. Decisions and why

| # | Decision | Why |
|---|---|---|
| D1 | **Leaks are detected with flow, not pressure.** The pressure rule (`LEAK_PRESSURE_DROP`, from Alghamdi et al. [2], for the pressure-drop leak rule and valve closure) was removed. | Pressure drops along the whole shared pipe, so it blames the wrong flat and misses small leaks; flow caught every leak with no false alarms. Pressure only helped speed on big leaks. |
| D2 | **Leaks and usage use simple statistics, not machine learning.** ML only for water quality. | No training data needed, tiny memory, easy to explain and tune. Of the papers, only Sharma et al. [1, for the 8-bit on-device neural network for water quality] uses ML; [2] uses a pressure threshold, [3] a compression rate, [6] a flow balance. |
| D3 | **Detection runs on the node** [Babazadeh [3], for anomaly detection on the node with only anomalies sent over LoRa]. | A node keeps protecting its flat if the gateway or radio fails (tested in the radio cut), and the radio carries 15 times fewer messages than sending every reading. |
| D4 | **24-hour learning period on each node.** | Each flat has its own habits; the work is tiny (24 x 3 numbers per sensor). |
| D5 | **The gateway does not learn a baseline.** | Its checks rest on physics (water cannot vanish) or fixed rules, so they work from minute one and cover day 1 while nodes learn. A learned meter offset may be added once real meter error is known. |
| D6 | **Silent node: pause and say so, do not guess.** | Filling in a missing node's usual amount could hide or invent a leak. Alerting after about 3 missed summaries is common practice for LoRa networks. |
| D7 | **Supply event from the tank level only.** | It used to count flats' low-pressure flags, which went away with D1. |
| D8 | **Time slots for summaries, listen before talk for alerts.** | Random sending gave about 10% collisions (489); slots brought it to 2 in about 4,066. Alerts are urgent, so they do not wait for a slot. |
| D9 | **Live simulation is the main test; Webots parked.** | Webots water is fixed in advance and its radio has no LoRa timing. Webots may come back as a 3D demo if the course expects one. |
| D10 | **Pi fit will be tested on a real Pi Zero 2 W**, not by limiting Docker containers. | A laptop container can only estimate Pi processor time. |
| D11 | **Valve closes only on the night check.** | The too-much check cannot tell a leak from a busy day. |
| D12 | **Hardware: each node is a Raspberry Pi Zero 2 W on mains power (no battery); the gateway is LoRa module + ESP32 + laptop.** | Mains removes the battery-life problem of a Pi running Linux (about 0.5 to 0.7 W even when idle). The laptop has the power and screen for gateway analysis and a dashboard; the ESP32 only bridges the radio. |

---

## 6. Open decisions (need the team)

1. **Water-quality model classes:** normal / anomaly (matches the GECCO 2019 dataset directly) or normal / runoff / chemical (needs extra labels we make ourselves)? Blocks the TinyML work. Datasets surveyed in `tinyml/README.md`.
2. **Timestamps** (section 3.4): agree UTC Unix seconds and ISO 8601 logs, then build.
3. **Webots:** keep as 3D demo or drop (was it promised in the proposal?).
4. **Pass marks** in `simulation/live/check_comms.py` are placeholders; agree final values.
5. **What happens on a daytime too-much alert:** alert the resident only, or more?

Settled on 11 November 2026: nodes on mains power, gateway = LoRa + ESP32 + laptop (decision D12).

---

## 7. Known problems and limits

| Problem | Note |
|---|---|
| Simulated water use is too smooth (about 0.015 L/s at the 7 am peak, one identical daily pattern, no bursts like showers) | "0 false alarms" is weak evidence. Test the too-much check on real household data (Cache County HydroShare, 31 homes, 4-second readings, CC BY). |
| Volume = flow x 60 from one reading per minute | Real hardware should count flow-sensor pulses. |
| Dripping taps are below the 0.02 L/s night limit | Check the chosen flow sensor's minimum flow. |
| Learned baseline is not saved to the SD card | A node forgets "normal" after a power cut. With mains-only nodes every building power cut restarts every node, so this matters: save the 24-hour table to the SD card. |
| Mains only: a building power cut switches nodes off | No detection until power returns, and the tank still feeds the flats by gravity, so a leak keeps running. The simulated "power cut" event only stops the pump; nodes stay on. On return, nodes need about 30 s or more to boot. Sudden power loss can also damage an SD card being written; keep writes small. |
| Baseline learned once, from one day | A leak on day 1 is learned as normal; habits change; weekdays differ from Sundays. |
| A full radio cut gives 21 separate `NODE_SILENT` | Better: one "all nodes silent: radio or gateway problem". |
| A leak that drains the tank can trigger `SUPPLY_EVENT` | Seen in the flow-leak run at 10:31 and 17:56. |
| An alert can collide with the first slot at quarter-hour boundaries | Seen once (B-203 at 02:30:00.2 vs B-401's summary); recovered by the 2-minute re-send. |
| Retry storm during long radio cuts | About 13,000 re-send attempts in 3.5 h; add backoff. |
| No location for downtake leaks | Building check says "pipe leak", not which floor. |
| Simulation-only parts | CAD, radio, clock setting and valves are imitated; ns-3 uses EU868 (no Indian band in the module); one uplink channel modelled (IN865 has 3); packets are JSON text. |
| `matplotlib` is in `simulation/pyproject.toml` but nothing imports it directly | Can probably be removed. |

---

## 8. Next steps (suggested order)

1. Decide open decision 1 (quality model classes).
2. **Real Pi Zero 2 W test:** run one node's firmware with simulated readings; measure memory (`/usr/bin/time -v`), processor (`htop`), temperature (`vcgencmd measure_temp`).
3. **Hardware layers** (same interface as `NetHAL` in `simulation/live/node.py`):
   - `firmware/pi_hal.py` for the nodes: real LoRa module with CAD, DS3231 clock, SD card, flow pulses, buzzer, LED, valve.
   - A laptop hardware layer for the gateway that exchanges packets with the ESP32 over USB serial, plus a small ESP32 program that bridges LoRa and serial (and sends the ACKs the laptop asks for).
4. **TinyML model:** train on the chosen dataset, quantize to 8 bits, replace `classify_quality`.
5. Timestamps (UTC Unix seconds, ISO 8601 logs).
6. Save the baseline to SD; one combined "all silent" alert; re-send backoff.
7. Test the too-much check on Cache County data.
8. Slot fit check if any node ever needs SF10 or above; set slots at installation; re-run the ns-3 link table once the gateway position is known.
9. Monitoring dashboard on the laptop.
10. Merge `live-simulation` into `main` after team review.

---

## 9. How to run and check

From the repo root:

```
sh simulation/check_all.sh          # everything: self-checks, offline test, two live scenarios (~4 min)
```

From `simulation/`:

```
sh live/run_local.sh --scenario live/scenarios/standard.jsonl     # live run, no Docker (~1 min)
python3 live/check_comms.py                                       # PASS/FAIL report for the last run
docker compose -f live/compose.yaml up                            # one container per node; panel at localhost:8501
uv run streamlit run live/control_panel.py                        # control panel for the no-Docker run
python3 webots/run_offline.py                                     # 1-second firmware test
uv run python ../leak_detection/compare_leaks.py                  # flow vs pressure (~5 min; see note in file)
sh ns3/run_ns3.sh                                                 # ns-3 link table (first build 20-30 min)
sh ns3/run_ns3.sh replay                                          # ns-3 replay of the last live run
```

Scenarios are JSON lines in `simulation/live/scenarios/` (leaks, quality events, radio cuts, at given times).

---

## 10. Working conventions

- **Branch:** work is on `live-simulation`; the team repo is `riyaa42/aqua_edge`. Push after every significant change.
- **Before pushing:** run `sh simulation/check_all.sh`; stage only the files you changed (check `git status`).
- **Keep out of git:** paper PDFs (copyrighted; the repo is public; see `docs/presentations/first/references.md`), downloaded datasets (`tinyml/data/`), run output (`simulation/live/run/`, `leak_detection/results/`).
- **One firmware copy:** every test loads `firmware/`. Never copy firmware into a test folder.
- **Every folder has a README** saying what is inside; update it with the code.
- **Self-checks:** each piece of logic has one runnable check (usually an `if __name__ == "__main__":` block with `assert`s).
- **`ponytail:` comments** in the code mark deliberate shortcuts (with the reason), so they can be found and revisited later.
- **Writing style for project documents:** simple English; explain technical terms; no em dashes; every paper mention says what we use it for, as in [Ali et al. [6], for comparing supply with branch flows].

### Papers

| # | Paper | What we use it for |
|---|---|---|
| [1] | Sharma et al., Scientific Reports 2026 | 8-bit on-device neural network for water quality (planned TinyML model) |
| [2] | Alghamdi et al., Sensors 2022 | Pressure-drop leak rule and valve closure over LoRaWAN (tested, then removed in favour of flow) |
| [3] | Babazadeh, ISA Transactions 2019 | Anomaly detection on the node, only anomalies sent over LoRa |
| [4] | Mariadhason et al., Sensors 2026 | Timestamped store-and-forward to SD card; DS3231 clock |
| [5] | Pagano et al. (SWI-FEED), arXiv 2024 | Pairing a water simulator (EPANET) with ns-3 |
| [6] | Ali et al., Internet of Things 2022 | Mass balance of supply vs branch flows for pipe leaks |

Full citations: `docs/presentations/first/references.md`.

---

## 11. History (branch `live-simulation`)

| Commit | What |
|---|---|
| 513369c | Live simulation: water engine, nodes, radio model, control panel, checker |
| 6723768 | Alerts re-send when a problem comes back; gateway marks repeats |
| ec1f38f | Time slots for summaries, listen before talk for alerts (collisions 489 to 2) |
| fe488c4 | ns-3 link table and replay check; Docker containers |
| ff9c8dc | Live simulation write-up |
| 0442ff1 | Flow vs pressure comparison |
| 51cfc47 | Flow leak scenario |
| b355475 | Leak detection write-up |
| 4f7867a | Repo reorganised into firmware/, leak_detection/, tinyml/, simulation/ |
| f2be60b | Flow-only firmware; silent-node alert, building check re-arm, clock check; timing docs |
| 07fd3bd | ns3/ moved back after an accidental move |
| 67e849f | docs/ folder; `check_all.sh` |

Earlier work (on `main`): first presentation, components list, Webots test bench, early quality-check script.

---

## 12. Glossary

| Term | Meaning |
|---|---|
| Node | One Pi with sensors in a flat or at the tank |
| Gateway | The building's receiver; collects node messages and runs building-wide checks |
| LoRa | Long-range, low-power radio; small, infrequent messages; no internet |
| SF (spreading factor) | LoRa setting: higher SF reaches further but each packet takes longer |
| CAD | LoRa "channel activity detection": listening before sending |
| ACK | "Got it" reply from the gateway |
| Downtake | The main pipe from the rooftop tank down to the flats |
| Mass balance / building check | Water out of the tank compared with water used by all flats |
| Baseline | A node's learned normal: average and spread per hour of day |
| EPANET / WNTR | Pipe-network water simulator / Python library that drives it |
| ns-3 | Research network simulator used to check our radio model |
| HAL | Hardware layer: the small object through which firmware reads sensors and uses the radio, so the same firmware runs in simulation and on the Pi |
| TinyML | Small machine-learning models that run on tiny devices |
