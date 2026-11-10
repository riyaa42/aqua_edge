# Aqua Edge: How Leak Detection Works

This document explains, step by step, how Aqua Edge finds water leaks in building B: which sensors are used, where each decision is made, the simple math behind every check, how the results reach the gateway, how well it worked in testing, and what still needs fixing.

All numbers below come from the code in `../firmware/aqua_edge.py` (the node firmware) and `../firmware/gateway.py` (the gateway).

### Where the code is

| What | File | Where in it |
|---|---|---|
| Learning "normal" (hourly average and spread) | `../firmware/aqua_edge.py` | `class Baseline` |
| All settings (limits, waits) | `../firmware/aqua_edge.py`, `../firmware/gateway.py` | Constants at the top of each file |
| Night check, too-much check, pressure early warning (every flat node) | `../firmware/aqua_edge.py` | `FlatNode.check` |
| Litres per 15 minutes sent to the gateway (flat node) | `../firmware/aqua_edge.py` | `FlatNode.accumulate` |
| Tank outflow check, tank level flag (tank node) | `../firmware/aqua_edge.py` | `TankNode.check` |
| Building check (water in = water out) | `../firmware/gateway.py` | `Gateway.mass_balance` |
| Supply-event rule | `../firmware/gateway.py` | `Gateway.supply_rule` |
| New vs repeat alert | `../firmware/gateway.py` | `Gateway.check_repeat` |
| Flow vs pressure comparison | `compare_leaks.py` (this folder) | |
| Live test scenario | `../simulation/live/scenarios/flow_leaks.jsonl` | |

The same firmware files run in the simulation now and are meant to run on the Raspberry Pi nodes later.

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

**Decision:** flow is the leak detector. Pressure is, at most, an early warning that tells a flat to check sooner. It never decides on its own (see section 6).

---

## 3. Where each decision is made

Most leak decisions are made **on the node itself**, not on the gateway. This is the main idea of the project: it keeps working offline.

| Check | Runs on | Why there |
|---|---|---|
| Night check | Each flat node | Needs only that flat's own meter |
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

---

## 5. The flow checks, one by one

Every check has a **"minutes in a row"** rule. A single odd reading never causes an alarm. The condition has to stay true for a while. This is what keeps false alarms down, and it is also why detection takes 15 to 60 minutes.

### 5.1 Night check (flat node)

> **Rule:** between 1 am and 5 am, if flow stays above **0.02 L/s** for **30 minutes in a row**, raise `LEAK_NIGHT_FLOW`.

- **Idea:** everyone is asleep, so the meter should read almost zero. A leak never sleeps.
- **Example:** a running toilet leaks 0.05 L/s (180 litres an hour). At 2 am that is far above 0.02, so after 30 minutes the alarm goes off.
- **Why 30 minutes:** a toilet flush or a glass of water at night lasts a minute or two, not half an hour.
- **Good to know:** 0.02 L/s is a fixed number, not learned. So even a leak that was already there during day 1 is still caught.
- **Limit:** a dripping tap is far smaller than 0.02 L/s and is not caught.

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
- **What it cannot see:** a leak inside a flat. That water passes through the flat's meter first, so it counts as "used". The flat's own checks (5.1 and 5.2) catch those.

---

## 6. Where pressure fits in

The firmware still contains an older rule from Alghamdi et al. [2, for the pressure-drop leak rule]. It uses pressure **only to check sooner**, and flow still makes the decision.

> **Rule (`LEAK_PRESSURE_DROP`):**
> 1. Pressure in this flat stays far below its normal for 10 minutes: the node sends a "pressure low" flag to the gateway.
> 2. The node waits 10 minutes for the gateway's answer.
> 3. If the gateway did not call a supply problem, **and** this flat's flow is above its too-much limit, raise `LEAK_PRESSURE_DROP`.

"Far below normal" means: pressure < this hour's average - the bigger of (4 x spread) and 0.3 m.

**Think of pressure as a smoke alarm that says "look now". Flow confirms it and says which flat.**

- **Big leaks:** pressure drops, so the flat checks after about 20 minutes instead of 60. In testing it caught a medium leak 40 minutes sooner.
- **Small leaks:** pressure hardly changes, so this rule does nothing. The night check catches them.
- **Pressure alone is never trusted.** It also drops for a leak in another flat, a low tank, or a stopped pump.

**Open decision:** keep this early warning, or remove it and make the too-much check faster by shortening its 60-minute wait.

### The supply-event rule (gateway)

When the pump stops or the tank runs low, **every** flat's pressure drops at once. That is not a leak. So the gateway calls a `SUPPLY_EVENT` (and tells nodes not to treat it as a leak) if either:

- half or more of the flats sent a "pressure low" flag within 15 minutes, or
- the tank node reported its level is far below normal.

This rule is our own addition, not from a paper.

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

---

## 8. Test results

### 8.1 Full live system test

The whole system ran for 48 simulated hours: real firmware on 22 nodes, the real gateway, the LoRa radio model. Scenario: `../simulation/live/scenarios/flow_leaks.jsonl`.

| Leak added | Caught by | Reached gateway |
|---|---|---|
| Small leak in flat B-203 at 02:00 | Night check on B-203, 02:30 (30 min) | 02:33 |
| Medium leak in flat B-402 at 10:00 | Pressure early warning 10:20; too-much check 11:00 | 10:21 / 11:01 |
| Large burst on downtake B-D1 at 18:00 | Tank outflow check 18:15; gateway building check 18:31 (67% missing) | 18:16 / 18:31 |

- 4,061 of 4,063 radio messages got through. The 2 collisions were re-sent automatically.
- No message was lost.
- No alarms from flats without a leak.

### 8.2 Flow vs pressure comparison

See section 2. Flow caught every leak with no false alarms. Its detection times are set by the "minutes in a row" waits (30 or 60 min for flats, 15 to 30 min for the downtake), not by the sensor.

---

## 9. Known problems and limits

| Problem | What happens | Status |
|---|---|---|
| **One silent node blocks the building check** | The gateway waits for all 21 summaries. If one node dies, the check never runs again. Tested: 90% of water missing, 0 alarms. | To fix. Choice needed: skip the block and warn "node X silent", or fill in its usual amount. |
| **Building check fires only once** | `pipe_alerted` is never reset, so a second pipe leak later gets no alarm. | To fix: re-arm after a few normal blocks, like flat alarms. |
| **Wrong supply-event call** | A medium flat leak drained the tank faster than normal; the tank reported "level low" and the gateway called a supply problem. | To look into. |
| **Simulated water use is too smooth** | Each simulated flat uses a steady trickle (about 0.015 L/s at the 7 am peak). Real use comes in bursts (a shower is 0.1 to 0.15 L/s for 10 minutes). So "0 false alarms" is weak evidence. | Test the too-much check on real home data (Cache County, readings every 4 seconds). |
| **Volume from one reading per minute** | The code works out litres as flow x 60 from a single reading. A 20-second flush between readings is missed. | On real hardware, count the flow sensor's pulses instead. Each pulse is a fixed amount of water. |
| **Very small leaks** | A dripping tap is below the 0.02 L/s night limit. Cheap flow sensors may not even measure flows that low. | Check the minimum flow in the chosen sensor's datasheet. |
| **No location on the downtake** | The building check says "pipe leak", not which floor. | Accepted for now. |
| **Valves** | Valve closing is logged but has no effect on the simulated water. | Left for later. |

---

## 10. Summary of all the numbers

| Setting | Value | Used in |
|---|---|---|
| Learning period | 24 h | All checks |
| Night window | 1 am to 5 am | Night check |
| Night flow limit | 0.02 L/s for 30 min | Night check |
| Too-much limit | max(3 x hourly average, 0.05 L/s) for 60 min | Too-much check |
| Tank outflow limit | average + max(4 x spread, 0.5 L/s) for 15 min | Tank outflow check |
| Missing water limit | 10% for 2 blocks of 15 min, blocks under 5 L skipped | Building check |
| Meter error allowed for | up to 2% per meter | Building check |
| Pressure low limit | average - max(4 x spread, 0.3 m) for 10 min | Pressure early warning |
| Wait for supply verdict | 10 min | Pressure early warning |
| Supply event | half the flats flag low pressure within 15 min, or tank level low | Gateway |
| Alarm re-arm | after 10 min back to normal | All flat and tank alarms |
| Re-send if no reply | every 2 min | All messages |
| Summary period | 15 min | Building check |

---

## 11. How to run the tests

From the `simulation/` folder (it holds the Python environment and the water model):

```
sh live/run_local.sh --scenario live/scenarios/flow_leaks.jsonl   # full live system, three leaks
python3 live/check_comms.py                                       # PASS/FAIL report for that run
uv run python ../leak_detection/compare_leaks.py                  # flow vs pressure, 35 runs (~5 min)
uv run python ../leak_detection/compare_leaks.py --check          # self-check of the scoring
```

Results of the comparison are written to `leak_detection/results/compare_leaks.csv`.
