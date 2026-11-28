# Aqua Edge: TinyML (water quality)

**Since 2026-11-28 the safety rule is a plain limit check, and this model only guesses the cause.** Once a minute the tank node averages its last 3 readings and checks them against the drinking water limits of IS 10500: pH 6.5 to 8.5, TDS up to 500 ppm, turbidity up to 5 NTU (`check_standards` in `../firmware/quality_model.py`, from the team's communication branch). Three bad averages in a row raise `QUALITY_LIMIT_VIOLATION` and shut the tank valve. The limits need no training and a model result can never hide a broken limit. The model below runs alongside as a separate track to say what the bad water looks like, so the caretaker knows where to look (rain getting in, or a chemical). Runoff and chemical also shut the valve.

The water-quality model for the tank node. Once a minute it reads pH, TDS (total dissolved solids, in ppm) and turbidity (cloudiness, in NTU) and decides: **NORMAL**, **RUNOFF** (rain or surface water got in) or **CHEMICAL** (a cleaning chemical got in). Three bad readings in a row raise `QUALITY_RUNOFF` or `QUALITY_CHEMICAL`. The idea comes from Sharma et al. [1, for the small 8-bit neural network that classifies water quality on the device].

Only water quality uses a model. Leaks and abnormal usage use simple statistics (see `../leak_detection/`), because they need no training data and are easy to explain.

**Status: done (2026-11-23).** Trained, quantized to 8 bits, tested on real data, running in the firmware and in every simulation.

| File | What it is |
|---|---|
| `train_quality.py` | Builds the data, trains, quantizes, tests, writes the model. `simulation/.venv/bin/python tinyml/train_quality.py` (about 10 s; needs the datasets in `data/`) |
| `results.txt` | Test results of the last training run |
| `../firmware/quality_model.py` | The code the node runs: integer-only network, plain Python. Self-check: `python3 firmware/quality_model.py` |
| `../firmware/quality_model.json` | The trained model (1.3 KB) |
| `pre-quality-check/quality.py` | A teammate's first version on made-up data (kept for history; replaced by the files above) |

## 1. The model

- **Network:** 3 inputs, two hidden layers of 16 and 8, 3 outputs (a multilayer perceptron, the same shape as Sharma et al. [1]).
- **Inputs:** pH, log(1 + TDS), log(1 + turbidity). TDS and turbidity range over 1000x, so the logarithm helps a small network.
- **8-bit (int8) numbers:** after training, every weight is rounded to a whole number from -127 to 127, with one scale per layer. The node only does whole-number multiplications and sums, the same scheme as TensorFlow Lite Micro. 200 weights + 27 biases = **308 bytes**.
- **Speed:** 24 microseconds per reading on the laptop, about 0.4 ms on a Pi Zero 2 W (assumed 15x slower). The node reads once a minute, so this is nothing.
- **Why plain Python, not TensorFlow Lite:** the firmware uses only the Python standard library. The Pi Zero 2 W has full Linux, so it is not limited to microcontroller tools, but a 308-byte model does not need a 100 MB library. The integer math is the same.
- **Temperature is not an input.** Sharma et al. [1] use it, but no labelled data links temperature to contamination. In the Kenya data, the unsafe sources are simply warmer (26 vs 22 °C), so a model with temperature would learn "warm = unsafe", which is wrong. Temperature still matters for the TDS probe, which corrects its own reading for temperature. The tank node checks temperature on its own with a plain limit instead: above 35 °C gives `QUALITY_HOT` (Legionella bacteria grow fastest near 37 °C).

## 2. The data

No public dataset has runoff and chemical labels (the dataset of Sharma et al. [1] is not public). So:

| Part | Where it comes from |
|---|---|
| **Normal water** | Real readings: the "Safe" rows (treated tap and bottled water) of the Kenya dataset. 70% for training, 30% kept apart for testing. TDS is stretched x0.8 to x2.5, because Indian supplies run 100 to 400 ppm and Kenyan tap water 30 to 210. |
| **Runoff samples** | Real tap water mixed with rainwater runoff: pH 5.6 to 7.0 (rain is about 5.6), TDS 10 to 150 ppm, turbidity 10 to 300 NTU; 0.3% to 90% runoff. |
| **Chemical samples** | Real tap water mixed with an alkaline cleaner (diluted bleach or caustic soda): pH 10.5 to 12.5, TDS 2,000 to 60,000 ppm; 0.01% to 5% cleaner. |
| **Labels** | The Indian drinking-water standard **IS 10500:2012**: a mixture is unsafe if pH is outside 6.5 to 8.5, TDS is above 500 ppm (acceptable limit) or turbidity is above 5 NTU (permissible limit; Kenyan tap water already reaches 4.5 NTU). An unsafe mixture gets its contaminant's class; a mixture still inside every limit is NORMAL. |

- TDS and turbidity mix in proportion. pH mixes through its acid and base content (H+ minus OH-), solved again for the mixture.
- Simplification: tap water's own buffering is ignored, so a small chemical dose moves pH a bit more than in reality. Labels come from the resulting reading, so they stay consistent.
- Sensor noise is added: pH ±0.05, TDS ±3%, turbidity ±0.2 NTU.
- 90,000 training samples (balanced to about 43,000), 30,000 test samples (about 12,800 after balancing).

### Datasets

| Dataset | Used for | Licence |
|---|---|---|
| [Kenya water sources](https://data.mendeley.com/datasets/575ctg3nnr/2), Ooko et al., Kabarak University, 2026 [for real normal water and real unsafe water to test against]: 100,800 readings of pH, TDS, turbidity and temperature from an IoT rig, Safe/Unsafe by source. | Training (safe rows) and tests 2 | CC BY 4.0 |
| [GECCO 2019 drinking-water challenge](https://zenodo.org/records/4304080), Thüringer Fernwasserversorgung [for a different site's normal water and its labelled events]: 218,880 one-minute readings from a German utility. Conductivity (S/m) is converted to TDS with x10,000 x 0.65; turbidity in FNU is treated as NTU. | Test 3 | CC BY 4.0 |
| [UT Austin distribution system](https://data.mendeley.com/datasets/wwy39n8p7z/1) | Not used: no labels, and no TDS | |
| Kaggle "water potability" | Not used: lab samples with many missing values, not sensor readings | |

Download into `tinyml/data/` (not stored in git: large files, licensed separately):

```sh
mkdir -p tinyml/data/kenya tinyml/data/gecco
curl -L -o tinyml/data/kenya/water_quality_dataset1.csv https://data.mendeley.com/public-files/datasets/575ctg3nnr/files/abdd84be-97a2-4275-9f24-c22330542176/file_downloaded
curl -L -o tinyml/data/gecco/5_gecco2019_all_water_quality.csv https://zenodo.org/api/records/4304080/files/5_gecco2019_all_water_quality.csv/content
```

## 3. Results

From `results.txt` (the script fails if a pass mark is missed):

| Test | Result | Pass mark |
|---|---|---|
| 1. Held-out mixtures, built on tap water the model never saw | **99.45%** correct (8-bit); the full-precision model 99.82%; the two agree on 99.56% | 97%, agree 99% |
| 2a. Real Kenya safe water, held out (9,072 readings) | **0 false alarms** | at most 1% |
| 2b. Real Kenya unsafe water (70,560 readings: rivers, lakes, boreholes, sea) | Flagged 96.0%. Of the 67,855 readings that break an IS 10500 limit, **99.6%** flagged. Mostly called RUNOFF (turbid surface water), high-TDS water called CHEMICAL. | 95% of those over a limit |
| 3. GECCO, a German utility | Of its normal readings that are inside the Indian limits, **100%** called NORMAL. But its pH is above 8.5 in 81% of normal readings, so most are flagged: correct under IS 10500, not under German rules. | Reported only |
| 4. Sensor drift (held-out mixtures) | pH +0.2: 97.9%, pH -0.2: 95.5%, TDS ±10%: 99.5%, turbidity +1 NTU: 99.1% | Reported only |
| 5. The simulation's quality profiles | Normal, runoff and chemical: **100%** each | 100% |
| End to end | `simulation/check_all.sh`: runoff and chemical events caught in 2 minutes, no false quality alerts | All checks pass |

**Confusion (rows = truth, columns = model, test 1):** NORMAL 4265 / 0 / 5; RUNOFF 46 / 4224 / 0; CHEMICAL 19 / 0 / 4251. The few misses sit right at a limit (for example, a runoff mixture at turbidity 5.1 NTU).

## 4. Limits and what to watch

- **The thresholds come from a standard, not from lab-tested contamination.** The model learns the IS 10500 limits plus the pattern that tells runoff from chemical. Real contamination tests (tap water spiked with known amounts of runoff or cleaner) would be the next step.
- **Different water, different normal.** The GECCO water is fine in Germany but above the Indian pH limit. The model follows IS 10500, so it would raise alerts there. For Indian municipal supply this is the behaviour we want.
- **GECCO's labelled events cannot be scored fairly.** 29 of 36 are flagged, but mostly because of the site's pH, not because of the events. Those events are small changes that a "normal vs unusual for this tank" check would catch better. Possible later addition: the tank also learns its own normal pH/TDS/turbidity (like the leak baseline) and flags big jumps.
- **pH drift hurts most** (95.5% at -0.2). Cheap pH probes drift, so they need regular calibration (improvement plan section 6).
- **Acid chemicals (for example toilet cleaner) are not in the training data.** They would show as low pH with normal turbidity, which the model may call RUNOFF. Either way, an alert is raised.

## 5. Papers and sources

- [1] Sharma et al., Scientific Reports 2026 [for the 4-input, two-hidden-layer, 3-class 8-bit network on the device; our shape and classes]
- Ooko et al., IWA Water Practice and Technology 2026 [for the Kenya dataset and evidence that a quantized network runs on a microcontroller at about 92.6% accuracy]
- Bureau of Indian Standards, IS 10500:2012 (Amendment 2, 2018) [for the pH, TDS and turbidity limits used as labels]
- Missouri DNR and US EPA stormwater screening guides [for rainwater pH about 5.6 and turbidity as a runoff marker]
