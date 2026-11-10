# Aqua Edge: TinyML (water quality)

The water-quality model for the tank node: it reads temperature, pH, TDS and turbidity and decides whether the water is normal or not. The idea comes from Sharma et al. [1, for the small 8-bit neural network that classifies water quality on the device].

Only water quality uses a model. Leaks and abnormal usage use simple statistics instead (see `../leak_detection/`), because they need no training data, use almost no memory, and are easy to explain.

## Current state

- The tank node in `../firmware/aqua_edge.py` uses **threshold rules** as a stand-in (`classify_quality`): TDS > 600 or pH > 8.5 is chemical; TDS < 120 and pH < 6.8 is runoff.
- `pre-quality-check/quality.py` is an early re-implementation of the paper's pipeline (4 inputs, two small hidden layers, 3 classes, 8-bit quantization), trained on **made-up** data, because the paper's dataset is not public.

## Datasets found

| Dataset | What is in it | Fit |
|---|---|---|
| [GECCO 2019 drinking-water challenge](https://zenodo.org/records/4304080) | Real readings from a German water utility, one per minute, 132,480 rows. Columns: temperature, pH, conductivity, turbidity (plus SAC and PFM) and an **Event** label (212 event minutes). CC BY 4.0. | **Best fit.** Real data with labelled events. Conductivity converts to TDS by roughly x0.5 to x0.7. |
| [Kenya water sources](https://data.mendeley.com/datasets/575ctg3nnr/1) | 100,800 readings from an IoT rig, every 2 minutes: pH, TDS, turbidity, temperature. Binary safe/unsafe label. CC BY 4.0. | Same four sensors as ours, but the label is set by **where the water came from** (tap vs river), not by what was measured. |
| [UT Austin distribution system](https://data.mendeley.com/datasets/wwy39n8p7z/1) | Temperature, conductivity, turbidity, pH, ORP, chlorine and more, every 5 minutes. | No labels. Useful for learning what normal looks like. |
| Kaggle "water potability" | 3,276 lab samples, potable yes/no. | **Skip:** no temperature, many missing pH values, lab tests instead of sensors like ours. |

None of these has our three classes (normal / runoff / chemical). They only say "normal vs event".

## Open decision

Should the model be:

- **normal / anomaly**, which matches the GECCO data directly, or
- **normal / runoff / chemical**, which needs us to make the extra labels ourselves (lab tests, or labelling by rules)?

## Data in this folder

Downloaded datasets go in `tinyml/data/`, which is not stored in git (large files, licensed separately).
