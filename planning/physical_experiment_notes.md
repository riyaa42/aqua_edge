# Physical 4-Node Experiment & Pre-Experiment Fixes

Notes on `gateway/physical_4node.json`, simulated-time summary alignment, and `unmetered_homes` mass-balance behaviour for live hardware experiments and the separate Cloud baseline branch.

## 1. Four-Node Topology (`gateway/physical_4node.json`)

- **Assignment:** `B-203` on the physical Raspberry Pi (LoRa + GPIO); `B-TANK`, `B-101`, and `B-102` in Docker containers (UDP + simulated actuators).
- **Hierarchy:** `B-TANK` (`valve: true`, `unmetered_homes: true`) → `B-D2` (`"2"`) → `B-203` (`valve: true`) + `B-D1` (`"1"`) → `B-101` (`valve: true`), `B-102` (`valve: true`), matching `gateway/building_B.json`.
- **Docker Compose note:** `compose.yaml` provides `node-tank`, `node-b101`, and `node-b203`, but no `node-b102` service. Do not use `--profile three-nodes` when `B-203` runs on the physical Pi; start `mqtt`, `gateway`, `dashboard`, `node-tank`, `node-b101` and run `B-102` via `docker compose run -d --no-deps ... node-b101 python -m edge.edge_based B-102 ...`.
- **Keys:** Generate `gateway_keys.json` and per-node `*keys.json` locally using `hal/packet.py` / `secrets.token_hex(16)` (`*keys.json` is git-ignored).

## 2. Simulated-Time Summary Aggregation (`edge/aqua_edge.py`)

- **Fix:** `Node.step()` now calls `self.summarise(td, r)` using `td = self.sample_time()` instead of wall-clock `hal.now()`.
- **Behaviour:**
  - 15-minute `SUM` block indices (`i = int(td // 900)`) align to dataset time across all nodes at both `--step-s 1` and `--step-s 60`, even if node processes start at different wall-clock times.
  - Across Day 2 of `simulation/webots/data/sensors.csv` (`t = 86400..172740`, `1440` rows) after `--prime-s 86400`, each node emits `95` complete `SUM` blocks (`96..190`, or `96` when a boundary row at `t = 172800` follows).
  - Packet metadata (`m["t"]`, `ts_utc`, `ts_iso`) and real monotonic operational timers (`next_try`, `AirtimeBudget`, `STUCK_S`, `STALE_S`, `HEARD_S`) are unchanged.

## 3. `unmetered_homes` Mass-Balance Behaviour

- In `sensors.csv`, `B-TANK` supplies all 20 Building B flats, while `physical_4node.json` monitors only 3 flats (`B-203`, `B-101`, `B-102`). Day-1 unmetered consumption varies from `8.7 L` to `249.2 L` per 15-minute block (`std = 70.1 L`), exceeding the `10%` `MASS_BALANCE_PCT` threshold.
- `unmetered_homes: true` in `detection/leakage/mass_balance.py` is preserved: it safely disables `mass_balance()` (`last_balance_pct` stays `None`, no false `PIPE_LEAK`).
- `physical_4node.json` therefore validates multi-node LoRa + UDP communication, tank water-quality/supply alerts, flat leaks, valve actuation, and dashboard correlation—**not** riser `PIPE_LEAK` detection. Demonstrating `PIPE_LEAK` requires a separate fully metered topology and matching dataset (`unmetered_homes: false`).

## 4. Compatibility with the Separate Cloud Baseline Branch

- No packet formats (`hal/packet.py`), radio interfaces (`hal/`), topology schema (`gateway/topology.py`), or `simulation/webots/data/sensors.csv` were modified.
- The Cloud baseline branch can pull these commits cleanly from `main` without breaking shared HAL, topology, or dataset contracts.
