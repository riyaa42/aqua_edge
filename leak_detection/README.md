# Aqua Edge: How Leak Detection Works

This document explains, step by step, how Aqua Edge finds water leaks in building B: which sensors are used, where each decision is made, the simple math behind every check, how the results reach the gateway, how well it worked in testing, and what still needs fixing.

All numbers below come from the code in `../firmware/aqua_edge.py` (the node firmware) and `../firmware/gateway.py` (the gateway).

### Where the code is

| What | File | Where in it |
|---|---|---|
| Learning "normal" (hourly average and spread) | `../firmware/aqua_edge.py` | `class Baseline` |
| All settings (limits, waits) | `../firmware/aqua_edge.py`, `../firmware/gateway.py` | Constants at the top of each file |
| Night check, continuous-flow check, pressure-assisted leak check, too-much check (every flat node) | `../firmware/aqua_edge.py` | `FlatNode.check` |
| Litres, `p_min`, and `p_mean` per 15 minutes sent to the gateway (flat node) | `../firmware/aqua_edge.py` | `FlatNode.accumulate` |
| Tank outflow check, tank level flag (tank node) | `../firmware/aqua_edge.py` | `TankNode.check` |
| Building check (water in = water out) | `../firmware/gateway.py` | `Gateway.mass_balance` |
| Supply-event rule | `../firmware/gateway.py` | `Gateway.supply_rule` |
| Silent nodes, clock check | `../firmware/gateway.py` | `Gateway.check_silence`, `Gateway.check_clock` |
| New vs repeat alert | `../firmware/gateway.py` | `Gateway.check_repeat` |
| Flow vs pressure comparison | `compare_leaks.py` (this folder) | |
| Live test scenario | `../simulation/live/scenarios/flow_leaks.jsonl` | |

The same firmware files run in the simulation now and are meant to run on the Raspberry Pi 4 nodes and the gateway laptop later.

---

## 1. The big picture

Building B has a rooftop tank. Water flows from the tank down one vertical pipe (the **downtake**) and branches off into each of the 20 flats.

```
            [ TANK ]  <- tank node: measures water leaving the tank
               |
             B-D4 ---- flats on floor 4 (B-401 ... B-404)
               |
             B-D3 ---- flats on floor 3
               |
             B-D2 ---- flats on floor 2
               |
             B-D1 ---- flats on floor 1
               |
             B-D0 ---- ground floor flats (B-G01 ... B-G04)

   Every flat has its own node with a flow meter and a pressure sensor.
   All nodes talk to one gateway over LoRa radio.
```

There are two kinds of leak:

| Kind of leak | Where it is | Who can see it |
|---|---|---|
| **Flat leak** | Inside a flat, after its flow meter (a running toilet, a burst pipe under a sink) | That flat's own node |
| **Pipe leak** | On the downtake, between the tank and the flats | The tank node and the gateway |

---

## 2. Why we use flow, not pressure

We tested both methods on 35 simulated runs (`compare_leaks.py`). Leaks were placed in 5 flats and on all 5 downtake sections, in three sizes, plus 5 runs with no leak at all.

| | Flow | Pressure |
|---|---|---|
| Leaks caught | **30 out of 30** | Missed small leaks in practice |
| False alarms | **0** | **748** (almost all from the wrong flat) |
| Speed on big flat leaks | 60 min | 10 min |

**The reason, in plain words:**

- **Flow is measured on each flat's own pipe.** Water only goes into a flat if someone uses it or it leaks out. A leak in B-203 adds water only to B-203's meter. So a flow alarm always points to the right flat.
- **Pressure is shared.** All flats hang off the same downtake. A hole anywhere lowers the pressure for everyone on that pipe, like one person turning on a hose and every tap in the house getting weaker. So pressure can say "there is a leak somewhere" but not "where". A small leak barely changes pressure at all, so it hides inside normal sensor noise.

**Update (current `edge-simulation` firmware):** Pressure-assisted flat leak detection (`LEAK_PRESSURE_DROP`) has been **restored** by requiring **both** a pressure drop from the flat's learned hourly baseline (`p_drop > max(3.0 * p_std, 0.35 m)`) **and** abnormally elevated flow on that same flat (`flow > max(3.0 * f_mean, f_mean + 3.0 * f_std, 0.20 L/s)`) for `10 min`. This eliminates all `748` neighbor false alarms (`0` false alarms across all 35 runs in `compare_leaks.py` and `0` across 14 normal days) while retaining **`10 min`** detection on medium (`0.5 L/s`) and large (`1.5 L/s`) flat leaks. See `validation_results.md` for full multi-dataset results.

---

## 3. Where each decision is made

Most leak decisions are made **on the node itself**, not on the gateway. This is the main idea of the project: it keeps working offline.

| Check | Runs on | Why there |
|---|---|---|
| Night check | Each flat node | Needs only that flat's own meter |
| Continuous-flow check | Each flat node | Needs only that flat's own meter |
| Too-much check | Each flat node | Needs only that flat's own meter and its learned normal |
| Tank outflow check | Tank node | Needs only the tank's own meter |
| Building check (water in = water out) | Gateway | Needs numbers from every node at once, and only the gateway receives them all |

If the radio or the gateway goes down, flats still catch their own leaks and still sound their buzzer. Only the building check pauses, and it catches up when the stored summaries arrive.

---

## 4. Day 1: learning what "normal" looks like

For the first 24 hours (`LEARN_S`), nodes only watch and learn. No alarms are raised.

For **each hour of the day separately**, each node remembers two numbers for each sensor:

- **Average:** add up all readings in that hour, divide by how many there were.
- **Spread** (standard deviation): how much the readings usually wobble around that average.

So a flat learns that 7 am is busy (showers) and 3 am is quiet. A big family and a single person each learn their own normal, so nobody gets an alarm just for using more water than their neighbours.

This is the `Baseline` class in `aqua_edge.py`. We checked its math against Python's own statistics functions and it matches to about 15 decimal places.

**Kept up to date after day 1.** Every hour, that hour's readings move the learned value 1/14 of the way towards today's (EWMA, an average that gives recent days more weight; it remembers about 2 weeks), so changed habits are learned. An hour with an active alert is skipped, so a leak is not learned as normal. The night and continuous-flow checks use fixed numbers and never learn.

**Saved to the SD card.** Once an hour the node saves what it has learned (`<name>_baseline.json`, next to the message queue), using the same power-cut-safe save as the queue. After a power cut the node loads it and carries on; at most the unfinished hour of learning is lost. Without this, every building power cut (our nodes have no battery) would wipe what every node had learned.

---

## 5. The flow checks, one by one

Every check has a **"minutes in a row"** rule. A single odd reading never causes an alarm. The condition has to stay true for a while. This is what keeps false alarms down, and it is also why detection takes 15 to 60 minutes.

### 5.1 Night check (flat node)

> **Rule:** between 1 am and 5 am, if flow stays above **0.02 L/s** for **30 minutes in a row**, raise `LEAK_NIGHT_FLOW`.

- **Idea:** everyone is asleep, so the meter should read almost zero. A leak never sleeps.
- **Example:** a running toilet leaks 0.05 L/s (180 litres an hour). At 2 am that is far above 0.02, so after 30 minutes the alarm goes off.
- **Why 30 minutes:** a toilet flush or a glass of water at night lasts a minute or two, not half an hour.
- **Good to know:** 0.02 L/s is a fixed number, not learned. So even a leak that was already there during day 1 is still caught.
- **Limit:** a leak below 0.02 L/s is not caught by this check. The continuous-flow check (5.5) catches any leak above 0.005 L/s.

### 5.2 Too-much check (flat node)

> **Rule:** if flow stays above **3 times this hour's normal** (and at least **0.05 L/s**) for **60 minutes in a row**, raise `ABNORMAL_CONSUMPTION`.

```
limit = the bigger of (3 x average flow for this hour) and 0.05 L/s
```

- **Idea:** like your electricity bill suddenly tripling.
- **Example:** a flat normally uses 0.015 L/s at 10 am, so the limit is max(0.045, 0.05) = 0.05 L/s. A medium leak pushes flow to 0.46 L/s. After 60 minutes above the limit, the alarm goes off.
- **Why the 0.05 L/s floor:** in quiet hours the average is close to zero, and 3 times almost nothing is still almost nothing. The floor stops tiny normal use from tripping it.
- **Why 60 minutes:** normal heavy use (a shower, a washing machine) comes and goes. A leak stays.

### 5.3 Tank outflow check (tank node)

> **Rule:** if water leaving the tank stays above **this hour's average + the bigger of (4 x spread) and 0.5 L/s** for **15 minutes in a row**, raise `ABNORMAL_SUPPLY`.

- **Idea:** a big leak anywhere in the building makes the tank empty faster than usual.
- **Why "4 x spread":** normal wobble almost never goes 4 spreads above average by chance (about 3 times in 100,000). Crossing it means something real is happening.
- **What it cannot tell you:** where the leak is. It is a fast "something big is happening" signal.

### 5.4 Building check (gateway)

This is the mass balance idea from Ali et al. [6, for comparing the tank supply against the flats to find pipe leaks].

> **Rule:** every 15 minutes, compare water leaving the tank with water arriving at all flats. If more than **10%** goes missing for **2 blocks in a row** (30 minutes), raise `PIPE_LEAK`.

```
missing % = (tank out - (flat 1 + flat 2 + ... + flat 20)) / tank out x 100
```

- **Idea:** water cannot vanish. Like sending 100 rupees and only 85 arriving: someone along the way took 15.
- **How it gets the numbers:** every 15 minutes, each node sends a short summary ("I used X litres"). The gateway waits for all of them, then does the subtraction.
- **Example from testing:** a large burst on downtake B-D1. The tank sent out water, but 67% of it never reached any flat, so the gateway raised `PIPE_LEAK`.
- **Why 10%:** cheap meters are never perfect. Each can be up to 2% off. Worst case, the tank reads 2% high and all flats read 2% low: (1.02 - 0.98) / 1.02 = 3.9%. So 10% leaves a safe gap.
- **Why 2 blocks in a row:** one odd block (a glitch) is not enough.
- **Blocks under 5 litres are skipped:** at night the tank might send out half a litre. A tiny meter error would then look like a huge percentage.
- **What it cannot see:** a leak inside a flat. That water passes through the flat's meter first, so it counts as "used". The flat's own checks (5.1, 5.2 and 5.5) catch those.

---

## 6. Pressure-assisted flat leaks, supply disambiguation & cross-floor localisation

Earlier firmware had a pressure-drop rule (`PRESSURE_LOW`) that triggered without a flat-level flow confirmation, causing `748` neighbor false alarms. In the current firmware, `LEAK_PRESSURE_DROP` is restored **only with flat-level flow confirmation**:

- `p_drop > max(3.0 * p_std, 0.35 m)` **and** `flow > max(3.0 * f_mean, f_mean + 3.0 * f_std, 0.20 L/s)` for `10 min`.
- Never triggers on pressure alone; catches medium (`0.5 L/s`) and large (`1.5 L/s`) flat leaks in `9–10 min` with `0` neighbor false alarms.
- `FlatNode` also sends `p_min` and `p_mean` in each 15-minute `SUM` packet so the gateway can localize riser leaks and separate supply interruptions from leaks.

### The supply-event rule (gateway)

When the pump stops or the tank runs low, `TankNode` checks `level_m` against its learned normal operating floor (`min_level - 0.03 m`, avoiding false alarms from normal `0.5–1.9 m` tank cycling) and emits `SUPPLY_LOW` after `10 min`. The gateway (`Gateway.supply_rule`) combines `SUPPLY_LOW` with tank inflow, cross-floor pressure drops, and `PIPE_LEAK`/`ABNORMAL_SUPPLY`/flat-leak evidence to emit:

- `SUPPLY_EVENT` — low tank with low inflow (`< 0.1 L/s`) and no leak evidence (including buffered `SUPPLY_LOW` delivered after a LoRa outage).
- `LEAK_TANK_DRAIN` — low tank with active leak evidence (`PIPE_LEAK`, `ABNORMAL_SUPPLY`, or flat leak).
- `SUPPLY_UNCERTAIN` — low tank while inflow is active (`>= 0.1 L/s`) and no leak is confirmed.

### Cross-floor riser localisation (gateway)

When `PIPE_LEAK` triggers, `Gateway._localize_floor` uses the 15-minute `p_mean` drops relative to clean per-flat baselines and computes the downward riser headloss gradient (`step[f] = drop[f] - drop[f_above]`). It reports `suspected_floor`, `suspected_range`, and `localization_confidence` (`"high"`, `"medium"`, or `"unlocalised"` when medium-leak gradients are smaller than 15-minute burst noise).

### Silent nodes (gateway)

A node that sends nothing at all (no alarms, no summaries) may have lost power, have a broken sensor or a radio problem. The building check needs every node's litres, so it cannot be done honestly without that node: its water would look "missing" and cause a false pipe-leak alarm. So the gateway:

1. raises `NODE_SILENT` when a node has not been heard for **45 minutes** (3 missed summaries),
2. raises `BALANCE_PAUSED`, naming the silent nodes, so it is clear the building check is not running,
3. raises `NODE_BACK` when the node is heard again, and checks the late blocks from the summaries that waited on its SD card.

This follows common practice for LoRa networks: track when each device was last heard and alert after 2 to 3 missed sends, instead of guessing its numbers.

---

## 7. How an alarm reaches the gateway

1. **The node decides.** A check trips, the node sounds its buzzer and turns its light red.
2. **It saves the alarm to the SD card first,** so nothing is lost if the power or radio fails.
3. **It sends the alarm straight away.** Alarms skip the queue. They listen first to check the radio channel is free (listen before talk), then send.
4. **The gateway replies "got it"** (an ACK). The reply also carries the gateway's time, so node clocks stay in step.
5. **No reply within 2 minutes?** The node sends it again, and keeps trying. During a radio outage, alarms wait on the SD card and go out once the link returns.
6. **The gateway labels it.** The first alarm of a type from a node is `NEW_ALERT`. If the same problem comes back later, it is `REPEAT_ALERT`, with a count and the minutes since last time.

**One alarm per problem:** while a problem continues, the node does not repeat the alarm every minute. Once the condition has been normal for 10 minutes (`ALERT_CLEAR_S`), the alarm re-arms and can fire again if the problem returns.

**Summaries:** every 15 minutes, each node also sends a summary (litres used, lowest pressure). These go out in the node's own time slot, so they do not collide. The building check is built from these.

### 5.5 Continuous-flow check (flat node)

> **Rule:** if flow stays above **0.005 L/s** for **3 hours (180 min) in a row**, at any time of day, raise `LEAK_CONTINUOUS_FLOW`.

- **Idea:** in a normal home the water is off now and then (nobody runs a tap all day). A leak never stops. Commercial ultrasonic meters use the same idea (Ultrimis: steady low flow for 720 minutes).
- **What it adds:**
  - a small leak that starts in the daytime is caught hours before the night check sees it
  - leaks between 0.005 and 0.02 L/s, which the night check never sees, are caught
- **Why 3 hours (retuned from 8 hours):** earlier smooth-demand simulations ran up to 6 h 1 min nonstop, requiring 8 h (`480 min`). With bursty residential demand (Building B max 15 min over 280 flat-days) and real indoor-focused Cache County homes (Sites `006`, `010`, `020`: max 91 min over 739 house-days), `180 min` cuts small-leak latency by 2.67× with `0` false alarms on indoor residential demand.
- **Good to know:** like the night check, it uses fixed numbers, not the learned baseline. A leak that was already there on day 1 is still caught.

---

## 7b. Timing: how often checks run and when messages are sent

**Every check runs once a minute.** A check only fires after its condition has held for some minutes in a row:

| Check | Runs every | Fires after |
|---|---|---|
| Night check (flat) | 1 min | 30 min |
| Continuous-flow check (flat) | 1 min | 3 h (180 min) |
| Pressure-assisted leak check (flat) | 1 min | 10 min |
| Too-much check (flat) | 1 min | 60 min |
| Tank outflow (tank) | 1 min | 15 min |
| Water quality (tank) | 1 min | 3 min |
| Building check (gateway) | every 15 min | 2 blocks (30 min) |
| Silent node (gateway) | 1 min | 45 min with nothing heard |

**Alarms** are sent in the same minute they fire: listen before talk, then send. If the channel is busy, wait a random 0.1 to 1 s and listen again (up to 5 times). The packet takes about 0.06 s on air at SF7 (21-byte signed alert), so the gateway should have it about 1 second after the decision. If it is lost (for example a collision), the node re-sends after 2 minutes; this is the worst-case extra delay. Example from the live run: B-203's night alarm at 02:30:00.2 collided with B-401's summary sent in the same instant, and arrived on the re-send at 02:32:01.

**Summaries** cover 15-minute blocks. Each is made at :00, :15, :30 and :45 for the block just ended, and sent in that same minute at the node's own slot second:

```
slot second = node number x 60 / 21 + 0.2 s
```

| Node | Sends at | Example |
|---|---|---|
| B-401 (1st) | 0.2 s | 10:15:00.2 |
| B-203 (11th) | 28.77 s | 10:15:28.8 |
| B-G04 (20th) | 54.49 s | 10:15:54.5 |
| B-TANK (21st) | 57.34 s | 10:15:57.3 |

So every summary reaches the gateway within 1 minute of its block ending.

**Timestamps:** every message carries `t`, the time the node measured or decided, from the node's own clock; the gateway adds `rx`, the time it arrived. A message stored during an outage keeps its original `t`. Node clocks are reset from the gateway time in every ACK. If a message's `t` is more than 1 minute **in the future**, the gateway raises `CLOCK_SKEW` (an old `t` is normal, a future one proves a bad clock). The simulation counts seconds from the start of the run; using UTC Unix seconds from the DS3231 clock and ISO 8601 times in log files is planned next, after discussion.

In the simulation results, "reached gateway +1 min" comes from the simulator, which delivers arrivals at the next simulated minute; real hardware would not have that extra minute.

---

## 8. Test results

### 8.1 Full live system test

The whole system ran for 48 simulated hours: real firmware on 22 nodes, the real gateway, the LoRa radio model. Scenario: `../simulation/live/scenarios/flow_leaks.jsonl`.

| Leak added | Caught by | Reached gateway |
|---|---|---|
| Small leak in flat B-203 at 02:00 | Night check on B-203, 02:30 (30 min); continuous-flow check 10:00 (8 h) | 02:33 |
| Medium leak in flat B-402 at 10:00 | Too-much check on B-402, 11:00 (60 min) | 11:01 |
| Large burst on downtake B-D1 at 18:00 | Tank outflow check 18:15; gateway building check 18:31 (67% missing) | 18:16 / 18:31 |

- 4,021 of 4,023 radio messages got through (signed binary packets). The 2 collisions were re-sent automatically.
- No message was lost.
- No alarms from flats without a leak.

### 8.2 Flow vs pressure comparison

See section 2. Flow caught every leak with no false alarms. Its detection times are set by the "minutes in a row" waits (30 or 60 min for flats, 15 to 30 min for the downtake), not by the sensor.

---

## 9. Known problems and limits

| Problem | What happens | Status |
|---|---|---|
| **One silent node blocked the building check** | The gateway waited for all 21 summaries with no warning. | **Fixed:** `NODE_SILENT`, `BALANCE_PAUSED`, `NODE_BACK` (section 6). |
| **Building check fired only once** | A second pipe leak later got no alarm. | **Fixed:** re-arms after 2 normal blocks. |
| **Whole-network outage gives 21 silent alarms** | During a radio cut every node goes silent at once, so the gateway raises one `NODE_SILENT` per node. | To improve: report "all nodes silent: radio or gateway problem" once. |
| **Wrong supply-event call** | A medium flat leak drained the tank faster than normal; the tank reported "level low" and the gateway called a supply problem. | **Fixed:** multi-signal `SUPPLY_EVENT` / `LEAK_TANK_DRAIN` / `SUPPLY_UNCERTAIN` + learned tank floor (section 6). |
| **Timestamps** | Simulation uses seconds since start. | Next, after discussion: UTC Unix seconds from the DS3231, ISO 8601 in log files. |
| **Simulated water use is too smooth** | Each simulated flat uses a steady trickle (about 0.015 L/s at the 7 am peak). Real use comes in bursts (a shower is 0.1 to 0.15 L/s for 10 minutes). So "0 false alarms" is weak evidence. | Test the too-much check on real home data (Cache County, readings every 4 seconds). |
| **Volume from one reading per minute** | The code works out litres as flow x 60 from a single reading. A 20-second flush between readings is missed. | On real hardware, count the flow sensor's pulses instead. Each pulse is a fixed amount of water. |
| **Very small leaks** | Below the 0.02 L/s night limit. | **Improved:** the continuous-flow check (5.5) catches anything above 0.005 L/s. Sensors are simulated and ideal; real sensor limits are a parked note in `../docs/IMPROVEMENT_PLAN.md` section 7. |
| **No location on the downtake** | The building check says "pipe leak", not which floor. | **Improved:** confidence-aware `p_mean` downward-gradient localization (`suspected_floor`, `suspected_range`, `localization_confidence`). |

### Detailed System, Dataset & Validation Limitations

For full benchmark numbers, confusion matrices, and multi-dataset results, see [`validation_results.md`](validation_results.md).

#### Performance Limitations
1. **Small flat leaks take ~3 hours to detect (`0.05 L/s`):** Caught by `LEAK_CONTINUOUS_FLOW` (`179–180 min` median vs. `9 min` for `0.5`/`1.5 L/s` via `LEAK_PRESSURE_DROP`), because `0.05 L/s` produces insufficient flow (`~0.055 L/s < 0.20 L/s`) and pressure drop (`~0.064 m < 0.35 m`) to trigger `LEAK_PRESSURE_DROP`. Substantially faster than the prior `480 min` rule, but still relatively slow.
2. **Medium downtake leaks (`0.5 L/s`) cannot be reliably localised:** `PIPE_LEAK` detection is `10/10` for both `0.5` and `1.5 L/s`, and `1.5 L/s` achieves `10/10` Top-1 localisation, but `0.5 L/s` only achieves `1/10` Top-1 (`7/10` Top-2, `8/10` `unlocalised`). Inter-floor pressure steps for `0.5 L/s` are smaller than 15-minute burst-induced pressure variation, so the gateway reports uncertainty (`unlocalised`) rather than inventing a floor.
3. **Small/medium leaks in SWI-FEED `NET_6` are difficult to localise:** `LEAK_PRESSURE_DROP` detects `12/12` in `9 min`, with `6/8` Top-1 localisation on the 8 base runs using hour-matched baselines, but `6/12` overall when 4 held-out junctions are included (only `10` junctions monitored out of `4,419` junctions / `5,066` pipes).

#### Dataset Limitations
4. **Cache County contains no actual pipe-leak labels:** Real 4-second household flow measurements with no pressure readings and end-use labels only (`faucet`, `toilet`, `shower`, `clotheswasher`, `irrigation`). The `6/6` leak detections (`179 min`) used artificially injected flow leaks; Cache County validates normal bursty demand and controlled anomaly detection, not real-world pipe-leak accuracy.
5. **Outdoor irrigation causes false continuous-flow alerts:** Cache County `Site 031` (`SprinklerSystem`, longest run `406 min`) produced `5` `LEAK_CONTINUOUS_FLOW` false alerts over `29` test days (`0.1724/day`). Sites `003, 006, 010, 020` had `0` false leak alerts over `116` test days. Prolonged outdoor irrigation can be mistaken for a leak if deployed on properties with gardens/sprinklers.
6. **Cache County evaluation covers 5 of 31 homes:** Only Sites `003, 006, 010, 020, 031` were downloaded and evaluated, so normal-demand generalisation across all 31 homes is not established.
7. **SWI-FEED `NET_6` is unsuitable for single-flat zero-flow rules:** `NET_6` models aggregate municipal DMA demand (never zero-flow), causing `LEAK_NIGHT_FLOW` and `LEAK_CONTINUOUS_FLOW` to fire `20` false alerts per no-leak run (`0` false alarms from `LEAK_PRESSURE_DROP`).

#### Validation & Robustness Limitations
8. **Limited WNTR supply-interruption scenario count:** The corrected detector achieved `2/2` genuine WNTR supply interruptions detected and `0` false `SUPPLY_EVENT`s over `14` normal days, but only 2 genuine WNTR interruption scenarios were tested; broader pump curves, supply schedules, and tank sizes remain untested.
9. **Day-1 supply baseline dependence:** `TankNode` learns `min_level` from Day 1 (`1800 s .. LEARN_S`) using the 10th percentile (`min_level - 0.03 m`), which rejects startup transients and brief (`< 2.4 h`) Day-1 outages, but a multi-hour (`> 2.4 h`) Day-1 outage or shallow Day-1 cycling can still skew the learned floor (see `test_supply_robustness.py`).
10. **Inflow suppression threshold (`inflow_lps >= 0.1`):** `Gateway.supply_rule` suppresses `SUPPLY_EVENT` (emitting `SUPPLY_UNCERTAIN` instead) whenever residual tank inflow remains `>= 0.1 L/s`, even if inflow is below building demand (`test_supply_robustness.py` case `2c`).
11. **Simulated hydraulics rather than physical deployment:** Building B and `NET_6` use EPANET/WNTR simulations, and Cache County has no pipe-leak labels or pressure data; there is no physical building deployment with real sensors yet.
12. **Sparse sensor coverage in `NET_6`:** Monitoring only `10` of `4,419` junctions limits spatial localisation at unseen nodes; denser sensor placement would raise hardware/deployment cost.

---

## 10. Summary of all the numbers

| Setting | Value | Used in |
|---|---|---|
| Learning period | 24 h | All checks |
| Night window | 1 am to 5 am | Night check |
| Night flow limit | 0.02 L/s for 30 min | Night check |
| Continuous flow limit | above 0.005 L/s for 3 h (180 min), any time of day | Continuous-flow check |
| Pressure-assisted leak limit | `p_drop > max(3*std, 0.35 m)` & `flow > max(3*mean, mean+3*std, 0.20 L/s)` for 10 min | `LEAK_PRESSURE_DROP` |
| Too-much limit | max(3 x hourly average, 0.05 L/s) for 60 min | Too-much check |
| Tank outflow limit | average + max(4 x spread, 0.5 L/s) for 15 min | Tank outflow check |
| Missing water limit | 10% for 2 blocks of 15 min, blocks under 5 L skipped | Building check |
| Meter error allowed for | up to 2% per meter | Building check |
| Tank level low | average - max(4 x spread, 0.3 m) for 10 min | Supply event |
| Silent node | nothing heard for 45 min | Gateway |
| Building check re-arm | 2 normal blocks in a row | Gateway |
| Clock skew | message time over 1 min in the future | Gateway |
| Alarm re-arm | after 10 min back to normal | All flat and tank alarms |
| Re-send if no reply | every 2 min | All messages |
| Summary period | 15 min | Building check |

---

## 11. How to run the tests

From the repository root (using the root `uv` environment):

```
sh simulation/live/run_local.sh --scenario simulation/live/scenarios/flow_leaks.jsonl   # full live system, three leaks
python3 simulation/live/check_comms.py                                                  # PASS/FAIL report for that run
python3 firmware/gateway.py                                                             # gateway self-check (re-arm, silent node, clock)
uv run python leak_detection/compare_leaks.py                                           # flow vs pressure, 35 runs (~5 min; see note in the file)
uv run python leak_detection/compare_leaks.py --check                                   # self-check of the scoring
```

Results of the comparison are written to `leak_detection/results/compare_leaks.csv`.
