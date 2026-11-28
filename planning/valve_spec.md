# Aqua Edge — Three-LED & Valve Control Specification

## 1. Objective

Define a simple visual communication system using three physical LEDs (`GREEN`, `YELLOW`, and `RED`) on each Raspberry Pi edge node.

The LEDs must communicate:
- Overall operational condition of the node.
- Whether attention is required.
- The broad category of the problem.

The system uses both LED colour and blinking pattern to communicate information, rather than using colour alone.

The dashboard provides detailed diagnostics; the physical LEDs provide an immediate indication of node condition.

---

## 2. LED States and Meanings

### GREEN — Normal Operation

**Pattern: Solid ON**

Meaning:
- No active local alerts.
- No unresolved problems.
- No communication or delivery warning.
- Node is operating normally.

### YELLOW — Attention Required

Yellow indicates a warning or degraded condition that does not currently constitute an active critical problem.

**Pattern Y1 — One blink**

Sequence:
- ON: `200 ms`
- OFF: `1800 ms`
- Repeat.

Meaning: Communication and packet-delivery issues.

Examples:
- Packets waiting in the SD-card outbox.
- Packet unacknowledged for more than `30 s` (`STUCK_S`).
- Delayed acknowledgements.
- Recoverable LoRa communication problems.

**Pattern Y2 — Two blinks**

Sequence:
- ON: `200 ms`
- OFF: `200 ms`
- ON: `200 ms`
- OFF: `1400 ms`
- Repeat.

Meaning: Sensor and monitoring issues.

Examples:
- Missing sensor readings.
- Stale sensor data.
- Sensor availability problems.
- Clock synchronisation warnings.

### RED — Active Problem

Red indicates an active water-system or operational problem requiring attention.

**Pattern R1 — One blink**

Sequence:
- ON: `200 ms`
- OFF: `1800 ms`
- Repeat.

Meaning: Water-quality problems.

Examples:
- IS 10500 parameter violations.
- `QUALITY_LIMIT_VIOLATION`.
- `QUALITY_TEMPORAL_ANOMALY`.
- `QUALITY_RUNOFF`.
- `QUALITY_CHEMICAL`.

**Pattern R2 — Two blinks**

Sequence:
- ON: `200 ms`
- OFF: `200 ms`
- ON: `200 ms`
- OFF: `1400 ms`
- Repeat.

Meaning: Water distribution and consumption problems.

Examples:
- `LEAK_NIGHT_FLOW`.
- `LEAK_CONTINUOUS_FLOW`.
- `LEAK_PRESSURE_DROP`.
- `PIPE_LEAK`.
- `ABNORMAL_SUPPLY`.
- `SUPPLY_EVENT`.
- `LEAK_TANK_DRAIN`.
- `ABNORMAL_CONSUMPTION`.
- `UNUSUAL_NIGHT_CONSUMPTION`.
- `SUDDEN_CONSUMPTION_CHANGE`.
- `PROLONGED_ZERO_FLOW`.

---

## 3. Pattern Summary

| LED | Pattern | Meaning |
|---|---|---|
| Green | Solid ON | Normal operation |
| Yellow | One blink | Communication / packet-delivery warning |
| Yellow | Two blinks | Sensor / monitoring warning |
| Red | One blink | Water-quality problem |
| Red | Two blinks | Leakage / supply / consumption problem |

The same blinking patterns are deliberately reused across colours. Colour communicates severity, while blink count communicates the problem category.

---

## 4. Priority and Conflict Resolution Rules

Only one LED colour and one pattern should be displayed at a time.

Use the following priority:
1. **RED** — Active problem.
2. **YELLOW** — Warning or degraded operation.
3. **GREEN** — Normal operation.

Rules:
- Any active red condition overrides all yellow and green conditions.
- If no red condition exists, any active yellow condition overrides green.
- Green is displayed only when no active red or yellow conditions exist.
- When a higher-priority condition clears, the LED must return to the next applicable lower-priority state.
- Clearing one alert must not clear other independently active alerts.
- The LED state must reflect currently active conditions, not merely the most recently received event.

---

## 5. Multiple Conditions Within the Same Colour

If multiple conditions exist within the same severity category:
- **Water-quality problems** take precedence over **distribution problems** for red pattern selection (`R1` over `R2`).
- **Communication/delivery problems** take precedence over **sensor/monitoring warnings** for yellow pattern selection (`Y1` over `Y2`).

Examples:
- Chemical contamination + pipeline leakage → `RED`, one blink (`R1`).
- Pipeline leakage + pending SD packet → `RED`, two blinks (`R2`).
- Pending SD packet + missing sensor reading → `YELLOW`, one blink (`Y1`).
- After the pipeline leak clears while the SD packet remains pending → `YELLOW`, one blink (`Y1`).
- Once all conditions clear → `GREEN`, solid ON.

The selected pattern should remain stable until the underlying active-condition set changes. Do not rapidly alternate patterns as new events arrive.

---

## 6. Buzzer Behaviour

The buzzer is independent of the LED pattern:
- **GREEN:** No buzzer.
- **YELLOW:** No buzzer.
- **RED:** Intermittent buzzer.

The buzzer should not sound continuously during prolonged incidents. Acknowledgement or muting must not automatically clear the underlying alert or change the LED status.

---

## 7. Valve Closure Mapping & Gateway Valve Signaling

### RED LED & Valve Closure Rule
- **RED (`R1` or `R2`) = Close Valve:** When a physical shutoff valve is fitted, any active **RED** condition (local detection or gateway `CMD`) commands the valve **closed**.
- **YELLOW (`Y1` or `Y2`) & GREEN = Keep Valve Open:** Communication/sensor warnings (`YELLOW`) and normal operation (`GREEN`) must **not** close the water valve.
- **Explicit Re-Open Only:** The valve re-opens only when all active `RED` conditions on that node clear via explicit recovery messages (`*_CLEARED` / `PIPE_LEAK_CLEARED` / `SUPPLY_CLEARED`), never on silence, `ACK`, or buzzer muting.
- **Current Hardware Status:** In software today, `hal.on_gateway_alert(code, active)` is a callback hook; no physical valve hardware is attached yet (`RED` drives the LED and buzzer).

### How the Gateway Signals Valve Closure (`gateway/core.py` → `edge/aqua_edge.py`)
1. **Targeted Dispatch (`Gateway.dispatch`):** When the gateway detects a cross-node issue (`PIPE_LEAK`, `SUPPLY_EVENT`, `LEAK_TANK_DRAIN`, `SUPPLY_UNCERTAIN`, or `PIPE_LEAK_CAUSE`), it queues a signed `CMD` packet (`over=0`) to **only the affected nodes**:
   - `PIPE_LEAK`: tank + flats fed through the suspected junction (`Topology.flats_below()`), or **tank only** if the leak cannot be localized.
   - `SUPPLY_EVENT` / `LEAK_TANK_DRAIN` / `SUPPLY_UNCERTAIN`: tank node.
   - `PIPE_LEAK_CAUSE`: the specific flat with a pressure-drop alert during a pipe leak.
2. **Reliable Delivery (`CMD` / `GOT`):** The gateway repeats queued `CMD`s from its persisted `outbox` until each target node replies with a signed `GOT` confirmation.
3. **Node Execution (`Node.on_gateway_event`):** On receiving a `CMD` (`over=0`), the node records `self.remote[code] = t` in `<node>_open.json`, switches the LED to **RED**, sounds the intermittent buzzer, and calls **`hal.on_gateway_alert(code, True)`** (where the valve driver closes the valve).
4. **Gateway Recovery (`*_CLEARED`):** When the gateway confirms recovery (e.g., 2 tight mass-balance blocks for `PIPE_LEAK_CLEARED`, or `SUPPLY_RECOVERED` for `SUPPLY_CLEARED`), it sends the clearing `CMD`. The node removes `code` from `self.remote`, calls **`hal.on_gateway_alert(code, False)`** to reopen the valve (if no other `RED` condition remains active), and transitions the LED back to `YELLOW` or `GREEN`.
5. **Missed Historical Events (`over=1`):** If an incident started and ended before delivery, the `CMD` arrives marked `over=1` and is logged in `self.missed` without closing the valve.

---

## 8. Important Operational Rules

- A node can only indicate conditions it knows about through its local detection or gateway messages.
- A flat node must not indicate a water-quality problem detected by a separate tank node unless that information is explicitly communicated to it.
- Gateway-originated remote alerts must be considered when selecting the local LED state and valve state.
- Informational gateway messages must not automatically trigger `RED` or close a valve.
- A gateway `NODE_SILENT` alert cannot make a powered-off node illuminate its LEDs. The gateway/dashboard should report the node as silent instead.
- The LED and valve hooks must not interfere with LoRa transmission, acknowledgements, SD-card buffering, or gateway communication.

---

## 9. Design Principle

- Colour communicates severity.
- One blink communicates the first category.
- Two blinks communicate the second category.
- Solid green communicates normal operation.

The physical LEDs provide quick status information. Detailed readings, alert names, evidence, localisation, and recommended actions remain available through the Aqua Edge dashboard.

Do not introduce additional colours or blinking patterns without revising this specification.
