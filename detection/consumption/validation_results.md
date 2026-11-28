# Abnormal Consumption Detection — Validation Results

> **Scope:** Independent evaluation of four abnormal-consumption detectors in `detection/consumption/detectors.py` across Building B (EPANET/WNTR bursty demand) and Cache County (real 4-second household water-use data), plus firmware integration tests (`detection/consumption/test_integration.py`).

---

## 1. Datasets & Chronological Splits

| Dataset | Preparation & Units | Split & Ground Truth |
|---|---|---|
| **Building B (EPANET/WNTR)** (`eval_building_b.py`) | `build_society.py` (`bursty=True`, 60s steps, `L/s`). `3` flats (`B-401`, `B-203`, `B-G01`) × `3` test seeds (`303, 404, 505`) = `9` flat-series per scenario (`72 h` each). | Day 1 (`0–24h`) train; Days 2–3 test. `9` injected events per category (`HIGH`, `NIGHT`, `ZERO`, `SUDDEN`, `IRREGULAR`) + `9` normal runs (`18` test flat-days). |
| **Cache County** (`eval_cache_county.py`) | HydroShare [`0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e) (5 homes: `003, 006, 010, 020, 031`). 4s pulse counts resampled to 60s `L/s` (`Pulses * MeterResolution * 3.785411784 / 60`). | Series start at midnight; minutes without samples stay missing. Days `1–7` train, `8–14` validation, `15–30` test: `30.0` of `80` test house-days have data. No pipe-leak/anomaly labels. Injections on Day 17 of Sites `006, 010, 020`; only `020` has data then. |

---

These are not untouched hold-outs: detector design was revised after seeing results on them.

---

## 2. Building B Results (Seeds `303, 404, 505`)

| Approach | Normal FP (`/flat/day`) | `HIGH` F1 (lat) | `NIGHT` F1 (lat) | `ZERO` F1 (lat) | `SUDDEN` F1 (lat) | `IRREGULAR` F1 (lat) | Speed (`us/step`) | Peak RAM (`KB`) |
|---|---|---|---|---|---|---|---|---|
| **Statistical Baseline** | `0` (`0.00`) | `1.00` (`59m`) | `1.00` (`14m`) | `0.00` (missed) | `1.00` (`156m`) | `0.00` (missed) | `24.95` | `13.89` |
| **Statistical + Zero-Flow (Selected)** | **`0` (`0.00`)** | **`1.00` (`59m`)** | **`1.00` (`14m`)** | **`1.00` (`1439m`)** | **`1.00` (`156m`)** | `0.00` (missed) | **`18.38`** | `13.71` |
| **LZ78 Compression (Babazadeh [3])** | `52` (`2.89`) | `0.00` | `0.00` | `0.00` | `0.00` | `0.21` (`23m`, `68` FP) | `38.74` | `9.43` |
| **Combined (Stat + LZ78)** | `52` (`2.89`) | `0.60` (`59m`) | `0.64` (`14m`) | `1.00` (`1439m`) | `0.60` (`156m`) | `0.21` (`23m`) | `59.25` | `19.28` |

Speed and RAM are from the original development host; speed varies between machines.

- **Before/After Improvement on `SUDDEN_CONSUMPTION_CHANGE`:** Switching from global 24h mean comparison (`P=0.69`, `4` normal FPs during morning peaks) to same-time-of-day 6h baseline comparison improved Precision from `0.69` to **`1.00`** (`0` normal FPs). The earlier rule is not in the code, so the "before" figures cannot be regenerated.

---

## 3. Cache County Results (Days `15–30`, `30.0` House-Days With Data)

An earlier version of this section was wrong: the script filled recording gaps with zero flow (the source of its `95` zero-flow alerts) and treated each file's first sample as midnight.

| Test days with data, by site | `003` | `006` | `010` | `020` | `031` |
|---|---|---|---|---|---|
| out of `16` | `16.0` | `3.9` | `0` | `8.7` | `1.4` |

- **Alerts on unmodified data** (unlabelled, so not confirmed false positives); in brackets, Sites `006, 010, 020`:
  - `Statistical Baseline`: `14` (`6`): `11` sudden change, `2` night, `1` high.
  - `Statistical + Zero-Flow`: `14` (`6`), identical; **no** zero-flow alerts.
  - `LZ78 Compression`: `56` (`24`), all irregular-burst.
  - `Combined`: `71` (`30`).
- **Injections (Site `020` only, one per category):**
  - `Statistical + Zero-Flow`: `HIGH` `1/1` (`59 min`), `NIGHT` `1/1` (`14 min`), `ZERO` `1/1` (`1383 min` after injection start; flow had already been zero for about an hour), `SUDDEN` `1/1` (`164 min`), `IRREGULAR` `0/1`.
  - `LZ78 Compression`: `IRREGULAR` `1/1` (`44 min`), `0/1` on the other four.
  - Sites `006` and `010`: not scored (no data in the injection window).

---

## 4. Firmware Integration & Leakage Regression (`test_integration.py` + `test_regression.py`)

- **Integration (`detection/consumption/test_integration.py`):** Passed all 5 suites (normal bursty `0` alerts, `NaN`/missing readings handled cleanly, multi-household simultaneous anomalies across a `3.5 h` LoRa outage with `queue == 0` after reconnect, `PROLONGED_ZERO_FLOW` during a `25 h` shutoff, and `0` leak-score contamination).
- **Leakage regression (`detection/leakage/test_regression.py`):** **`ALL 8 REGRESSION SCENARIOS PASSED`** (unchanged).

---

## 5. Selection Rationale & Limitations

- **Why `StatisticalZeroFlowDetector` was selected:** Achieves `F1 = 1.00` across 4 of 5 categories on Building B with `0` normal FPs and `18.38 us/step`, whereas LZ78 produced `2.89` FPs/flat/day (`F1 = 0.21` on irregular bursts) and degraded precision when combined.
- **Limitations:**
  1. `PROLONGED_ZERO_FLOW` (24h) assumes occupied residences; an unoccupied home will trigger it.
  2. `IRREGULAR_BURST_PATTERN` is not detected in production firmware because LZ78 had too many false alarms (`P = 0.12`).
  3. Cache County has no labelled anomaly/leak ground truth, large recording gaps, injections on only one home, and outdoor irrigation at all five homes (hose or sprinkler).
  4. The consumption detector's learned baseline is not saved across a reboot.
