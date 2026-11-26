# Aqua Edge: TinyML (Water Quality)

The water-quality pipeline for the edge node. It combines **two-stage noise filtering** (a 3-sample moving average before inference, followed by 3-consecutive-reading validation), an **independent deterministic standards compliance check** (IS 10500:2012), and a **4-feature `int8`-quantized TinyML MLP** (`4 → 16 → 8 → 3`: **NORMAL**, **RUNOFF**, **CHEMICAL**), matching Sharma et al. [1].

Only water quality uses a neural network. Leakage (`../leak_detection/`) and abnormal consumption (`../abnormal_consumption/`) use lightweight statistical and compression-rate methods.

| File | What it is |
|---|---|
| `train_quality.py` | Leakage-free 5-fold CV, training, `int8` quantization, and external evaluation (`uv run python tinyml/train_quality.py`, ~32 s) |
| `results.txt` | Metrics from the latest training & validation run |
| `../firmware/quality_model.py` | Pure-Python `int8` inference (`QualityModel`), `check_standards`, and two-stage `QualityMonitor` (`python3 firmware/quality_model.py`) |
| `../firmware/quality_model.json` | Trained 4-feature `int8` model (`1,435 bytes` JSON; `324 bytes` weights + biases) |
| `pre-quality-check/quality.py` | Earlier 4-feature synthetic prototype (kept for reference) |

## 1. Model & Filtering Design

- **4 Inputs (consistent with Paper [1]):** `Temperature (°C)`, `pH`, `log(1 + TDS_ppm)`, `log(1 + Turbidity_NTU)`.
- **Network:** `4 → 16 → 8 → 3` MLP, `216` `int8` weights + `27` `int32` biases = **324 bytes** (`~33 µs` per reading on host, `< 1 ms` on Raspberry Pi 4).
- **Standards Compliance vs. TinyML Classification:**
  - `check_standards()` independently checks whether readings violate **IS 10500:2012** (or German **TrinkwV** when evaluating GECCO 2019), providing an interpretable safety net even if ML misclassifies a borderline sample.
  - `QualityModel` classifies contamination patterns (`NORMAL`, `RUNOFF`, `CHEMICAL`).
- **Two-Stage Noise Filtering (`QualityMonitor` in `../firmware/quality_model.py`):**
  1. **Pre-inference smoothing:** 3-sample sliding-window moving average to reduce random sensor noise without hiding sudden contamination events.
  2. **Consecutive-reading validation:** Requires **3 consecutive** non-`NORMAL` / non-compliant intervals before raising an alert (`10` normal intervals to clear).

## 2. Training Data, Temperature Handling & Cross-Validation

- **Temperature decorrelation:** In raw Kenya data, `Safe` tap samples (~22 °C) are cooler than `Unsafe` surface sources (~26 °C). To prevent temperature from acting as a spurious class shortcut, `Temperature_C` is sampled from the **same overlapping distribution (`10–35 °C`)** across all three classes (`NORMAL`, `RUNOFF`, `CHEMICAL`), with no invented temperature-contamination relationship.
- **Leakage-free Stratified 5-Fold CV:** Original Kenya `Safe` source rows are split into 5 folds **first**, and synthetic mixtures (`RUNOFF` and `CHEMICAL`) are generated separately inside each fold so closely related mixtures never span train and validation.

### Datasets (stored in `tinyml/data/`, ignored by git)

| Dataset | Role | Licence |
|---|---|---|
| [Kenya water sources](https://data.mendeley.com/datasets/575ctg3nnr/2) (Ooko et al., 2026) | `Safe` rows for 5-fold CV & held-out FPR; `Unsafe` rows for TPR | CC BY 4.0 |
| [GECCO 2019](https://zenodo.org/records/4304080) (Thüringer Fernwasserversorgung) | External German utility validation (evaluated under both IS 10500 and German *TrinkwV*) | CC BY 4.0 |
| [CSU ROSS 2019](https://www.hydroshare.org/resource/521e317e189e498581d0aaa08c3f32bc/) (Poudre Water Quality Network) | External high-frequency river sensor validation (`-0.1–20.7 °C`, raw vs. QAQC'd) | Open / HydroShare |
| [Loddon Catchment](https://doi.org/10.5285/331659d7-da72-48a2-9b52-63c003557990) (UKCEH / EIDC) | External storm-window (`-6h / storm / +6h`) evaluation using `SONDETEMP`, `SONDEPH`, `SONDECOND`, `SONDETURBIDITY` | Open / EIDC |
| [Mendeley 2019 WSN](https://data.mendeley.com/datasets/pbb76sbbrg/1) (Sithole et al., 2019) | Diagnostic only (missing Temperature, uses `LDR kΩ` instead of calibrated NTU, Tables 7–10 omit Conductivity) | CC BY 4.0 |

## 3. Latest Results (`results.txt`)

- **Leakage-free 5-Fold CV (expanded overlapping mixtures):** `float32` `0.9848 ± 0.0012` | `int8` `0.9839 ± 0.0013` | `int8`/`float32` agreement `0.9956` | macro-F1 `0.9839` (`NORMAL` `0.9930`, `RUNOFF` `0.9804`, `CHEMICAL` `0.9784`).
- **`RUNOFF` vs. `CHEMICAL` Overlap Zone (`Turb 5–40 NTU`, `pH 5.5–8.5`, `N=1,779`):** **99.44%** 2-class accuracy (`RUNOFF`: `1,748/1,752`, `CHEMICAL`: `21/27`).
- **Held-out Kenya:** `Safe` (`N=6,048`): `FPR = 0.0000` (`0` flagged); `Unsafe` exceeding IS 10500 (`N=67,855`): `TPR = 0.9998`.
- **GECCO 2019 (`N=215,690` normal):** Under IS 10500 (`N=40,695` compliant): **`FPR = 0.0000`**. Under German *TrinkwV* (`N=215,688` compliant, `81.13%` with `pH > 8.5`): TinyML flags `26.38%` (trained on IS 10500 cutoffs).
- **CSU ROSS 2019 (`N=44,133`):** IS-10500-compliant river rows (`N=16,451`): `FPR = 0.0218`; over-IS (`N=27,682`): `TPR = 0.9758`.
- **Loddon Catchment Storm-Window Comparison:** Pre-storm (`N=76`, med `10.4 NTU`, `391 ppm`): `RUNOFF=0.605, CHEM=0.395`; During storm (`N=207`, med `9.4 NTU`, `396 ppm`): `RUNOFF=0.541, CHEM=0.449`; Post-storm (`N=54`, med `12.4 NTU`, `388 ppm`): `RUNOFF=0.648, CHEM=0.352` (demonstrating that raw river water exceeds drinking-water limits year-round and storm detection on rivers requires site-relative baselines).
- **Temperature stress test (`NORMAL` `N=2,560`):** `10 °C`: `0.0055`, `15 °C`: `0.0043`, `20 °C`: `0.0039`, `25 °C`: `0.0051`, `30 °C`: `0.0047`, `35 °C`: `0.0047`.
