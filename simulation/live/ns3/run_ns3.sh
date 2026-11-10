#!/bin/sh
# ns-3 for Aqua Edge, inside Docker (nothing to install by hand). Run from anywhere:
#   sh ns3/run_ns3.sh            link table -> ns3/link_table.csv (used by live/radio.py)
#   sh ns3/run_ns3.sh replay     replay live/run/tx_log.csv -> ns3/replay_result.csv (read by check_comms.py)
set -e
cd "$(dirname "$0")/.."
docker build -q -t aqua-ns3 ns3/ >/dev/null          # ~20-30 min the first time, then cached
python3 ns3/positions.py
if [ "$1" = replay ]; then
    args="--replay=/sim/live/run/tx_log.csv --out=/sim/ns3/replay_result.csv"
else
    args="--out=/sim/ns3/link_table.csv"
fi
out=$(echo "$args" | sed 's/.*--out=//')
docker run --rm -v "$PWD":/sim aqua-ns3 sh -c "cp /sim/ns3/aqua_lora.cc scratch/ \
    && ./ns3 run --quiet 'aqua_lora --positions=/sim/ns3/positions.csv $args' && chown $(id -u):$(id -g) $out"
echo "done: $args"
