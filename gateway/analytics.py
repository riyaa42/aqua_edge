"""Gateway-side analytics: incident lifecycle, root-cause correlation, risk scoring,
maintenance prioritization, delivery-latency accounting, and dashboard snapshot export.

Kept separate from transport/protocol handling in core.py so the same analytics
work identically in simulation, USB-serial hardware mode, and dashboard feeds.
"""

FLAT_LEAK_ALERTS = {"LEAK_NIGHT_FLOW", "LEAK_CONTINUOUS_FLOW", "LEAK_PRESSURE_DROP"}
CONSUMPTION_ALERTS = {
    "ABNORMAL_CONSUMPTION",
    "UNUSUAL_NIGHT_CONSUMPTION",
    "PROLONGED_ZERO_FLOW",
    "SUDDEN_CONSUMPTION_CHANGE",
}
QUALITY_ALERTS = {
    "QUALITY_CHEMICAL",
    "QUALITY_RUNOFF",
    "QUALITY_LIMIT_VIOLATION",
    "QUALITY_TEMPORAL_ANOMALY",
}
SUPPLY_EVENTS = {"SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN"}
# recovery message from a node -> the incident category it ends
RECOVERIES = {
    "FLAT_LEAK_CLEARED": ("FLAT_LEAK",),
    "CONSUMPTION_CLEARED": ("ABNORMAL_CONSUMPTION",),
    "ZERO_FLOW_CLEARED": ("PROLONGED_ZERO_FLOW",),
    "SUPPLY_FLOW_CLEARED": ("ABNORMAL_SUPPLY",),
    "QUALITY_CLEARED": ("QUALITY_CHEMICAL", "QUALITY_RUNOFF", "QUALITY_LIMIT_VIOLATION"),
    "QUALITY_TEMPORAL_CLEARED": ("QUALITY_TEMPORAL_ANOMALY",),
}
# counters and latest values that survive a gateway restart (incidents and cleared_at: see to_state)
SAVED = ("resolved_history", "last_sum", "last_quality", "last_supply", "last_balance_pct", "pipe_leak_info",
         "bad_packets", "skewed_packets", "online_alerts", "offline_alerts", "buffered_recovered",
         "delay_count", "delay_sum", "delay_max")

CORRELATION_WINDOW_S = 1800  # 30 min window for grouping co-occurring root-cause symptoms
MAX_HISTORY = 200

LOW_RISK_CAPS = {
    "NODE_SILENT": 35.0,
    "CLOCK_SKEW": 35.0,
    "PROLONGED_ZERO_FLOW": 44.0,
    "ABNORMAL_CONSUMPTION": 44.0,
    "SUPPLY_UNCERTAIN": 44.0,
    "SUPPLY_EVENT": 54.0,
}

BASE_WEIGHTS = {
    "QUALITY_CHEMICAL": 55.0,
    "QUALITY_LIMIT_VIOLATION": 52.0,
    "QUALITY_RUNOFF": 45.0,
    "QUALITY_TEMPORAL_ANOMALY": 40.0,
    "PIPE_LEAK": 42.0,
    "FLAT_LEAK": 32.0,
    "LEAK_TANK_DRAIN": 30.0,
    "SUPPLY_EVENT": 28.0,
    "SUPPLY_UNCERTAIN": 18.0,
    "ABNORMAL_CONSUMPTION": 16.0,
    "PROLONGED_ZERO_FLOW": 14.0,
    "NODE_SILENT": 10.0,
    "CLOCK_SKEW": 8.0,
}

ACTIONS = {
    "QUALITY_CHEMICAL": "Isolate tank distribution and flush/test water immediately (chemical hazard).",
    "QUALITY_LIMIT_VIOLATION": "Inspect tank water quality against IS 10500 limits immediately.",
    "QUALITY_RUNOFF": "Inspect rooftop tank cover/inlet for contamination or turbidity ingress.",
    "QUALITY_TEMPORAL_ANOMALY": "Inspect tank water quality sensors for sudden drift or early contamination.",
    "PIPE_LEAK": "Inspect vertical riser/downtake section and shut down affected branch.",
    "FLAT_LEAK": "Inspect flat plumbing/fixtures for internal leak.",
    "LEAK_TANK_DRAIN": "Locate active leak draining rooftop tank and verify pump capacity.",
    "SUPPLY_EVENT": "Check building pump, power supply, and municipal inlet.",
    "SUPPLY_UNCERTAIN": "Inspect tank inflow/level sensor and verify pump operation.",
    "ABNORMAL_CONSUMPTION": "Notify resident of unusual water consumption.",
    "PROLONGED_ZERO_FLOW": "Verify occupancy or check flat meter/shutoff valve.",
    "NODE_SILENT": "Check node power, SD card, and SX1278 LoRa link.",
    "CLOCK_SKEW": "Verify node RTC sync and ACK reception.",
}


def category_of(alert):
    """The incident category an alert name belongs to."""
    if alert in FLAT_LEAK_ALERTS:
        return "FLAT_LEAK"
    if alert in CONSUMPTION_ALERTS:
        return "PROLONGED_ZERO_FLOW" if alert == "PROLONGED_ZERO_FLOW" else "ABNORMAL_CONSUMPTION"
    return alert


def _severity_label(score):
    if score >= 70:
        return "CRITICAL"
    if score >= 45:
        return "HIGH"
    if score >= 25:
        return "MEDIUM"
    return "LOW"


def _popcount(mask):
    return bin(int(mask or 0)).count("1")


class GatewayAnalytics:
    """Maintains active incidents, root-cause groups, risk rankings, and latency stats."""

    def __init__(self, topology):
        self.tree = topology
        self.flats = set(topology.flats)
        self.tank = topology.tank
        self.total_flats = max(len(self.flats), 1)
        self.incidents = {}        # (target, category) -> dict
        self.resolved_history = [] # list of resolved incident dicts (bounded by MAX_HISTORY)
        self.last_sum = {}         # node -> latest SUM dict
        self.last_quality = {
            "class": "NORMAL",
            "compliant": True,
            "viol_mask": 0,
            "temporal_anomaly": False,
            "ph": None,
            "tds": None,
            "turb": None,
            "score": None,
        }
        self.last_supply = {"state": "NORMAL", "level_m": None, "inflow_lps": None, "t": None}
        self.last_balance_pct = None
        self.pipe_leak_info = None
        self.bad_packets = 0
        self.skewed_packets = 0
        self.online_alerts = 0
        self.offline_alerts = 0
        self.buffered_recovered = 0
        self.delay_count, self.delay_sum, self.delay_max = 0, 0.0, 0.0
        self.cleared_at = {}       # (target, category) -> node time of its latest recovery

    def to_state(self):
        return {**{name: getattr(self, name) for name in SAVED},
                "incidents": list(self.incidents.values()),
                "cleared_at": [[*key, t] for key, t in self.cleared_at.items()]}

    def load_state(self, state):
        for name in SAVED:
            setattr(self, name, state[name])
        self.incidents = {(it["target"], it["category"]): it for it in state["incidents"]}
        self.cleared_at = {(target, cat): t for target, cat, t in state["cleared_at"]}

    def is_stale(self, target, category, t_event):
        """True for something that happened before a recovery already received (late delivery)."""
        return t_event <= self.cleared_at.get((target, category), float("-inf"))

    def _clear(self, target, category, t_clear, reason):
        """A recovery ends only what is older than itself."""
        key = (target, category)
        self.cleared_at[key] = max(t_clear, self.cleared_at.get(key, t_clear))
        if key in self.incidents and self.incidents[key]["t_last"] <= t_clear:
            self._resolve(target, category, t_resolved=t_clear, reason=reason)

    def _affected_flats_for_floor(self, floor):
        """How many flats are fed through the suspected junction (all of them if unknown)."""
        junction = self.tree.labels.get(floor)
        if junction is None:
            return self.total_flats
        return max(len(self.tree.flats_below(junction)), 1)

    def _upsert(self, target, category, t_event, rx, **evidence):
        if self.is_stale(target, category, t_event):
            return
        key = (target, category)
        cur = self.incidents.get(key)
        if cur is None:
            self.incidents[key] = {
                "target": target,
                "category": category,
                "t_onset": t_event,
                "t_last": t_event,
                "rx_last": rx,
                "repeat_count": 1,
                "evidence": dict(evidence),
            }
        else:
            cur["t_last"] = max(cur["t_last"], t_event)
            cur["rx_last"] = max(cur["rx_last"], rx)
            cur["repeat_count"] += 1
            cur["evidence"].update({k: v for k, v in evidence.items() if v is not None})

    def _resolve(self, target, category, t_resolved=None, reason=None):
        inc = self.incidents.pop((target, category), None)
        if inc is not None:
            self.resolved_history.append({
                **inc,
                "evidence": dict(inc["evidence"]),
                "t_resolved": t_resolved,
                "resolution_reason": reason or "resolved",
            })
            if len(self.resolved_history) > MAX_HISTORY:
                self.resolved_history = self.resolved_history[-MAX_HISTORY:]

    def clear_pipe_leak(self, t_resolved=None):
        self.pipe_leak_info = None
        self._resolve(self.tank, "PIPE_LEAK", t_resolved=t_resolved, reason="mass_balance_rearmed")
        for (tgt, cat) in list(self.incidents):
            if cat == "PIPE_LEAK":
                self._resolve(tgt, cat, t_resolved=t_resolved, reason="mass_balance_rearmed")

    def on_packet(self, rx, m):
        raw_delay = float(rx) - float(m["t"])
        valid_delay = raw_delay >= 0.0
        if valid_delay:
            self.delay_count += 1
            self.delay_sum += raw_delay
            self.delay_max = max(self.delay_max, raw_delay)
            if raw_delay >= 60.0:
                self.buffered_recovered += 1
        else:
            self.skewed_packets += 1

        k, n = m["k"], m["n"]
        if k == "SUM":
            self.last_sum[n] = {key: v for key, v in m.items() if key not in ("k", "s")}
            if n == self.tank:
                if "level_m" in m:
                    self.last_supply["level_m"] = m["level_m"]
                if "in_l" in m:
                    self.last_supply["inflow_lps"] = round(m["in_l"] / 900.0, 4)
            return

        if k == "FLAG" and n == self.tank:
            if "level" in m:
                self.last_supply["level_m"] = m["level"]
            if "inflow" in m:
                self.last_supply["inflow_lps"] = m["inflow"]
            return

        if k != "ALERT":
            return

        if valid_delay:
            if raw_delay < 60.0:
                self.online_alerts += 1
            else:
                self.offline_alerts += 1

        a = m["a"]
        if a in RECOVERIES:
            for category in RECOVERIES[a]:
                self._clear(n, category, m["t"], a)
            still_open = any((n, category) in self.incidents for category in RECOVERIES[a])
            if a == "QUALITY_CLEARED" and not still_open:
                self.last_quality.update({"class": "NORMAL", "compliant": True, "viol_mask": 0})
            elif a == "QUALITY_TEMPORAL_CLEARED" and not still_open:
                self.last_quality["temporal_anomaly"] = False
            return
        if self.is_stale(n, category_of(a), m["t"]):
            return

        if a in QUALITY_ALERTS:
            if a in ("QUALITY_CHEMICAL", "QUALITY_RUNOFF"):
                self.last_quality["class"] = a.replace("QUALITY_", "")
            if a == "QUALITY_LIMIT_VIOLATION":
                self.last_quality["compliant"] = False
                self.last_quality["viol_mask"] = m.get("viol", self.last_quality["viol_mask"])
            if a == "QUALITY_TEMPORAL_ANOMALY":
                self.last_quality["temporal_anomaly"] = True
                self.last_quality["score"] = m.get("score")
            for fld in ("ph", "tds", "turb"):
                if fld in m:
                    self.last_quality[fld] = m[fld]
            self._upsert(n, a, m["t"], rx, ph=m.get("ph"), tds=m.get("tds"),
                         turb=m.get("turb"), viol=m.get("viol"), score=m.get("score"))
        elif a in FLAT_LEAK_ALERTS:
            self._upsert(n, "FLAT_LEAK", m["t"], rx, rule=a, flow=m.get("flow"), p_min=m.get("p_min"))
        elif a in CONSUMPTION_ALERTS:
            self._upsert(n, category_of(a), m["t"], rx, rule=a, flow=m.get("flow"))
        elif a == "ABNORMAL_SUPPLY":
            self._upsert(n, "ABNORMAL_SUPPLY", m["t"], rx, flow=m.get("flow"))

    def on_event(self, rx, ev):
        kind = ev["event"]
        t_ev = ev["t"]
        if kind == "BAD_PACKET":
            self.bad_packets += int(ev.get("count", 1))
        elif kind == "NODE_SILENT":
            self._upsert(ev["node"], "NODE_SILENT", t_ev, rx, last_heard_min=ev.get("last_heard_min"))
        elif kind == "NODE_BACK":
            self._resolve(ev["node"], "NODE_SILENT", t_resolved=t_ev, reason="NODE_BACK")
        elif kind == "CLOCK_SKEW":
            self._upsert(ev["node"], "CLOCK_SKEW", t_ev, rx, ahead_s=ev.get("ahead_s"))
        elif kind == "CLOCK_OK":
            self._resolve(ev["node"], "CLOCK_SKEW", t_resolved=t_ev, reason="CLOCK_OK")
        elif kind == "SUPPLY_RECOVERED":
            for s in SUPPLY_EVENTS:
                self._clear(self.tank, s, ev["event_t"], kind)
            self.last_supply.update({"state": "NORMAL", "t": ev["event_t"]})
        elif kind == "PIPE_LEAK":
            fl = ev.get("suspected_floor")
            tgt = self.tree.labels.get(fl) or "B-RISER"
            self.pipe_leak_info = {
                "target": tgt,
                "suspected_floor": fl,
                "suspected_node": ev.get("suspected_node"),
                "isolation_valve": ev.get("isolation_valve"),
                "suspected_range": ev.get("suspected_range", []),
                "localization_confidence": ev.get("localization_confidence", "unlocalised"),
                "error_pct": ev.get("error_pct"),
                "floor_drops": ev.get("floor_drops", {}),
            }
            self._upsert(
                tgt, "PIPE_LEAK", t_ev, rx,
                suspected_floor=fl,
                suspected_node=self.pipe_leak_info["suspected_node"],
                isolation_valve=self.pipe_leak_info["isolation_valve"],
                suspected_range=self.pipe_leak_info["suspected_range"],
                localization_confidence=self.pipe_leak_info["localization_confidence"],
                error_pct=self.pipe_leak_info["error_pct"],
                floor_drops=self.pipe_leak_info["floor_drops"],
            )
        elif kind in SUPPLY_EVENTS:
            t_ev = ev.get("event_t", t_ev)      # when the tank saw it, to compare with a recovery
            self.last_supply["state"] = kind
            self.last_supply["t"] = t_ev
            for s in SUPPLY_EVENTS:
                if s != kind:
                    self._resolve(self.tank, s, t_resolved=t_ev, reason=f"superseded_by_{kind}")
            self._upsert(self.tank, kind, t_ev, rx, inflow_lps=ev.get("inflow_lps"))
        elif kind in ("NEW_ALERT", "REPEAT_ALERT") and ev.get("alert") in FLAT_LEAK_ALERTS:
            cur = self.incidents.get((ev["node"], "FLAT_LEAK"))
            if cur and t_ev >= cur["t_last"]:   # only the newest alert decides the label
                if ev.get("possible_cause"):
                    cur["evidence"]["possible_cause"] = ev["possible_cause"]
                else:
                    cur["evidence"].pop("possible_cause", None)

    def correlate_incidents(self, now):
        """Group co-occurring symptoms into root-cause incidents."""
        correlated = []
        used = set()

        # 1. Tank Water Contamination Group (within CORRELATION_WINDOW_S of primary quality event)
        q_keys = [(tgt, cat) for (tgt, cat) in self.incidents if cat in QUALITY_ALERTS]
        if q_keys:
            q_items = [self.incidents[k] for k in q_keys]
            primary = max(q_items, key=lambda it: (BASE_WEIGHTS.get(it["category"], 0), it["t_last"]))
            grouped = [
                (k, self.incidents[k])
                for k in q_keys
                if abs(self.incidents[k]["t_last"] - primary["t_last"]) <= CORRELATION_WINDOW_S
            ]
            used.update(k for k, _ in grouped)
            items = [it for _, it in grouped]
            ev_merged = {}
            for it in items:
                ev_merged.update(it["evidence"])
            correlated.append({
                "target": primary["target"],
                "category": primary["category"],
                "symptoms": sorted(it["category"] for it in items),
                "t_onset": min(it["t_onset"] for it in items),
                "t_last": max(it["t_last"] for it in items),
                "repeat_count": max(it["repeat_count"] for it in items),
                "affected_flats": self.total_flats,
                "evidence": ev_merged,
            })

        # 2. Main Riser Leak Group (correlates symptoms within CORRELATION_WINDOW_S of PIPE_LEAK)
        pipe_keys = [(tgt, cat) for (tgt, cat) in self.incidents if cat == "PIPE_LEAK"]
        if pipe_keys:
            used.update(pipe_keys)
            p_inc = self.incidents[pipe_keys[0]]
            win_lo = p_inc["t_onset"] - CORRELATION_WINDOW_S
            win_hi = p_inc["t_last"] + CORRELATION_WINDOW_S
            symptoms = ["PIPE_LEAK"]
            for (tgt, cat), it in list(self.incidents.items()):
                if not (win_lo <= it["t_last"] <= win_hi):
                    continue
                if cat in ("ABNORMAL_SUPPLY", "LEAK_TANK_DRAIN") or (
                    cat == "FLAT_LEAK" and it["evidence"].get("possible_cause") == "PIPE_LEAK"
                ):
                    used.add((tgt, cat))
                    symptoms.append(f"{tgt}:{cat}")
            fl = p_inc["evidence"].get("suspected_floor")
            correlated.append({
                "target": p_inc["target"],
                "category": "PIPE_LEAK",
                "symptoms": sorted(set(symptoms)),
                "t_onset": p_inc["t_onset"],
                "t_last": p_inc["t_last"],
                "repeat_count": p_inc["repeat_count"],
                "affected_flats": self._affected_flats_for_floor(fl),
                "evidence": dict(p_inc["evidence"]),
            })

        # 3. All remaining independent incidents (drop standalone ABNORMAL_SUPPLY if no PIPE_LEAK yet -> keep visible)
        for key, it in self.incidents.items():
            if key in used:
                continue
            tgt, cat = key
            aff = self.total_flats if tgt == self.tank else 1
            correlated.append({
                "target": tgt,
                "category": cat,
                "symptoms": [cat],
                "t_onset": it["t_onset"],
                "t_last": it["t_last"],
                "repeat_count": it["repeat_count"],
                "affected_flats": aff,
                "evidence": dict(it["evidence"]),
            })
        return correlated

    def rank_maintenance(self, now):
        """Score correlated incidents (0-100) and return ranked maintenance queue."""
        queue = []
        for inc in self.correlate_incidents(now):
            cat = inc["category"]
            base = BASE_WEIGHTS.get(cat, 15.0)
            pop_pts = 20.0 * (inc["affected_flats"] / self.total_flats)
            dur_min = max(0.0, (now - inc["t_onset"]) / 60.0)
            ev = inc["evidence"]
            mag_pts = 0.0
            if cat in QUALITY_ALERTS:
                viol_cnt = _popcount(ev.get("viol", 0))
                if "QUALITY_LIMIT_VIOLATION" in inc["symptoms"] or viol_cnt > 0:
                    mag_pts += 10.0 + min(10.0, max(0, viol_cnt - 1) * 5.0)
                if len(inc["symptoms"]) >= 2:
                    mag_pts += 5.0
            elif cat == "PIPE_LEAK":
                mag_pts += min(15.0, float(ev.get("error_pct") or 0.0) / 4.0)
                mag_pts += min(10.0, dur_min / 6.0)
            elif cat == "FLAT_LEAK":
                mag_pts += min(15.0, float(ev.get("flow") or 0.0) * 12.0)
                mag_pts += min(10.0, dur_min / 15.0)
            else:
                mag_pts += min(15.0, dur_min / 15.0 + max(0, inc["repeat_count"] - 1) * 2.0)

            cap = LOW_RISK_CAPS.get(cat, 100.0)
            risk_score = round(min(cap, base + pop_pts + mag_pts), 1)
            queue.append({
                "target": inc["target"],
                "category": cat,
                "risk_score": risk_score,
                "severity": _severity_label(risk_score),
                "affected_flats": inc["affected_flats"],
                "duration_min": round(dur_min, 1),
                "symptoms": inc["symptoms"],
                "evidence": ev,
                "recommended_action": ACTIONS.get(cat, "Inspect node and sensor readings."),
            })
        queue.sort(key=lambda x: (-x["risk_score"], x["target"]))
        for idx, item in enumerate(queue, 1):
            item["rank"] = idx
        return queue

    def snapshot(self, gw, now=None):
        """Return a JSON-serializable gateway analytics snapshot for dashboard/API."""
        if now is None:
            now = gw.hal.now()
        queue = self.rank_maintenance(now)
        top_sev = queue[0]["severity"] if queue else "NORMAL"
        flat_leaks = [it for it in queue if it["category"] == "FLAT_LEAK"]
        consumption = [it for it in queue if it["category"] in ("ABNORMAL_CONSUMPTION", "PROLONGED_ZERO_FLOW")]
        mean_delay = round(self.delay_sum / self.delay_count, 2) if self.delay_count else 0.0
        max_delay = round(self.delay_max, 2)
        nodes = {}
        for n in sorted(self.flats | {self.tank}):
            nodes[n] = {
                "status": "silent" if n in gw.silent else "ok",
                "last_seen": None if gw.last_seen is None else gw.last_seen.get(n),
                "last_sum": self.last_sum.get(n),
            }
        return {
            "t": now,
            "building_status": top_sev,
            "maintenance_queue": queue,
            "active_incidents": self.correlate_incidents(now),
            "incident_history": [dict(it) for it in self.resolved_history],
            "water_quality": dict(self.last_quality),
            "leakage": {
                "pipe_leak_active": gw.pipe_alerted,
                "last_balance_pct": self.last_balance_pct,
                "pipe_leak": self.pipe_leak_info,
                "flat_leaks": flat_leaks,
            },
            "supply": dict(self.last_supply),
            "abnormal_consumption": consumption,
            "network_health": {
                "silent_nodes": sorted(gw.silent),
                "balance_paused": gw.balance_paused,
                "bad_packets": self.bad_packets,
                "skewed_packets": self.skewed_packets,
                "clock_skew_nodes": sorted(gw.skewed),
                "online_alerts": self.online_alerts,
                "offline_alerts": self.offline_alerts,
                "buffered_recovered": self.buffered_recovered,
                "mean_delay_s": mean_delay,
                "max_delay_s": max_delay,
            },
            "nodes": nodes,
        }
