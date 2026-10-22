"""Write the NET_6 files for opening in the EPANET program (epanet_gui/), all set to 24 hours.

    NET_6_normal.inp            the SWI-FEED network as published (Pagano et al. [5])
    NET_6_leak_6562.inp         a 4 L/s leak at junction 6562 (a dead end: only that node loses pressure)
    NET_6_burst_1016_2222.inp   two main-pipe bursts on the far left (a visible low-pressure zone)

EPANET cannot start a leak part-way through a run, so leaks here run from hour 0.
Needs data/swifeed/NET_6.inp (scripts/get_swifeed_data.sh). Run from the simulation/ folder.
"""
import math
import wntr

SRC, OUT = "data/swifeed/NET_6.inp", "epanet_gui/"


def network():
    wn = wntr.network.WaterNetworkModel(SRC)
    wn.options.time.duration = 23 * 3600
    return wn


if __name__ == "__main__":
    wntr.network.write_inpfile(network(), OUT + "NET_6_normal.inp")

    wn = network()  # leak = EPANET emitter, Q = C * sqrt(pressure); same hole as leak_rules.py (2e-4 m2, Cd 0.75)
    wn.get_node("6562").emitter_coefficient = 0.75 * 2e-4 * math.sqrt(2 * 9.81)
    wntr.network.write_inpfile(wn, OUT + "NET_6_leak_6562.inp")

    wn = network()  # ~125-175 L/s bursts on 10-inch mains far from the main source
    for n in ("1016", "2222"):
        wn.get_node(n).emitter_coefficient = 0.03
    wntr.network.write_inpfile(wn, OUT + "NET_6_burst_1016_2222.inp")
    print("wrote epanet_gui/NET_6_normal.inp, NET_6_leak_6562.inp, NET_6_burst_1016_2222.inp")
