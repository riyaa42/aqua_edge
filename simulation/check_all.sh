#!/bin/sh
# Every Aqua Edge check in one go. Stops at the first failure. About 4 minutes.
#   sh simulation/check_all.sh        (from anywhere)
# Run it after any change to firmware/ or simulation/ and before pushing.
set -e
cd "$(dirname "$0")"
step() { printf '\n=== %s ===\n' "$1"; }

step "gateway self-check (re-arm, silent node, clock)"
python3 ../firmware/gateway.py
step "radio self-check"
python3 live/radio.py
step "water engine self-check"
uv run python live/water.py
step "offline firmware test (planted events, no false valve closures)"
out=$(python3 webots/run_offline.py 2>&1) || { echo "$out"; exit 1; }   # on failure, show the full report
echo "$out" | tail -3
step "leak comparison scoring self-check"
uv run python ../leak_detection/compare_leaks.py --check
for s in standard flow_leaks; do
    step "live simulation: $s scenario"
    sh live/run_local.sh --scenario live/scenarios/$s.jsonl > /dev/null 2>&1
    out=$(python3 live/check_comms.py 2>&1) || { echo "$out"; exit 1; }
    echo "$out" | grep -E "^(PASS|FAIL)"
done
printf '\nAll checks passed.\n'
