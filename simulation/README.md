# Aqua Edge: Simulation and Network Design

Everything used to test the system before the hardware exists: the building B water network, the live simulation, the LoRa radio model and ns-3, and the Webots 3D test bench. The node and gateway code they run lives in `../edge/`, `../gateway/`, `../hal/` and `../detection/`.

Full write-up of mechanisms, results and next steps: [`LIVE_SIMULATION.md`](LIVE_SIMULATION.md).

| Folder | What is in it |
|---|---|
| `live/` | Live simulation: water engine (WNTR), timekeeper, one process or container per node, LoRa radio model, control panel, checker, scenarios |
| `ns3/` | ns-3 (LoRaWAN module, in Docker): signal strength and spreading factor per node, and replay of live runs |
| `webots/` | Building B network and sensor scenario (`make_scenario.py`), 3D world, offline firmware test (`run_offline.py`) |
| `scripts/build_society.py` | Builds the water network (rooftop tank, downtake, 4 flats per floor); `make_scenario.py` uses building B from it |

## How to run

Uses [uv](https://docs.astral.sh/uv/) (configured at the repository root). From this `simulation/` folder:

```
sh live/run_local.sh --scenario live/scenarios/standard.jsonl     # no Docker: one process per node, 48 h in ~1 min
sh live/run_local.sh --scenario live/scenarios/flow_leaks.jsonl   # three flow leaks (see ../detection/leakage/)
docker compose -f live/compose.yaml up                            # one container per node; panel at localhost:8501
uv run streamlit run live/control_panel.py                        # control panel for the no-Docker run
python3 live/check_comms.py                                       # PASS/FAIL report for the last run
sh ns3/run_ns3.sh                                                 # ns-3 link table (first run builds ns-3: ~20-30 min)
sh ns3/run_ns3.sh replay                                          # ns-3 replay of the last live run
```

Self-checks: `python3 live/radio.py`, `uv run python live/water.py`, `python3 webots/run_offline.py`.
All of them plus both live scenarios in one go: `sh check_all.sh` (about 4 minutes, stops at the first failure).

## Results so far (standard scenario)

100% of uplink attempts got through while the radio was up (2 collisions out of about 4,066), 0 messages lost, all 6 planted events detected, and ns-3 and the Python radio model agree on every node (gap 0.0 points). All nodes reach the gateway on SF7 with at least 26 dB to spare.

## NS-3 Scope Note (`ns3/aqua_lora.cc`)

`simulation/ns3/aqua_lora.cc` is a **custom LoRa PHY replay** over India's 3 IN865 channels (`865.0625, 865.4025, 865.985 MHz`), **not** a complete LoRaWAN Class A/B/C MAC simulation (`EndDeviceLorawanMac::Send` and `gwPhy->SetReceiveOkCallback` are bypassed). This matches the project's firmware architecture, which uses plain LoRa with custom TDMA summary slots, listen-before-talk (CAD) for alerts, and signed binary packets (`hal/packet.py`) rather than LoRaWAN join/ADR.
