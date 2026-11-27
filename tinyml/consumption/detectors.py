"""Abnormal Household Water-Consumption Detection Subsystem (Offline-First).

Logically separated from pipeline leakage detection (tinyml/leakage/) and
water-quality TinyML (tinyml/quality/). Implements four comparable detectors:
  1. StatisticalBaselineDetector: hourly mean + 3x baseline rule (existing).
  2. StatisticalZeroFlowDetector: adds time-of-day-aware prolonged zero-flow.
  3. LZCompressionDetector: Babazadeh (2019) [3] inspired LZ78 complexity rate
     on quantized 1-minute flow windows.
  4. CombinedConsumptionDetector: interpretable statistical + zero-flow rules
     combined selectively with LZ78 complexity for irregular burst patterns.

Event categories (distinct from LEAK_* alerts):
  - UNUSUAL_HIGH_CONSUMPTION
  - UNUSUAL_NIGHT_CONSUMPTION
  - PROLONGED_ZERO_FLOW
  - SUDDEN_CONSUMPTION_CHANGE
  - IRREGULAR_BURST_PATTERN
"""
from collections import deque
import math

DAY_S = 86400
HOUR_S = 3600
LEARN_S = DAY_S

EVENT_TYPES = (
    "UNUSUAL_HIGH_CONSUMPTION",
    "UNUSUAL_NIGHT_CONSUMPTION",
    "PROLONGED_ZERO_FLOW",
    "SUDDEN_CONSUMPTION_CHANGE",
    "IRREGULAR_BURST_PATTERN",
)


class HourlyBaseline:
    """Lightweight per-hour mean/std + EWMA (like firmware Baseline, but blends std, not squares)."""

    def __init__(self, alpha=1 / 14):
        self.alpha = alpha
        self.n = [0] * 24
        self.mean_v = [0.0] * 24
        self.m2 = [0.0] * 24
        self.std_v = [0.0] * 24
        self.cur_n = 0
        self.cur_sum = 0.0
        self.cur_ssq = 0.0

    def add(self, h, x):
        self.n[h] += 1
        d = x - self.mean_v[h]
        self.mean_v[h] += d / self.n[h]
        self.m2[h] += d * (x - self.mean_v[h])
        self.std_v[h] = math.sqrt(self.m2[h] / self.n[h])

    def collect(self, x):
        self.cur_n += 1
        self.cur_sum += x
        self.cur_ssq += x * x

    def blend(self, h, clean=True):
        if clean and self.cur_n:
            m = self.cur_sum / self.cur_n
            v = max(self.cur_ssq / self.cur_n - m * m, 0.0)
            self.mean_v[h] = (1 - self.alpha) * self.mean_v[h] + self.alpha * m
            self.std_v[h] = (1 - self.alpha) * self.std_v[h] + self.alpha * math.sqrt(v)
        self.cur_n = 0
        self.cur_sum = 0.0
        self.cur_ssq = 0.0

    def mean(self, h):
        return self.mean_v[h]

    def std(self, h):
        return self.std_v[h]


def lz78_phrase_count(symbols):
    """Count distinct LZ78 dictionary phrases in a symbol sequence (O(W) time)."""
    dictionary = {}
    phrase = ()
    phrases = 0
    for s in symbols:
        nxt = phrase + (s,)
        if nxt in dictionary:
            phrase = nxt
        else:
            phrases += 1
            dictionary[nxt] = phrases
            phrase = ()
    if phrase:
        phrases += 1
    return phrases


def lz78_complexity_rate(symbols):
    """Normalized LZ78 complexity c(W) * log2(c(W) + 1) / W."""
    w = len(symbols)
    if w == 0:
        return 0.0
    c = lz78_phrase_count(symbols)
    return (c * math.log2(c + 1)) / w


def quantize_flow(flow_lps):
    """Quantize 1-min flow (L/s) into 6 physically meaningful discrete symbols."""
    if flow_lps <= 0.002:
        return 0  # off
    if flow_lps <= 0.02:
        return 1  # trickle
    if flow_lps <= 0.06:
        return 2  # tap
    if flow_lps <= 0.15:
        return 3  # shower
    if flow_lps <= 0.35:
        return 4  # multi-fixture high
    return 5      # extreme burst


class BaseConsumptionDetector:
    name = "base"

    def __init__(self, learn_s=LEARN_S):
        self.learn_s = learn_s
        self.base = HourlyBaseline()
        self.hour = 0
        self.clean = True
        self.active = {}  # event_type -> last_triggered_t
        self.last_t = None  # time of the last reading actually received

    def step(self, t_s, flow_lps, missing=False):
        """Process one 1-minute observation causally. Returns list of (event_type, info)."""
        if missing or flow_lps is None or math.isnan(flow_lps):
            return []
        if self.last_t is not None and t_s - self.last_t > 90:
            self._on_gap(t_s - self.last_t)
        self.last_t = t_s
        hour = int((t_s % DAY_S) // HOUR_S)
        if hour != self.hour:
            self.base.blend(self.hour, self.clean)
            self.hour = hour
            self.clean = True
        if t_s < self.learn_s:
            self.base.add(hour, flow_lps)
            self._learn_extra(t_s, hour, flow_lps)
            return []
        self.base.collect(flow_lps)
        events = self._check(t_s, hour, flow_lps)
        if events:
            self.clean = False
        return events

    def _on_gap(self, gap_s):
        """At least one 1-minute reading is missing (marked missing, or never passed in)."""

    def _learn_extra(self, t_s, hour, flow_lps):
        pass

    def _check(self, t_s, hour, flow_lps):
        return []

    def _emit_once(self, ev_type, t_s, cooldown_s=3600, **info):
        prev = self.active.get(ev_type, -1e9)
        if t_s - prev >= cooldown_s:
            self.active[ev_type] = t_s
            return (ev_type, info)
        return None


class StatisticalBaselineDetector(BaseConsumptionDetector):
    """Approach 1: Hourly mean + 3x rule + night burst + rolling volume shift."""
    name = "statistical_baseline"

    def __init__(self, learn_s=LEARN_S, high_min=60, high_factor=3.0, high_floor=0.05):
        super().__init__(learn_s=learn_s)
        self.high_min = high_min
        self.high_factor = high_factor
        self.high_floor = high_floor
        self.high_run = 0
        self.night_run = 0
        self.win_6h = deque(maxlen=360)
        self.day_mean_6h = 0.0
        # Event types whose condition is true now, or cannot be judged yet. Events have a
        # cooldown, so "no new event" does not mean the condition ended.
        self.holding = set()

    def _on_gap(self, gap_s):
        self.high_run = self.night_run = 0      # "for N minutes" means N consecutive readings
        if gap_s > HOUR_S:
            self.win_6h.clear()                 # too old to count as the last 6 hours

    def _learn_extra(self, t_s, hour, flow_lps):
        self.win_6h.append(flow_lps)
        self.day_mean_6h = sum(self.base.mean_v) / 24.0

    def _check(self, t_s, hour, flow_lps):
        out = []
        # 1. UNUSUAL_HIGH_CONSUMPTION (existing 3x hourly mean for 60 min)
        limit = max(self.high_factor * self.base.mean(hour), self.high_floor)
        self.high_run = self.high_run + 1 if flow_lps > limit else 0
        if self.high_run >= self.high_min:
            ev = self._emit_once("UNUSUAL_HIGH_CONSUMPTION", t_s, cooldown_s=3600, flow=round(flow_lps, 4))
            if ev:
                out.append(ev)

        # 2. UNUSUAL_NIGHT_CONSUMPTION (bursty/elevated night flow 01:00-05:00 >= 0.06 L/s for 15 min)
        at_night = 1 <= hour < 5
        night_lim = max(self.base.mean(hour) + 3.0 * self.base.std(hour), 0.06)
        self.night_run = self.night_run + 1 if (at_night and flow_lps > night_lim) else 0
        if self.night_run >= 15:
            ev = self._emit_once("UNUSUAL_NIGHT_CONSUMPTION", t_s, cooldown_s=3600, flow=round(flow_lps, 4))
            if ev:
                out.append(ev)

        # 3. SUDDEN_CONSUMPTION_CHANGE (rolling 6h mean vs same-time-of-day 6h baseline)
        self.win_6h.append(flow_lps)
        self.holding = {name for name, true_now in (("UNUSUAL_HIGH_CONSUMPTION", flow_lps > limit),
                                                    ("UNUSUAL_NIGHT_CONSUMPTION", at_night and flow_lps > night_lim),
                                                    ("SUDDEN_CONSUMPTION_CHANGE", len(self.win_6h) < 360)) if true_now}
        if len(self.win_6h) == 360:
            m6 = sum(self.win_6h) / 360.0
            expected_6h = sum(self.base.mean((hour - k) % 24) for k in range(6)) / 6.0
            if m6 > max(2.2 * expected_6h, 0.025):
                self.holding.add("SUDDEN_CONSUMPTION_CHANGE")
                ev = self._emit_once("SUDDEN_CONSUMPTION_CHANGE", t_s, cooldown_s=6 * 3600, mean_6h=round(m6, 4))
                if ev:
                    out.append(ev)
        return out


class StatisticalZeroFlowDetector(StatisticalBaselineDetector):
    """Approach 2: Statistical baseline + prolonged zero-flow (24 h continuous)."""
    name = "statistical_zero_flow"

    def __init__(self, learn_s=LEARN_S, active_zero_min=1440, **kw):
        super().__init__(learn_s=learn_s, **kw)
        # 24 h continuous zero-flow avoids false alarms from overnight sleep + 10 h workday absences.
        self.active_zero_min = active_zero_min
        self.zero_run = 0

    def _on_gap(self, gap_s):
        super()._on_gap(gap_s)
        self.zero_run = 0                       # unknown flow is not zero flow

    def _check(self, t_s, hour, flow_lps):
        out = super()._check(t_s, hour, flow_lps)
        self.zero_run = self.zero_run + 1 if flow_lps <= 0.002 else 0
        if self.zero_run >= self.active_zero_min:
            ev = self._emit_once("PROLONGED_ZERO_FLOW", t_s, cooldown_s=12 * 3600, zero_min=self.zero_run)
            if ev:
                out.append(ev)
        return out


class LZCompressionDetector(BaseConsumptionDetector):
    """Approach 3: Babazadeh (2019)-inspired LZ78 complexity-rate detector."""
    name = "lz_compression"

    def __init__(self, learn_s=LEARN_S, window_min=120, z_thresh=3.5):
        super().__init__(learn_s=learn_s)
        self.window_min = window_min
        self.z_thresh = z_thresh
        self.sym_win = deque(maxlen=window_min)
        self.cr_base = HourlyBaseline()
        self.cr_hour = 0
        self.high_cr_run = 0
        self.low_cr_run = 0

    def _learn_extra(self, t_s, hour, flow_lps):
        self.sym_win.append(quantize_flow(flow_lps))
        if len(self.sym_win) == self.window_min:
            cr = lz78_complexity_rate(self.sym_win)
            self.cr_base.add(hour, cr)

    def _check(self, t_s, hour, flow_lps):
        out = []
        if hour != self.cr_hour:
            self.cr_base.blend(self.cr_hour, self.clean)
            self.cr_hour = hour
        self.sym_win.append(quantize_flow(flow_lps))
        if len(self.sym_win) < self.window_min:
            return out

        cr = lz78_complexity_rate(self.sym_win)
        self.cr_base.collect(cr)
        cr_m = self.cr_base.mean(hour)
        cr_s = max(self.cr_base.std(hour), 0.06)
        z = (cr - cr_m) / cr_s

        if z > self.z_thresh:
            self.high_cr_run += 1
            self.low_cr_run = 0
        elif z < -self.z_thresh and cr < 0.12:
            self.low_cr_run += 1
            self.high_cr_run = 0
        else:
            self.high_cr_run = 0
            self.low_cr_run = 0

        if self.high_cr_run >= 15:
            ev = self._emit_once("IRREGULAR_BURST_PATTERN", t_s, cooldown_s=3600, cr=round(cr, 3), z=round(z, 2))
            if ev:
                out.append(ev)
        if self.low_cr_run >= 120 and sum(self.sym_win) == 0 and (6 <= hour < 22):
            ev = self._emit_once("PROLONGED_ZERO_FLOW", t_s, cooldown_s=12 * 3600, cr=round(cr, 3))
            if ev:
                out.append(ev)
        return out


class CombinedConsumptionDetector(StatisticalZeroFlowDetector):
    """Approach 4: Statistical + time-of-day zero-flow + selective LZ78 irregular-burst detector."""
    name = "combined"

    def __init__(self, learn_s=LEARN_S, window_min=120, lz_z_thresh=3.5, **kw):
        super().__init__(learn_s=learn_s, **kw)
        self.window_min = window_min
        self.lz_z_thresh = lz_z_thresh
        self.sym_win = deque(maxlen=window_min)
        self.cr_base = HourlyBaseline()
        self.cr_hour = 0
        self.high_cr_run = 0

    def _learn_extra(self, t_s, hour, flow_lps):
        super()._learn_extra(t_s, hour, flow_lps)
        self.sym_win.append(quantize_flow(flow_lps))
        if len(self.sym_win) == self.window_min:
            self.cr_base.add(hour, lz78_complexity_rate(self.sym_win))

    def _check(self, t_s, hour, flow_lps):
        out = super()._check(t_s, hour, flow_lps)
        if hour != self.cr_hour:
            self.cr_base.blend(self.cr_hour, self.clean)
            self.cr_hour = hour
        self.sym_win.append(quantize_flow(flow_lps))
        if len(self.sym_win) == self.window_min:
            cr = lz78_complexity_rate(self.sym_win)
            self.cr_base.collect(cr)
            cr_m = self.cr_base.mean(hour)
            cr_s = max(self.cr_base.std(hour), 0.06)
            z = (cr - cr_m) / cr_s
            self.high_cr_run = self.high_cr_run + 1 if z > self.lz_z_thresh else 0
            if self.high_cr_run >= 15:
                ev = self._emit_once("IRREGULAR_BURST_PATTERN", t_s, cooldown_s=3600, cr=round(cr, 3), z=round(z, 2))
                if ev:
                    out.append(ev)
        return out
