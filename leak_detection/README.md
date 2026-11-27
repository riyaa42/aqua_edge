# Aqua Edge: How Leak Detection Works

This document explains, step by step, how Aqua Edge finds water leaks in building B: which sensors are used, where each decision is made, the simple math behind every check, how the results reach the gateway, how well it worked in testing, and what still needs fixing.

All numbers below come from the code in `../firmware/edge_node/aqua_edge.py` (the node firmware) and `../firmware/gateway/gateway.py` (the gateway).

### Where the code is

| What | File | Where in it |
|---|---|---|
| Learning "normal" (hourly average and spread) | `../firmware/edge_node/aqua_edge.py` | `class Baseline` |
| All settings (limits, waits) | `../firmware/edge_node/aqua_edge.py`, `../firmware/gateway/gateway.py` | Constants at the top of each file |
| Night check, continuous-flow check, pressure-assisted leak check, too-much check (every flat node) | `../firmware/edge_node/aqua_edge.py` | `FlatNode.check` |
| Litres, `p_min`, and `p_mean` per 15 minutes sent to the gateway (flat node) | `../firmware/edge_node/aqua_edge.py` | `FlatNode.accumulate` |
| Tank outflow check, tank level flag (tank node) | `../firmware/edge_node/aqua_edge.py` | `TankNode.check` |
| Building check (water in = water out) | `../firmware/gateway/gateway.py` | `Gateway.mass_balance` |
| Supply-event rule | `../firmware/gateway/gateway.py` | `Gateway.supply_rule` |
| Building layout: junctions, what is upstream, valves | `../firmware/gateway/building_B.json`, `../firmware/gateway/topology.py` | `class Topology` |
| Locating a pipe leak and choosing who is alerted | `../firmware/gateway/gateway.py` | `Gateway._localize_floor`, `Gateway.pipe_leak_nodes` |
| Silent nodes, clock check | `../firmware/gateway/gateway.py` | `Gateway.check_silence`, `Gateway.check_clock` |
| New vs repeat alert | `../firmware/gateway/gateway.py` | `Gateway.check_repeat` |
| Flow vs pressure comparison | `compare_leaks.py` (this folder) | |
| Live test scenario | `../simulation/live/scenarios/flow_leaks.jsonl` | |

The same firmware files run in the simulation now and are meant to run on the Raspberry Pi 3B nodes and the gateway laptop later.

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

We tested both methods on 35 simulated runs (`compare_leaks.py`). Leaks were placed in 5 flats and on all 5 downtake sections, in three sizes, plus 5 runs with no leak at all. **The table below is historical:** it comes from the firmware of commit `4f7867a`, which had a pressure-only rule (rerun on that commit: same numbers). For today's firmware see `validation_results.md` section 4.

| | Flow | Pressure |
|---|---|---|
| Leaks caught | **30 out of 30** | Missed small leaks in practice |
| False alarms | **0** | **748** (almost all from the wrong flat) |
| Speed on big flat leaks | 60 min | 10 min |

**The reason, in plain words:**

- **Flow is measured on each flat's own pipe.** Water only goes into a flat if someone uses it or it leaks out. A leak in B-203 adds water only to B-203's meter. So a flow alarm always points to the right flat.
- **Pressure is shared.** All flats hang off the same downtake. A hole anywhere lowers the pressure for everyone on that pipe, like one person turning on a hose and every tap in the house getting weaker. So pressure can say "there is a leak somewhere" but not "where". A small leak barely changes pressure at all, so it hides inside normal sensor noise.

**Update (current firmware):** pressure-assisted flat leak detection (`LEAK_PRESSURE_DROP`) is back, but it needs **both** a pressure drop from the flat's learned hourly baseline (`p_drop > max(3.0 * p_std, 0.35 m)`) **and** unusually high flow on that same flat (`flow > max(3.0 * f_mean, f_mean + 3.0 * f_std, 0.20 L/s)`) for `10 min`. In the 35 runs of `compare_leaks.py` it gives no false flat leak alerts and catches medium (`0.5 L/s`) and large (`1.5 L/s`) flat leaks in `10 min`; the 14 normal days of `validate_campaign.py` also have none. **It is not free of neighbour false alarms:** in that campaign, in 10 of 20 downtake-leak runs (all 10 with demand seed `101`), flat B-402, which had no leak, raised it 49 min after the downtake leak started: the leak lowered the pressure while B-402's own use pushed its flow up. The gateway now **labels** such alerts: a flat `LEAK_PRESSURE_DROP` stamped during an active `PIPE_LEAK` gets `possible_cause=PIPE_LEAK` on its `NEW_ALERT` / `REPEAT_ALERT` event (all 10 above are labelled; no real flat-leak alert in the 27 flat-leak runs is). The alert is not hidden and the flat's light still turns red.

---

## 3. Where each decision is made

Most leak decisions are made **on the node itself**, not on the gateway. This is the main idea of the project: it keeps working offline.

| Check | Runs on | Why there |
|---|---|---|
| Night check | Each flat node | Needs only that flat's own meter |
| Continuous-flow check | Each flat node | Needs only that flat's own meter |
| Too-much check | Each flat node | Needs only that flat's own meter and its learned normal |
| Pressure-assisted leak check | Each flat node | Needs only that flat's own meter, pressure sensor and learned normal |
| Tank level check | Tank node | Needs only the tank's own level sensor (the gateway then decides what a low tank means) |
| Tank outflow check | Tank node | Needs only the tank's own meter |
| Building check (water in = water out) | Gateway | Needs numbers from every node at once, and only the gateway receives them all |

If the radio or the gateway goes down, flats still catch their own leaks turn their light red and sound their buzzer. Only the gateway's work pauses, such as the building check; it catches up when the stored summaries arrive, and then sends the affected nodes any alerts it finds.

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

**Not everything is saved.** Only the hourly averages and spreads are. After a reboot past Day 1, the tank node's learned low level (`min_level`) is gone and is not relearned, so its tank check uses only the limits based on hourly readings (section 6). The consumption detector's own baseline is also lost (`../abnormal_consumption/README.md`).

---

## 5. The flow checks, one by one

Besides the checks here, each flat node also runs the consumption detector (`../abnormal_consumption/`) and the pressure-assisted check (section 6). A missing sensor reading restarts the count of every check that uses it.

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

> **Rule:** every 15 minutes, compare water leaving the tank with water arriving at all flats. If more than **10%** goes missing for **2 blocks in a row** (two blocks next to each other in time, 30 minutes), raise `PIPE_LEAK`.

```
missing % = (tank out - (flat 1 + flat 2 + ... + flat 20)) / tank out x 100
```

- **Idea:** water cannot vanish. Like sending 100 rupees and only 85 arriving: someone along the way took 15.
- **How it gets the numbers:** every 15 minutes, each node sends a short summary ("I used X litres"). The gateway waits for all of them, then does the subtraction.
- **Example from testing:** a large burst on downtake B-D1. The tank sent out water, but 69.9% of it never reached any flat, so the gateway raised `PIPE_LEAK`.
- **Why 10%:** cheap meters are never perfect. Each can be up to 2% off. Worst case, the tank reads 2% high and all flats read 2% low: (1.02 - 0.98) / 1.02 = 3.9%. So 10% leaves a safe gap.
- **Why 2 blocks in a row:** one odd block (a glitch) is not enough.
- **Order of arrival does not matter:** a block that is missing or skipped breaks the pair. Two normal blocks next to each other re-arm the check. If summaries delivered late show an earlier leak that the check has already moved past, it is logged as `PIPE_LEAK_PAST` and the current state stays as it is; if they show the active leak began earlier, only its start time moves back.
- **Blocks under 5 litres are skipped:** at night the tank might send out half a litre. A tiny meter error would then look like a huge percentage.
- **What it cannot see:** a leak inside a flat. That water passes through the flat's meter first, so it counts as "used". The flat's own checks (5.1, 5.2 and 5.5) catch those.

### 5.5 Continuous-flow check (flat node)

> **Rule:** if flow stays above **0.005 L/s** for **3 hours (180 min) in a row**, at any time of day, raise `LEAK_CONTINUOUS_FLOW`.

- **Idea:** in a normal home the water is off now and then (nobody runs a tap all day). A leak never stops. Commercial ultrasonic meters use the same idea (Ultrimis: steady low flow for 720 minutes).
- **What it adds:**
  - a small leak that starts in the daytime is caught hours before the night check sees it
  - leaks between 0.005 and 0.02 L/s, which the night check never sees, are caught
- **Why 3 hours (retuned from 8 hours):** earlier smooth-demand simulations ran up to 6 h 1 min nonstop, which required 8 h (`480 min`). With bursty demand the longest nonstop run in the simulated flats is 15 min (the 7-day and 48 h normal runs). In real Cache County data, Sites `006`, `010` and `020` never exceed 91 min, but that rests on **121.5 days of recorded data** (not the 739 house-days quoted earlier, which counted recording gaps). The other two homes examined do exceed 3 h: Site `003` has 11 such runs (longest 630 min) and Site `031` has 5 (longest 406 min). So 3 h is **not shown to be free of false alarms** in real homes. It cuts small-leak latency from 8 h to 3 h.
- **Good to know:** like the night check, it uses fixed numbers, not the learned baseline. A leak that was already there on day 1 is still caught.

---

## 6. Pressure-assisted flat leaks, supply disambiguation & cross-floor localisation

Earlier firmware had a pressure-drop rule (`PRESSURE_LOW`) that triggered without a flat-level flow confirmation; in the 35-run benchmark it raised `748` false alarms. The current `LEAK_PRESSURE_DROP` needs **flat-level flow confirmation**:

- `p_drop > max(3.0 * p_std, 0.35 m)` **and** `flow > max(3.0 * f_mean, f_mean + 3.0 * f_std, 0.20 L/s)` for `10 min`.
- Never triggers on pressure alone; catches medium (`0.5 L/s`) and large (`1.5 L/s`) flat leaks in `9–10 min`. A downtake leak that coincides with a flat's own heavy use can still trigger it on that flat; the gateway labels that case (section 2).
- `FlatNode` also sends `p_min` and `p_mean` in each 15-minute `SUM` packet so the gateway can localize riser leaks and separate supply interruptions from leaks.

### The supply-event rule (gateway)

`TankNode` emits `SUPPLY_LOW` when the level has been below **both** its learned floor (`min_level - 0.03 m`, where `min_level` is the 10th percentile of Day-1 levels) **and** this hour's average minus `0.3 m` for `10 min`. (Until `min_level` is learned, the floor is the hourly average minus `max(4 x spread, 0.3 m)`.) The gateway (`Gateway.supply_rule`) combines `SUPPLY_LOW` with tank inflow, cross-floor pressure drops, and `PIPE_LEAK`/`ABNORMAL_SUPPLY`/flat-leak evidence to emit:

- `SUPPLY_EVENT` — low tank, no leak evidence, and either low inflow (`< 0.1 L/s`, including buffered `SUPPLY_LOW` delivered after a LoRa outage) or, if inflow is unknown, pressure down on nearly every floor.
- `LEAK_TANK_DRAIN` — low tank with active leak evidence (`PIPE_LEAK`, `ABNORMAL_SUPPLY`, or flat leak).
- `SUPPLY_UNCERTAIN` — low tank with no leak evidence otherwise (for example inflow `>= 0.1 L/s`). Leak evidence that arrives later can still add `LEAK_TANK_DRAIN`.

**This rule still gives false supply events.** The pump is off for most of the time, so near-zero inflow is normal, and the level limit can sit just above the pump's switch-on level (`0.5 m`). `validate_campaign.py` gets **7 false `SUPPLY_EVENT`s** in its two 7-day normal runs (5 and 2). In the one that was traced, the learned floor was `0.68 m`, but the limit that applied was the lower one, this hour's average minus `0.3 m` = `0.53 m`; an ordinary cycle stayed below it for 10 min shortly before the pump refilled the tank. The other 6 were not traced. The live scenario of section 8.1 also raises a `SUPPLY_EVENT` at 11:10, during the B-402 flat leak; its cause has **not** been traced. (Flat-leak and tank-outflow alerts count as leak evidence only within 15 min.)

### Cross-floor riser localisation (gateway)

When `PIPE_LEAK` triggers, `Gateway._localize_floor` uses the 15-minute `p_mean` drops relative to clean per-flat baselines and computes the downward riser headloss gradient (`step[s] = drop[s] - drop[junction above s]`). Which flats share a junction, and which junction is above which, come from the [building tree](../firmware/README.md#building-tree), not from flat names. The suspected junction is the furthest one down with a significant step; if two separate branches have one, the leak is `unlocalised`. It reports `suspected_floor` (the junction's label), `suspected_node` (its id), `suspected_range` (the suspect and the junctions directly below it), `isolation_valve` (nearest valve at or above it, recorded only) and `localization_confidence` (`"high"`, `"medium"`, or `"unlocalised"` when medium-leak gradients are smaller than 15-minute burst noise).

**Who is alerted:** the tank and every flat fed through the suspected junction, so a leak located at floor 1 alerts floors 1 and G. If the leak is `unlocalised`, only the tank is alerted. For building B the new tree-based code gave the same localisation as the earlier name-based code on 200,000 random pressure-drop patterns.

### Silent nodes (gateway)

A node that sends nothing at all (no alarms, no summaries) may have lost power, have a broken sensor or a radio problem. The building check needs every node's litres, so it cannot be done honestly without that node: its water would look "missing" and cause a false pipe-leak alarm. So the gateway:

1. raises `NODE_SILENT` when a node has not been heard for **45 minutes** (3 missed summaries),
2. raises `BALANCE_PAUSED`, naming the silent nodes, so it is clear the building check is not running,
3. raises `NODE_BACK` when the node is heard again, and checks the late blocks from the summaries that waited on its SD card.

Only a message the gateway has not seen before counts as "heard": a repeated or replayed packet is acknowledged but is no sign of life. (A captured packet that never reached the gateway would still count once.)

This follows common practice for LoRa networks: track when each device was last heard and alert after 2 to 3 missed sends, instead of guessing its numbers.

---

## 7. How an alarm reaches the gateway

1. **The node decides.** A check trips, and in the same step the node turns its light red and sounds the buzzer (driven on the Pi by `pi_hal.py`; not verified on hardware in these tests). No gateway is needed for this.
2. **It saves the alarm to the SD card first,** so nothing is lost if the power or radio fails.
3. **It sends the alarm straight away.** Alarms skip the queue. They listen first to check the radio channel is free (listen before talk), then send.
4. **The gateway replies "got it"** (an ACK). The reply also carries the gateway's time for setting node clocks. The firmware does not set any clock itself; only the live simulation's hardware layer uses it.
5. **No reply?** While the gateway has been heard in the last 15 s, an alarm is sent again after 1-2 s, then 2-4 s, then every 4-8 s. Otherwise it waits on the SD card and is tried about every 30 s. The gateway sends a beacon every 5 s, and when it comes back, stored alarms go out at once. Problems only the gateway can find are sent back to the nodes concerned ([`../firmware/README.md`](../firmware/README.md#gateway-alerts-sent-to-nodes)).
6. **The gateway labels it.** The first alarm of a type from a node is `NEW_ALERT`. If the same problem comes back later, it is `REPEAT_ALERT`, with a count and the minutes since last time.

**One alarm per problem:** while a problem continues, the node does not repeat the alarm every minute. Once the condition has been normal for 10 minutes (`ALERT_CLEAR_S`), the alarm re-arms and can fire again if the problem returns.

**Recovery:** silence never closes a problem. The node sends a recovery message when the problem has looked over for 10 minutes of readings in a row, and the gateway then moves the problem to its history. A flat leak counts as over only when the water is off (flow at most `0.005 L/s`), not when the night check's hours end or the pressure recovers. A recovery closes only what is older than itself, and recovery messages are not counted as new or repeat alerts or as leak evidence. The table of rules, and what survives a restart of a node or of the gateway, is in [`../firmware/README.md`](../firmware/README.md#recovery-and-restarts).

**Summaries:** every 15 minutes, each node also sends a summary (litres used, lowest pressure). These go out in the node's own time slot, so they do not collide. The building check is built from these.

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

**Alarms** are sent as soon as they fire: listen before talk, then send. A 21-byte signed alert takes about 0.06 s on air at SF7. If it is lost, it is sent again within seconds (section 7, step 5). These delivery times have not been measured with real radios or with airtime and collisions simulated.

**Summaries** cover 15-minute blocks. Each is made at :00, :15, :30 and :45 for the block just ended, and sent in that same minute at the node's own slot second:

```
slot second = (node number - 1) x 60 / 21 + 0.2 s
```

| Node | Sends at | Example |
|---|---|---|
| B-401 (1st) | 0.2 s | 10:15:00.2 |
| B-203 (11th) | 28.77 s | 10:15:28.8 |
| B-G04 (20th) | 54.49 s | 10:15:54.5 |
| B-TANK (21st) | 57.34 s | 10:15:57.3 |

So every summary reaches the gateway within 1 minute of its block ending.

**Timestamps:** every message carries `t`, the time the node measured or decided, from the node's own clock; the gateway adds `rx`, the time it arrived. A message stored during an outage keeps its original `t`. Each ACK carries the gateway time, but setting the node clock from it is left to the hardware layer (only simulated so far). If a message's `t` is more than 1 minute **in the future**, the gateway raises `CLOCK_SKEW` (an old `t` is normal, a future one proves a bad clock). The simulation counts seconds from the start of the run. The queue and gateway log already have UTC and ISO 8601 fields (`ts_utc`, `ts_iso`, `rx_utc`, `rx_iso`), filled from `hal.utc_now()` when a hardware layer provides it; no DS3231 hardware layer exists yet, and only `t` is sent over the radio.

In the simulation results, "reached gateway +1 min" comes from the simulator, which delivers arrivals at the next simulated minute; real hardware would not have that extra minute.

---

## 8. Test results

### 8.1 Full live system test

The whole system ran for 48 simulated hours: real firmware on 22 nodes, the real gateway, the LoRa radio model. Scenario: `../simulation/live/scenarios/flow_leaks.jsonl`.

| Leak added | Caught by | Reached gateway |
|---|---|---|
| Small leak in flat B-203 at 02:00 | Night check on B-203, 02:30 (30 min); continuous-flow check 05:00 (3 h) | 02:31 |
| Medium leak in flat B-402 at 10:00 | Pressure-assisted check on B-402, 10:10; too-much check 11:00; continuous-flow check 13:00 | 10:11 |
| Large burst on downtake B-D1 at 18:00 | Tank outflow check 18:14; gateway building check 18:31 (69.9% missing) | 18:15 / 18:31 |

**These live-run results were measured before the instant-delivery change.** The live simulation has not been updated or rerun since, so its message counts and delivery times do not describe the current firmware.

- 4,041 of 4,041 radio messages got through (signed binary packets): 2 collisions, both re-sent.
- Recovery messages: the flat-leak problems of B-203 and B-402 and the tank outflow problem from the downtake burst stayed open, as those leaks are never fixed. B-203's night-use report was cleared at 03:54, when its flow fell back under that rule's limit, although the leak went on (the leak itself stayed open). Before the burst, the tank's `ABNORMAL_SUPPLY` cleared and returned several times (cleared 10:49, 13:29, 14:14, 15:39, 16:19) while the flat leaks continued. The tank reported low at 11:09 and recovered at 11:57.
- `check_comms.py` passes, including "no alerts from nodes with no event". It does not check the gateway's supply events or the tank's alerts, and this run has two false ones: a `QUALITY_TEMPORAL_ANOMALY` at 00:16 with no water-quality event, and a `SUPPLY_EVENT` at 11:10 while only the B-402 flat leak (and the older B-203 one) was active.
- The flats with leaks also raise consumption alerts (`../abnormal_consumption/`). The tank also repeats `ABNORMAL_SUPPLY` several times before the downtake burst, while only the flat leaks are active.

### 8.2 Flow vs pressure comparison

On today's firmware the 35 runs catch 30 of 30 leaks with no false flat leak, downtake or supply-outflow alarms. Consumption and supply events are not scored; each of the 5 no-leak runs does raise one `UNUSUAL_NIGHT_CONSUMPTION`. Median detection: small flat leaks 180 min, medium and large flat leaks 10 min (`LEAK_PRESSURE_DROP`), downtake leaks 29 / 20 / 15 min (small / medium / large).

---

## 9. Known problems and limits

| Problem | What happens | Status |
|---|---|---|
| **One silent node blocked the building check** | The gateway waited for all 21 summaries with no warning. | **Fixed:** `NODE_SILENT`, `BALANCE_PAUSED`, `NODE_BACK` (section 6). |
| **Building check fired only once** | A second pipe leak later got no alarm. | **Fixed:** re-arms after 2 normal blocks. |
| **Whole-network outage gives 21 silent alarms** | During a radio cut every node goes silent at once, so the gateway raises one `NODE_SILENT` per node. | To improve: report "all nodes silent: radio or gateway problem" once. |
| **Wrong supply-event call** | A medium flat leak drained the tank faster than normal; the tank reported "level low" and the gateway called a supply problem. | **Only partly fixed:** `LEAK_TANK_DRAIN` / `SUPPLY_UNCERTAIN` and a learned tank floor were added, but false `SUPPLY_EVENT`s still happen, in normal operation and during a flat leak (section 6). |
| **Timestamps** | Simulation uses seconds since start. | UTC/ISO fields exist in the queue and gateway log; the DS3231 hardware layer is not written. |
| **Simulated water use was too smooth** | A steady trickle per flat made "0 false alarms" weak evidence. | **Improved:** the simulations now use bursty per-flat demand, and Cache County adds real household data (see limits below). |
| **Volume from one reading per minute** | The code works out litres as flow x 60 from a single reading. A 20-second flush between readings is missed. | On real hardware, count the flow sensor's pulses instead. Each pulse is a fixed amount of water. |
| **Very small leaks** | Below the 0.02 L/s night limit. | **Improved:** the continuous-flow check (5.5) catches anything above 0.005 L/s. Sensors are simulated and ideal; real sensor limits are a parked note in `../docs/IMPROVEMENT_PLAN.md` section 7. |
| **No location on the downtake** | The building check says "pipe leak", not which floor. | **Improved:** confidence-aware `p_mean` downward-gradient localization (`suspected_floor`, `suspected_range`, `localization_confidence`). |

### Detailed System, Dataset & Validation Limitations

For full benchmark numbers, confusion matrices, and multi-dataset results, see [`validation_results.md`](validation_results.md).

#### Performance Limitations
1. **Small flat leaks take ~3 hours to detect (`0.05 L/s`):** Caught by `LEAK_CONTINUOUS_FLOW` (`179–180 min` median vs. `9 min` for `0.5`/`1.5 L/s` via `LEAK_PRESSURE_DROP`), because `0.05 L/s` produces insufficient flow (`~0.055 L/s < 0.20 L/s`) and pressure drop (`~0.067 m < 0.35 m`) to trigger `LEAK_PRESSURE_DROP`. Substantially faster than the prior `480 min` rule, but still relatively slow.
2. **Medium downtake leaks (`0.5 L/s`) cannot be reliably localised:** `PIPE_LEAK` detection is `10/10` for both `0.5` and `1.5 L/s`, and `1.5 L/s` achieves `10/10` Top-1 localisation, but `0.5 L/s` only achieves `1/10` Top-1 (`7/10` Top-2, `8/10` `unlocalised`). Inter-floor pressure steps for `0.5 L/s` are smaller than 15-minute burst-induced pressure variation, so the gateway reports uncertainty (`unlocalised`) rather than inventing a floor, and then alerts only the tank, not the flats.
3. **Small/medium leaks in SWI-FEED `NET_6` are difficult to localise:** `LEAK_PRESSURE_DROP` detects `12/12` in `9 min`, with `6/8` Top-1 localisation on the 8 base runs using hour-matched baselines, but `6/12` overall when 4 held-out junctions are included (only `10` junctions monitored out of `4,419` junctions / `5,066` pipes). This localisation is computed inside `validate_swifeed.py` from noise-free simulated pressures; the gateway firmware does not do it.

#### Dataset Limitations
4. **Cache County contains no actual pipe-leak labels:** Real 4-second household flow measurements with no pressure readings and end-use labels only. The `4/4` leak detections (`179 min`) used artificially injected flow leaks on Sites `010` and `020`; Site `006` could not be scored because its data stops soon after the injection time. This shows how the continuous-flow rule behaves, not real-world pipe-leak accuracy.
5. **False leak alerts on real data:** in the 30-day windows, Site `031` produced `10` false flat leak alerts (`6` night, `4` continuous) over `14.4` recorded test days (`0.69/day`). By the dataset's end-use labels, 8 of them are on irrigation, mostly running overnight, and 2 on a run labelled `unknown`. Sites `003, 006, 010, 020` had none over `70.9` recorded test days. Over the full record, Site `003` also has 11 runs of 3 h or more, outside that window (mostly labelled `faucet`, `0.009–0.032 L/s`; the labels cannot tell a drip from a leak).
6. **Cache County evaluation covers 5 of 31 homes, with large recording gaps:** only Sites `003, 006, 010, 020, 031` were evaluated. The test windows hold `85.3` recorded days, not the `145` the calendar span suggests.
7. **SWI-FEED `NET_6` is unsuitable for single-flat zero-flow rules:** `NET_6` models aggregate municipal DMA demand (never zero-flow), causing `LEAK_NIGHT_FLOW` and `LEAK_CONTINUOUS_FLOW` to fire `20` false alerts per no-leak run (`0` false alarms from `LEAK_PRESSURE_DROP`).

#### Validation & Robustness Limitations
8. **Supply-event detection is not reliable:** both simulated interruptions were detected (about 20 h after the cut, when the tank got low), but the two 7-day normal runs gave `7` false `SUPPLY_EVENT`s. Only 2 interruption scenarios were tested.
9. **Day-1 supply baseline dependence:** `TankNode` learns `min_level` from Day 1 (`1800 s .. LEARN_S`) using the 10th percentile (`min_level - 0.03 m`), which rejects startup transients and brief (`< 2.4 h`) Day-1 outages, but a multi-hour (`> 2.4 h`) Day-1 outage or shallow Day-1 cycling can still skew the learned floor (see `test_supply_robustness.py`). `min_level` is also lost at any reboot after Day 1 (section 4).
10. **Inflow suppression threshold (`inflow_lps >= 0.1`):** `Gateway.supply_rule` suppresses `SUPPLY_EVENT` (emitting `SUPPLY_UNCERTAIN` instead) whenever residual tank inflow remains `>= 0.1 L/s`, even if inflow is below building demand (`test_supply_robustness.py` case `2c`).
11. **Simulated hydraulics rather than physical deployment:** Building B and `NET_6` use EPANET/WNTR simulations, and Cache County has no pipe-leak labels or pressure data; there is no physical building deployment with real sensors yet.
12. **Sparse sensor coverage in `NET_6`:** Monitoring only `10` of `4,419` junctions limits spatial localisation at unseen nodes; denser sensor placement would raise hardware/deployment cost.
13. **A downtake leak can raise a flat leak alert** on a flat that is using water at the time (section 2). The gateway's `possible_cause=PIPE_LEAK` label only means *possibly*: a real flat leak during a pipe leak is labelled too. There is no label if the flat alert reaches the gateway before `PIPE_LEAK` is decided, which includes after a radio outage, because alerts are sent before summaries. The label is in the gateway's event only.
14. **Partial sensor dropouts:** if a flow reading is missing for only part of a 15-minute block, that node's litres are too low and the building check may count the difference as missing water. If it is missing for the whole block, that block is not balanced.

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
| Tank level low | below the Day-1 floor (10th percentile - 0.03 m) and below the hourly average - 0.3 m, for 10 min | Supply event |
| Silent node | nothing heard for 45 min | Gateway |
| Building check re-arm | 2 normal blocks next to each other in time | Gateway |
| Clock skew | message time over 1 min in the future | Gateway |
| Alarm re-arm | after 10 min back to normal | All flat and tank alarms |
| Recovery message | after 10 min of readings in a row showing the problem is over | Flat leak, consumption, zero use, tank outflow |
| Re-send if no reply | alarms: 1-2 s rising to 4-8 s (about 30 s with the gateway away); summaries: 10-60 s (5 min away) | All messages |
| Summary period | 15 min | Building check |

---

## 11. How to run the tests

From the repository root (using the root `uv` environment). The two data validations need downloads, which Git ignores:

- SWI-FEED: from `simulation/`, run `sh scripts/get_swifeed_data.sh`.
- Cache County ([HydroShare `0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e)): put `sites.csv`, and `siteNNNqc_data.csv` and `LabelledEvents_site_NNN.csv` for sites 003, 006, 010, 020, 031, in `simulation/data/cache_county/`.

```
sh simulation/live/run_local.sh --scenario simulation/live/scenarios/flow_leaks.jsonl   # full live system, three leaks
python3 simulation/live/check_comms.py                                                  # PASS/FAIL report for that run
python3 firmware/gateway/gateway.py                                                             # gateway self-check (re-arm, silent node, clock)
uv run python firmware/tests/test_gateway_analytics.py                                  # recovery, replay, block order, restart
uv run python leak_detection/compare_leaks.py                                           # flow vs pressure, 35 runs (~5 min; see note in the file)
uv run python leak_detection/compare_leaks.py --check                                   # self-check of the scoring
uv run python leak_detection/test_regression.py          # 8 leak and supply scenarios
uv run python leak_detection/test_offline_supply.py      # LoRa outages
uv run python leak_detection/test_supply_robustness.py   # prints supply-rule behaviour in edge cases
uv run python leak_detection/validate_campaign.py        # Building B campaign
uv run python leak_detection/validate_swifeed.py         # needs SWI-FEED data
uv run python leak_detection/validate_cache_county.py    # needs Cache County data
```

Results of the comparison are written to `leak_detection/results/compare_leaks.csv`.
