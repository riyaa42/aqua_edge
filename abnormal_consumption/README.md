# Abnormal Water Consumption Detection

Offline-first detection and evaluation of abnormal household water-consumption patterns (Slide 2 & Paper [3] Babazadeh, 2019), kept separate from pipeline leakage (`../leak_detection/`).

## Scope
- **Sudden consumption spikes:** Flow significantly exceeding the node's learned hourly baseline.
- **Unexpected night-time bursts:** Short/high-volume deviations during quiet hours that differ from steady leaks.
- **Prolonged zero-flow periods:** Extended zero-flow intervals (e.g., meter tampering, blocked pipe, or supply/shutoff anomalies), as specified on Slide 2.
- **Lempel–Ziv compression-rate anomaly detection (Paper [3]):** On-node edge analytics flagging anomalies when the Lempel–Ziv compression rate of quantized flow time-series windows shifts.

## Planned Contents
- Real residential consumption dataset preprocessing (e.g., Cache County bursty household demand).
- Standalone Precision, Recall, F1, and detection-latency evaluation scripts (separate from leak scoring).
