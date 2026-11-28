# Hardware Abstraction Layers (HAL)

LoRa communication between the nodes and the gateway, and nothing else. Both ends share this folder; each has its own way in:
- `pi_hal.py`: a node, with the SX1278 on the Raspberry Pi's GPIO pins;
- `gate_hal.py`: the gateway, with the LoRa module behind an ESP8266 on USB serial.

## Pinouts

The SX1278 module (for example Ra-02) is a **3.3 V** part: do not power it or drive its pins with 5 V. **Connect the antenna before anything transmits.** These are the pins the code uses; **neither wiring has been tested on real hardware.**

### Node: Raspberry Pi

Pins are BCM GPIO numbers, with the physical header pin in brackets.

| SX1278 module | Raspberry Pi |
|---|---|
| 3.3V | 3.3 V (pin 1) |
| GND | Ground (pin 6) |
| SCK | GPIO 11, SPI0 SCLK (pin 23) |
| MISO | GPIO 9, SPI0 MISO (pin 21) |
| MOSI | GPIO 10, SPI0 MOSI (pin 19) |
| NSS | GPIO 8, SPI0 CE0 (pin 24) |
| RESET | GPIO 22 (pin 15) |
| DIO0 | GPIO 25 (pin 22). **Optional:** it wakes the Pi as soon as a packet is in. Without it the radio is checked every 10 ms |

The node board (`../edge/board.py`) also uses these. Give each LED a series resistor.

| Part | Raspberry Pi |
|---|---|
| Green, yellow, red LED | GPIO 17 (pin 11), GPIO 27 (pin 13), GPIO 5 (pin 29) |
| Buzzer module | GPIO 18 (pin 12) |

### Gateway: NodeMCU ESP8266

| SX1278 module | NodeMCU label | ESP8266 GPIO |
|---|---|---|
| 3.3V | 3V3 (**not** VIN or 5 V) | |
| GND | GND | |
| SCK | D5 | GPIO 14 |
| MISO | D6 | GPIO 12 |
| MOSI | D7 | GPIO 13 |
| NSS | D1 | GPIO 5 |
| RESET | D2 | GPIO 4 |
| DIO0 | Not connected | |

The NodeMCU connects to the computer with a USB data cable, which also powers it. NSS is deliberately not on D8: the board does not boot if that pin is pulled high at power-on. To use other pins, change `PIN_NSS` and `PIN_RESET` at the top of the sketch.

**Contents:** [How it fits together](#how-it-fits-together) · [What exists](#what-exists) · [Files](#files) · [Using the HAL](#using-the-hal) · [Encryption](#encryption) · [Radio settings](#radio-settings-both-ends) · [Radio contract](#radio-contract) · [Running on the Pi](#running-on-the-pi) · [Gateway modem](#gateway-modem-esp8266) · [Delivery](#radio-and-instant-delivery) · [Testing](#testing)

## How it fits together

`edge/aqua_edge.py` and `gateway/core.py` run unchanged across environments because they call a small `hal` object supplied from outside. For radio it needs `send` and `receive` (packet bytes) and `key(name)`; both wrap it in `MessageLink` (`link.py`), which sends and returns whole messages: packed, [encrypted](#encryption) and checked. Queues, acknowledgements and retries stay in the node and the gateway. The other parts of a node's `hal` are outside this folder:
- Simulation HAL (connects to `simulation/live/` or offline CSVs)
- Raspberry Pi 3B dataset-replay: `../bogo/csv_replay.py` reads local datasets / EPANET outputs row-by-row from microSD, `../edge/board.py` drives the clock, LEDs and buzzer, and the radio comes from here

## What exists
| Layer | State |
|---|---|
| Node, Raspberry Pi: `pi_hal.py` | SX1278 SPI driver (in `lora_pi.py`), tested with a fake radio chip, not on hardware (`python3 -m hal.lora_pi --check`, from the repository root). The node runner is `../edge/edge_based.py`. Setup: [Running on the Pi](#running-on-the-pi) |
| Gateway laptop + ESP8266: `gate_hal.py` | Radio link written (`esp8266_passthrough.py` and the ESP sketch), tested with a fake radio chip, not on hardware. The gateway server that uses it is [`../gateway/edge_based.py`](../gateway/README.md#running-the-server): until it runs, a real node gets no beacon, acknowledgement or gateway alert. Setup: [Gateway modem (ESP8266)](#gateway-modem-esp8266) |

## Files

| File | Runs on | What it does |
|---|---|---|
| `packet.py` | Nodes and gateway | Radio packets: compact binary, encrypted and authenticated with AES-SIV (beacons: signed only) using a key shared between each node and the gateway. See [Encryption](#encryption) |
| `link.py` | Nodes and gateway | `MessageLink`: `send(message)` and `receive()` over any byte radio. It packs, encrypts and checks; which keys it is given decides what that end can read. No queues, acknowledgements or retries |
| `pi_hal.py` | Raspberry Pi nodes | The node's way in: `open_radio()` and `MessageLink` |
| `gate_hal.py` | Gateway | The gateway's way in: `open_radio(port)` and `MessageLink` |
| `lora433.py` | Nodes and gateway | SX1278 433 MHz radio settings, time on air, transmit-time limit, retry timing |
| `lora_pi.py` | Raspberry Pi nodes | The SX1278 on the Pi's GPIO pins: `SX1278` (chip driver) and `LoRaLink` (point-to-point byte link: `send`, `receive`, receive thread, recovery from radio faults) |
| `esp8266_passthrough.py` | Gateway | The SX1278 behind the ESP8266: `ESPLink` (same `send` and `receive` as `LoRaLink`, reconnects after faults) and `SerialPort` |
| `esp8266_lora/esp8266_lora.ino` | The ESP8266 | The sketch that makes a NodeMCU a LoRa modem |
| `common.py` | Nodes and gateway | What the two radio drivers share: radio settings not in `lora433.py`, the packet length rule, the radio test's listening loop, and `RadioLink` (received packets, receive thread, recovery) |
| `__init__.py` | Nodes and gateway | Makes the packet, radio and link functions importable from `hal` itself: `MessageLink`, `pack`, `unpack`, `peek`, `load_keys`, `sim_key`, `violation_mask`, `airtime_s`, `retry_wait`, `AirtimeBudget` |

## Using the HAL

Each end has one function that opens its radio. Run from the repository root.

| | Node | Gateway |
|---|---|---|
| Open | `pi_hal.open_radio()` | `gate_hal.open_radio("/dev/ttyUSB0")` |
| Needs | An SX1278 on the Pi | A flashed ESP8266 with its SX1278, and `pyserial` |

Both return a link with the same methods:

| Method | What it does |
|---|---|
| `send(packet, cad=True)` | Sends 1 to 255 bytes. With `cad`, listens first and backs off while the channel is busy (5 tries), then sends anyway. `False` means the send failed or was not confirmed. A wrong length raises `ValueError` |
| `receive()` | `[(arrival time, packet bytes)]` since the last call. The time is this computer's `time.time()` |
| `close()` | Stops the receive thread and releases the radio |

Opening raises an error if the radio cannot be used. After that, radio and USB faults are logged instead of raised.

**Whole messages.** `MessageLink` (`link.py`) packs, [encrypts](#encryption) and checks messages. It expects `receive()` to return bytes only, so **do not pass it the link from `open_radio()` directly**: drop the arrival times first, as `../edge/board.py` does for a node. For the gateway:

```python
from hal.gate_hal import MessageLink, open_radio
from hal.packet import load_keys


class BytesOnly:
    def __init__(self, radio):
        self.radio, self.send = radio, radio.send

    def receive(self):
        return [packet for _, packet in self.radio.receive()]


keys = load_keys("/path/outside/the/repository/keys.json")
radio = open_radio("/dev/ttyUSB0")
link = MessageLink(BytesOnly(radio), keys.__getitem__)

link.send({"k": "BEACON", "n": "GATEWAY", "s": 1, "gt": 0})
messages, rejected = link.receive()      # rejected: broken, changed, wrong key, or no key for it
radio.close()
```

- The keys given to `MessageLink` decide what that end can read. Give a node only its own key and the `GATEWAY` key.
- `MessageLink.send` returns `False` only when its optional `admit` check refuses the message (for the transmit-time limit). It does **not** pass on a radio failure, and `True` does not mean the message arrived.
- Queues, acknowledgements and retries are the node's and gateway's job. So is the transmit-time limit (`AirtimeBudget` in `lora433.py`): the radio links do not enforce it.

## Encryption

Plain LoRa protects nothing, so `packet.py` encrypts and authenticates packets before they reach a radio. `MessageLink` does this for you. The radio drivers and the ESP8266 only carry the finished bytes: **the ESP needs no keys**. The raw `send()` of a radio link sends exactly the bytes it is given, so **anything sent without `pack` or `MessageLink` goes out unprotected** (the radio tests send a fixed plain text on purpose).

**Check the legal notes in `lora433.py` before transmitting: they have not been checked for encrypted traffic either.**

### Packet format

| Packet | Bytes on the air |
|---|---|
| Every kind except `BEACON` | Header (10 bytes, readable) + AES-SIV tag (16 bytes) + encrypted: format version (1 byte) and the message fields |
| `BEACON` | Header (10 bytes, readable) + tag (4 bytes, first bytes of HMAC-SHA256) |

- **Header:** kind, node, counter and time. It stays readable so that the receiver can pick the key, but changing it makes the packet fail the check.
- **AES-SIV** (RFC 5297, from the `cryptography` package) with the header as associated data. It needs no nonce, which matters here: one counter value is used by several kinds of packet (an alert and its acknowledgement), and a node's counter starts again if its SD card is wiped. If exactly the same packet is made twice, the two are identical on the air; that is all a repeat reveals.
- **Keys:** each node shares one 16-byte secret with the gateway. The 32-byte AES-SIV key is derived from it with HKDF-SHA256 (no salt, label `aqua-edge packet encryption v1`). This does not make the secret any stronger.
- **Beacons are not encrypted.** They carry only the gateway's clock and boot number, are signed with the shared `GATEWAY` key, and are not allowed to carry any message fields.
- A packet that is changed, cut short, made with another key or in another format is rejected: `unpack` returns `None`, and `MessageLink.receive` counts it as rejected.

### Setting up keys

Real devices need their own random keys. `sim_key` is for simulations and tests only: anyone can work those keys out. Run these from the repository root, and keep the files **outside the repository**. The first command makes the gateway's file with every node's key. The second makes one node's file, with only its own key and the `GATEWAY` key.

```bash
(umask 077; uv run python -c 'import json, secrets; from hal.packet import NAMES; print(json.dumps({n: secrets.token_hex(16) for n in NAMES}, indent=2))' > ~/gateway_keys.json)
(umask 077; uv run python -c 'import json, sys; k = json.load(open(sys.argv[1])); print(json.dumps({n: k[n] for n in (sys.argv[2], "GATEWAY")}))' ~/gateway_keys.json B-203 > ~/B-203_keys.json)
```

- Running the first command again **replaces every key**, and each node then needs its new file.
- Copy each node's file to that node only, over a connection you trust, and keep it readable by its user only (`chmod 600`).
- `.gitignore` excludes files ending in `keys.json`, as a safety net only.
- Anyone who gets a node's file can read that node's packets, including ones recorded earlier, and can pose as that node or as the gateway towards it. Replace that node's key at both ends. If the `GATEWAY` key leaks, replace it everywhere.

### What it does not protect

- **Metadata:** anyone listening can see which node, what kind of packet (an `ALERT` is visibly an alert, but not which one), the counter, the time and the length. The length can hint at the contents.
- **Replay:** encryption does not stop an old packet being sent again. The checks that do:

| Replayed | What stops it |
|---|---|
| A node's alert, flag or summary, to the gateway | The gateway remembers each node's message numbers and acts on each once (it still acknowledges a repeat) |
| A command, to a node | The node remembers recent command numbers and ignores older ones |
| A beacon, to a node | The node ignores one that is not newer than the last it used |
| An acknowledgement, to a node | It names one message number, which is not reused. For the clock, the Pi (`../edge/board.py`) uses only an acknowledgement newer than the last it used |

  These checks have limits. The Pi does not save the newest acknowledgement time, so **just after a restart** an old recorded acknowledgement can still set its clock back until a newer one arrives. The other side of the clock rule: if the gateway's clock is ever set back, nodes ignore its time until it passes the old value or the node restarts. A wiped SD card also forgets what a node had seen.
- **Jamming** or blocking packets.
- **No forward secrecy**, and there is no automatic key change. This design has had no outside security review.

### Cost and compatibility

An encrypted packet is 13 bytes longer than the earlier signed-only format. Beacons are unchanged. Calculated time on air at SF7:

| Message | Bytes, before → now | Time on air, before → now |
|---|---|---|
| Acknowledgement | 14 → 27 | 46 → 67 ms |
| Alert (night-flow leak) | 21 → 34 | 57 → 77 ms |
| Summary (volume and pressure) | 29 → 42 | 67 → 87 ms |

This counts towards the 10% transmit-time limit. **The old and new formats do not work together:** update every node and the gateway at the same time. The key files stay the same, and the ESP8266 does not need reflashing.

`cryptography` is a normal dependency (`uv sync` installs it), so everything that imports `hal` needs it, radio tests included. With Raspberry Pi OS packages, install `python3-cryptography` (version 38 or newer). **Neither route has been tried on a Pi**, and the speed has not been measured there.

## Radio settings (both ends)

Both ends must use these. Point to point means plain LoRa, not LoRaWAN: the LoRa payload is the [protected packet](#encryption) from `packet.py`, with nothing added.

| Setting | Value |
|---|---|
| Frequency | 433.175 MHz |
| Spreading factor, bandwidth, coding rate | SF7, 125 kHz, 4/5 |
| Preamble | 8 symbols |
| Sync word | `0x12` |
| Header, payload CRC | Explicit header, CRC on |
| IQ inversion | Off, both directions |
| Payload | 1 to 255 bytes |

Output power is 10 dBm. The values come from `lora433.py` and `common.py`; the ESP sketch repeats them. For the legal limits, see [Radio and instant delivery](#radio-and-instant-delivery).

## Radio contract
The method list is at the top of `../edge/aqua_edge.py`. A real layer must:
- use the settings in `lora433.py` (check the legal notes there first);
- stay in receive mode whenever it is not sending;
- call `Node.radio_poll()` when a packet arrives and at the time it returns, as well as `Node.step()` once a minute;
- call `Gateway.step()` when a packet arrives and at the time it returns, at least every 0.5 s (through the ESP8266 on USB serial);
- for `send(packet, cad=True)`, run channel activity detection first; if the channel is busy, wait a short random time and try again;
- call these from one thread (the classes are not thread-safe);
- optionally provide `buzzer(code)` and `on_gateway_alert(code, active)`.

## Running on the Pi

Wiring: [Node: Raspberry Pi](#node-raspberry-pi).

`lora_pi.py` drives the SX1278 on the Pi's GPIO pins, and `../edge/edge_based.py` runs a node on top of it. **None of this has been tested on real hardware.**

### Setup

Enable SPI once (`sudo raspi-config`, Interface Options, SPI). Then use **one** of these:

- **uv:** `uv sync --extra pi` installs `spidev`, `gpiozero` and `lgpio`. `spidev` is compiled during the install, which needs `sudo apt install build-essential python3-dev`. `uv run` keeps these installed, but a later plain `uv sync` removes them. Not tried on a Pi yet.
- **Raspberry Pi OS packages:** `sudo apt install python3-spidev python3-gpiozero python3-lgpio python3-cryptography`, then use `python3 -m ...` instead of the `uv run` scripts. The node needs only `edge/`, `hal/`, `bogo/` and `tinyml/`, plus a sensor CSV (the default is `simulation/webots/data/sensors.csv`; use `--csv` for another path).

Real nodes need a key file with this node's key and the `GATEWAY` key: see [Setting up keys](#setting-up-keys). The keys are secrets, so **keep the file outside the repository**.

### Commands (from the repository root)

| With uv | With system Python | What it does |
|---|---|---|
| `uv run pi-radio-test` | `python3 -m hal.lora_pi --radio-test` | Checks the module is found, sends one test packet, listens 10 s |
| `uv run edge-based-node B-203 --keys <file> --sd <folder> --slot 5 20` | `python3 -m edge.edge_based B-203 ...` | Runs a node in real time. Add `--step-s 1` for a fast demo |
| `uv run python -m hal.lora_pi --check` | `python3 -m hal.lora_pi --check` | Self-check with a fake radio chip, on any computer |

To check that two modules hear each other, start the radio test on one, then on the other within 10 s. The one started first prints the other's packet as hex. Swap the order to test the other direction.

### If the radio fails

If the module is missing or miswired at startup, the program stops with a message. Once running, a radio fault (send timeout, SPI error, or a radio that reset itself) is logged and the radio is set up again, at most every 5 s while it stays faulty. The node keeps running its checks, buzzer and LEDs, and keeps unacknowledged messages. The link also checks every 5 s that the radio is still receiving.

Before sending or listening before talk, the driver waits up to about 0.45 s for a packet that is already coming in, so that it is not cut off. This relies on the chip's modem status bits and **needs checking on a real module**: if they stayed set on a quiet channel, every send would be delayed and the channel would always look busy.

## Gateway modem (ESP8266)

The gateway computer reaches the nodes through a NodeMCU ESP8266 on USB, wired to an SX1278 and running `esp8266_lora/esp8266_lora.ino`. The ESP does the timing-critical radio work: listen before talk, receiving, and setting a faulty radio up again. Only whole packets cross USB. Keys, signing, acknowledgements and retries stay on the computer, so the ESP needs reflashing only when its radio code or settings change. **The sketch compiles for NodeMCU, but none of this has been flashed or tested on a real board or radio.**

Wiring: [Gateway: NodeMCU ESP8266](#gateway-nodemcu-esp8266).

### Flashing

The sketch needs only the **ESP8266 board package** (tried with 3.1.2), no other libraries.

- **Arduino IDE:** open `hal/esp8266_lora/esp8266_lora.ino`, choose the board **NodeMCU 1.0 (ESP-12E Module)** and its port, and upload.
- **Command line** (`arduino-cli`, which Arduino IDE 2 includes), from the repository root: `arduino-cli compile --upload --fqbn esp8266:esp8266:nodemcuv2 --port /dev/ttyUSB0 hal/esp8266_lora`

On Linux your user needs access to the serial port. Run `sudo usermod -aG uucp $USER` (Arch-based systems; the group is `dialout` on Debian and Ubuntu), then log out and in. Close the Arduino serial monitor before running the gateway side: only one program can use the port.

### Commands (from the repository root)

| Command | What it does |
|---|---|
| `uv sync --extra gateway` | Installs `pyserial`. `uv run` keeps it installed, but a later plain `uv sync` removes it |
| `uv run gate-radio-test --port /dev/ttyUSB0` | Checks the ESP, its firmware, radio settings and SX1278, sends one test packet, listens 10 s. The port shown is the default |
| `uv run python -m hal.esp8266_passthrough --check` | Self-check with a fake ESP, on any computer |

To check the radio link to a node, run this and `pi-radio-test` on the Pi: start one, then the other within 10 s. The one started first prints the other's packet. Swap the order to test the other direction.

In code, `gate_hal.open_radio(port)` gives the same `send(packet, cad=True)` and `receive()` (arrival time and bytes) as the Pi's `pi_hal.open_radio()`. `Gateway` expects `receive()` to return only the bytes, so the gateway server (`../gateway/edge_based.py`) drops the arrival times, as `../edge/board.py` does for a node.

### If the modem fails

At startup, `open_radio` raises an error if the port cannot be opened, the ESP does not answer, it runs another protocol version or different radio settings, or its SX1278 does not answer. Once running, faults are logged and never stop the program: an unplugged or silent ESP makes the port be reopened, at most every 5 s, and the ESP sets a faulty SX1278 up again by itself. `send` returns `False` for a packet that was not confirmed as sent and does not resend it over USB. If only the confirmation was lost, the packet may still have gone out. A send waits for the ESP's answer: usually well under a second, a few seconds on a busy channel, and 12 s if the ESP has stopped answering.

As on the Pi, the wait for a packet that is already coming in **needs checking on a real module**.

### Serial protocol

115200 baud. Each frame is COBS-encoded, carries a CRC-16 and ends in a zero byte, so the boot loader's noise and damaged bytes are dropped. The messages are listed at the top of the sketch. If you change them, change `esp8266_passthrough.py` too and raise `PROTOCOL` in both. The radio settings are written in both languages (`lora433.py` and `common.py`, and the constants in the sketch); the computer refuses an ESP that reports different values. The radio behaviour in the sketch repeats `lora_pi.py` in C++, so a fix to one usually belongs in the other.

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

## Testing

Self-checks, from the repository root: `python3 -m hal.packet`, `python3 -m hal.lora433`, `python3 -m hal.link`, `python3 -m hal.lora_pi --check`, `python3 -m hal.esp8266_passthrough --check`. `uv run test-hal` runs them all, plus `../tests/test_hal.py` (both openers, hardware setup, radio tests, shared code, and the example above). They use fake radios and need no hardware. `spidev`, `gpiozero` and `pyserial` are faked too, so the real libraries are not tested. Encryption is tested by the `hal.packet` self-check (every bit of each sample packet flipped, truncation, format switching, noise, and the wire format rebuilt from the recipe above) and by two tests in `test_hal.py` through both radios; the Pi's replayed-acknowledgement rule is in `../tests/test_pi_node.py`. None of these tests compile or run the ESP sketch: compile it after changing it, with `arduino-cli compile --fqbn esp8266:esp8266:nodemcuv2 hal/esp8266_lora`.
