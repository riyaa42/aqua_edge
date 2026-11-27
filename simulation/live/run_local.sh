#!/bin/sh
# Run the live simulation on this laptop: timekeeper + gateway + one process per node.
#   sh live/run_local.sh --scenario live/scenarios/standard.jsonl
# Arguments go to the timekeeper (see live/timekeeper.py). Run from simulation/.
cd "$(dirname "$0")/.."
RUN=${AQUA_RUN:-live/run}; rm -rf "$RUN"
uv run python live/timekeeper.py "$@" &
while [ ! -f "$RUN/nodes.txt" ]; do sleep 0.2; done
for n in $(cat "$RUN/nodes.txt") GATEWAY; do
    NODE_ID=$n python3 live/node.py &
done
wait
