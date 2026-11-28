# Aqua Edge: Dashboard & Buffered Backhaul

Receives unified `Gateway.snapshot()` payloads over a store-and-forward Internet buffer from either the **edge-based** (`mode="edge"`) or **cloud-based** (`mode="cloud"`) gateway and renders building status, ranked maintenance priorities, pipe-tree health, and telemetry history with mode-specific visual theming.

**What exists:** the server's API (`server.py`, `store.py`) and the gateway's buffered sender (`shipper.py`, used by [`../gateway/dashboard_link.py`](../gateway/dashboard_link.py)). **There is no web page yet:** the data is read as JSON or as a live stream. The contract for sending is in [`API_SPEC.md`](API_SPEC.md).

## Running it

**With the gateway and broker:** [Docker Compose](../gateway/README.md#run-with-docker-compose). The API is then at `http://127.0.0.1:8000`.

**On its own,** from the repository root:

```sh
AQUA_DASHBOARD_TOKEN=<secret> uv run dashboard-server
uv run edge-based-gateway --keys /full/path/gateway_keys.json --dashboard-url http://127.0.0.1:8000/api/v1/ingest   # same AQUA_DASHBOARD_TOKEN set
```

| Variable | Meaning |
|---|---|
| `AQUA_DASHBOARD_TOKEN` | Needed to send. Run on its own, the server uses `dev-token` if unset, which anyone can guess; Docker Compose has no default |
| `AQUA_DASHBOARD_DB` | Database file, default `dashboard/run/dashboard.db` |
| `AQUA_DASHBOARD_HOST`, `AQUA_DASHBOARD_PORT` | Where `dashboard-server` listens, default `127.0.0.1:8000` |
| `AQUA_TOPOLOGY_PATH` | The [building tree](../gateway/README.md#building-tree) served at `/api/v1/topology` |
| `AQUA_ENABLE_OUTAGE_SIM` | Allows `POST /api/v1/sim/outage`, which makes the server refuse data, for outage demos. On by default on its own, off in Docker Compose |

**Reading needs no token.** Anyone who can reach the server can read everything, so keep it on `127.0.0.1` unless something in front of it adds a login and TLS.

| To read | Address |
|---|---|
| Health, and which gateways have sent | `GET /api/v1/health`, `GET /api/v1/gateways` |
| Newest snapshot | `GET /api/v1/snapshots/latest` |
| Tank and balance history | `GET /api/v1/telemetry` |
| Incidents | `GET /api/v1/incidents` |
| Building tree | `GET /api/v1/topology` |
| Live stream of new snapshots (server-sent events) | `GET /api/v1/stream` |

## How the gateway sends

The gateway takes a snapshot at least every 60 s, and within about 2 s of a new decision (no closer than 2 s apart). A separate thread saves each one to disk and then sends it, so **a slow or unreachable dashboard never delays the radio**. Unsent snapshots stay in `dashboard-outbox/` in the gateway's data folder, across restarts, and go in order when the dashboard returns, retried at most about 30 s apart.

- **Limits:** the shipper stops adding at 50 MB of unsent snapshots, or with under 100 MB of free disk. Snapshots are then skipped; the gateway holds up to 1,000 of their decisions in memory for the next stored one.
- **A crash** can lose what had not yet reached the disk. A normal stop saves it, unless the sender is stuck waiting for an answer.
- **Keep the two data stores together.** The server ignores a frame whose gateway name and number it already has. If the gateway's data is deleted but the dashboard's database is kept, numbering starts again and **new data is silently ignored** until the numbers pass the old ones. Delete both, or use a new `--dashboard-id`.

Tests: `uv run test-dashboard`.
