#!/bin/sh
# Download the SWI-FEED NET_6 network and published results (Pagano et al. [5], EPANET and NS-3
# co-simulation of a water network) into data/swifeed/. Run from the simulation/ folder.
set -e
mkdir -p data/swifeed
cd data/swifeed
B=https://raw.githubusercontent.com/WITS-Restart/WDN-IoT-Dataset-Workbench/main
for p in dataset-network-epanet-src/NET_6.inp \
         dataset-network-epanet-src/NET_6_topology.png \
         dataset-network-out/NET_6/h_24/NET_6_bd_0_005_h_24_links.csv \
         dataset-network-out/NET_6/h_24/NET_6_bd_0_005_h_24_nodes.csv \
         dataset-network-out/NET_6/h_24/NET_6_bd_pattern_h_24_leak_area_0_links.csv \
         dataset-network-out/NET_6/h_24/NET_6_bd_pattern_h_24_leak_area_0_nodes.csv; do
    curl -sSfL --retry 5 --retry-all-errors -O "$B/$p"
done
ls -la
