"""Parity & integration test: standalone Experiment 2 vs integrated firmware TemporalModel."""
import os
import sys
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path[:0] = [os.path.join(ROOT, "firmware", "edge_node"), os.path.join(ROOT, "firmware", "communication")]

import exp2_temporal_anomaly as E2
from quality_model import TemporalModel, QualityMonitor
from packet import pack, unpack, sim_key


def main():
    _, gx, gev, _ = E2.load_gecco()
    mid = len(gx) // 2
    norm_1 = ~gev[:mid]
    zg = E2.feats(gx)
    ew_s = E2.ewma_series(zg, 1 / 15)
    ew_l = E2.ewma_series(zg, 1 / 720)
    z_temp = np.hstack([zg - ew_s, zg - ew_l])

    z_t1 = z_temp[:mid][norm_1]
    c_idx = int(0.8 * len(z_t1))
    mu, sd = z_t1[:c_idx].mean(0), np.maximum(z_t1[:c_idx].std(0), 1e-6)
    z_trn = (z_t1[:c_idx] - mu) / sd

    tae = E2.TemporalAE((8, 12, 4, 12, 8))
    tae.fit(z_trn, epochs=60, lr=0.01)
    tae.quantize_int8(z_trn)

    # Compare on first 3,000 samples from index 0 (identical EWMA initial state z[0])
    n_check = 3000
    stand_scores = tae.mse_int8(((z_temp[:n_check] - mu) / sd))

    fw_model = TemporalModel()
    fw_scores = []
    for i in range(n_check):
        _, sc, _ = fw_model.step(*gx[i], ts_s=i * 60)
        fw_scores.append(sc)
    fw_scores = np.array(fw_scores)

    # Skip index 0 (initialization step returns 0.0 before first delta)
    max_diff = float(np.max(np.abs(stand_scores[1:] - fw_scores[1:])))
    print(f"Parity check over {n_check} GECCO readings: max |standalone - firmware| = {max_diff:.2e}")
    assert max_diff < 1e-6, f"Mismatch: {max_diff}"

    # Verify packet serialization for QUALITY_TEMPORAL_ANOMALY & QUALITY_TEMPORAL_CLEARED
    k = sim_key("B-TANK")
    m1 = {"k": "ALERT", "n": "B-TANK", "s": 10, "t": 1200, "a": "QUALITY_TEMPORAL_ANOMALY", "score": 14.25}
    m2 = {"k": "ALERT", "n": "B-TANK", "s": 11, "t": 1800, "a": "QUALITY_TEMPORAL_CLEARED"}
    assert unpack(pack(m1, k), lambda _: k) == m1
    assert unpack(pack(m2, k), lambda _: k) == m2
    print("Temporal packet & parity checks passed")


if __name__ == "__main__":
    main()
