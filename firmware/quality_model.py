"""Water-quality monitoring modules for the edge node (100% offline, pure Python).

Three distinct modules with separate responsibilities:
  1. Module 1 (check_standards): Authoritative deterministic regulatory compliance check
     (IS 10500:2012 or German TrinkwV). Never overridden by ML scores.
  2. Module 2 (TemporalModel): Primary ML detector — int8-quantized Temporal Autoencoder
     (8 -> 12 -> 4 -> 12 -> 8) operating on causal [15-min short delta, 12-h deviation]
     across all 4 sensors. Handles startup warm-up, missing readings, and timestamp gaps.
  3. Module 3 (QualityModel): Secondary contamination pattern classifier
     (4 -> 16 -> 8 -> 3 int8 MLP: NORMAL / RUNOFF / CHEMICAL).

QualityMonitor orchestrates all three modules with pre-inference moving-average smoothing
and consecutive-reading validation.
"""
from collections import deque
import json
import math
import os

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quality_model.json")
TEMPORAL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temporal_model.json")

STANDARDS = {
    "IS_10500": {"ph": (6.5, 8.5), "tds_max": 500.0, "turb_max": 5.0},
    "TRINKW_V": {"ph": (6.5, 9.5), "tds_max": 1625.0, "turb_max": 1.0},
}


def features(temp_c, ph, tds_ppm, turbidity_ntu):
    """4 features matching Paper [1]: temp_C, pH, log1p(TDS), log1p(turbidity)."""
    return [temp_c, ph, math.log1p(max(tds_ppm, 0.0)), math.log1p(max(turbidity_ntu, 0.0))]


def q8(v):
    return max(-127, min(127, round(v)))


def q8_sym(v):
    return max(-128, min(127, round(v)))


def _is_valid_reading(temp_c, ph, tds_ppm, turbidity_ntu):
    vals = (temp_c, ph, tds_ppm, turbidity_ntu)
    if any(v is None for v in vals):
        return False
    return all(math.isfinite(float(v)) for v in vals)


def check_standards(ph, tds_ppm, turbidity_ntu, standard="IS_10500"):
    """Module 1: Independent deterministic drinking-water standards check.

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
    """Module 3: Secondary 3-class contamination pattern classifier (4 -> 16 -> 8 -> 3)."""

    def __init__(self, path=PATH):
        with open(path) as f:
            d = json.load(f)
        self.classes = d["classes"]
        self.mean = d["mean"]
        self.std = d["std"]
        self.in_scale = d["in_scale"]
        self.layers = d["layers"]

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


class TemporalModel:
    """Module 2: Primary ML temporal anomaly detector (8 -> 12 -> 4 -> 12 -> 8 int8 AE).

    Maintains causal short-term and long-term EWMA state across consecutive readings.
    Suppresses alerts during startup warm-up, on missing readings, and across timestamp gaps.
    """

    def __init__(self, path=TEMPORAL_PATH):
        with open(path) as f:
            d = json.load(f)
        self.alpha_short = d["alpha_short"]
        self.alpha_long = d["alpha_long"]
        self.warmup_samples = d["warmup_samples"]
        self.max_gap_s = d["max_gap_s"]
        self.threshold = d["threshold"]
        self.confirm_n = d["confirm_n"]
        self.clear_n = d["clear_n"]
        self.mu = d["mu"]
        self.sd = d["sd"]
        self.layers = d["layers"]
        self.ewma_short = None
        self.ewma_long = None
        self.last_ts = None
        self.warmup_seen = 0

    def score_vector(self, z_norm):
        """Pure-Python int8 AE reconstruction MSE matching TemporalAE.mse_int8()."""
        a = list(z_norm)
        n_layers = len(self.layers)
        for idx, layer in enumerate(self.layers):
            w = layer["w"]
            b = layer["b"]
            scale = layer["sa"] * layer["sw"]
            aq = [q8_sym(v / layer["sa"]) for v in a]
            out = [sum(aq[i] * w[i][j] for i in range(len(aq))) * scale + b[j] for j in range(len(b))]
            a = [max(v, 0.0) for v in out] if idx < n_layers - 1 else out
        return sum((a[i] - z_norm[i]) ** 2 for i in range(len(z_norm))) / len(z_norm)

    def step(self, temp_c, ph, tds_ppm, turbidity_ntu, ts_s=None):
        """Update EWMA state with one reading and return (ready, score, is_anomaly)."""
        if not _is_valid_reading(temp_c, ph, tds_ppm, turbidity_ntu):
            return False, 0.0, False

        f = features(float(temp_c), float(ph), float(tds_ppm), float(turbidity_ntu))

        if self.ewma_short is None:
            self.ewma_short = list(f)
            self.ewma_long = list(f)
            self.warmup_seen = 1
            self.last_ts = ts_s
            return False, 0.0, False

        if ts_s is not None and self.last_ts is not None and (ts_s - self.last_ts) > self.max_gap_s:
            # Timestamp gap (e.g. after outage): re-anchor short-term EWMA & re-warm
            self.ewma_short = list(f)
            self.warmup_seen = 1
            self.last_ts = ts_s
            return False, 0.0, False

        self.last_ts = ts_s
        a_s, a_l = self.alpha_short, self.alpha_long
        self.ewma_short = [(1.0 - a_s) * s + a_s * v for s, v in zip(self.ewma_short, f)]
        self.ewma_long = [(1.0 - a_l) * l + a_l * v for l, v in zip(self.ewma_long, f)]
        self.warmup_seen += 1

        z_raw = [f[i] - self.ewma_short[i] for i in range(4)] + [f[i] - self.ewma_long[i] for i in range(4)]
        z_norm = [(z_raw[i] - self.mu[i]) / self.sd[i] for i in range(8)]
        score = self.score_vector(z_norm)
        if self.warmup_seen < self.warmup_samples:
            return False, score, False
        return True, score, score > self.threshold


class QualityMonitor:
    """Orchestrates Module 1 (Standards), Module 2 (Temporal AE), and Module 3 (Classifier)."""

    def __init__(self, model=None, temporal_model=None, window=3, confirm_n=3, clear_n=10, standard="IS_10500"):
        self.model = model or QualityModel()
        self.temporal = temporal_model
        self.buf = deque(maxlen=window)
        self.confirm_n = confirm_n
        self.clear_n = clear_n
        self.standard = standard
        self.bad_run = 0
        self.good_run = 0
        self.active_class = None
        self.t_bad_run = 0
        self.t_good_run = 0
        self.temporal_active = False

    def update(self, temp_c, ph, tds_ppm, turbidity_ntu, ts_s=None):
        """Process one reading.

        Returns (ml_class, compliant, violations, reg_event, temporal_event).
        """
        if not _is_valid_reading(temp_c, ph, tds_ppm, turbidity_ntu):
            return "NORMAL", True, [], None, None

        # Stage 1: short moving-average smoothing for Module 1 + Module 3
        self.buf.append((float(temp_c), float(ph), float(tds_ppm), float(turbidity_ntu)))
        n = len(self.buf)
        avg = tuple(sum(r[i] for r in self.buf) / n for i in range(4))

        # Module 1: Authoritative deterministic standards check (never overridden by ML)
        compliant, violations = check_standards(avg[1], avg[2], avg[3], self.standard)
        # Module 3: Secondary contamination classification
        ml_class = self.model.classify(*avg)

        is_bad = (not compliant) or (ml_class != "NORMAL")
        effective_class = ml_class if ml_class != "NORMAL" else ("LIMIT_VIOLATION" if not compliant else "NORMAL")

        reg_event = None
        if is_bad:
            self.bad_run += 1
            self.good_run = 0
            if self.bad_run >= self.confirm_n and self.active_class is None:
                self.active_class = effective_class
                reg_event = ("ALERT", effective_class, avg, violations)
        else:
            self.bad_run = 0
            self.good_run += 1
            if self.active_class is not None and self.good_run >= self.clear_n:
                self.active_class = None
                reg_event = ("CLEARED", "NORMAL", avg, violations)

        # Module 2: Primary ML temporal anomaly detection (independent of Module 1/3)
        temporal_event = None
        if self.temporal is not None:
            ready, score, t_anom = self.temporal.step(temp_c, ph, tds_ppm, turbidity_ntu, ts_s=ts_s)
            if ready and t_anom:
                self.t_bad_run += 1
                self.t_good_run = 0
                if self.t_bad_run >= self.temporal.confirm_n and not self.temporal_active:
                    self.temporal_active = True
                    temporal_event = ("TEMPORAL_ALERT", score, (temp_c, ph, tds_ppm, turbidity_ntu))
            elif ready:
                self.t_bad_run = 0
                self.t_good_run += 1
                if self.temporal_active and self.t_good_run >= self.temporal.clear_n:
                    self.temporal_active = False
                    temporal_event = ("TEMPORAL_CLEARED", score, (temp_c, ph, tds_ppm, turbidity_ntu))

        return ml_class, compliant, violations, reg_event, temporal_event


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

    # Edge-case checks for TemporalModel + QualityMonitor
    tmod = TemporalModel()
    # 1. Warm-up suppression (< warmup_samples): never alerts during warm-up
    for i in range(tmod.warmup_samples - 1):
        ready, _, anom = tmod.step(10.0, 9.0, 5000.0, 50.0, ts_s=i * 60)
        assert not ready and not anom, "Must not alert during startup warm-up"

    # 2. Missing readings: skipped cleanly without raising or corrupting state
    ready, _, anom = tmod.step(None, float("nan"), 140.0, 0.2, ts_s=1000)
    assert not ready and not anom

    # 3. Timestamp gap (> max_gap_s): resets short-term EWMA & re-warms
    ready, _, anom = tmod.step(20.0, 7.5, 200.0, 1.0, ts_s=1000 + tmod.max_gap_s + 60)
    assert not ready and not anom, "Must re-warm after large timestamp gap"

    # 4. Standards checker never overridden by temporal model
    mon = QualityMonitor(model=model, temporal_model=TemporalModel())
    for i in range(3):
        _, comp, viol, reg_ev, _ = mon.update(25.0, 9.1, 200.0, 0.5, ts_s=i * 60)
        assert not comp and "pH" in viol
    assert reg_ev is not None and reg_ev[0] == "ALERT"

    print("quality model self-check passed")
