# Leakage-Detection Validation Results (Post-Fix Evaluation)

> **Scope:** validation of leakage detection, supply-event discrimination, cross-floor localisation and offline buffering (`edge/aqua_edge.py`, `gateway/core.py`, `hal/packet.py`). Abnormal-consumption and water-quality modules are scored separately.
>
> **Re-run note:** the current-firmware results below were re-run after fixing the evaluation code and two firmware faults (a gateway crash on a summary with no volume; "minutes in a row" counts carrying across missing readings). Earlier versions of this file reported more favourable supply-event, false-alarm and Cache County results than the code reproduces. "Before" columns describe earlier code and were **not** re-run, except the `4f7867a` benchmark in section 4.

---

## 1. Datasets, Configurations, Seeds & Licences

| Source | Configuration & Preprocessing | Seeds / Coverage | Ground Truth & Licence |
|---|---|---|---|
| **A. SWI-FEED `NET_6`** (`simulation/data/swifeed/`) | 4,419 junctions, 5,066 pipes, 3 reservoirs; 10 monitored junctions (`1000`, `162`, `2055`, `2467`, `420`, `5350`, `5857`, `6424`, `6834`, `8116`), 48h @ 60s (`EpanetSimulator`) | Seeds `10–23`; 6 junctions (`4` base + `2` held-out) × 2 severities (`0.5`, `1.5 L/s`) + 2 no-leak runs | **12** injected leaks, **2** no-leak runs. Published CSVs verified to have `0` leak labels (`has_leak == False` for all `106,128` rows). |
| **B1. Building B — Flat Leaks** (`validate_campaign.py`) | 20 flats + `B-TANK`, bursty demand (`build_society.py`), 54h (`24h` learn + leak at `t=34h`) | `B-401`, `B-203`, `B-G01` × `small` (`0.05`), `medium` (`0.5`), `large` (`1.5 L/s`) × 3 demand seeds (`101`, `202`, `303`) | **27** injected flat leaks (`9` each). |
| **B2. Building B — Downtake Leaks** (`validate_campaign.py`) | 20 flats + `B-TANK`, bursty demand, 48h (leak at `t=34h`) | `B-D4..B-D0` × `medium` (`0.5`), `large` (`1.5 L/s`) × 2 demand seeds (`101`, `202`) | **20** injected downtake leaks (`10` each). |
| **B3. Building B — Supply (WNTR)** (`validate_campaign.py`) | Full WNTR/EPANET 6-scenario suite (36–48h) | Demand seed `42` (supply cases), `77` (normal) | **2** supply cuts (`24h–45h`), **1** leak-drain, **1** simultaneous, **2** non-interruption. |
| **B4. Building B — 7-Day Normal** (`validate_campaign.py`) | 2 × 168h (14 days, the first day of each run used for learning: 12 test days = 240 flat-days), baseline noise (`0.05 m`) + elevated noise/drift (`+0.10 m`, `±0.25 m` 6h drift) | Demand seeds `501`, `602` | **0** leaks, **0** supply interruptions. |
| **B5. 35-Run Benchmark** (`compare_leaks.py`) | 54h minute-by-minute WNTR (`Water`) | `rng(1)` start times, seeds `100–134` | **15** flat leaks, **15** downtake leaks, **5** no-leak runs. |
| **B6. Offline + Supply Suite** (`test_offline_supply.py`) | 8 scenarios (36h) with `Store`, `Gateway`, signed binary packets, 30m & 3.5h LoRa outages | Deterministic synthetic + outage windows | **3** flat-leak runs, **2** supply-cut runs, **2** leak-drain/simul runs, **1** normal. |
| **C. Cache County** (`simulation/data/cache_county/`) | HydroShare [`0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e) (Bastidas Pacheco et al., CC-BY). 4s pulse counts resampled to 60s `L/s` (`Pulses * MeterResolution * 3.785411784 / 60`); recording gaps kept as missing; each series starts at its first midnight. | 5 homes (`003, 006, 010, 020, 031`), first 30 calendar days (`24h` learn + `29` test days): `85.3` recorded test house-days of `145` | **0** pipe-leak labels (end-uses only); synthetic flow-leak injections (`0.05`, `0.5 L/s`): **4** scoreable (Sites `010, 020`); Site `006` not scoreable. |

---

## 2. Source A — SWI-FEED `NET_6`

### 2.1 Reference Hydraulic Reproduction & Label Audit
- **WNTR vs. Published CSVs (`NET_6_bd_pattern_h_24_leak_area_0_{nodes,links}.csv`):**
  - Pressure (`105,816` values): `mean |diff| = 6.71e-9 m`, `max |diff| = 1.49e-6 m`, `r = 1.000000`.
  - Flow (`121,200` values): `mean |diff| = 3.34e-9 m³/s`, `max |diff| = 1.01e-6 m³/s`, `r = 0.9999999999997884`.
- **Label Audit:** `has_leak == False` for all `106,128` rows in both `NET_6_bd_pattern_h_24_leak_area_0_nodes.csv` and `NET_6_bd_0_005_h_24_nodes.csv`.

### 2.2 Detection & Hour-Matched Localisation (`14` Runs: `8` Base + `4` Held-Out + `2` No-Leak)
- **Detection (`LEAK_PRESSURE_DROP`):** **12 / 12 (100%)** injected leaks detected in **9.0 min** (`Precision = 1.00`, `Recall = 1.00`, `F1 = 1.00`, `0` `LEAK_PRESSURE_DROP` false alarms). Each leak is placed at a monitored junction and its flow is added to that junction's meter.
- **Single-Home Zero-Flow Rules:** `LEAK_NIGHT_FLOW` and `LEAK_CONTINUOUS_FLOW` triggered `20` false alerts per run on `NET_6` because `NET_6` nodes represent aggregate municipal DMA demand that never reaches zero.
- **Localisation (Before vs. After Hour-Matched Baseline):** computed inside `validate_swifeed.py` from noise-free simulated pressures, not by the gateway firmware.
  - **Before (`pre - post`):** `2 / 8 (25%)` Top-1 (diurnal `+0.27 m` swing masked leak drops).
  - **After (hour-matched `d1_same_hour - d2_post`):** **`6 / 8 (75%)`** Top-1 on the original 8 runs (`4/4` on `1.5 L/s`, `2/4` on `0.5 L/s`), and **`6 / 12 (50%)`** overall including the 4 held-out runs (`2055`, `5350`, where junction `1000`'s normal `0.609 m` Day-1-to-Day-2 difference dominated).

---

## 3. Source B — Building B (EPANET / WNTR)

### 3.1 Flat-Level Leakage Detection (`27` Multi-Seed Runs)

| Severity | Flow (L/s) | Detected | Mechanism | Latency (min) Before → After | Pre-Leak False | Neighbor False Leaks |
|---|---|---|---|---|---|---|
| **Small** | `0.05` | **9 / 9 (100%)** | `LEAK_CONTINUOUS_FLOW` (9/9) | `479.0` → **`179.0`** (`-62.6%`) | `0` | `0` |
| **Medium** | `0.50` | **9 / 9 (100%)** | `LEAK_PRESSURE_DROP` (9/9) | `9.0` → **`9.0`** | `0` | `0` |
| **Large** | `1.50` | **9 / 9 (100%)** | `LEAK_PRESSURE_DROP` (9/9) | `9.0` → **`9.0`** | `0` | `0` |

- Latencies are medians (small: `169–179 min`; one medium run `10 min`).
- **Precision = `1.00`, Recall = `1.00`, F1 = `1.00`** for these 27 flat-leak runs. This does not cover the false flat alerts in the downtake runs (3.2).

### 3.2 Main Downtake / Riser Leakage & Cross-Floor Localisation (`20` Multi-Seed Runs)

| Severity | `PIPE_LEAK` Detected | Latency (min) | Top-1 Accuracy Before → After | Top-2 Accuracy Before → After | Confidence Breakdown |
|---|---|---|---|---|---|
| **Medium (`0.5 L/s`)** | **10 / 10 (100%)** | `30.0` | `3/10 (30%)` → `1/10 (10%)` | `4/10 (40%)` → **`7/10 (70%)`** | `8/10` `unlocalised`, `2/10` `high` |
| **Large (`1.5 L/s`)** | **10 / 10 (100%)** | `30.0` | `5/10 (50%)` → **`10/10 (100%)`** | `8/10 (80%)` → **`10/10 (100%)`** | `10/10` `high` |

- Switching from `p_min` to `p_mean` and using the downward riser headloss gradient (`step[f] = drop[f] - drop[f_above]`) gives **100% Top-1 localisation on large bursts** in these runs, and marks **`8/10` medium leaks as `unlocalised`**.
- **False flat leak alerts:** in **10 of the 20** runs (every run with demand seed `101`, all sites and both sizes), B-402, which has no leak, raises `LEAK_PRESSURE_DROP` 49 min after the downtake leak starts. The leak lowers its pressure while its own use is high. These 10 are repeats of one demand pattern, not 10 independent events.
- **Gateway label:** all `10` get `possible_cause=PIPE_LEAK` (the alert comes at 49 min, `PIPE_LEAK` at 30 min); `0` unlabelled. In the `27` flat-leak runs, `0` real flat-leak alerts are labelled. The label does not remove the alert, so these still count as `10` false positives. These runs have no radio outage, and none has a flat leak and a pipe leak together.

### 3.3 Supply-Event Discrimination (`6` Full WNTR Scenarios + `4` Unit Tests)

| Scenario (WNTR) | Ground Truth | Before | After |
|---|---|---|---|
| `low_tank_no_interruption_no_leak` | No interruption | `[]` | `[]` |
| `pump_failure_no_leak` (`24h–45h`) | Supply interruption | `[]` (**missed**) | **`SUPPLY_EVENT` (`44.15h`)** |
| `municipal_supply_interruption` (`24h–45h`) | Supply interruption | `[]` (**missed**) | **`SUPPLY_EVENT` (`44.15h`)** |
| `leak_abnormal_tank_drain` | Leak draining tank | `PIPE_LEAK` + `LEAK_TANK_DRAIN` | `PIPE_LEAK` (`26.5h`) + `LEAK_TANK_DRAIN` (`28.18h`, `34.12h`) |
| `simultaneous_supply_and_leak` | Both | `PIPE_LEAK` + `LEAK_TANK_DRAIN` | `PIPE_LEAK` (`26.5h`) + `LEAK_TANK_DRAIN` (`27.48h`) |
| `normal_variable_demand` | Normal (48 h) | `[]` | `[]` |

- Both interruptions are detected, about **20 h** after the cut, when the tank has drained far enough.
- Counting the 7-day normal runs below, `SUPPLY_EVENT` has `TP = 2`, `FP = 7`, `FN = 0` → **Precision = `0.22`, Recall = `1.00`, F1 = `0.36`**. The 4 deterministic supply cases in `test_regression.py` pass.

### 3.4 Seven-Day Normal-Operation Robustness (`14` Simulated Days, `12` After Learning = `240` Flat-Days)

| Run | Flat Leak Alerts | `LEAK_PRESSURE_DROP` | `PIPE_LEAK` | `ABNORMAL_CONSUMPTION` | False `SUPPLY_EVENT` |
|---|---|---|---|---|---|
| `7d_baseline_noise` (seed `501`) | `0` | `0` | `0` | `0` | **`5`** |
| `7d_elevated_noise_drift` (seed `602`) | `0` | `0` | `0` | `0` | **`2`** |
| **Combined (12 test days)** | **`0`** | **`0`** | **`0`** | **`0`** | **`7` (`0.58/day`)** |

- Only `ABNORMAL_CONSUMPTION` is counted among consumption alerts; the alerts from `detection/consumption/detectors.py` are not.
- **Cause of the supply false alarms** (only the first one was traced): the learned floor was `0.68 m`, but the node uses the lower of that and this hour's average minus `0.3 m`, which was `0.53 m` then. On an ordinary pump cycle the level stayed below `0.53 m` for 10 min, shortly before the pump (switch-on level `0.5 m`) refilled the tank, with inflow `0`, which the gateway reads as a supply interruption. All 7 report zero inflow. The same 5 events of seed `501` also occur on commit `990279d`. Not fixed: changing the rule is a design decision.

---

## 4. Before / After 35-Run Benchmark (`detection/leakage/compare_leaks.py`)

| Metric | Prior Baseline (`4f7867a`) | Pre-Fix (`CONT_FLOW_MIN=480`) | Post-Fix (`CONT_FLOW_MIN=180`, `p_mean`) |
|---|---|---|---|
| **Total leaks caught** (flow signals / flat pressure) | `30 / 30` | `30 / 30` | **`30 / 30`** |
| **Flat `small` (`0.05 L/s`) median** | `60 min` (flow) | `480 min` (flow) | **`180 min`** (flow) |
| **Flat `medium` (`0.5 L/s`) median** | `60 min` (flow); pressure-only `10 min` | `10 min` (`LEAK_PRESSURE_DROP`) | **`10 min`** (`LEAK_PRESSURE_DROP`) |
| **Flat `large` (`1.5 L/s`) median** | `60 min` (flow); pressure-only `10 min` | `10 min` (`LEAK_PRESSURE_DROP`) | **`10 min`** (`LEAK_PRESSURE_DROP`) |
| **Downtake (`small`/`med`/`large`)** | Caught | `29 / 20 / 15 min` | **`29 / 20 / 15 min`** |
| **Total false alarms (all 35 runs)** | Flow `0` · Pressure `748` | Flow `0` · Pressure `0` | **Flow `0` · Pressure `0`** |

The score covers leak, supply-outflow and pipe-balance signals only. On the current firmware each of the 5 no-leak runs also raises one `UNUSUAL_NIGHT_CONSUMPTION`.

---

## 5. Source C — Cache County Household Consumption Data

Full-record audit (4 s sampling, `0` pipe-leak labels). A recording gap ends a continuous run.

| Site | `sites.csv` irrigation | First-to-last span (days) | Days with data | Longest run `> 0.005 L/s` | Runs `>= 180 min` |
|---|---|---|---|---|---|
| `003` | Hose | `201.1` | `156.6` | `630 min` | `11` |
| `006` | Hose | `160.0` | `43.0` | `85 min` | `0` |
| `010` | Hose | `316.9` | `42.8` | `83 min` | `0` |
| `020` | SprinklerSystem | `261.9` | `35.7` | `91 min` | `0` |
| `031` | SprinklerSystem | `113.3` | `31.6` | `406 min` | `5` |

- **About the 3 h limit:** it was supported by "Sites `006, 010, 020`: max 91 min over 739 house-days". The maximum is right, but `739` is the calendar span including gaps; there are **`121.5`** days of data. These sites are not an "indoor-only" group (one has a sprinkler system), and the two excluded homes exceed 3 h. Site `003`'s 11 long runs are mostly labelled `faucet` at `0.009–0.032 L/s`; the labels cannot show whether that was a drip, a leak or use.
- **False positives, first 30 days:**

| Site | Recorded test days | Flat leak alerts |
|---|---|---|
| `003` | `29.0` | `0` |
| `006` | `11.7` | `0` |
| `010` | `8.5` | `0` |
| `020` | `21.7` | `0` |
| `031` | `14.4` | **`10`** (`6` `LEAK_NIGHT_FLOW`, `4` `LEAK_CONTINUOUS_FLOW`; `0.69/day`) |

  By the end-use labels, 8 of Site `031`'s alerts are on irrigation, mostly running overnight, and 2 on a run of about 3 h labelled `unknown`. The night alerts were missing from earlier results because the evaluation treated the first sample as midnight.
- **Controlled leak injections** (day 3, 10:00; flow only, no pressure data): **4 / 4** scoreable runs (Sites `010, 020`) detected in **`179.0 min`** by `LEAK_CONTINUOUS_FLOW`, with no earlier leak alert. Both Site `006` runs are not scored: only `17%` of the 24 h after injection is recorded. A detection counts only if the same data without the leak does not raise that alert at that time.

---

## 6. Combined Offline & Supply-Event Validation (`test_offline_supply.py`)

All **8 / 8** scenarios passed: queues empty after reconnecting, no duplicate packet logged, no message stamped later than it arrived, expected leak and supply events delivered after 30 min and 3.5 h LoRa outages. The test now also fails on any flat leak alert from a flat without a leak. Earlier, its synthetic flats drew a constant `0.025 L/s`, so **every flat raised false night and continuous-flow alerts, even in the "normal" case**; `test_regression.py` (supply cases) and `test_supply_robustness.py` had the same fault. All three now use intermittent use. One result depended on those false alerts: in robustness case `2d` the gateway now reports `SUPPLY_UNCERTAIN` first and `LEAK_TANK_DRAIN` about 5 min later. The "internet unavailable" case does not differ technically from the others, since nothing uses the internet.

---

## 7. Event-Level Confusion Matrices

| Suite | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|
| **Building B Flat Leaks (27 leak + 12 normal test days)** | `27` | `0` | `0` | `240 flat-days` | `1.00` | `1.00` | `1.00` |
| **Building B Downtake `PIPE_LEAK` (20 leak + 12 normal test days)** | `20` | `0` | `0` | `12 days` | `1.00` | `1.00` | `1.00` |
| **Building B flat alerts during the 20 downtake runs** | — | `10` | — | — | — | — | — |
| **Building B `SUPPLY_EVENT` (6 WNTR + 12 normal test days)** | `2` | `7` | `0` | — | `0.22` | `1.00` | `0.36` |
| **35-Run `compare_leaks.py` Benchmark (leak signals)** | `30` | `0` | `0` | `5` | `1.00` | `1.00` | `1.00` |
| **SWI-FEED `NET_6` (`LEAK_PRESSURE_DROP` only, 12+2)** | `12` | `0` | `0` | `2` | `1.00` | `1.00` | `1.00` |
| **Cache County Normal, Sites `003, 006, 010, 020`** | — | `0` | — | `70.9 recorded days` | — | — | — |
| **Cache County Normal, Site `031`** | — | `10` | — | `14.4 recorded days` | — | — | — |
| **Cache County Injected Leaks (Sites `010, 020`)** | `4` | `0` | `0` | — | — | `1.00` | — |

---

## 8. Final Assessment & Limitations

- **Detected in these tests:** medium (`0.5 L/s`) and large (`1.5 L/s`) flat leaks (`9–10 min`); small (`0.05 L/s`) flat leaks (`169–180 min`); downtake `PIPE_LEAK` (`30 min` in the campaign); both simulated supply interruptions (about 20 h after the cut); buffered delivery after LoRa outages (`8/8` scenarios). All data with leaks is simulated or injected.
- **Not reliable:** `SUPPLY_EVENT` (7 false events in 12 normal test days, and one during a flat leak in the live scenario, see `README.md` 8.1); flat attribution during a downtake leak (10 false flat alerts); the 3 h continuous-flow limit on homes with overnight irrigation or long low flows.
- **Localisation:** large (`1.5 L/s`) downtake leaks achieve **`100%` Top-1** in simulation; medium (`0.5 L/s`) ones remain difficult (`8/10` marked `unlocalised`, `7/10` Top-2).
- **Limitations:** Cache County lacks pressure sensors and naturally occurring pipe-leak labels; outdoor lawn sprinklers (`Site 031`) exceed `180 min` continuous flow; `NET_6` continuous municipal demand is incompatible with single-flat zero-flow rules.
