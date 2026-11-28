"""The building check (water in = water out), after Ali et al. [6]: water leaving the tank minus
water used by all flats, per 15-minute summary. Above 10% for 2 blocks next to each other in time
means a leak in the pipes between the tank and the flats. It needs every node's summary, so the
gateway runs it. How it works and how well it did: README.md in this folder.

PipeLeakCheck is part of gateway.core.Gateway, which fills `sums`, `p_sums` and `p_mean_sums` from
the summaries it receives, saves the state named here, and provides: tree, tank, flats, silent,
analytics, decide() and end_pipe_leak().
"""
from detection.leakage.localize import cross_floor_drops, localize_floor
from edge.aqua_edge import SUMMARY_S

MASS_BALANCE_PCT = 10.0        # Ali et al. [6] threshold
MASS_BALANCE_INTERVALS = 2     # blocks in a row above threshold before alerting, and normal before re-arming
MIN_SUPPLY_L = 5.0             # ignore near-empty blocks (division by tiny numbers)
KEEP_BLOCKS = 7 * 86400 // SUMMARY_S    # per-block data kept: 7 days, for late summaries


class PipeLeakCheck:
    def init_pipe_check(self):
        self.sums = {}              # interval -> {node: volume}
        self.p_sums = {}            # interval -> {flat: p_min}
        self.p_base = {}            # flat -> clean baseline p_min (EWMA)
        self.p_mean_sums = {}       # interval -> {flat: p_mean}
        self.p_mean_base = {}       # flat -> clean baseline p_mean (EWMA)
        self.balance_done = set()
        self.verdicts = {}          # interval -> (leaking, error %) of each block balanced
        self.reported = set()       # leaking blocks already covered by a PIPE_LEAK(_PAST)
        self.last_pair = -1         # first block of the newest pair of equal verdicts
        self.pipe_alerted = self.balance_paused = False
        self.pipe_first = None      # first block of the active pipe leak
        self.pipe_since = None      # its start time

    def pressure_drops(self, interval=None):
        """Each section's pressure drop below its clean baseline, in one block (default: newest)."""
        return cross_floor_drops(self.tree, self.p_sums, self.p_base, self.p_mean_sums,
                                 self.p_mean_base, interval)

    def mass_balance(self, now):
        # One side only, or a tank that also feeds homes with no node: nothing to compare. All of
        # that water would otherwise look like a pipe leak
        if not (self.tank and self.flats) or self.tree.unmetered_homes:
            self._forget_old_blocks()
            return
        if self.silent and not self.balance_paused:
            self.balance_paused = True
            self.decide(now, "BALANCE_PAUSED", silent=sorted(self.silent))
        elif not self.silent:
            self.balance_paused = False
        for i in sorted(self.sums):
            got = self.sums[i]
            if i in self.balance_done or self.tank not in got or not self.flats <= set(got):
                continue                                    # wait until every node's summary for i arrived
            self.balance_done.add(i)
            supply = got[self.tank]
            if supply < MIN_SUPPLY_L:
                continue
            err = 100 * (supply - sum(got[n] for n in self.flats)) / supply
            self.analytics.last_balance_pct = round(err, 2)
            leaking = err > MASS_BALANCE_PCT
            if not leaking and not self.pipe_alerted and i in self.p_sums:
                for f, p_v in self.p_sums[i].items():
                    self.p_base[f] = p_v if f not in self.p_base else 0.9 * self.p_base[f] + 0.1 * p_v
                for f, p_v in self.p_mean_sums.get(i, {}).items():
                    self.p_mean_base[f] = p_v if f not in self.p_mean_base else 0.9 * self.p_mean_base[f] + 0.1 * p_v
            self.verdicts[i] = (leaking, round(err, 1))
            self._extend_leak_back()
            # Only two blocks next to each other in time decide anything, in whatever order
            # their summaries arrived
            for first in (i - 1, i):
                if first in self.verdicts and first + 1 in self.verdicts \
                        and self.verdicts[first][0] == self.verdicts[first + 1][0]:
                    self._judge_pair(now, first, self.verdicts[first][0])
        self._forget_old_blocks()

    def _extend_leak_back(self):
        """Late summaries can show that the active leak began earlier than first known."""
        if not self.pipe_alerted:
            return
        while self.verdicts.get(self.pipe_first - 1, (False,))[0]:
            self.pipe_first -= 1
            self.reported.add(self.pipe_first)
        self.pipe_since = self.pipe_first * SUMMARY_S

    def _judge_pair(self, now, first, leaking):
        newest = first >= self.last_pair
        self.last_pair = max(first, self.last_pair)
        if not leaking:
            if newest and self.pipe_alerted:
                self.pipe_alerted = False                   # re-arm: a later leak alerts again
                self.analytics.clear_pipe_leak(t_resolved=now)
                self.end_pipe_leak(now)
            return
        pair, error_pct = {first, first + 1}, self.verdicts[first + 1][1]
        if newest and not self.pipe_alerted:
            self.pipe_alerted, self.pipe_first = True, first
            self.reported |= pair
            self._extend_leak_back()
            drops = self.pressure_drops(first + 1)
            suspected, floor_range, conf = localize_floor(self.tree, drops)
            self.decide(now, "PIPE_LEAK", interval=first + 1, error_pct=error_pct,
                        suspected_floor=suspected, suspected_range=floor_range,
                        localization_confidence=conf, floor_drops=drops)
            return
        if not newest and not pair & self.reported:         # an earlier leak, known only now
            suspected, floor_range, conf = localize_floor(self.tree, self.pressure_drops(first + 1))
            self.decide(now, "PIPE_LEAK_PAST", interval=first + 1, error_pct=error_pct,
                        began_t=first * SUMMARY_S, suspected_floor=suspected, suspected_range=floor_range,
                        localization_confidence=conf)
        self.reported |= pair

    def _forget_old_blocks(self):
        if not self.sums:
            return
        oldest = max(self.sums) - KEEP_BLOCKS
        for data in (self.sums, self.p_sums, self.p_mean_sums, self.verdicts):
            for i in [i for i in data if i < oldest]:
                del data[i]
        self.balance_done = {i for i in self.balance_done if i >= oldest}
        self.reported = {i for i in self.reported if i >= oldest}
