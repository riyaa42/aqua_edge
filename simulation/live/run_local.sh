#!/bin/sh
# Run the live simulation on this laptop: timekeeper + gateway + one process per node.
#   sh live/run_local.sh --scenario live/scenarios/standard.jsonl
# Arguments go to the timekeeper (see live/timekeeper.py). Run from simulation/.
cd "$(dirname "$0")/.."
rm -rf live/run
uv run python live/timekeeper.py "$@" &
while [ ! -f live/run/nodes.txt ]; do sleep 0.2; done
for n in $(cat live/run/nodes.txt) GATEWAY; do
    NODE_ID=$n python3 live/node.py &
done
wait
