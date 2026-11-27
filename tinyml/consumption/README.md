# Abnormal Water Consumption Detection

Offline-first detection and evaluation of abnormal household water-consumption patterns (Slide 2 & Paper [3] Babazadeh, 2019), logically separated from pipeline leakage (`../leakage/`) and TinyML water quality (`../quality/`).

---

## 1. Event Categories (Strictly Separate from `LEAK_*`)

| Event | Meaning | Firmware Alert | Distinction from Pipeline Leakage |
|---|---|---|---|
| `UNUSUAL_HIGH_CONSUMPTION` | Flow exceeds `max(3 × hourly_mean, 0.05 L/s)` for `60 min` | `ABNORMAL_CONSUMPTION` | Simultaneous taps/guests raise flat flow without indicating a pipe leak |
| `UNUSUAL_NIGHT_CONSUMPTION` | Quiet-hour (`01:00–05:00`) burst `> max(mean + 3×std, 0.06 L/s)` for `15 min` | `UNUSUAL_NIGHT_CONSUMPTION` | Short/high-flow night usage vs. steady low-flow `LEAK_NIGHT_FLOW` (`0.02 L/s`, `30 min`) |
| `PROLONGED_ZERO_FLOW` | Continuous zero flow (`<= 0.002 L/s`) for `24 h` (`1440 min`) | `PROLONGED_ZERO_FLOW` | Indicates extended shutoff/vacancy/meter halt; does not prove pipe blockage |
| `SUDDEN_CONSUMPTION_CHANGE` | Rolling 6h mean `> max(2.2 × same-time-of-day 6h baseline, 0.025 L/s)` | `SUDDEN_CONSUMPTION_CHANGE` | Sustained schedule/habit shift relative to that household's own history |
| `IRREGULAR_BURST_PATTERN` | Rapid erratic fixture switching (evaluated via LZ78 complexity rate) | Research/eval only (not deployed) | Evaluated separately; not deployed due to high false-positive rate |

In the firmware, `ABNORMAL_CONSUMPTION` comes from `aqua_edge.py`'s own copy of the high-flow rule (same thresholds, using the node's baseline); the detector's `UNUSUAL_HIGH_CONSUMPTION` event is discarded. "For N min" means consecutive readings: a missing minute restarts the count, and a gap over `1 h` also empties the 6h window.

None of these events trigger `PIPE_LEAK`, `LEAK_TANK_DRAIN`, or count as leak detections in `../leakage/compare_leaks.py` or `../../simulation/live/check_comms.py`.

---

## 2. How Experimentation Was Done

All four detectors in [`detectors.py`](detectors.py) were evaluated causally (1-minute steps, no future readings) on the same test sets. These are **not untouched hold-outs**: design choices in Section 3 were made after looking at results on them, so treat the scores as development results.

1. **Building B EPANET/WNTR (`eval_building_b.py`):**
   - Uses independent stochastic bursty household schedules (`../../simulation/scripts/build_society.py`, `bursty=True`, 60s steps, `L/s`).
   - **Test seeds (`303, 404, 505`)** (the script runs no separate validation seeds) across 3 flats (`B-401`, `B-203`, `B-G01`) produced `9` flat-series × `6` scenarios (`normal` + 5 injected categories) = `54` runs (`72 h` each: Day 1 learn, Days 2–3 test). False positives are counted on the `normal` runs and before each injection; alerts after an injection window are not scored.
   - Resource usage was measured with `time.perf_counter()` (`us/step`) and `tracemalloc` (`peak_ram_kb`).

2. **Cache County Real Household Data (`eval_cache_county.py`):**
   - Source: HydroShare [`0b72cddfc51c45b188e0e6cd8927227e`](https://www.hydroshare.org/resource/0b72cddfc51c45b188e0e6cd8927227e) (5 homes downloaded under `../../simulation/data/cache_county/`: Sites `003, 006, 010, 020, 031`).
   - `4 s` pulse counts are converted to 60s `L/s` via `Pulses * MeterResolution * 3.785411784 / 60`. The data has no pipe-leak or anomaly labels.
   - **The recordings have long gaps.** Minutes without samples are kept missing, not counted as zero flow. Each series starts at the first midnight, so hour-of-day rules see clock time.
   - **Chronological split (no random shuffling):** Days `1–7` train, Days `8–14` validation, Days `15–30` test. Of the `80` calendar house-days in the test window, only **`30.0` contain data** (Site `003`: `16.0`; `006`: `3.9`; `010`: `0`; `020`: `8.7`; `031`: `1.4`).
   - Synthetic anomalies (one per category per site) are planned for Day 17 of Sites `006, 010, 020`. Sites `006` and `010` have no data then, so **only Site `020` is scored**. A hit counts only if the same alert does not also fire without the injection.
   - **Correction:** an earlier version filled gaps with zero flow and treated each file's first sample as midnight. Its Cache County figures (`80` house-days, `95` zero-flow alerts, `3/3` injections) were wrong.

---

## 3. Design Decisions (Whys & Hows)

### Why `StatisticalZeroFlowDetector` Was Selected (and LZ78 Was Not Deployed)
| Approach | Building B Normal FP (`/flat/day`) | `HIGH` F1 | `NIGHT` F1 | `ZERO` F1 | `SUDDEN` F1 | `IRREGULAR` F1 | Speed (`us/step`)\* | Peak RAM (`KB`)\* |
|---|---|---|---|---|---|---|---|---|
| `StatisticalBaselineDetector` | `0` (`0.00`) | `1.00` | `1.00` | `0.00` | `1.00` | `0.00` | `24.95` | `13.89` |
| **`StatisticalZeroFlowDetector` (Selected)** | **`0` (`0.00`)** | **`1.00`** | **`1.00`** | **`1.00`** | **`1.00`** | `0.00` | **`18.38`** | `13.71` |
| `LZCompressionDetector` (Babazadeh [3]) | `52` (`2.89`) | `0.00` | `0.00` | `0.00` | `0.00` | `0.21` | `38.74` | `9.43` |
| `CombinedConsumptionDetector` | `52` (`2.89`) | `0.60` | `0.64` | `1.00` | `0.60` | `0.21` | `59.25` | `19.28` |

\* Speed and RAM come from one 72 h series on the original development host; speed varies between machines (a Linux rerun was about `2-3×` faster).

- **Why LZ78 (`LZCompressionDetector`) failed in practice:** Quantizing 1-min flows into 6 discrete symbols over `120 min` windows and monitoring normalized LZ78 complexity `c(W)*log2(c(W)+1)/W` failed to detect steady high consumption, night bursts, prolonged zero-flow, or scale shifts (`0/9` recall) because uniform/steady anomalies have *low* entropy rather than high entropy. Meanwhile, normal bursty household switching triggered `52` false alarms (`2.89/flat/day` in Building B; `56` alerts on unmodified Cache County data, all `IRREGULAR_BURST_PATTERN`; the data has no labels to confirm them as false) and gave `F1 = 0.21` (`P = 0.12`) on irregular bursts. Combining LZ78 with statistical rules degraded overall precision without helping any other category, so LZ78 is retained in `detectors.py` solely as a documented research baseline.
- **Why `SUDDEN_CONSUMPTION_CHANGE` uses a same-time-of-day 6h baseline:** Comparing a rolling 6h mean against the global 24h mean initially produced `4` false positives (`P = 0.69`; a historical figure, as the earlier rule is not in the code) during normal morning peaks (`07:00–10:00`). Comparing against the household's own learned baseline for those exact 6 hours eliminated all `4` false positives (`P = 1.00`).
- **Why `PROLONGED_ZERO_FLOW` requires `24 h` (`1440 min`) continuous zero flow:** The aim is not to alert on overnight sleep or ordinary workday absences; the rule catches multi-day shutoffs (`F1 = 1.00` on Building B). The corrected Cache County data gives no zero-flow alerts, so it does not show how often real absences would trigger it. An earlier claim that a `10 h` daytime rule fired `95` times is withdrawn: no script implements that rule, and `95` was the `24 h` rule's count on gaps filled with zeros.

---

## 4. Firmware Integration

- `FlatNode` (`../../edge/aqua_edge.py`) runs `StatisticalZeroFlowDetector` locally (offline, `NaN`/missing-safe) alongside `ABNORMAL_CONSUMPTION`, emitting separate alert codes defined in `../../hal/packet.py`.
- While a condition lasts, an alert can repeat: the detector re-emits after `1 h` (high, night), `6 h` (sudden change) or `12 h` (zero flow), and the node forgets an alert after `10 min` without a new event, so each re-emission is sent again.
- **The detector's learned baseline is not saved.** The node's own baseline is restored from the SD card after a reboot, but `StatisticalZeroFlowDetector`'s is not. After a reboot past Day 1 it starts from zero and does not repeat the learning day; it only blends in later hours at weight `1/14`. Until then, its night and sudden-change limits sit at or near their floors (`0.06` and `0.025 L/s`), which can cause false alerts.
- `../../gateway/core.py` logs, deduplicates, and acknowledges consumption alerts without triggering `PIPE_LEAK` or `LEAK_TANK_DRAIN`.

---

## 5. Limitations

1. **Unoccupied homes will trigger `PROLONGED_ZERO_FLOW`:** the rule assumes an occupied residence; deployments with frequent multi-day travel should disable it or raise the threshold (e.g. `48–72 h`).
2. **`IRREGULAR_BURST_PATTERN` is not enabled in firmware:** Neither simple hourly statistics nor LZ78 compression detected irregular switching bursts with acceptable precision (`LZ78` `P = 0.12`).
3. **Cache County dataset scope:** The evaluation uses `5` of `31` homes (`003, 006, 010, 020, 031`). Only `30` of `80` test house-days have data, injections are scored on one home (too few to estimate recall), and there are no labelled consumption anomalies or pipe leaks. None of these homes is indoor-only: `sites.csv` lists hose irrigation for `003, 006, 010` and a sprinkler system for `020, 031`.
4. **Reboots:** the consumption detector's baseline is lost (Section 4).

---

## 6. Running the Tests

Cache County needs `sites.csv` and `site{003,006,010,020,031}qc_data.csv` from HydroShare in `../../simulation/data/cache_county/`. Evaluation JSON is written to `results/` (ignored by Git).

```bash
uv run python -m tinyml.consumption.eval_building_b
uv run python -m tinyml.consumption.eval_cache_county
uv run python -m tinyml.consumption.test_integration
```

See [`validation_results.md`](validation_results.md) for per-category confusion matrices and detailed logs.
