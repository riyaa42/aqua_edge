"""The radio link: one SX1278 at 433 MHz on every node and on the gateway. Settings, time on
air, the transmit-time limit and the retry timing, shared by the nodes, the gateway and the
simulation. Standard library only.

    python3 -m hal.lora433      # self-check

One SX1278 listens on one channel and one spreading factor at a time and cannot receive while
it sends, so the whole building shares one channel.

Power and transmit-time limits follow India's WPC G.S.R. 347(E) (May 9, 2022) licence
exemption for 433.05-434.79 MHz: 10 mW ERP (10 dBm) max, 10% duty cycle, no 10 kHz
bandwidth cap (125 kHz LoRa allowed), and integral/built-in antenna requirement. TX_DBM is
the module's output setting: radiated power (ERP) also depends on antenna gain and cable
loss, and WPC Equipment Type Approval (ETA) applies to commercial/imported hardware.
"""
import math
from collections import deque

FREQ_HZ = 433_175_000
BW_HZ, SF, CODING_RATE, PREAMBLE = 125_000, 7, 1, 8      # 125 kHz, SF7, 4/5, 8 symbols
TX_DBM = 10
DUTY_LIMIT, DUTY_ROUTINE = 0.10, 0.08   # share of an hour on air: alerts may use 10%, routine 8%
DUTY_WINDOW_S = 3600

# Delivery timing. The gateway beacons so that a node knows when it can be heard.
BEACON_S = 5
HEARD_S = 15                    # no beacon or reply for this long: the gateway counts as away
AWAY_ALERT_S = 30               # still try now and then (the downlink alone may be broken)
AWAY_ROUTINE_S = 300
FLUSH_STEP_S = 0.15             # gateway back: each node starts this much after the one before
FLUSH_ROUTINE_AFTER_S = 3.5     # stored summaries start after every node's stored alerts


def airtime_s(nbytes, sf=SF, bw=BW_HZ):
    """Time on air of one LoRa packet (Semtech AN1200.13): explicit header, CRC on."""
    t_sym = 2 ** sf / bw
    de = 1 if sf >= 11 and bw == 125_000 else 0          # low data rate optimisation
    n = 8 + max(math.ceil((8 * nbytes - 4 * sf + 28 + 16) / (4 * (sf - 2 * de))) * (CODING_RATE + 4), 0)
    return (PREAMBLE + 4.25) * t_sym + n * t_sym


def retry_wait(tries, urgent, heard, rng):
    """Seconds to wait for a reply to send number tries+1 before sending again. Urgent (alerts,
    tank flags, gateway commands): 1-2 s, doubling to 4-8 s while the other end is there."""
    if not heard:
        longest = AWAY_ALERT_S if urgent else AWAY_ROUTINE_S
        return rng.uniform(0.85 * longest, longest)
    shortest = min(2 ** min(tries, 5), 4) if urgent else min(10 * 2 ** min(tries, 5), 30)
    return rng.uniform(shortest, 2 * shortest)


class AirtimeBudget:
    """This radio's time on air over the last hour."""

    def __init__(self):
        self.sent, self.total = deque(), 0.0

    def take(self, now, seconds, urgent):
        """Record a transmission and return True, or False if it would break the limit."""
        while self.sent and self.sent[0][0] <= now - DUTY_WINDOW_S:
            self.total -= self.sent.popleft()[1]
        if self.total + seconds > (DUTY_LIMIT if urgent else DUTY_ROUTINE) * DUTY_WINDOW_S:
            return False
        self.sent.append((now, seconds))
        self.total += seconds
        return True

    def refund_last(self):
        """Undo the most recent take(), e.g. when the radio failed to send."""
        if self.sent:
            _, seconds = self.sent.pop()
            self.total = max(0.0, self.total - seconds)


if __name__ == "__main__":
    import random
    assert abs(airtime_s(75, 7) - 0.1334) < 0.001
    rng = random.Random(1)
    for tries, lo in enumerate([1, 2, 4, 4, 4]):
        assert all(lo <= retry_wait(tries, True, True, rng) <= 2 * lo for _ in range(100))
    assert retry_wait(9, True, False, rng) <= AWAY_ALERT_S and retry_wait(0, False, False, rng) <= AWAY_ROUTINE_S
    b = AirtimeBudget()
    assert all(b.take(i, 1.0, False) for i in range(288))                     # 8% of an hour
    assert not b.take(300, 1.0, False) and b.take(300, 1.0, True)             # alerts still go
    assert all(b.take(300, 1.0, True) for _ in range(71)) and not b.take(300, 1.0, True)
    assert b.take(4000, 1.0, False), "old transmissions drop out of the hour"
    beacon = airtime_s(14) / BEACON_S
    print(f"lora433 self-check passed (beacon uses {100 * beacon:.2f}% of the gateway's time)")
