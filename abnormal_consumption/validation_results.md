# Abnormal Consumption Detection — Validation Results

> **Scope:** Independent evaluation of four abnormal-consumption detectors in `abnormal_consumption/detectors.py` across Building B (EPANET/WNTR bursty demand) and Cache County (real 4-second household water-use data), plus firmware integration tests (`abnormal_consumption/test_integration.py`).

---

## 1. Datasets & Chronological Splits

| Dataset | Preparation & Units | Split & Ground Truth |
|---|---|---|
| **Building B (EPANET/WNTR)** (`eval_building_b.py`) | `build_society.py` (`bursty=True`, 60s steps, `L/s`). `3` flats (`B-401`, `B-203`, `B-G01`) × `3` held-out test seeds (`303, 404, 505`) = `9` flat-series per scenario (`72 h` each). | Day 1 (`0–24h`) train; Days 2–3 test. `9` injected events per category (`HIGH`, `NIGHT`, `ZERO`, `SUDDEN`, `IRREGULAR`) + `9` normal runs (`18` test flat-days). |
| **Cache County** (`eval_cache_county.py`) | HydroShare [`0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e) (5 homes: `003, 006, 010, 020, 031`). 4s pulse counts resampled to 60s `L/s` (`Pulses * MeterResolution * 3.785411784 / 60`). | **Chronological split:** Days `1–7` train, Days `8–14` validation, Days `15–30` held-out test (`80` real house-days). Natural data has **0** pipe-leak/anomaly labels; `3` controlled injections per category added to held-out Days `15–30` of Sites `006, 010, 020`. |

---

## 2. Building B Results (Held-Out Seeds `303, 404, 505`)

| Approach | Normal FP (`/flat/day`) | `HIGH` F1 (lat) | `NIGHT` F1 (lat) | `ZERO` F1 (lat) | `SUDDEN` F1 (lat) | `IRREGULAR` F1 (lat) | Speed (`us/step`) | Peak RAM (`KB`) |
|---|---|---|---|---|---|---|---|---|
| **Statistical Baseline** | `0` (`0.00`) | `1.00` (`59m`) | `1.00` (`14m`) | `0.00` (missed) | `1.00` (`156m`) | `0.00` (missed) | `24.95` | `13.89` |
| **Statistical + Zero-Flow (Selected)** | **`0` (`0.00`)** | **`1.00` (`59m`)** | **`1.00` (`14m`)** | **`1.00` (`1439m`)** | **`1.00` (`156m`)** | `0.00` (missed) | **`18.38`** | `13.71` |
| **LZ78 Compression (Babazadeh [3])** | `52` (`2.89`) | `0.00` | `0.00` | `0.00` | `0.00` | `0.21` (`23m`, `68` FP) | `38.74` | `9.43` |
| **Combined (Stat + LZ78)** | `52` (`2.89`) | `0.60` (`59m`) | `0.64` (`14m`) | `1.00` (`1439m`) | `0.60` (`156m`) | `0.21` (`23m`) | `59.25` | `19.28` |

- **Before/After Improvement on `SUDDEN_CONSUMPTION_CHANGE`:** Switching from global 24h mean comparison (`P=0.69`, `4` normal FPs during morning peaks) to same-time-of-day 6h baseline comparison improved Precision from `0.69` to **`1.00`** (`0` normal FPs).

---

## 3. Cache County Results (Held-Out Days `15–30`, `80` House-Days)

- **Natural Normal Consumption (No Injected Anomalies):**
  - `Statistical Baseline`: **`15`** alerts across all 5 homes (`5` across indoor-focused Sites `006, 010, 020` = `0.104 / home / day`).
  - `Statistical + Zero-Flow`: `110` alerts (`73` across `006, 010, 020`) — **`95` of these were `PROLONGED_ZERO_FLOW` during real multi-day household absences/vacations**.
  - `LZ78 Compression`: `74` false alerts (`41` on `006, 010, 020`).
  - `Combined`: `184` alerts (`114` on `006, 010, 020`).
- **Controlled Held-Out Injections (`Sites 006, 010, 020`, 3 per category):**
  - `Statistical + Zero-Flow`: `HIGH` **`3/3`** (`59 min`), `NIGHT` `2/3` (`14 min`), `ZERO` **`3/3`**, `SUDDEN` **`3/3`** (`34–300 min`), `IRREGULAR` `0/3`.
  - `LZ78 Compression`: `IRREGULAR` `3/3` (`25–37 min`), `0/3` on all other 4 categories.

---

## 4. Firmware Integration & Leakage Regression (`test_integration.py` + `test_regression.py`)

- **Integration (`abnormal_consumption/test_integration.py`):** Passed all 5 suites (normal bursty `0` alerts, `NaN`/missing readings handled cleanly, multi-household simultaneous anomalies across a `3.5 h` LoRa outage with `queue == 0` after reconnect, `PROLONGED_ZERO_FLOW` in `24 h`, and `0` leak-score contamination).
- **Leakage regression (`leak_detection/test_regression.py`):** **`ALL 8 REGRESSION SCENARIOS PASSED`** (unchanged).

---

## 5. Selection Rationale & Limitations

- **Why `StatisticalZeroFlowDetector` was selected:** Achieves `F1 = 1.00` across 4 of 5 categories on Building B with `0` normal FPs and `18.38 us/step`, whereas LZ78 produced `2.89` FPs/flat/day (`F1 = 0.21` on irregular bursts) and degraded precision when combined.
- **Limitations:**
  1. `PROLONGED_ZERO_FLOW` (24h) assumes occupied residences; real unoccupied homes on multi-day trips (observed in Cache County) trigger legitimate zero-flow alerts.
  2. `IRREGULAR_BURST_PATTERN` is not detected in production firmware because LZ78 had too many false alarms (`P = 0.12`).
  3. Cache County has no naturally labelled anomaly/leak ground truth and includes outdoor irrigation (`Site 031`).
