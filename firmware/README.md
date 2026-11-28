# Aqua Edge: Firmware

The real code for the nodes and the gateway. It uses the Python standard library plus one outside library, `cryptography` (for AES-SIV encryption in `packet.py`; preinstalled on Ubuntu and Raspberry Pi OS, and in `simulation/pyproject.toml`), and it never talks to hardware directly: it calls a small hardware layer (`hal`) for sensors, radio, clock, SD card and buzzer/LED. The response to a leak is the buzzer, the LED and an alert to the gateway. The one valve is on the pipe leaving the tank: the tank node shuts it on bad water and a person reopens it with a button. The simulation (`../simulation/`) plugs in a simulated hardware layer; the real hardware will get its own: one for the Raspberry Pi Zero 2 W nodes (mains powered), and one on the gateway laptop that exchanges packets with the ESP32 radio bridge over USB serial.

| File | Runs on | What it does |
|---|---|---|
| `aqua_edge.py` | Every node (20 flats + tank), Raspberry Pi Zero 2 W | Learns normal on day 1 (saved to SD hourly, so a power cut keeps it), leak checks (flat: night, continuous flow, too much, high flow with a pressure drop) and "problem over" messages, water quality (IS 10500 limits, model cause guess, temperature), the tank valve and tank outflow checks (tank), gateway beacon and commands, store-and-forward to SD card (saved safely, so a power cut never leaves half a file), sending in time slots, re-sending |
| `quality_model.py` + `quality_model.json` | Tank node | IS 10500 limit check (`check_standards`) and the water-quality model: 8-bit neural network (308 bytes), pH + TDS + turbidity in, NORMAL / RUNOFF / CHEMICAL out; integer-only, plain Python. Trained by `../tinyml/train_quality.py` |
| `packet.py` | Nodes and gateway | Radio packets: compact binary (about half the JSON size), encrypted and signed with AES-SIV (16-byte tag; header with type, node, counter and time stays readable but signed, as in LoRaWAN) using a key shared between each node and the gateway; the gateway beacon is only signed (4-byte HMAC-SHA256, building key); a message counter that never repeats |
| `pi_hal.py` | Every real node (Raspberry Pi Zero 2 W; a Pi 4 for testing) | The real hardware layer: SX1278 LoRa radio (433 MHz, e.g. Ra-02 module) over SPI (send, listen before talk, receive), clock (UTC from the DS3231, Indian hours), sensor readings fed row by row from a CSV (sensors are simulated), green/yellow/red/blue LEDs, buzzer, tank valve relay and reopen button. Sets its clock only from signed ACKs and beacons |
| `gateway.py` | Gateway laptop (LoRa module on an ESP32 bridges the radio) | Beacon once a minute, replies to every message (with the time), commands to flats (valve, pipe leak) repeated until confirmed, per-floor pipe checks (water in = water out, paused while the tank valve is shut), silent-node alerts, supply-event rule, new vs repeat alerts, clock check |

Checks run once a minute; alarms are sent the moment they fire, summaries every 15 minutes in each node's time slot (details and timings: `../leak_detection/README.md` section 7b).
Self-checks: `python3 aqua_edge.py` (power-cut-safe saves, baseline survives a restart, continuous-flow check, forged ACK ignored), `python3 quality_model.py` (normal, runoff and chemical readings classified right), `python3 packet.py` (packets come back unchanged, no extra bytes, values unreadable without the key, one changed bit or a wrong key is rejected), `python3 gateway.py` (includes: forged packet dropped, replay ignored even after a gateway restart), `python3 pi_hal.py --check` (with a fake radio: the planted leak goes out on air, signed ACK sets the clock and a forged one doesn't, time slot wait, busy channel).

How the leak checks work, with the math: [`../leak_detection/README.md`](../leak_detection/README.md).
Water-quality model, data and test results: [`../tinyml/README.md`](../tinyml/README.md).

## Running on the Pi

Raspberry Pi OS (Bookworm). The Pi 4 and the Pi Zero 2 W have the same pins, so everything below is the same for both.

**Wiring** (BCM numbers; physical pin in brackets). **Power the radio from 3.3 V, never 5 V, and screw the antenna on before it ever transmits** (sending without an antenna can damage the module).

| Part | Pin on the part | Pi |
|---|---|---|
| SX1278 (Ra-02) | 3.3V, GND | 3V3 (1), GND (6) |
| | MOSI, MISO, SCK | GPIO10 (19), GPIO9 (21), GPIO11 (23) |
| | NSS | CE0, GPIO8 (24) |
| | RESET | GPIO22 (15) |
| | DIO0 | not needed |
| DS3231 clock | VCC, GND, SDA, SCL | 3V3 (17), GND (9), GPIO2 (3), GPIO3 (5) |
| LEDs (each with a 330 ohm resistor to GND) | green, yellow, red, blue | GPIO17 (11), GPIO27 (13), GPIO5 (29), GPIO6 (31) |
| Relay module for the tank valve (tank node only) | IN | GPIO23 (16). Relay on = valve shut; use a normally open valve so the water stays on if the Pi dies |
| Reopen button (tank node only, to GND) | one leg | GPIO24 (18), internal pull-up |
| Active buzzer | + | GPIO18 (12) |
| Down pipe flow meter (flats 301, 201, 101, G01 only) | pulse | a spare GPIO, e.g. GPIO13 (33) |

Pins and radio settings are knobs at the top of `pi_hal.py`. The gateway's ESP32 radio must use the same radio settings (433.175 MHz, SF7, 125 kHz, 4/5, sync word 0x12, 10 dBm: India allows 10 mW and 10% time on air in 433.05 to 434.79 MHz).

**Setup, once per Pi**
1. `sudo raspi-config` -> Interface Options -> turn on **SPI** and **I2C**.
2. Clock: add `dtoverlay=i2c-rtc,ds3231` to `/boot/firmware/config.txt` and reboot. `sudo hwclock -r` should print a time. While the Pi is online, `sudo hwclock -w` copies the correct time into the DS3231 once. If the time is wrong after a reboot without internet, `sudo apt purge fake-hwclock`.
3. `sudo apt install python3-spidev python3-gpiozero` (usually already installed).
4. `git clone` this repository onto the Pi.
5. Keys, on the laptop: make `aqua_keys.json` with the command in `load_keys` (`packet.py`). Keep it on the gateway. Give each node a file with only its own key:
   `python3 -c "import json; k = json.load(open('aqua_keys.json')); json.dump({'B-203': k['B-203']}, open('B-203_keys.json', 'w'))"`
   Key files never go into git (`*keys.json` is in `.gitignore`).

**Run**
```sh
python3 firmware/pi_hal.py --radio-test        # expect: SX1278 found (version 0x12), channel free, sent
python3 firmware/pi_hal.py B-203 --keys ~/B-203_keys.json --sd ~/aqua --step-s 1     # fast demo
python3 firmware/pi_hal.py B-203 --keys ~/B-203_keys.json --sd ~/aqua --slot 5 22    # real time
```
- **Fast demo** (`--step-s 1`): one CSV row per second, and the clock follows the CSV, so the learning day takes 24 minutes. With the default CSV (`simulation/webots/data/sensors.csv`), B-203 gets its planted leak on day 2 at 02:00: the LED turns red and the buzzer beeps about 26 minutes in.
- **Real time**: one row per minute on the minute, UTC clock, Indian hours for the night check, sends in its own time slot (`--slot INDEX COUNT`, assigned at installation).
- Until the gateway exists, nothing replies: the LED shows yellow (messages waiting) and messages stay in the queue on the SD card (`~/aqua/B-203.jsonl`). The node keeps resending its oldest messages; alerts still go first.
- A Pi 4 is much faster than a Zero 2 W and has more memory, so speed and memory measured on it look better than they will on the real nodes.
