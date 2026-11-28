"""Water-quality model for the tank node: a tiny neural network with 8-bit (int8) numbers.

Inputs pH, TDS (ppm) and turbidity (NTU); classes NORMAL / RUNOFF / CHEMICAL (Sharma et al. [1],
for the small 8-bit network that classifies water on the device). Trained, quantized and tested
by tinyml/train_quality.py, which writes quality_model.json next to this file.

The plain IS 10500 limit check (check_standards) is the main safety rule; the network only says
what the bad water looks like (runoff or chemical).

Integer-only like TensorFlow Lite Micro: int8 inputs and weights, int32 sums, one multiplier per
layer to bring the sum back to int8. Plain Python, no libraries: about 200 multiplications.
"""
import json
import math
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_model.json")


# IS 10500:2012 drinking-water limits (pH range, TDS acceptable limit, turbidity permissible limit)
IS_10500 = {"ph": (6.5, 8.5), "tds_max": 500.0, "turb_max": 5.0}


def check_standards(ph, tds_ppm, turbidity_ntu, lim=IS_10500):
    """The main safety rule: which IS 10500 limits a reading breaks, [] if none. A plain if/else,
    never overridden by the network (the team's communication branch, detection/quality)."""
    broken = []
    if not lim["ph"][0] <= ph <= lim["ph"][1]:
        broken.append("pH")
    if tds_ppm > lim["tds_max"]:
        broken.append("TDS")
    if turbidity_ntu > lim["turb_max"]:
        broken.append("TURBIDITY")
    return broken


def features(ph, tds_ppm, turbidity_ntu):
    """TDS and turbidity span 1000x, so the network sees their logarithm."""
    return [ph, math.log1p(max(tds_ppm, 0.0)), math.log1p(max(turbidity_ntu, 0.0))]


def q8(v):
    return max(-127, min(127, round(v)))


class QualityModel:
    def __init__(self, path=PATH):
        with open(path) as f:
            d = json.load(f)
        self.classes, self.mean, self.std, self.in_scale = d["classes"], d["mean"], d["std"], d["in_scale"]
        self.layers = d["layers"]          # each: w[in][out] int8, b[out] int32, m = multiplier (None: last)

    def classify(self, ph, tds_ppm, turbidity_ntu):
        x = [q8((v - m) / s / self.in_scale)
             for v, m, s in zip(features(ph, tds_ppm, turbidity_ntu), self.mean, self.std)]
        for layer in self.layers:
            w = layer["w"]
            acc = [b + sum(xi * w[i][j] for i, xi in enumerate(x)) for j, b in enumerate(layer["b"])]
            if layer["m"] is None:
                return self.classes[acc.index(max(acc))]
            x = [q8(max(a, 0) * layer["m"]) for a in acc]       # ReLU, back to int8


if __name__ == "__main__":
    model = QualityModel()
    cases = [((7.2, 250, 0.5), "NORMAL"), ((7.4, 120, 1.5), "NORMAL"),
             ((6.3, 60, 25), "RUNOFF"), ((6.9, 90, 40), "RUNOFF"),
             ((9.2, 900, 0.6), "CHEMICAL"), ((7.6, 1500, 0.8), "CHEMICAL")]
    for (ph, tds, turb), want in cases:
        got = model.classify(ph, tds, turb)
        assert got == want, (ph, tds, turb, got, want)
    assert check_standards(7.2, 250, 0.5) == [] and check_standards(8.5, 500, 5.0) == []
    assert check_standards(8.6, 900, 12) == ["pH", "TDS", "TURBIDITY"] and check_standards(6.4, 100, 1) == ["pH"]
    print("quality model self-check passed")
