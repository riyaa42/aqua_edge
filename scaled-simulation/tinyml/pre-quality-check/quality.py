"""
TinyML water-quality classifier -- simplified re-implementation of the pipeline in
Sharma et al., "An Intelligent, Low-Cost Water Quality Monitoring System with
On-Device Machine Learning and Cloud Integration", Scientific Reports 16, 11106 (2026).

What the paper describes (and this script mirrors):
  * Inputs : 4 features  X = [temperature, pH, TDS, turbidity]
  * Classes: Normal / Rainwater Runoff / Chemical   (2,000 samples each = 6,000)
  * Model  : MLP -> 4 inputs, two dense ReLU hidden layers, softmax(3) output
  * Eval   : Stratified 5-fold CV, 50 epochs
  * Deploy : post-training 8-bit quantization (TFLite Micro on ESP32, ~14.5 KB)
  * Control: pump relay cut-off when a reading leaves its safe range (with hysteresis)

Assumptions (the paper's dataset and Table 2 hyperparameters are not public):
  * Hidden sizes 16 and 8, Adam optimiser, lr 0.01 -- my choice.
  * Data is SYNTHETIC, generated from my guess at each class's sensor profile.
  * Quantization is simulated in NumPy (int8 weights/activations, int32 accumulators).
    For a real ESP32 build, train the same architecture in Keras and convert with
    tf.lite.TFLiteConverter (optimizations=[DEFAULT], representative dataset).
"""

import numpy as np

CLASSES = ["Normal", "Rainwater Runoff", "Chemical"]
FEATURES = ["temperature_C", "pH", "TDS_ppm", "turbidity_pct"]
rng = np.random.default_rng(42)


# ----------------------------------------------------------------------------
# 1. Sensor conversion formulas (Eq. 1-4 in the paper), raw ADC -> physical units
# ----------------------------------------------------------------------------
def adc_to_ph(adc, v7_adc=365, vref=4.95):
    return 7 - (1000 * (adc - v7_adc) * vref) / (59.16 * 1023)


def adc_to_tds_ppm(adc, vref=3.3):
    v = adc * vref / 1024
    return (133.42 * v**3 - 255.86 * v**2 + 857.39 * v) * 0.5


def adc_to_turbidity_pct(adc):
    return adc / 1023 * 100  # 100 % = clearest water


# ----------------------------------------------------------------------------
# 2. Synthetic dataset: 2,000 samples per class, 4 features each
# ----------------------------------------------------------------------------
def make_dataset(n_per_class=2000):
    # (mean, std) per feature: temp, pH, TDS, turbidity  -- illustrative values
    profiles = {
        0: [(25, 1.5), (7.3, 0.3), (250, 45), (92, 4)],  # Normal: potable tap water
        1: [
            (24, 2.0),
            (6.1, 0.4),
            (40, 25),
            (78, 8),
        ],  # Rainwater: low TDS, slightly acidic
        2: [
            (25, 1.5),
            (9.6, 0.7),
            (650, 160),
            (86, 7),
        ],  # Chemical: bleach -> high pH/TDS
    }
    X, y = [], []
    for label, prof in profiles.items():
        cols = [rng.normal(m, s, n_per_class) for m, s in prof]
        X.append(np.column_stack(cols))
        y.append(np.full(n_per_class, label))
    X, y = np.vstack(X), np.concatenate(y)
    X[:, 2] = np.clip(X[:, 2], 0, None)  # TDS >= 0
    X[:, 3] = np.clip(X[:, 3], 0, 100)  # turbidity 0-100 %
    idx = rng.permutation(len(y))
    return X[idx], y[idx]


# ----------------------------------------------------------------------------
# 3. Small MLP (4 -> 16 -> 8 -> 3) trained with Adam, pure NumPy
# ----------------------------------------------------------------------------
class MLP:
    def __init__(self, sizes=(4, 16, 8, 3)):
        self.W = [
            rng.normal(0, np.sqrt(2 / a), (a, b)) for a, b in zip(sizes[:-1], sizes[1:])
        ]
        self.b = [np.zeros(b) for b in sizes[1:]]
        self.m = [np.zeros_like(p) for p in self.W + self.b]
        self.v = [np.zeros_like(p) for p in self.W + self.b]
        self.t = 0

    @staticmethod
    def softmax(z):
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def forward(self, x, keep=False):
        acts = [x]
        for i, (W, b) in enumerate(zip(self.W, self.b)):
            z = acts[-1] @ W + b
            acts.append(np.maximum(z, 0) if i < len(self.W) - 1 else self.softmax(z))
        return acts if keep else acts[-1]

    def fit(self, X, y, epochs=50, lr=0.01, batch=64):
        Y = np.eye(3)[y]
        n = len(X)
        for _ in range(epochs):
            order = rng.permutation(n)
            for s in range(0, n, batch):
                i = order[s : s + batch]
                acts = self.forward(X[i], keep=True)
                delta = (acts[-1] - Y[i]) / len(i)
                gW, gb = [], []
                for l in range(len(self.W) - 1, -1, -1):
                    gW.insert(0, acts[l].T @ delta)
                    gb.insert(0, delta.sum(axis=0))
                    if l > 0:
                        delta = (delta @ self.W[l].T) * (acts[l] > 0)
                self._adam(gW + gb, lr)

    def _adam(self, grads, lr, b1=0.9, b2=0.999, eps=1e-8):
        self.t += 1
        params = self.W + self.b
        for k, (p, g) in enumerate(zip(params, grads)):
            self.m[k] = b1 * self.m[k] + (1 - b1) * g
            self.v[k] = b2 * self.v[k] + (1 - b2) * g * g
            p -= (
                lr
                * (self.m[k] / (1 - b1**self.t))
                / (np.sqrt(self.v[k] / (1 - b2**self.t)) + eps)
            )

    def predict(self, X):
        return self.forward(X).argmax(axis=1)


# ----------------------------------------------------------------------------
# 4. Post-training 8-bit quantization (simulated integer inference, as on TFLM)
# ----------------------------------------------------------------------------
class QuantizedMLP:
    def __init__(self, model, X_calib):
        # symmetric per-tensor int8 weights
        self.s_w = [np.abs(W).max() / 127 for W in model.W]
        self.Wq = [np.round(W / s).astype(np.int8) for W, s in zip(model.W, self.s_w)]
        self.b = model.b
        # calibrate activation scales on representative data (like TFLite's representative_dataset)
        acts = model.forward(X_calib, keep=True)
        self.s_a = [np.abs(a).max() / 127 for a in acts[:-1]]

    def predict_proba(self, X):
        a = X
        for l, (Wq, s_w) in enumerate(zip(self.Wq, self.s_w)):
            a_q = np.clip(np.round(a / self.s_a[l]), -128, 127).astype(
                np.int32
            )  # int8 activations
            acc = a_q @ Wq.astype(np.int32)  # int32 accumulate
            z = acc * (self.s_a[l] * s_w) + self.b[l]  # rescale + bias
            a = np.maximum(z, 0) if l < len(self.Wq) - 1 else MLP.softmax(z)
        return a

    def predict(self, X):
        return self.predict_proba(X).argmax(axis=1)

    def size_bytes(self):
        return sum(W.size for W in self.Wq) + 4 * sum(b.size for b in self.b)


# ----------------------------------------------------------------------------
# 5. Stratified 5-fold cross-validation
# ----------------------------------------------------------------------------
def stratified_kfold(y, k=5):
    folds = [[] for _ in range(k)]
    for c in np.unique(y):
        idx = rng.permutation(np.where(y == c)[0])
        for i, chunk in enumerate(np.array_split(idx, k)):
            folds[i].extend(chunk)
    return [np.array(f) for f in folds]


# ----------------------------------------------------------------------------
# 6. Pump control with hysteresis (closed-loop safety cut-off)
# ----------------------------------------------------------------------------
SAFE = {"pH": (6.5, 8.5), "TDS_ppm": (0, 500)}
HYST = {"pH": 0.2, "TDS_ppm": 30}  # must come back this far inside range to re-enable


def pump_state(reading, pump_on):
    ph, tds = reading[1], reading[2]
    lo_ph, hi_ph = SAFE["pH"]
    hi_tds = SAFE["TDS_ppm"][1]
    violated = not (lo_ph <= ph <= hi_ph) or tds > hi_tds
    well_inside = (
        lo_ph + HYST["pH"] <= ph <= hi_ph - HYST["pH"]
    ) and tds <= hi_tds - HYST["TDS_ppm"]
    if pump_on and violated:
        return False
    if not pump_on and well_inside:
        return True
    return pump_on


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
if __name__ == "__main__":
    X, y = make_dataset()
    mu, sd = X.mean(axis=0), X.std(axis=0)  # normalisation baked into firmware
    Xn = (X - mu) / sd

    # --- 5-fold CV --------------------------------------------------------
    print("Stratified 5-fold CV (float32 vs int8-quantized)")
    folds, accs_f, accs_q = stratified_kfold(y), [], []
    for i, val in enumerate(folds):
        train = np.concatenate([f for j, f in enumerate(folds) if j != i])
        net = MLP()
        net.fit(Xn[train], y[train], epochs=50)
        qnet = QuantizedMLP(net, Xn[train][:500])
        accs_f.append((net.predict(Xn[val]) == y[val]).mean())
        accs_q.append((qnet.predict(Xn[val]) == y[val]).mean())
        print(f"  fold {i + 1}: float {accs_f[-1]:.4f} | int8 {accs_q[-1]:.4f}")
    print(f"  mean : float {np.mean(accs_f):.4f} | int8 {np.mean(accs_q):.4f}\n")

    # --- Final model on all data, quantize --------------------------------
    net = MLP()
    net.fit(Xn, y, epochs=50)
    qnet = QuantizedMLP(net, Xn[:500])
    print(f"Quantized weights+biases: ~{qnet.size_bytes()} bytes ")

    # --- Example input: raw ADC counts -> physical units -> classify ------
    print("Example inference from raw sensor readings")
    # (temp_C from MAX6675, ph_adc, tds_adc, turbidity_adc)
    samples = {
        # note: ~0.08 pH per ADC count with the paper's Eq. 1, so pH ADC values stay near 365
        "Tap water": (25.0, 360, 211, 940),  # ~pH 7.4, ~250 ppm
        "Fresh rainwater": (23.5, 376, 30, 800),  # ~pH 6.1, ~40 ppm
        "Bleach spike": (25.5, 333, 509, 880),  # ~pH 9.6, ~650 ppm
    }
    pump_on = True
    for name, (t, ph_adc, tds_adc, turb_adc) in samples.items():
        reading = np.array(
            [
                t,
                adc_to_ph(ph_adc),
                adc_to_tds_ppm(tds_adc),
                adc_to_turbidity_pct(turb_adc),
            ]
        )
        probs = qnet.predict_proba(((reading - mu) / sd)[None, :])[0]
        label = CLASSES[probs.argmax()]
        pump_on = pump_state(reading, pump_on)
        feats = ", ".join(f"{n}={v:.2f}" for n, v in zip(FEATURES, reading))
        print(
            f"  {name:16s} -> {label:17s} (p={probs.max():.2f}) | {feats} | pump={'ON' if pump_on else 'OFF'}"
        )
