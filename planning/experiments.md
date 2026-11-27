# Experiments & Evaluation

Controlled evaluation scripts comparing JalRakshak (`edge/`, `gateway/`) against [the baselines](baselines.md) across the 6 Slide 10 metrics:
1. Detection Accuracy (Precision, Recall, F1 per task)
2. Detection Latency
3. Alert Latency (online vs. controlled outage durations: 0 min to 24 h)
4. Data Loss & Sync Recovery %
5. Maintenance-Priority Accuracy (Top-1 / Top-k location ranking)
6. Edge Resource & Communication Footprint on Raspberry Pi 3B (RAM, Flash, CPU time, LoRa airtime)
