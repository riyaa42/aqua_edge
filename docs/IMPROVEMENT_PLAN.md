# Aqua Edge: Improvement Plan

Agreed on 2026-11-21. **Nothing in this plan is built yet.** This file is the to-do list and the reasons behind it.

It comes from two reviews:

- a review of the leak detection code
- research on how other systems already solve the problems we face

## 0. Ground rules

- **No valves.** The response to any problem is the buzzer, the LED and an alert to the gateway.
- **Sensors are simulated and ideal.** On the real hardware, each node Pi reads recorded sensor readings **row by row from a file** (one row per minute), in place of real sensors. Real sensor limits (accuracy, smallest flow a sensor can see) are therefore out of scope. A note about them is kept in section 7.
- **The radio stays plain LoRa.** We borrow three ideas from LoRaWAN: signatures, counters and binary messages.
- **Every step:**
  - every new piece of logic gets one runnable self-check
  - `sh simulation/check_all.sh` passes
  - the docs are updated
  - the work is pushed

## 1. Summary

| # | Item | Decision | Phase |
|---|---|---|---|
| 1 | SD queue lost if power cuts during a save | Fix with `os.replace` | 1 |
| 2 | Valve code still in the repo | Remove | 1 |
| 3 | Baseline lost on every reboot | Save to SD | 1 |
| 4 | Small daytime leaks wait up to about 20 h | Continuous-flow check | 1 |
| 5 | Anyone can fake or replay radio messages | Sign messages, use counters | 2 |
| 6 | JSON text wastes airtime | Binary messages | 2 |
| 7 | Baseline frozen after day 1 (fix 4) | Rolling update with EWMA: **discuss first** | 3 |
| 8 | Night water use in Indian homes causes false alarms | **Discuss first** | 3 |
| 9 | Building check says "a leak somewhere", not where | **Discuss first** | 3 |
| 10 | Flow meters never agree exactly (fix 6) | Calibration offset in the building check | 3 |
| 11 | Node reads rows from a file on the real Pi | Build the file-reading hardware layer | 4 |
| 12 | SD card damage on power cuts | Read-only system with an overlay | 4 |
| 13 | A frozen node stays frozen | Hardware watchdog | 4 |
| 14 | Legal radio limits in India | Read the 2021 rules, set channels | 4 |
| 15 | Dashboard, sensor calibration reminders | At the end | 5 |

## 2. Phase 1: keep data safe

### 2.1 Save the SD queue safely

- **Problem.** `Store._save()` in `firmware/aqua_edge.py` opens the queue file with `"w"`, which **empties the file first** and then writes it again. If the power cuts in between, every unsent message is lost. Our nodes have no battery, so this will happen.
- **Fix.** Write to a temporary file (`queue.tmp`), call `flush()` and `os.fsync()`, then `os.replace(tmp, real)`. `os.replace` is *atomic*: after a power cut you have either the old file or the new one, never half of each.
- **Reuse.** This becomes one small helper, used by the queue (2.1), the baseline (2.3) and the message counter (3.2).
- **Self-check.** Simulate a crash before the replace and check that the old queue is still complete.

### 2.2 Remove the valve code

Carried over from the leak detection review. Remove the following:

- `close_valve`, `open_valve` and `valve_closed` in the firmware
- `set_valve` in the hardware layers (`simulation/live/node.py`, `simulation/webots/webots_hal.py`)
- valve mentions in the docs

In `simulation/webots/run_offline.py`, replace the "no false valve closures" test with a "no false alerts" test.

### 2.3 Save the baseline to SD

- **Problem.** The baseline (the learned 24-hour usage table) lives only in memory. After every power cut, a flat goes about 24 h with no leak detection while it learns again.
- **Fix.** Save it with the 2.1 helper whenever it changes, and load it at startup.

### 2.4 Continuous-flow check

- **Problem.** A small leak that starts in the daytime is only caught by the night check, up to about 20 h later.
- **Fix.** Raise an alert if flow **never drops to zero for N hours**. Commercial ultrasonic meters do the same: Ultrimis raises a leak alarm after 720 minutes of steady low flow.
- **To decide.** The value of N (suggested 6 h) and what counts as "zero" (suggested 0.005 L/s).

## 3. Phase 2: secure, compact radio messages

### 3.1 Why

- **No security today.** Plain LoRa has no encryption and no way to prove who sent a message. With a cheap radio, someone could:
  - send fake LEAK alerts
  - replay old messages
  - **send fake ACKs**, which make nodes delete messages the gateway never received. This one is the most dangerous.
- **Big messages.** JSON text is several times bigger than the same data packed as binary. Bigger messages mean more airtime, more collisions between our 21 nodes, and less room under India's radio rules. Research reports 82 to 94% savings against JSON.

### 3.2 Design: one shared module, `firmware/packet.py`

It is used by the nodes and the gateway, and needs only the standard library (`struct`, `hmac`, `hashlib`).

| Field | Bytes | Meaning |
|---|---|---|
| type | 1 | SUM, ALERT, FLAG or ACK |
| node id | 1 | 0 to 20 (0 = tank), 255 = gateway |
| counter | 4 | Sequence number. **Never repeats**, saved to SD before every send |
| time | 4 | Seconds, from the node clock |
| body | depends on type | Values as scaled whole numbers (for example litres x 100). Alert kinds as code numbers |
| tag | 4 | First 4 bytes of HMAC-SHA256 over everything above. LoRaWAN also uses a 4-byte check |

- **Keys.** One secret key per node, kept in a file on that node's SD card. The gateway has a file with all the keys. Key files are **never committed** (add them to `.gitignore`).
- **Gateway checks, in order:**
  1. The tag is right. If not, drop the message.
  2. The (node, counter) pair has not been seen before. If it has, it is a replay or a duplicate: drop it.
  3. The existing checks (clock, repeat alert and the rest) run as today.
- **ACKs are signed by the gateway too.** A node only deletes a message after a correctly signed ACK.
- **Why we can't simply require "counter higher than the last one".** Store-and-forward and our priority order (alerts first) mean messages arrive out of order on purpose. The gateway therefore keeps a saved record of the counters it has already seen for each node. This is the same idea as today's duplicate removal, but saved to disk so it survives a gateway restart.
- **Simulation.** `simulation/live/radio.py` line 71 counts JSON bytes. Switch it to the packed length. Airtime and collision results will change, so re-run every check and update the result numbers in the docs.
- **Self-checks:**
  - pack then unpack returns the same message
  - a changed byte fails the tag check
  - a replayed message is dropped
  - a fake ACK is ignored

### 3.3 To discuss before building

1. Is a 4-byte tag enough for us, or should we use 8 bytes (safer, 4 more bytes on air)?
2. How do the keys get onto each node at installation, and what happens if one node's key leaks?
3. Should messages also be **encrypted** (hidden), or only signed (readable but tamper-proof)? Flat water use is personal data.

## 4. Phase 3: smarter leak detection (discuss first)

### 4.1 Rolling baseline with EWMA (fix 4)

- **Problem.** The baseline is frozen after day 1:
  - a leak present on day 1 is learned as "normal"
  - real changes in habits (a guest, summer) are never learned
- **Proposal.** EWMA (an average that gives recent days more weight). For each hour of the day:

  `mean = mean + a x (today - mean)`

  with a small `a`, for example 1/14, which remembers about 2 weeks. The baseline is **only updated while no alert is active**, so a leak is never learned as normal. A study of apartment leaks found EWMA caught leaks sooner than fixed thresholds.
- **To discuss:**
  1. How fast should the baseline adapt? Faster learns habits quickly, but also learns a slow leak quickly.
  2. Should we also add CUSUM (a running total of small differences) to catch leaks that grow slowly over weeks?

### 4.2 Night water use in Indian homes

- **Problem.** Filling buckets or tanks during night supply hours looks like a leak to the night check.
- **Standard method.** Minimum Night Flow (MNF), from the International Water Association, subtracts **legitimate night use** before calling something a leak. Research warns that guessing this value causes false alarms. It has to be learned.
- **Options:**
  - **A.** Raise the night threshold. This is simple, but it hides small leaks.
  - **B.** Learn each flat's normal night use as part of the baseline, then alert only on flow above it.
  - **C.** Test for **steadiness**. A leak is a steady flow, while filling is high, uneven and stops when the bucket is full. Alert only on steady flow for 30 minutes.
  - **D.** Ignore night flow during known supply hours (the tank's SUPPLY_LOW and refill pattern).
- **Suggested.** B + C.
- **To discuss.** When does supply actually come in the building we model, and is it the same every day?

### 4.3 Finding where the leak is

- **Today.** The building check uses water in vs water out, following Ali et al. [6] (we use their main-pipe vs branch flow comparison for our building check). It can only say "pipe leak somewhere in building B". Building B is one rooftop tank, one downtake pipe, then 5 floors with 4 flats each.
- **Options:**
  - **A. Floor meters.** One extra flow meter on each floor branch (5 meters) and a water balance per floor. The leak is then found to the floor. A New York building study found meters on each riser (vertical pipe) to be the best value for finding leaks.
  - **B. Use pressure we already measure.** Every flat node already reads `pressure_m`. A pipe leak lowers pressure most on the floors nearest to it, so the pattern across floors points to where the leak is. Alghamdi et al. [2] use a pressure-drop leak rule; we would use pressure drops to point to a floor. **No new hardware.**
- **Suggested.** Try B in the simulation first, since WNTR already computes pressure for every flat. Fall back to A if B is not clear enough.
- **To discuss.** Is "which floor" good enough for a maintenance person, or do we need "which pipe section"?

### 4.4 Calibration offset in the building check (fix 6)

- **Problem.** Two real flow meters never agree exactly. A meter that reads 3% high makes the building look like it has a 3% leak, or hides a real one.
- **Fix.** Add one constant, `METER_OFFSET_PCT`, measured during a quiet period with no leaks and subtracted before the 10% rule. It is a calibration knob, because real meters drift.
- **Self-check.** A planted 3% offset no longer causes PIPE_LEAK.

## 5. Phase 4: real hardware readiness

### 5.1 Reading sensor rows from a file

- **What.** On the real Pi, the node reads one row per minute from a CSV file in place of real sensors. Example columns: `t, flow_lps, pressure_m` for flats, and `t, outflow_lps, level_m, ph, tds, turbidity, temp_c` for the tank.
- **How.** One small hardware layer with the same interface as `NetHAL` in `simulation/live/node.py`. Its `read()` returns the next row. Each node's file is exported from the simulation, so hardware and simulation runs can be compared row for row.
- **Hardware parts.** The radio (LoRa module), clock, SD card, buzzer and LED are real.

### 5.2 Read-only system with an overlay

Sudden power loss is the classic cause of SD card damage on a Raspberry Pi. The fix, from Raspberry Pi's own whitepaper:

- lock the system files as read-only, with changes going to RAM (the overlay option in `raspi-config`)
- keep our data (queue, baseline, counter) on a small separate writable partition

### 5.3 Hardware watchdog

Set `RuntimeWatchdogSec=15` in systemd. If the node software freezes, the Pi reboots itself, and the saved queue and baseline (Phase 1) mean nothing is lost.

### 5.4 Legal radio limits in India

- India's band is **865 to 867 MHz**, under the 2021 licence exemption rules.
- Sources disagree on the duty cycle (the share of time a device may transmit): some say none, some say 1%.
- Read the official rules PDF and check our airtime against the limit using the radio log.
- Use the standard IN865 channels: 865.0625, 865.4025 and 865.985 MHz.

## 6. Phase 5: at the end

- **Dashboard.** Grafana (a ready-made dashboard tool) on the laptop, reading the gateway's data. It works offline.
- **Water-quality calibration reminders.** Cheap pH, TDS and turbidity probes drift; one river study needed weekly cleaning and recalibration. The tank node records the date of the last calibration and warns when it is overdue.

## 7. Parked notes (not doing now, and why)

- **Real flow sensor limits.** A common hobby sensor (YF-S201) only counts flow above about 1 L/min (0.017 L/s), with about 10% error. Our night threshold is 0.02 L/s, right at that limit. Ultrasonic meters start at about 1 L/hour (look for an ISO 4064 rating of R400 or higher). This does not matter while sensors are simulated and ideal. Revisit it if real sensors are ever used.
- **Full LoRaWAN with ChirpStack.** Not chosen: a proper LoRaWAN gateway needs an 8-channel radio, and our ESP32 with one LoRa module is not one. We borrow its ideas instead (Phase 2).
- **Mesh radio (Meshtastic, Reticulum) and Wi-Fi HaLow.** Not needed unless some flats cannot reach the gateway directly.
- **Pressure-hold leak tests (as in Moen Flo).** These need a shutoff valve. No valves in this project, so they are ruled out.
- **Wi-Fi instead of LoRa.** The Pi Zero 2 W has Wi-Fi built in. We keep LoRa so that alerts still reach the gateway when the building's router or internet is down. Be ready to explain this choice.

## 8. Order of work

1. **Phase 1, all of it:** small, and it stops data loss.
2. **Phase 2:** after the three questions in 3.3 are answered.
3. **Phase 3:** after the discussions in 4.1 to 4.3. Item 4.4 can be done at any time.
4. **Phase 4.** Item 5.4 is reading only and can be done at any time.
5. **Phase 5.**

## 9. Sources

- Raspberry Pi, [Making a more resilient file system](https://pip-assets.raspberrypi.com/categories/685-app-notes-guides-whitepapers/documents/RP-003610-WP-3-Making%20a%20more%20resilient%20file%20system.pdf) (why and how to make the Pi's SD card safe from power cuts)
- [systemd hardware watchdog](https://support.tq-group.com/en/general_information/hw-watchdog-systemd) (how to make a frozen Pi reboot itself)
- [The Things Network: LoRaWAN security](https://www.thethingsnetwork.org/docs/lorawan/security/) (signature and counter ideas we borrow)
- [Replay attacks and timestamps in LoRaWAN](https://arxiv.org/pdf/1905.01679) (why signatures alone don't stop every replay)
- [YACTS payload encoding, MDPI Sensors 2023](https://mdpi.com/1424-8220/23/11/5124/html) (size savings of binary messages over JSON)
- [India 865 to 867 MHz exemption rules, 2021](https://dotws.cdot.in/sites/default/files/The%20use%20of%20low%20power%20equipment%20in%20the%20frequency%20band%20865-867%20MHz%20for%20short%20range%20devices%20%28Exemption%20from%20License%29%20Rules%2C%202021.pdf) (legal radio limits)
- [Control charts for residential water use](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10439885/) (evidence that EWMA catches apartment leaks sooner)
- [Minimum Night Flow and legitimate night use](https://www.preprints.org/manuscript/202411.1010/v1) (why night use must be learned, not guessed)
- [Riser submetering in a New York building](https://habitatmag.com/Publication-Content/Building-Operations/2015/2015-June/Early-Warning-System-Tracks-Down-Leaks) (best-value meter placement for finding leaks)
- [Ultrimis ultrasonic meter](https://lora-alliance.org/wp-content/uploads/2025/09/2025.055.EN-Ultrimis-1.pdf) (industry continuous-flow leak alarm, 720 minutes)
- [YF-S201 flow sensor](https://components101.com/sensors/yf-s201-water-flow-measurement-sensor) (parked note on real sensor limits)
- [Long-term stability of low-cost water-quality sensors, MDPI Water 2024](https://mdpi.com/2073-4441/16/12/1708) (why calibration reminders are needed)
