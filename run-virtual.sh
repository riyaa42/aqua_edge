#!/usr/bin/env bash
# 4 virtual nodes + gateway + MQTT broker + dashboard, with Podman.
# Usage: ./run-virtual.sh [start|fresh|stop|logs [service]|status]
#   start  (default) start, keeping saved state
#   fresh  delete this setup's saved state (and dashboard history), then start
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

compose() { podman-compose --profile virtual-nodes "$@"; }
nodes="B-TANK B-101 B-203 B-301"

setup() {
    # keys are made only once; they stay outside the repository
    if [ ! -f "$HOME/gateway_keys.json" ]; then
        (umask 077; uv run python -c 'import json, secrets, sys; from gateway.topology import Topology; t = Topology.load(sys.argv[1]); print(json.dumps({n: secrets.token_hex(16) for n in ["GATEWAY", *sorted(t.devices)]}, indent=2))' gateway/virtual_4node.json > "$HOME/gateway_keys.json.tmp")
        mv "$HOME/gateway_keys.json.tmp" "$HOME/gateway_keys.json"
    fi
    for n in $nodes; do
        [ -f "$HOME/${n}_keys.json" ] && continue
        (umask 077; uv run python -c 'import json, sys; k = json.load(open(sys.argv[1])); print(json.dumps({n: k[n] for n in (sys.argv[2], "GATEWAY")}))' "$HOME/gateway_keys.json" "$n" > "$HOME/${n}_keys.json.tmp")
        mv "$HOME/${n}_keys.json.tmp" "$HOME/${n}_keys.json"
    done
    if [ ! -f .env ]; then
        (umask 077; cat > .env <<EOF
MQTT_GATEWAY_PASSWORD=$(openssl rand -hex 16)
MQTT_VIEWER_PASSWORD=$(openssl rand -hex 16)
AQUA_DASHBOARD_TOKEN=$(openssl rand -hex 32)
AQUA_OPERATOR_TOKEN=$(openssl rand -hex 32)
GATEWAY_KEYS=$HOME/gateway_keys.json
GATEWAY_TREE=./gateway/virtual_4node.json
NODE_TANK_KEYS=$HOME/B-TANK_keys.json
NODE_B101_KEYS=$HOME/B-101_keys.json
NODE_B203_KEYS=$HOME/B-203_keys.json
NODE_B301_KEYS=$HOME/B-301_keys.json
NODE_STEP_S=0.4
EOF
        )
    fi
}

start() {
    compose up -d --build
    port=$(sed -n 's/^DASHBOARD_PORT=//p' .env)
    url="http://127.0.0.1:${port:-8000}"
    echo "Waiting for the dashboard..."
    for _ in $(seq 60); do
        if curl -fs "$url/api/v1/health" >/dev/null; then
            echo "Dashboard: $url/"
            echo "Reopen button token: grep AQUA_OPERATOR_TOKEN .env"
            return
        fi
        sleep 1
    done
    echo "The dashboard did not answer. See: ./run-virtual.sh logs dashboard" >&2
    return 1
}

case "${1:-start}" in
    start)  setup; start ;;
    fresh)  setup; compose down -v; start ;;
    stop)   compose down ;;
    logs)   shift; compose logs -f "${@:-gateway}" ;;
    status) podman ps -a --filter name=aqua-edge ;;
    *)      echo "usage: $0 [start|fresh|stop|logs [service]|status]" >&2; exit 2 ;;
esac
