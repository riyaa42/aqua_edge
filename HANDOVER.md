# Aqua Edge: Handover

Status as of 11 November 2026, branch `live-simulation`. This file is the single place to catch up on the project: what exists, why it was built that way, how to check it, and what is still open. Read it first, then follow the links into the folder READMEs for detail.

**Suggested reading order:** this file, then `leak_detection/README.md` (detection method and math), `simulation/LIVE_SIMULATION.md` (simulation and radio), `firmware/README.md`, `tinyml/README.md`, `docs/components.md` (hardware).

---

## 1. The project in one paragraph

Aqua Edge (CSD457 Internet of Things; earlier names: JalRakshak, HydroDrive) is an **offline-first** water monitoring system for one residential building, "building B": 20 flats on 5 floors (G, 1 to 4; 4 flats per floor) fed by a rooftop tank through a shared downtake pipe. Every flat and the tank get a small node (Raspberry Pi Zero 2 W on mains power) with sensors. Nodes detect **leaks, abnormal consumption and unsafe water on the device itself**, warn locally (buzzer, LED; the project has no valves), and send alerts and 15-minute summaries over **LoRa** (long-range, low-power radio; no internet needed) to a gateway in the building: a LoRa module on an ESP32 board, connected by USB to a laptop that runs the gateway program and does building-wide checks. Nothing depends on the internet or a cloud.

---

## 2. Repository map

| Path | What it is |
|---|---|
| `firmware/aqua_edge.py` | **The real node code.** Flat node and tank node. Standard library only. Talks to hardware only through a small hardware layer object (`hal`). |
| `firmware/gateway.py` | **The real gateway code.** ACKs, building check, silent nodes, supply events, repeat alerts, clock check. Has a self-check (`python3 firmware/gateway.py`). |
| `leak_detection/README.md` | How leak detection works: checks, math, timing, results, known problems. |
| `leak_detection/compare_leaks.py` | Flow vs pressure experiment (35 runs). Run from `simulation/`. Results go to `leak_detection/results/` (not in git). |
| `tinyml/` | Water-quality model: `train_quality.py` (data, training, 8-bit quantization, tests), results, datasets. The node runs `firmware/quality_model.py`. |
| `simulation/live/` | **Main test system.** Live simulation: water engine, timekeeper, one process or Docker container per node, LoRa radio model, dashboard, checker, scenarios. |
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
  - *Night check:* flow above 0.02 L/s for 30 minutes in a row between 1 and 5 am gives `LEAK_NIGHT_FLOW` (nobody runs water steadily at night).
  - *Continuous-flow check:* flow above 0.005 L/s for 8 hours in a row, any time of day, gives `LEAK_CONTINUOUS_FLOW` (a home's water is off now and then; a leak never stops). Catches small daytime leaks early and leaks below the night limit.
  - *Too-much check:* flow above max(3 x this hour's learned average, 0.05 L/s) for 60 minutes gives `ABNORMAL_CONSUMPTION` (it could be guests, not a leak).
  - Pressure is still read, learned and sent in summaries, but raises no alarm (section 5, decision D1).
- **Tank node:**
  - *Outflow check:* outflow above average + max(4 x spread, 0.5 L/s) for 15 minutes gives `ABNORMAL_SUPPLY` (pipe burst).
  - *Level low:* level below average - max(4 x spread, 0.3 m) for 10 minutes sends a `SUPPLY_LOW` flag (pump or power problem).
  - *Water quality:* pH, TDS, turbidity (NTU) into an **8-bit neural network** (`firmware/quality_model.py`, 308 bytes): NORMAL / RUNOFF / CHEMICAL. Trained on real Kenyan tap water mixed with runoff or cleaner, labelled by the Indian standard IS 10500; 99.5% on held-out data, 0 false alarms on real held-out tap water (`tinyml/README.md`). Fires after 3 bad readings in a row (about 2 to 3 minutes); `QUALITY_CLEARED` when it is normal again.
- **One alarm per problem:** an alert re-arms after 10 quiet minutes (`ALERT_CLEAR_S`).
- **Messages:** `ALERT` (urgent), `FLAG`, `SUM` (15-minute summary: litres used, lowest pressure). Every message carries node id `n`, sequence `s`, node time `t`.

### 3.2 Gateway (`firmware/gateway.py`)

Hardware: LoRa module + ESP32 (radio bridge: passes packets between LoRa and USB serial) + laptop. `gateway.py` runs **on the laptop**; it needs a small laptop-side hardware layer that reads and writes packets over the USB serial link.

1. **ACK** every message, 1.5 s into the sender's own time slot (`ACK_AT_S`), so a reply never lands in another node's slot; the ACK carries the gateway time `gt`, and nodes reset their clocks from it. Duplicates are dropped; each logged message gets the receive time `rx`.
2. **Down pipe checks, per floor (2026-11-27):** a flow meter above each lower floor, read by flats 301, 201, 101, G01 (`pipe_l` field). Each floor's stretch: meter in minus meter out minus its 4 flats. Fast check: more than 30 L lost in a 15-minute block; night check: more than 20 L from 02:00 to 04:00; both give `PIPE_LEAK` with `where` (floor) and `check`. Limits measured on 3 clean simulated days (leak_detection/README.md 5.4). Without pipe meter numbers it falls back to the whole-pipe night check described next. **Tank temperature:** `QUALITY_HOT` above 35 C (Legionella grows fastest near 37 C), clears like the other quality alerts. **Earlier whole-pipe check, once a night (mass balance)** [Ali et al. [6], for comparing tank supply with the flats' use to find pipe leaks]: litres out of the tank minus litres used by all flats from 02:00 to 04:00, the quietest hours (like a water company's minimum night flow check). More than 20 L missing gives `PIPE_LEAK` (with the litres and the leak size in L/s). Alerts once, re-arms after a clean night. Clean nights measured -4 to +5 L with realistic meters; a 0.005 L/s pipe leak gives about 40 L, so leaks from about 0.003 L/s are found, but only at the next night. Big bursts are found in about 15 minutes by the tank node. Replaced a 10%-per-15-minutes rule, which nearly missed a small leak at the 7 am peak (12%) and could false-alarm at night (4 L of meter error on 14 L read as 24%). Cannot see leaks inside a flat (those pass the flat's meter; the flat's own checks catch them).
3. **Silent nodes:** nothing heard for 45 minutes gives `NODE_SILENT`; `BALANCE_PAUSED` names the silent nodes (the night check waits rather than guess); `NODE_BACK` when heard again, and the late blocks are then checked from the stored summaries.
4. **Supply event:** the tank's `SUPPLY_LOW` flag gives `SUPPLY_EVENT` (our own rule, not from a paper).
5. **Repeat check:** each alert is marked `NEW_ALERT` or `REPEAT_ALERT` (count, minutes since last).
6. **Clock check:** a message stamped more than 1 minute in the future gives `CLOCK_SKEW`.

### 3.3 Communication

| Method | Used for |
|---|---|
| Own time slot per node (TDMA) | Summaries: node k sends at second k x 60/21 + 0.2 of the minute (2.86 s slots, 0.2 s guard) |
| Listen before talk (LoRa CAD) + random backoff 0.1 to 1 s, up to 5 tries | Alerts: sent the same minute they fire |
| Gateway replies inside the sender's slot (idea from LoRaWAN Class A) | Every ACK: the gateway is one radio and cannot hear while it sends, so its replies stay out of other nodes' slots |
| Re-send if no ACK within 2 minutes | Everything |
| Encrypted, signed binary packets (`firmware/packet.py`) | Every packet, ACKs included: about a third of the JSON size, body encrypted with AES-128 in counter mode (adds no bytes; header with type, node, counter and time stays readable, as in LoRaWAN), 4-byte signature (HMAC-SHA256) with a key shared by that node and the gateway. Forged or changed packets are dropped (`BAD_PACKET`); a repeated counter is a duplicate or replay and is not used again, even after a gateway restart. The encryption key is derived from the node's key, so there is still one key file. |
| Store-and-forward to SD card | Messages wait during radio cuts [Mariadhason et al. [4], for timestamped store-and-forward to SD card] |
| At most 4 messages per node per minute; 10 ms gap between own packets | Keeps traffic inside the slot |

Airtime is about 67 ms per 29-byte summary and 46 ms per 14-byte ACK at SF7 (was 133 ms for the old 75-byte JSON) (SF = spreading factor, LoRa's speed vs range setting). ns-3 says every node in building B reaches the gateway on SF7 with about 26 dB to spare.

### 3.4 Time

- Every message has `t` (when the node measured or decided) and, at the gateway, `rx` (when it arrived). Stored messages keep their original `t`.
- Simulation: seconds since the start of the run. Real nodes: DS3231 real-time clock (rated +-2 ppm), reset from every ACK.
- **Planned, to be discussed first:** UTC Unix seconds on the nodes, ISO 8601 UTC times in log files, local time (IST) only for display.

### 3.5 Simulation and testing

| Setup | Water | Radio | Use |
|---|---|---|---|
| **Live simulation** (`simulation/live/`) | EPANET through WNTR (Python library driving the EPANET pipe simulator), stepped minute by minute, so leaks can be added mid-run | `live/radio.py`: airtime, collisions, cuts, weak links; checked against ns-3 replay | **Main test.** Real firmware in 22 processes, or 24 Docker containers (nodes, gateway, timekeeper, dashboard) |
| **Offline test** (`simulation/webots/run_offline.py`) | Pre-made 48 h file `webots/data/sensors.csv` | Outage times only | 1-second firmware regression test |
| **Webots** (`simulation/webots/`) | Same pre-made file | Simple in range / out of range | 3D view only; parked |

Docker: `simulation/live/Dockerfile` builds **one** image (Python 3.12 + libraries, no code); `simulation/live/compose.yaml` starts one container per node from it, mounts `simulation/` and `firmware/` into each, and gives each its `NODE_ID`. Editing the firmware needs no rebuild.

Water quality values in the simulation are made up (EPANET cannot model pH or TDS).

---

## 4. Current results

From `sh simulation/check_all.sh` on 11 November 2026: **all checks pass.**

| Scenario | Result |
|---|---|
| Standard (48 h): 3.5 h radio cut, small flat leak, runoff, downtake burst, chemical | All events caught; stored alarms delivered 4 min after the radio returned; 100% of attempts through while radio up; 0 lost; 0 false alarms; radio model vs ns-3 gap 0.0 points. Gateway modelled as one half-duplex radio on the shared channel (2026-11-27): 0 uplinks or replies lost with replies in the sender's slot (31 and 26 lost with replies at a random second) |
| Flow leaks: small leak in B-203 at 02:00, medium leak in B-402 at 10:00, large burst on downtake B-D1 at 18:00 | Night check 30 min (continuous-flow check also fires at 8 h); too-much check 60 min; tank outflow 15 min and building check 31 min (67% of water missing); 0 false alarms |
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
| D11 | **No valves.** The response to any problem is the buzzer, the LED and an alert to the gateway. | Team decision (November 2026). The valve code was removed. |
| D12 | **Hardware: each node is a Raspberry Pi Zero 2 W on mains power (no battery); the gateway is LoRa module + ESP32 + laptop.** | Mains removes the battery-life problem of a Pi running Linux (about 0.5 to 0.7 W even when idle). The laptop has the power and screen for gateway analysis and a dashboard; the ESP32 only bridges the radio. |
| D13 | **Water-quality model: real tap water mixed with runoff or cleaner, labelled by IS 10500; no temperature input; 8-bit network in plain Python.** | No public data has runoff/chemical labels. Temperature only told sources apart in the Kenya data, not contamination. A 308-byte model needs no TensorFlow on the Pi. |

---

## 6. Open decisions (need the team)

1. **Water-quality model classes:** settled as normal / runoff / chemical (the firmware's alerts and Sharma et al. [1]); labels from IS 10500 on mixtures of real tap water. Revisit if the team wants a "normal vs unusual for this tank" check too (`tinyml/README.md` section 4).
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
| Leaks below the 0.02 L/s night limit | Now caught by the continuous-flow check above 0.005 L/s. Sensors are simulated and ideal. |
| Mains only: a building power cut switches nodes off | No detection until power returns, and the tank still feeds the flats by gravity, so a leak keeps running. The simulated "power cut" event only stops the pump; nodes stay on. On return, nodes need about 30 s or more to boot. Sudden power loss can also damage an SD card being written; keep writes small. |
| Baseline learned from day 1, then a slow EWMA (about 2 weeks, paused during alerts) | A leak on day 1 is still learned at first (the fixed night and continuous-flow checks catch it); weekdays differ from Sundays. |
| A full radio cut gives 21 separate `NODE_SILENT` | Better: one "all nodes silent: radio or gateway problem". |
| A leak that drains the tank can trigger `SUPPLY_EVENT` | Seen in the flow-leak run at 10:31 and 17:56. |
| An alert can collide with the first slot at quarter-hour boundaries | Seen once (B-203 at 02:30:00.2 vs B-401's summary); recovered by the 2-minute re-send. |
| Retry storm during long radio cuts | About 13,000 re-send attempts in 3.5 h; add backoff. |
| No location for downtake leaks | Building check says "pipe leak", not which floor. |
| Simulation-only parts | CAD, radio and clock setting are imitated; radios are SX1278 at 433.175 MHz and 10 dBm (India: 10 mW, 10% time on air in 433.05 to 434.79 MHz); ns-3 has no 433 MHz plan, so it keeps an 868 MHz channel name with 6 dB less distance loss; one channel (our gateway is one SX1278); keys are made from the node name (`sim_key`); real keys come from a key file on the SD card (`load_keys` in `firmware/packet.py`), used by `firmware/pi_hal.py`. |
| `matplotlib` is in `simulation/pyproject.toml` but nothing imports it directly | Can probably be removed. |

---

## 8. Next steps (suggested order)

**Simulation only from 2026-11-27: no hardware.** The project is shown through the simulation and the dashboard. `firmware/pi_hal.py` stays as the written design for a real node (untested); the Pi test, the gateway laptop's USB layer and the ESP32 bridge are dropped.

1. **Encryption:** done (2026-11-27). Packet bodies are encrypted with AES-128 in counter mode (`firmware/packet.py`).
2. **Dashboard leak demo:** done (2026-11-27). `python3 live/dashboard.py`, then open localhost:8080 (standard library only, works offline; replaces the Streamlit control panel). Press Start simulation: the flats race through their learning day and the run pauses at day 2, 00:00. Pick a flat on the building, pick Small / Medium / Severe or any size on the slider (0.01 to 2 L/s), start it now or at a set hour. Add as many leaks as you like over time and run the clock yourself (Pause, Play, speed), or press Start and fast-forward: the simulation runs ahead until that flat's alert reaches the gateway, then pauses. Each leak in the run is listed with its own Fix button and shows when the leak started, which check caught it, how long that took and when the gateway knew. **Start again** wipes the run and starts fresh from day 1 at any time (laptop run only; under Docker, restart the containers). The building is drawn as a see-through cutaway from the real node positions (rooms coloured by what the gateway knows, each node's own red light and buzzer, rising and down pipes, leak spots on the down pipe, tank water level and colour, gateway waves when an alert arrives); phones get simple tiles. An alarm banner with a beep lists every problem the gateway knows about (leaks, runoff, chemical, tank emptying too fast) with what to do, until it is fixed or the water is clean again. A card lists what each node measures. Also: gateway feed, tank level and quality, water-quality and radio-cut buttons. On a 1920x1080 laptop the important parts fit one screen in three columns (building; add a leak and a Water quality card with the clean/not-safe verdict, the pH and TDS sent with the last bad-water alert, the latest runoff or chemical test step by step and the tank level; alarms, leak list, gateway feed) with three radio numbers, the radio cut and Stop in the top bar; the sensor list, drawing key and full radio numbers sit below. Example: a small leak (0.05 L/s) in flat 203 at midnight was caught by the night-flow check at 01:29 and reached the gateway at 01:30.
3. **Security demo:** an attacker in the radio model that sends forged packets, replays old ones and listens in, plus a checker line proving all forged and replayed packets were rejected and the bodies were unreadable. Also make `NetHAL` (simulated node) check the ACK signature before setting its clock, as `pi_hal.py` already does.
4. **Retry storm:** wait longer between re-sends the longer a radio cut lasts (about 13,000 re-send attempts in a 3.5 h cut today).
5. **Realistic water use:** test the leak checks on real household data (Cache County HydroShare, 31 homes, 4-second readings), because the simulated use is too smooth for "0 false alarms" to mean much.
6. One combined "all nodes silent" alert instead of 21 separate `NODE_SILENT`.
7. Timestamps in logs (ISO 8601).
8. Agree final pass marks in `simulation/live/check_comms.py`.
9. Slot fit check if any node ever needs SF9 or above (gateway replies included).

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
docker compose -f live/compose.yaml up                            # one container per node; dashboard at localhost:8080
python3 live/dashboard.py                                        # dashboard and leak test at localhost:8080
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
| [1] | Sharma et al., Scientific Reports 2026 | 8-bit on-device neural network for water quality (our model's shape and classes) |
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
| d5f05c3 | Power-cut-safe SD saves (`os.replace`); all valve code removed |
| a470ed8 | Baseline saved to SD hourly; continuous-flow check |
| 411ee85 | Signed binary radio packets; message counter that never repeats; replay record on the gateway |
| 65b767b | Rolling baseline (EWMA, 2 weeks, paused during alerts) |
| (next) | TinyML water-quality model: 8-bit network trained on real tap water + IS 10500 labels; turbidity now in NTU |

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
