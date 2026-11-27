# Aqua Edge: Gateway

| File | Runs on | What it does |
|---|---|---|
| `core.py` | Gateway: laptop (ESP8266 over USB serial to an SX1278 433 MHz LoRa module) | Combines data from all nodes. Replies to every message (with the time), building check (water in = water out), silent-node alerts, supply-event rule, new vs repeat alerts, clock check, saved state, and delegates analytics to `GatewayAnalytics` |
| `building_B.json` + `topology.py` | Gateway | The building's pipes as a tree of nodes, and its loader and checks ([Building tree](#building-tree)) |
| `analytics.py` | Gateway | Open incidents, closed by explicit recovery messages, cross-node root-cause correlation (tank contamination & main riser leak groups), composite risk scoring (0–100) & ranked maintenance queue, online vs. offline delivery-latency accounting, and `Gateway.snapshot()` JSON export |
| `edge_based.py` | Gateway | Server entry point, edge-based solution: nodes decide their own alerts, and the gateway decides what needs several nodes' data. Will reach the LoRa antenna through an ESP8266. `uv run edge-based-gateway`. **Placeholder, not implemented yet** |
| `cloud_based.py` | Gateway | Server entry point, cloud-based solution: nodes only buffer and stream sensor data, and all ML and every decision happen on this side. Will reach the LoRa antenna through an ESP8266. `uv run cloud-based-gateway`. **Placeholder, not implemented yet** |

The rest of this README describes the gateway logic that exists today, which belongs to the edge-based solution.

Self-checks, from the repository root: `python3 -m gateway.topology`, `python3 -m gateway.core`.

## Building tree

The gateway is told the building's layout as a tree: `Gateway(hal, Topology.load(path))`. The default file is [`building_B.json`](building_B.json). **A parent is upstream of its children:** water passes the parent first. It is JSON because JSON nests naturally and Python reads it with no extra library (TOML is awkward for deep trees; YAML needs a dependency).

```json
{"schema_version": 1, "building": "B",
 "root": {"id": "B-TANK", "type": "tank", "valve": true, "children": [
   {"id": "B-D4", "type": "junction", "label": "4", "valve": false, "children": [
     {"id": "B-401", "type": "flat", "valve": true},
     {"id": "B-D3", "type": "junction", "label": "3", "valve": false, "children": [
       {"id": "B-301", "type": "flat", "valve": true}]}]}]}}
```

The example is cut short. Building B has this shape:

```
B-TANK (tank, valve)
└─ B-D4 "4"        ── B-401 .. B-404
   └─ B-D3 "3"     ── B-301 .. B-304
      └─ B-D2 "2"  ── B-201 .. B-204
         └─ B-D1 "1"     ── B-101 .. B-104
            └─ B-D0 "G"  ── B-G01 .. B-G04
```

| Top-level key | Meaning |
|---|---|
| `schema_version` | `1` |
| `building` | The building's name |
| `root` | The tank entry; everything else hangs under it |

| Entry key | Needed | Meaning |
|---|---|---|
| `id` | Yes | Unique name. For `tank` and `flat` it is the node's radio name |
| `type` | Yes | `tank` (the root, exactly one), `junction` (a point in the pipes with no device) or `flat` |
| `valve` | Yes | `true` if a valve sits on the pipe **just before** this entry. It controls the water to this entry and everything under it |
| `children` | Junctions | The entries fed through this one. Optional for the tank (the tree needs at least one flat); not allowed on a flat, whose meter would then count their water twice |
| `label` | No, junctions only | Short unique name used in gateway events (`"4"` .. `"G"`); defaults to the `id` |

Other keys are rejected, and so is a junction that uses a radio name. The loader stops with a message naming the problem.

**Valve flags in `building_B.json` are an assumption:** `true` for the tank and every flat, `false` for every junction, because no junction valve is documented (the water model has no valves). Set a junction to `true` where one is really fitted. Valve flags are only used to record `isolation_valve` on a pipe leak: the nearest valve at or above the suspected junction. Nothing opens or closes.

**What the gateway takes from the tree:** the list of nodes; which junction feeds each flat (the old "floor", no longer read from the flat's name); what is upstream of what, for locating pipe leaks; who is fed through a suspected junction; and the affected-flat count in the maintenance ranking. Pipe lengths, diameters and heights are not in it.

**Adding a node:** add its entry to the JSON file. If the radio name is new, also append it to the end of `NAMES` in `../hal/packet.py` (packets carry a node's position in that list, so never reorder it), give it a key, and update every device. Restart the gateway to load a changed tree. Saved silence timers and unconfirmed alerts for removed nodes are dropped.

**Limits:** one tank and one tree per gateway. Branching pipes are allowed, and the self-check covers a two-branch example, but the pressure thresholds have only been tested on building B. The building check compares the tank with all flats together, not branch by branch. No program yet starts the gateway on the real laptop.

## Gateway alerts sent to nodes

Nodes act by themselves on local anomalies (triggering their own buzzer/LED immediately without LoRa or Internet) and buffer messages for the gateway. What needs several nodes' data, the gateway also sends back to the affected nodes, as a signed command (`CMD`) repeated until the node confirms it (`GOT`) and kept across gateway restarts. The same results stay available to the dashboard through `Gateway.snapshot()`. Sending is on by default; `Gateway(..., enable_commands=False)` keeps results on the gateway only.

| Gateway result | Sent to |
|---|---|
| Pipe leak, and its end | The tank and every flat fed through the suspected junction (its floor and all below it: a leak at `B-D1` alerts floors 1 and G). **Only the tank** if the spot cannot be told |
| Supply event, leak draining the tank, uncertain supply, and recovery | The tank |
| A flat's pressure alert during a pipe leak | That flat (its leak alert may not be its own leak) |

The spot is decided once, when the pipe leak is first raised. It is an estimate: if the real leak is just above the suspected junction, the floor above is not alerted, and isolating it may need the next valve up. When the spot is unknown, which in tests includes medium (0.5 L/s) pipe leaks, no flat gets the pipe leak alert; a flat with its own pressure alert is still told the likely cause (last row).

Silent nodes, clock errors and new/repeat labels are only recorded. A node acts on each command once, even after a reboot. A new problem sounds the buzzer, turns the LED red and calls `hal.on_gateway_alert(code, active)`. **Nothing closes a valve**; that hook is where it would go. A problem that started and ended before it could be delivered still arrives, marked over, and raises no continuing alarm. A node that is off cannot be reached until it returns.

The beacon is signed with a building-wide key (`hal.key("GATEWAY")`). It only makes nodes send and never deletes data.

## Recovery and restarts

The gateway saves `<storage>_state.json` before sending ACKs (an older `_seen.json` is still read). It keeps 7 days of per-block data. Its in-memory message log and event list are not saved.

The recovery rules are in [`../edge/README.md`](../edge/README.md#recovery-and-restarts).
