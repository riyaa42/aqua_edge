"""Water-quality model and two-stage monitor for the edge node.

Implements:
  1. QualityModel: 4-input (temperature_C, pH, TDS_ppm, turbidity_NTU) int8-quantized
     MLP (4 -> 16 -> 8 -> 3: NORMAL / RUNOFF / CHEMICAL), matching Sharma et al. [1].
     Trained, quantized, and evaluated by tinyml/train_quality.py.
  2. check_standards: independent deterministic compliance check (IS 10500:2012 or
     German TrinkwV) acting as an interpretable safety layer alongside TinyML.
  3. QualityMonitor: two-stage noise filtering (short moving-average smoothing before
     inference) + consecutive-reading validation (3 bad readings to alert, 10 to clear).
"""
from collections import deque
import json
import math
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_model.json")

STANDARDS = {
    "IS_10500": {"ph": (6.5, 8.5), "tds_max": 500.0, "turb_max": 5.0},
    "TRINKW_V": {"ph": (6.5, 9.5), "tds_max": 1625.0, "turb_max": 1.0},
}


def features(temp_c, ph, tds_ppm, turbidity_ntu):
    """4 features matching Paper [1]: temp_C, pH, log1p(TDS), log1p(turbidity)."""
    return [temp_c, ph, math.log1p(max(tds_ppm, 0.0)), math.log1p(max(turbidity_ntu, 0.0))]


def q8(v):
    return max(-127, min(127, round(v)))


def check_standards(ph, tds_ppm, turbidity_ntu, standard="IS_10500"):
    """Independent deterministic drinking-water standards check.

    Returns (is_compliant: bool, violations: list[str]).
    """
    lim = STANDARDS[standard]
    violations = []
    if ph < lim["ph"][0] or ph > lim["ph"][1]:
        violations.append("pH")
    if tds_ppm > lim["tds_max"]:
        violations.append("TDS")
    if turbidity_ntu > lim["turb_max"]:
        violations.append("TURBIDITY")
    return len(violations) == 0, violations


class QualityModel:
    def __init__(self, path=PATH):
        with open(path) as f:
            d = json.load(f)
        self.classes = d["classes"]
        self.mean = d["mean"]
        self.std = d["std"]
        self.in_scale = d["in_scale"]
        self.layers = d["layers"]  # each: w[in][out] int8, b[out] int32, m = multiplier

    def classify(self, temp_c, ph, tds_ppm, turbidity_ntu):
        x = [
            q8((v - m) / s / self.in_scale)
            for v, m, s in zip(features(temp_c, ph, tds_ppm, turbidity_ntu), self.mean, self.std)
        ]
        for layer in self.layers:
            w = layer["w"]
            acc = [b + sum(xi * w[i][j] for i, xi in enumerate(x)) for j, b in enumerate(layer["b"])]
            if layer["m"] is None:
                return self.classes[acc.index(max(acc))]
            x = [q8(max(a, 0) * layer["m"]) for a in acc]


class QualityMonitor:
    """Two-stage edge water-quality processor:
    Stage 1: Moving-average smoothing over a short window to filter random sensor noise.
    Stage 2: TinyML classification + independent standards check, requiring
             `confirm_n` consecutive bad intervals before raising an alert.
    """

    def __init__(self, model, window=3, confirm_n=3, clear_n=10, standard="IS_10500"):
        self.model = model
        self.buf = deque(maxlen=window)
        self.confirm_n = confirm_n
        self.clear_n = clear_n
        self.standard = standard
        self.bad_run = 0
        self.good_run = 0
        self.active_class = None

    def update(self, temp_c, ph, tds_ppm, turbidity_ntu):
        self.buf.append((temp_c, ph, tds_ppm, turbidity_ntu))
        n = len(self.buf)
        avg = tuple(sum(r[i] for r in self.buf) / n for i in range(4))
        ml_class = self.model.classify(*avg)
        compliant, violations = check_standards(avg[1], avg[2], avg[3], self.standard)

        # Event is unsafe if TinyML flags contamination OR standards check fails
        is_bad = (ml_class != "NORMAL") or (not compliant)
        effective_class = ml_class if ml_class != "NORMAL" else ("LIMIT_VIOLATION" if not compliant else "NORMAL")

        event = None
        if is_bad:
            self.bad_run += 1
            self.good_run = 0
            if self.bad_run >= self.confirm_n and self.active_class is None:
                self.active_class = effective_class
                event = ("ALERT", effective_class, avg, violations)
        else:
            self.bad_run = 0
            self.good_run += 1
            if self.active_class is not None and self.good_run >= self.clear_n:
                self.active_class = None
                event = ("CLEARED", "NORMAL", avg, violations)
        return ml_class, compliant, violations, event


if __name__ == "__main__":
    model = QualityModel()
    cases = [
        ((25.0, 7.2, 250, 0.5), "NORMAL"),
        ((30.0, 7.4, 120, 1.5), "NORMAL"),
        ((24.0, 6.3, 60, 25), "RUNOFF"),
        ((26.0, 6.9, 90, 40), "RUNOFF"),
        ((25.0, 9.2, 900, 0.6), "CHEMICAL"),
        ((25.0, 7.6, 1500, 0.8), "CHEMICAL"),
    ]
    for (t, ph, tds, turb), want in cases:
        got = model.classify(t, ph, tds, turb)
        assert got == want, (t, ph, tds, turb, got, want)
    print("quality model self-check passed")
