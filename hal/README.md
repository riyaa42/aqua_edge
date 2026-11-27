# Hardware Abstraction Layers (HAL)

Adapters that let `edge/aqua_edge.py` and `gateway/core.py` run unchanged across environments:
- Simulation HAL (connects to `simulation/live/` or offline CSVs)
- Raspberry Pi 3B dataset-replay HAL (reads local datasets / EPANET outputs row-by-row from microSD and drives real LoRa, RTC, and buzzer hardware)

## What exists
| Layer | State |
|---|---|
| Node, Raspberry Pi: `pi_hal.py` | SX1278 SPI driver, LEDs and buzzer; sensor readings replayed from a CSV; calls `radio_poll()` between sensor steps. `python3 -m hal.pi_hal --check` (from the repository root) tests it with a fake radio, not on hardware |
| Gateway laptop + ESP8266 | Not in the repository yet: until it is, a real node gets no beacon, acknowledgement or gateway alert |

The buzzer sounds for a node's own alerts and for gateway alerts that concern it, and stops when the LED leaves red. `pi_hal.py` has no `on_gateway_alert` (the valve hook).

## Radio contract
The method list is at the top of `../edge/aqua_edge.py`. A real layer must:
- use the settings in `lora433.py` (check the legal notes there first);
- stay in receive mode whenever it is not sending;
- call `Node.radio_poll()` when a packet arrives and at the time it returns, as well as `Node.step()` once a minute;
- call `Gateway.step()` when a packet arrives and at the time it returns, at least every 0.5 s (through the ESP8266 on USB serial);
- for `send(packet, cad=True)`, run channel activity detection first; if the channel is busy, wait a short random time and try again;
- call these from one thread (the classes are not thread-safe);
- optionally provide `buzzer(code)` and `on_gateway_alert(code, active)`.

## Files

| File | Runs on | What it does |
|---|---|---|
| `packet.py` + `pi_hal.py` | Nodes and gateway | Radio packets: compact binary (about a third of the JSON size), signed with a 4-byte tag (HMAC-SHA256) using a key shared between each node and the gateway; a message counter that never repeats, plus hardware abstraction (`pi_hal.py`) |
| `lora433.py` | Nodes and gateway | SX1278 433 MHz radio settings, time on air, transmit-time limit, retry timing |
| `offline_radio.py` | Tests | Lets a fake radio with no airtime deliver replies within one step |
| `esp8266_passthrough.py` | Gateway | Placeholder for the LoRa-through-ESP8266 passthrough (empty for now) |
| `__init__.py` | Nodes and gateway | Makes the existing packet and radio functions importable from `hal` itself: `pack`, `unpack`, `peek`, `load_keys`, `sim_key`, `violation_mask`, `airtime_s`, `retry_wait`, `AirtimeBudget` |

Self-check, from the repository root: `python3 -m hal.packet`.

## Radio and instant delivery

**One SX1278 per device.** It listens on one channel and one spreading factor at a time and cannot receive while sending, so the whole building shares 433.175 MHz, SF7, 125 kHz, CR 4/5, 8-symbol preamble (`lora433.py`). Power is set to 10 dBm, and each device limits itself to 10% of an hour on air (summaries pause at 8%), matching India's WPC **G.S.R. 347(E) (May 9, 2022)** licence exemption for 433.05–434.79 MHz (`10 mW ERP` max, `10%` duty cycle, no 10 kHz bandwidth cap, integral antenna). A 10 dBm module setting is not automatically 10 mW radiated power (antenna gain and cable loss count), and WPC Equipment Type Approval (ETA) applies to commercial/imported hardware.

**Timing.** A node has two entry points: `step()` once a minute (sensors and checks) and `radio_poll()`, to be called when a packet arrives and at the time it returns. The gateway's `step()` works the same way.

| Case | What happens |
|---|---|
| A node raises an alert or tank flag | Local buzzer and LED at once (no gateway needed); the message is sent in the same call |
| No reply, gateway heard in the last 15 s | Sent again after 1-2 s, then 2-4 s, then every 4-8 s |
| No reply, gateway away | Kept on the SD card; tried about every 30 s (summaries every 5 min) |
| Gateway comes back (beacon after silence, or after a restart) | Stored alerts go at once, each node 0.15 s after the one before; summaries follow from 3.5 s, one per acknowledgement, oldest first |
| Gateway running | Beacon every 5 s and at startup (about 0.9% of its time on air) |

These cover delivery after a decision. Detection windows (for example 3 h of continuous flow, or two 15-minute blocks for a pipe leak) are unchanged, and repeated packet loss can still delay a message. **None of this timing has been measured on real radios or in a simulation with airtime and collisions:** the tests use a fake radio with no airtime, and the live simulation has not been updated for it.
