# Abnormal Water Consumption Detection

Offline-first detection and evaluation of abnormal household water-consumption patterns (Slide 2 & Paper [3] Babazadeh, 2019), logically separated from pipeline leakage (`../leak_detection/`) and TinyML water quality (`../tinyml/`).

---

## 1. Event Categories (Strictly Separate from `LEAK_*`)

| Event | Meaning | Firmware Alert | Distinction from Pipeline Leakage |
|---|---|---|---|
| `UNUSUAL_HIGH_CONSUMPTION` | Flow exceeds `max(3 × hourly_mean, 0.05 L/s)` for `60 min` | `ABNORMAL_CONSUMPTION` | Simultaneous taps/guests raise flat flow without indicating a pipe leak |
| `UNUSUAL_NIGHT_CONSUMPTION` | Quiet-hour (`01:00–05:00`) burst `> max(mean + 3×std, 0.06 L/s)` for `15 min` | `UNUSUAL_NIGHT_CONSUMPTION` | Short/high-flow night usage vs. steady low-flow `LEAK_NIGHT_FLOW` (`0.02 L/s`, `30 min`) |
| `PROLONGED_ZERO_FLOW` | Continuous zero flow (`<= 0.002 L/s`) for `24 h` (`1440 min`) | `PROLONGED_ZERO_FLOW` | Indicates extended shutoff/vacancy/meter halt; does not prove pipe blockage |
| `SUDDEN_CONSUMPTION_CHANGE` | Rolling 6h mean `> max(2.2 × same-time-of-day 6h baseline, 0.025 L/s)` | `SUDDEN_CONSUMPTION_CHANGE` | Sustained schedule/habit shift relative to that household's own history |
| `IRREGULAR_BURST_PATTERN` | Rapid erratic fixture switching (evaluated via LZ78 complexity rate) | Research/eval only (not deployed) | Evaluated separately; not deployed due to high false-positive rate |

None of these events trigger `PIPE_LEAK`, `LEAK_TANK_DRAIN`, or count as leak detections in `../leak_detection/compare_leaks.py` or `../simulation/live/check_comms.py`.

---

## 2. How Experimentation Was Done

All four detectors in [`detectors.py`](detectors.py) were evaluated causally (1-minute steps, no future readings) on identical untouched held-out test sets:

1. **Building B EPANET/WNTR (`eval_building_b.py`):**
   - Uses independent stochastic bursty household schedules (`../simulation/scripts/build_society.py`, `bursty=True`, 60s steps, `L/s`).
   - Validation seeds (`101, 202`) were used to verify threshold sanity; **held-out test seeds (`303, 404, 505`)** across 3 flats (`B-401`, `B-203`, `B-G01`) produced `9` flat-series × `6` scenarios (`normal` + 5 injected categories) = `54` runs (`72 h` each: Day 1 learn, Days 2–3 test).
   - Resource usage was measured with `time.perf_counter()` (`us/step`) and `tracemalloc` (`peak_ram_kb`).

2. **Cache County Real Household Data (`eval_cache_county.py`):**
   - Source: HydroShare [`0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e) (5 homes downloaded under `../simulation/data/cache_county/`: Sites `003, 006, 010, 020, 031`).
   - Audited schema & units: `4 s` pulse counts converted to 60s `L/s` via `Pulses * MeterResolution * 3.785411784 / 60`, `0` NaNs, and `0` naturally occurring pipe-leak or anomaly labels (`LabelledEvents` contains end-uses only).
   - **Chronological split (no random shuffling):** Days `1–7` train, Days `8–14` validation, **Days `15–30` held-out test** (`16` days/home × `5` = `80` real house-days). Controlled synthetic anomalies (`3` per category) were injected only into held-out Days `15–30` of indoor-focused Sites `006, 010, 020` and reported separately from natural observations.

---

## 3. Design Decisions (Whys & Hows)

### Why `StatisticalZeroFlowDetector` Was Selected (and LZ78 Was Not Deployed)
| Approach | Building B Normal FP (`/flat/day`) | `HIGH` F1 | `NIGHT` F1 | `ZERO` F1 | `SUDDEN` F1 | `IRREGULAR` F1 | Speed (`us/step`) | Peak RAM (`KB`) |
|---|---|---|---|---|---|---|---|---|
| `StatisticalBaselineDetector` | `0` (`0.00`) | `1.00` | `1.00` | `0.00` | `1.00` | `0.00` | `24.95` | `13.89` |
| **`StatisticalZeroFlowDetector` (Selected)** | **`0` (`0.00`)** | **`1.00`** | **`1.00`** | **`1.00`** | **`1.00`** | `0.00` | **`18.38`** | `13.71` |
| `LZCompressionDetector` (Babazadeh [3]) | `52` (`2.89`) | `0.00` | `0.00` | `0.00` | `0.00` | `0.21` | `38.74` | `9.43` |
| `CombinedConsumptionDetector` | `52` (`2.89`) | `0.60` | `0.64` | `1.00` | `0.60` | `0.21` | `59.25` | `19.28` |

- **Why LZ78 (`LZCompressionDetector`) failed in practice:** Quantizing 1-min flows into 6 discrete symbols over `120 min` windows and monitoring normalized LZ78 complexity `c(W)*log2(c(W)+1)/W` failed to detect steady high consumption, night bursts, prolonged zero-flow, or scale shifts (`0/9` recall) because uniform/steady anomalies have *low* entropy rather than high entropy. Meanwhile, normal bursty household switching triggered `52` false alarms (`2.89/flat/day` in Building B; `74` false alerts in Cache County) and gave `F1 = 0.21` (`P = 0.12`) on irregular bursts. Combining LZ78 with statistical rules degraded overall precision without helping any other category, so LZ78 is retained in `detectors.py` solely as a documented research baseline.
- **Why `SUDDEN_CONSUMPTION_CHANGE` uses a same-time-of-day 6h baseline:** Comparing a rolling 6h mean against the global 24h mean initially produced `4` false positives (`P = 0.69`) during normal morning peaks (`07:00–10:00`). Comparing against the household's own learned baseline for those exact 6 hours eliminated all `4` false positives (`P = 1.00`).
- **Why `PROLONGED_ZERO_FLOW` requires `24 h` (`1440 min`) continuous zero flow:** A shorter `10 h` daytime (`06:00–22:00`) rule fired `95` times on Cache County homes during normal weekday work/school absences. Requiring `24 h` continuous zero flow prevents overnight sleep and workday absences from firing while catching multi-day shutoffs (`F1 = 1.00` on Building B).

---

## 4. Firmware Integration

- `FlatNode` (`../firmware/aqua_edge.py`) runs `StatisticalZeroFlowDetector` locally (offline, `NaN`/missing-safe) alongside `ABNORMAL_CONSUMPTION`, emitting separate alert codes defined in `../firmware/packet.py`.
- `../firmware/gateway.py` logs, deduplicates, and acknowledges consumption alerts without triggering `PIPE_LEAK` or `LEAK_TANK_DRAIN`.

---

## 5. Limitations

1. **Unoccupied homes trigger `PROLONGED_ZERO_FLOW`:** In Cache County's real homes, multi-day absences/vacations produced `95` zero-flow alerts over `80` test house-days. The `24 h` rule assumes an occupied residence; deployments with frequent multi-day travel should disable it or raise the threshold (e.g. `48–72 h`).
2. **`IRREGULAR_BURST_PATTERN` is not enabled in firmware:** Neither simple hourly statistics nor LZ78 compression detected irregular switching bursts with acceptable precision (`LZ78` `P = 0.12`).
3. **Cache County dataset scope:** Only `5` of `31` homes were downloaded (`003, 006, 010, 020, 031`); there are no naturally labelled consumption anomalies or pipe leaks, and `Site 031` includes multi-hour outdoor lawn irrigation (`SprinklerSystem`).

---

## 6. Running the Tests

```bash
uv run python abnormal_consumption/eval_building_b.py
uv run python abnormal_consumption/eval_cache_county.py
uv run python abnormal_consumption/test_integration.py
```

See [`validation_results.md`](validation_results.md) for per-category confusion matrices and detailed logs.
