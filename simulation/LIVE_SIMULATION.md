# Aqua Edge: Live Simulation and LoRa Network

This document explains the live simulation on the `live-simulation` branch: what each part does, the mechanisms behind it, the results so far, and what is planned next.

## 1. Why a live simulation

The earlier offline test fed every node a pre-made 48-hour file of sensor values. We wanted something closer to the real system:

- Every node runs the **real node firmware** (the same code the Raspberry Pi will run).
- Water values come from a **water model that runs minute by minute**, so events can be triggered while it runs.
- A **control panel** triggers events (leaks, water-quality changes, radio cuts). It does not type in sensor values. The water model decides what the sensors would read.
- The LoRa radio is modelled realistically (airtime, collisions, signal strength), and checked against **ns-3**, a standard network simulator.
- A **checker** decides, with pass marks, whether communication worked.

## 2. The parts

| Part | File | What it does |
|---|---|---|
| Water engine | `live/water.py` | Runs the building B water network (WNTR) one minute at a time and gives each node its readings |
| Timekeeper | `live/timekeeper.py` | The clock of the whole simulation: steps the water, applies commands, passes messages through the radio model |
| Nodes and gateway | `live/node.py` | One process (or container) per node, running the real firmware |
| Firmware | `webots/firmware/aqua_edge.py`, `gateway.py` | Detection rules, alerts, sending, re-sending, SD card storage, gateway analysis |
| Radio model | `live/radio.py` | Decides which packets arrive: airtime, collisions, radio cuts, weak links |
| Control panel | `live/control_panel.py` | Streamlit web page to trigger events and watch alerts |
| Checker | `live/check_comms.py` | Scores a finished run with PASS/FAIL lines |
| ns-3 | `ns3/aqua_lora.cc`, `ns3/run_ns3.sh` | Builds the link table for building B and replays live runs |
| Containers | `live/Dockerfile`, `live/compose.yaml` | One Docker container per node, gateway, timekeeper and panel |

### How one simulated minute works

1. The timekeeper applies any commands that are due (from the control panel or a scenario file).
2. It sends every node **only its own readings** plus any radio messages addressed to it.
3. Every node runs one step of its firmware and replies with the packets it wants to send, and when.
4. The radio model decides which packets arrive. Arrivals are delivered in the next minute.
5. The water model moves forward by one minute.

The timekeeper waits for every node before moving on (this is called *lockstep*), so a 48-hour run can take about 1 minute, or run slowly for a demo (`--delay`).

## 3. Water mechanisms

- **Stepping WNTR live.** The water model is extended one minute at a time. Its results match a single uninterrupted run to within about 0.000000000002 m of pressure.
- **Leaks.** Leaks are added while the model runs, with three fixed sizes: small 0.05 L/s (a running toilet), medium 0.5 L/s and large 1.5 L/s. The leak hole size comes from the orifice formula `A = Q / (Cd * sqrt(2gP))`.
- **Leak locations.** Leaks can go in any flat or on any downtake pipe (`B-D*`).
- **Water quality.** Runoff (rainwater-like: low TDS, lower pH) and chemical contamination (high TDS, high pH) are switched on for a set number of minutes. Normal and event values are kept in one table in `webots/make_scenario.py`.
- **Not included for now.** Valves and power outages.

## 4. Detection and alerts (firmware)

- Flat nodes detect leaks with three rules: continuous night flow, abnormal consumption, and pressure drop with high flow.
- The tank node detects abnormal supply and water-quality events.
- The gateway runs a mass balance (supply flow against the sum of flat flows) to spot pipe leaks [Ali et al. [6], for comparing the tank supply against the flats to find pipe leaks].
- **Alerts re-arm.** Once a problem has been quiet for 10 minutes (`ALERT_CLEAR_S`), the alert can fire again if the problem comes back.
- **The gateway marks repeats.** Each alert is logged as `NEW_ALERT` or `REPEAT_ALERT` (with a count and the minutes since the last one), so the laptop can decide whether a problem is new or ongoing.

## 5. Communication mechanisms in use

| Method | What it is used for |
|---|---|
| **TDMA** (own time slot per node) | Summaries and flags: each node sends only in its own 2.86 s slot of every minute |
| **Guard time** | 0.2 s at the start of each slot, in case a clock is slightly off |
| **Time sync by piggybacking** | Every gateway ACK carries the gateway's time; nodes reset their clocks from it |
| **CSMA/CA** (listen before talk, LoRa CAD) | Alerts: they go out straight away, after checking the channel is free |
| **Random backoff** | A busy channel means waiting a random 0.1 to 1 s and listening again, up to 5 times |
| **ARQ** (re-send if no ACK) | Any message not acknowledged within 2 minutes is sent again |
| **Store-and-forward** | Unsent messages wait on the node's SD card, for example during a radio cut |
| **Rate limiting** | At most 4 messages per node per minute |
| **Turnaround gap** | 10 ms between a node's own back-to-back packets |

### Why two methods

Summaries are regular and predictable, so fixed slots make collisions impossible while clocks agree. Alerts are rare and urgent, so they should not wait up to a minute for a slot. They listen first and go out at once.

### Clock drift

Real clocks run slightly fast or slow. The DS3231 clock is rated at ±2 ppm, which is about 0.17 s per day. Each simulated node gets its own drift rate, and the ACK time sync resets it. A stress test at 200 ppm (100 times worse than the rating) still delivered 99.4% of attempts.

### Does a node's traffic fit its slot?

At SF7, four back-to-back packets plus the guard time take about 0.8 s, well inside the 2.86 s slot. The worst minute in a real run used 0.91 s. Four packets still fit up to SF9. At SF10 and above they would spill into the next node's slot. Nothing in the firmware prevents that yet (see Next steps).

## 6. Radio model and ns-3

### Python radio model (`live/radio.py`)

- **Airtime** follows the Semtech LoRa formula (75 bytes: 133 ms at SF7, 3.1 s at SF12) [Semtech AN1200.13, for the time-on-air formula].
- **Collision rule:** two uplinks that overlap in time on the same channel and spreading factor are both lost.
- **Other results:** a packet sent during a radio cut is marked `radio_cut`. A packet can also be lost at random according to the link's delivery rate (`weak_link`).
- **Listen before talk:** a CAD listen does not hear a packet that started less than 2 symbols earlier, so two alerts that start at almost the same instant still collide. That is realistic.
- **Every attempt** is logged to `live/run/tx_log.csv`.

### ns-3 (`ns3/`)

ns-3 runs inside Docker, using ns-3.48 with the LoRaWAN module v0.3.7. It does two jobs:

1. **Link table.** It models building B (18 × 18 m, 5 floors, concrete walls, shadowing) with the gateway outside. For each node, it works out the spreading factor needed, the signal strength, and the share of 200 lone test packets that arrive. The result goes in `ns3/link_table.csv`, which the Python radio model reads.
2. **Replay.** It re-sends every uplink of a live run at the same moment, with the same size and spreading factor. It then reports which packets ns-3 thinks arrive. `check_comms.py` compares this with the Python model, node by node.

This follows the idea of pairing a water simulator with ns-3 [Pagano et al. [5], for co-simulating a water network with ns-3].

## 7. Results (standard 48-hour scenario)

The scenario contains:
- a radio cut for 3.5 hours
- a small leak in flat B-203
- runoff water
- a medium leak in flat B-402, added while the run was going
- a large burst on downtake B-D1
- chemical contamination

| Check | Result |
|---|---|
| Uplink attempts that got through while the radio was up | **100.0%** (pass mark 95%; was 89.3% before slots) |
| Collisions | **2** out of about 4,066 (was 489) |
| Messages never delivered | **0** |
| Usual delay with the radio up | 1 minute |
| Stored alerts delivered after the radio cut | 4 minutes after the radio came back (pass mark 5) |
| Events detected | **All 6** |
| Alerts from nodes with no event | 0 |
| Python radio vs ns-3 replay | **0.0 points** gap (pass mark 5) |
| Link quality (ns-3) | All nodes on SF7, 100% delivery, weakest node B-404 at -97 dBm (about 26 dB to spare) |
| Docker run | Same PASS results as without Docker |

## 8. Problems found and fixed along the way

| Problem | Cause | Fix |
|---|---|---|
| Simulation got slower every minute | WNTR kept adding internal listeners on every step | Clear them before each step (48 h now takes about 1 minute) |
| Run froze with 22 nodes | The timekeeper accepted only one waiting connection at a time | Accept up to 64 waiting connections |
| A later pipe burst was hidden | The tank's earlier alert stayed switched on forever | Alerts re-arm after 10 quiet minutes; the gateway marks repeats |
| About 10% of packets collided | All summaries were sent at random in the same minute | Time slots for summaries, listen before talk for alerts |
| 90 packets had no result in the ns-3 replay | A node started its next packet the very instant the last one ended | 10 ms turnaround gap between a node's own packets |
| The replay sent 4 times too many packets | A hidden end-of-line character broke a check | Strip it when reading the log |
| The ns-3 module would not build | A missing `#include` in the module's source | Added automatically in `ns3/Dockerfile` |

## 9. How to run

From the `simulation/` folder:

```
sh live/run_local.sh --scenario live/scenarios/standard.jsonl   # no Docker, one process per node
docker compose -f live/compose.yaml up                          # one container per node; panel at localhost:8501
uv run streamlit run live/control_panel.py                      # control panel for the no-Docker run
python3 live/check_comms.py                                     # PASS/FAIL report for the last run
sh ns3/run_ns3.sh                                               # ns-3 link table (first time builds ns-3, 20 to 30 min)
sh ns3/run_ns3.sh replay                                        # ns-3 replay of the last live run
```

Self-checks: `python3 live/radio.py` and `python3 webots/run_offline.py`.

## 10. Known limits

- **What is real and what is imitated.** The timing decisions are in the real firmware: slots, listen before talk, ACK time, re-sending and SD storage. The actual CAD listening, the clock setting and the radio itself are only imitated by the simulator.
- **Hearing assumption.** The radio model assumes every node can hear every other node (one building). Hidden nodes are not checked.
- **Uplink channels.** Only one uplink channel is used, although IN865 offers 3.
- **Frequency band in ns-3.** ns-3 uses the EU868 frequency, because the module has no Indian band. The physics is the same.
- **Packet format.** Packets are sent as JSON text. Packing them into bytes would shorten airtime.
- **Leak modelling.** Leaks use internal WNTR settings (tested with wntr 1.5.0).
- **Water-quality classifier.** It is still threshold rules. The TinyML model is not trained yet.
- **Gateway repeat check.** The gateway cannot yet tell "the same leak flickering" from "a new leak". It only marks repeats.
- **Retry storm.** During a 3.5-hour radio cut, nodes made about 13,000 re-send attempts.

## 11. Next steps

1. **Raspberry Pi hardware layer.** A small file like `NetHAL` for the real LoRa module (including CAD), the DS3231 clock and the SD card.
2. **Slot fit check.** Make the firmware send only as many packets as fit in its slot. Alternatively, give slower nodes wider slots. This is needed if any node ever needs SF10 or above.
3. **Slot assignment on real nodes.** Set each node's slot at installation.
4. **Real gateway position.** Re-run the ns-3 link table once the gateway's location is decided, for example in another building.
5. **Hidden nodes.** Use ns-3 node-to-node links to check whether nodes can hear each other.
6. **Retry storm.** Back off re-sends during long radio cuts to save energy.
7. **New or ongoing leak.** Decide on the laptop whether a repeat alert is a new problem or the same one.
8. **TinyML model.** Train the water-quality model and replace the threshold rules.
9. **Monitoring dashboard.** Build a Grafana dashboard for the real output.
10. **Pass marks.** Agree final pass marks as a team.
