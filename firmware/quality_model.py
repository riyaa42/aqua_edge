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

        gap = ts_s is not None and self.last_ts is not None and (ts_s - self.last_ts) > self.max_gap_s
        if self.ewma_short is None or gap:
            # First reading, or first after an outage: drop all history and warm up again
            self.ewma_short = list(f)
            self.ewma_long = list(f)
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
        # [bad_run, good_run, active label]: limits and classifier confirm and clear separately,
        # so an ML class can never replace or hide a standards violation
        self.limit_state = [0, 0, None]
        self.class_state = [0, 0, None]
        self.t_bad_run = 0
        self.t_good_run = 0
        self.temporal_active = False

    @property
    def limit_active(self):
        return self.limit_state[2] is not None

    @property
    def active_class(self):
        return self.class_state[2]

    def _confirm(self, state, label):
        """Advance one alert track with this reading's label (None = fine).
        Returns ("ALERT" or "CLEARED", label), or None."""
        if label is not None:
            state[0] += 1
            state[1] = 0
            if state[0] >= self.confirm_n and state[2] is None:
                state[2] = label
                return "ALERT", label
        else:
            state[0] = 0
            state[1] += 1
            if state[2] is not None and state[1] >= self.clear_n:
                cleared, state[2] = state[2], None
                return "CLEARED", cleared
        return None

    def update(self, temp_c, ph, tds_ppm, turbidity_ntu, ts_s=None):
        """Process one reading.

        Returns (ml_class, compliant, violations, limit_event, class_event, temporal_event).
        Without pH, TDS or turbidity nothing runs (compliant is None: unknown). Without
        temperature only the standards check runs (ml_class is None); both ML modules skip.
        """
        values = (ph, tds_ppm, turbidity_ntu)
        if any(v is None or not math.isfinite(float(v)) for v in values):
            return None, None, [], None, None, None
        has_temp = _is_valid_reading(temp_c, *values)

        # Stage 1: short moving-average smoothing for Module 1 + Module 3
        self.buf.append((float(temp_c) if has_temp else None, *map(float, values)))
        n = len(self.buf)
        temps = [r[0] for r in self.buf if r[0] is not None]
        avg = (sum(temps) / len(temps) if has_temp else None,
               *(sum(r[i] for r in self.buf) / n for i in range(1, 4)))

        # Module 1: Authoritative deterministic standards check (never overridden by ML)
        compliant, violations = check_standards(avg[1], avg[2], avg[3], self.standard)
        ev = self._confirm(self.limit_state, None if compliant else "LIMIT_VIOLATION")
        limit_event = ev and (*ev, avg, violations)

        # Module 3: Secondary contamination classification (needs a real temperature)
        ml_class, class_event = None, None
        if has_temp:
            ml_class = self.model.classify(*avg)
            ev = self._confirm(self.class_state, None if ml_class == "NORMAL" else ml_class)
            class_event = ev and (*ev, avg, violations)

        # Module 2: Primary ML temporal anomaly detection (independent of Module 1/3)
        temporal_event = None
        if self.temporal is not None:
            ready, score, t_anom = self.temporal.step(temp_c, ph, tds_ppm, turbidity_ntu, ts_s=ts_s)
            if self.temporal.warmup_seen == 1:     # (re)initialised: old runs don't carry over
                self.t_bad_run = self.t_good_run = 0
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

        return ml_class, compliant, violations, limit_event, class_event, temporal_event


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

    # 3. Timestamp gap (> max_gap_s): drops all EWMA history & re-warms
    ready, _, anom = tmod.step(20.0, 7.5, 200.0, 1.0, ts_s=1000 + tmod.max_gap_s + 60)
    assert not ready and not anom, "Must re-warm after large timestamp gap"
    assert tmod.ewma_short == tmod.ewma_long == features(20.0, 7.5, 200.0, 1.0), "no history across a gap"

    # 4. Standards checker never overridden by temporal model
    mon = QualityMonitor(model=model, temporal_model=TemporalModel())
    for i in range(3):
        ml, comp, viol, limit_ev, class_ev, _ = mon.update(25.0, 9.1, 200.0, 0.5, ts_s=i * 60)
        assert not comp and "pH" in viol
    assert ml == "CHEMICAL" and class_ev[:2] == ("ALERT", "CHEMICAL")
    assert limit_ev[:2] == ("ALERT", "LIMIT_VIOLATION") and limit_ev[3] == ["pH"], "ML must not hide the violation"

    # 5. Missing temperature: standards still checked, both ML modules skipped (no stand-in value)
    mon = QualityMonitor(model=model, temporal_model=TemporalModel())
    for i, temp in enumerate((None, float("nan"), None)):
        ml, comp, _, limit_ev, class_ev, t_ev = mon.update(temp, 9.1, 200.0, 0.5, ts_s=i * 60)
        assert ml is None and class_ev is None and t_ev is None and not comp
    assert limit_ev[0] == "ALERT" and mon.temporal.ewma_short is None

    print("quality model self-check passed")
