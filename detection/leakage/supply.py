"""The supply rule: when the tank reports its level far below normal (SUPPLY_LOW), is it the supply
(pump or power), a leak draining the tank, or unclear? It weighs the tank's flag against its inflow,
the flats' pressure and any leak evidence, from several nodes, so the gateway runs it. How it works:
README.md in this folder.

SupplyRule is part of gateway.core.Gateway, which records the tank's flags and the leak evidence
(tank_*, flat_leak_*, last_tank_inflow), saves the state named here, and provides: pipe_alerted,
pressure_drops() (mass_balance.py) and decide().
"""
SUPPLY_WINDOW_S = 900          # a SUPPLY_LOW flag from the tank counts for this long
SUPPLY_CODES = ("SUPPLY_EVENT", "LEAK_TANK_DRAIN", "SUPPLY_UNCERTAIN")


class SupplyRule:
    def init_supply_rule(self):
        self.tank_low_t, self.tank_low_recv_t, self.supply_active_until = -1e9, -1e9, -1
        self.tank_drain_reported = False
        self.tank_abnormal_supply_recv_t, self.flat_leak_recv_t = -1e9, -1e9
        self.tank_abnormal_supply_t, self.flat_leak_t = -1e9, -1
        self.last_tank_inflow = None
        self.supply_cleared_t = -1e9    # tank time of the latest SUPPLY_RECOVERED

    def supply_rule(self, now):
        """Multi-signal supply classification: distinguishes supply interruption from leak-induced tank drain."""
        if now - self.tank_low_recv_t <= SUPPLY_WINDOW_S:
            leak_ev = (
                self.pipe_alerted
                or (now - self.tank_abnormal_supply_recv_t <= SUPPLY_WINDOW_S)
                or (abs(self.tank_abnormal_supply_t - self.tank_low_t) <= SUPPLY_WINDOW_S)
                or (now - self.flat_leak_recv_t <= SUPPLY_WINDOW_S)
                or (abs(self.flat_leak_t - self.tank_low_t) <= SUPPLY_WINDOW_S)
            )
            if leak_ev and not self.tank_drain_reported:
                self.tank_drain_reported = True
                self.decide(now, "LEAK_TANK_DRAIN", tank_low=True, inflow_lps=self.last_tank_inflow, event_t=self.tank_low_t)
                self.supply_active_until = now + SUPPLY_WINDOW_S
                return
            if now > self.supply_active_until:
                inflow_low = self.last_tank_inflow is not None and self.last_tank_inflow < 0.1
                inflow_active = self.last_tank_inflow is not None and self.last_tank_inflow >= 0.1
                drops = self.pressure_drops()
                uniform_drop = len(drops) >= 2 and sum(1 for d in drops.values() if d > 0.2) >= max(len(drops) - 1, 1)
                if inflow_low or (not inflow_active and uniform_drop):
                    self.decide(now, "SUPPLY_EVENT", tank_low=True, inflow_lps=self.last_tank_inflow, floor_drops=drops, event_t=self.tank_low_t)
                else:
                    self.decide(now, "SUPPLY_UNCERTAIN", tank_low=True, inflow_lps=self.last_tank_inflow, event_t=self.tank_low_t)
                self.supply_active_until = now + SUPPLY_WINDOW_S
        else:
            self.tank_drain_reported = False
        self.tank_abnormal_supply_recv_t, self.flat_leak_recv_t = -1e9, -1e9

    def supply_recovered(self, now, m):
        if m["t"] < self.tank_low_t:
            return                  # an old recovery arriving after a newer low-tank flag
        self.supply_rule(now)       # a low flag delivered together with this still gets decided
        self.supply_cleared_t = m["t"]
        self.tank_low_recv_t, self.supply_active_until, self.tank_drain_reported = -1e9, -1, False
        self.decide(now, "SUPPLY_RECOVERED", event_t=m["t"])
