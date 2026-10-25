"""Aqua Edge gateway firmware (one per building, e.g. a Raspberry Pi Zero 2 W with a LoRa HAT).

Same hardware layer as the nodes (hal.now / send / receive). Jobs:
  1. Acknowledge every node message (so nodes can delete it from their SD queue) and log it.
  2. Supply-event rule (our proposal): if many nodes report a pressure drop at about the same
     time, it is a supply problem (pump, power cut), not a leak, so tell nodes not to close valves.
  3. Mass balance (Ali et al. [6], supply vs branch flows): water leaving the tank minus
     water used by all flats, per 15-minute summary; above 10% means a leak in the pipes
     between the tank and the flats (a leak inside a flat is after its meter, so it is not counted).
  4. Repeat check: nodes send a fresh alert every time a problem comes back; the gateway
     marks each one NEW_ALERT or REPEAT_ALERT (same node and alert type as before).
"""
SUPPLY_FRACTION = 0.5          # this share of flat nodes flagging within the window = supply event
SUPPLY_WINDOW_S = 900
SUPPLY_REPEAT_S = 300          # re-broadcast the verdict so nodes that missed it still hear it
MASS_BALANCE_PCT = 10.0        # Ali et al. [6] threshold
MASS_BALANCE_INTERVALS = 2     # intervals in a row above threshold before alerting
MIN_SUPPLY_L = 5.0             # ignore near-empty intervals (division by tiny numbers)


class Gateway:
    def __init__(self, hal, flat_ids, tank_id):
        self.hal, self.flats, self.tank = hal, set(flat_ids), tank_id
        self.seen = set()
        self.log = []               # every unique message received, with receive time
        self.events = []            # gateway's own decisions
        self.alert_history = {}     # (node, alert) -> (time of latest, how many so far)
        self.flags = {}             # node -> time of its latest PRESSURE_LOW flag
        self.supply_active_until, self.last_supply_tx = -1, -1e9
        self.sums = {}              # interval -> {node: volume}
        self.balance_done, self.balance_run, self.pipe_alerted = set(), 0, False

    def step(self):
        now = self.hal.now()
        for m in self.hal.receive():
            if m.get("k") not in ("ALERT", "FLAG", "SUM"):
                continue
            self.hal.send({"k": "ACK", "n": m["n"], "s": m["s"]})
            if (m["n"], m["s"]) in self.seen:
                continue                                    # duplicate (our ACK was lost)
            self.seen.add((m["n"], m["s"]))
            self.log.append({**m, "rx": now})
            if m["k"] == "ALERT" and not m["a"].endswith("_CLEARED"):     # all-clear is not a problem
                self.check_repeat(m)
            elif m["k"] == "FLAG":
                self.flags[m["n"]] = m["t"]
            elif m["k"] == "SUM":
                self.sums.setdefault(m["i"], {})[m["n"]] = m["vol_l"]
        self.supply_rule(now)
        self.mass_balance(now)

    def check_repeat(self, m):
        """Nodes re-send an alert each time a problem comes back; decide if this one is new."""
        key = (m["n"], m["a"])
        prev = self.alert_history.get(key)
        self.alert_history[key] = (m["t"], prev[1] + 1 if prev else 1)
        if prev:
            self.decide(m["t"], "REPEAT_ALERT", node=m["n"], alert=m["a"], count=prev[1] + 1,
                        since_last_min=round((m["t"] - prev[0]) / 60))
        else:
            self.decide(m["t"], "NEW_ALERT", node=m["n"], alert=m["a"])

    def decide(self, now, kind, **info):
        self.events.append({"t": now, "event": kind, **info})

    def supply_rule(self, now):
        recent = [n for n, t in self.flags.items() if n in self.flats and now - t <= SUPPLY_WINDOW_S]
        tank_low = now - self.flags.get(self.tank, -1e9) <= SUPPLY_WINDOW_S     # tank node: level far below normal
        if tank_low or len(recent) >= SUPPLY_FRACTION * len(self.flats):
            if now > self.supply_active_until:
                self.decide(now, "SUPPLY_EVENT", nodes=len(recent), tank_low=tank_low)
            self.supply_active_until = now + SUPPLY_WINDOW_S
        if now <= self.supply_active_until and now - self.last_supply_tx >= SUPPLY_REPEAT_S:
            self.hal.send({"k": "SUPPLY", "t": now})
            self.last_supply_tx = now

    def mass_balance(self, now):
        for i in sorted(self.sums):
            got = self.sums[i]
            if i in self.balance_done or self.tank not in got or not self.flats <= set(got):
                continue                                    # wait until every node's summary for i arrived
            self.balance_done.add(i)
            supply = got[self.tank]
            if supply < MIN_SUPPLY_L:
                continue
            err = 100 * (supply - sum(got[n] for n in self.flats)) / supply
            self.balance_run = self.balance_run + 1 if err > MASS_BALANCE_PCT else 0
            if self.balance_run >= MASS_BALANCE_INTERVALS and not self.pipe_alerted:
                self.pipe_alerted = True
                self.decide(now, "PIPE_LEAK", interval=i, error_pct=round(err, 1))
