| Measure (fast replay, ms) | Samples | Median | 95th percentile | Max |
|---|---|---|---|---|
| B-TANK: one sensor reading, detection only | 1440 | 0.106 | 0.165 | 0.380 |
| B-TANK: whole step (detect, save, send) | 1440 | 0.151 | 0.392 | 0.917 |
| B-101: one sensor reading, detection only | 1440 | 0.008 | 0.046 | 0.251 |
| B-101: whole step (detect, save, send) | 1440 | 0.060 | 0.412 | 1.017 |
| B-203: one sensor reading, detection only | 1440 | 0.007 | 0.033 | 0.234 |
| B-203: whole step (detect, save, send) | 1440 | 0.054 | 0.405 | 1.016 |
| B-301: one sensor reading, detection only | 1440 | 0.008 | 0.046 | 0.242 |
| B-301: whole step (detect, save, send) | 1440 | 0.059 | 0.411 | 0.974 |
| gateway: per received packet (incl. saving state) | 187 | 0.200 | 0.572 | 2.879 |
| gateway CPU only, per received packet | 187 | 0.190 | 0.554 | 1.102 |
| node message queued to accepted by gateway | 396 | 9.381 | 19.659 | 21.267 |
| dashboard ingest request (HTTP round trip) | 16 | 3.871 | 8.631 | 9.670 |
| snapshot taken to stored in the dashboard | 16 | 3.653 | 6.054 | 7.873 |
