# JalRakshak: Components, Connections, and References

## 1. Components Required

### A. Monitoring Node (Physical Hardware Present)

1. Raspberry Pi 4
2. 5 V USB power cable connected directly to the Raspberry Pi (replaces the Battery HAT and 18650 Li-ion battery cells, which are not available)
3. LoRa transceiver module 1 with compatible antenna
4. RTC (Real-Time Clock) module
5. microSD card
6. Buzzer module for local alerts
7. USB cables, jumper wires, and compatible connectors

### B. Gateway Node (Physical Hardware Present)

8. LoRa transceiver module 2 with compatible antenna
9. ESP32 development board
10. USB cable to connect the ESP32 to the laptop
11. Laptop for gateway processing, cross-node analysis, maintenance prioritization, and dashboard

### C. Omitted Physical Components & Dataset-Replay Workaround

Because we do not have the Battery HAT, 18650 battery cells, the 8-channel ADC, or any of the physical water sensors (pH, TDS, turbidity, water temperature, water flow, and water pressure sensors), the following workarounds are used in place of physical sensing and battery hardware:

12. **Power workaround:** The Raspberry Pi 4 is powered directly via a 5 V USB cable rather than a Battery HAT and 18650 cells.
13. **Sensor & ADC workaround (Dataset-Replay Layer):** Instead of physical sensor connections, the Raspberry Pi 4 receives readings through a **dataset-replay layer** fed from two sources of data:
    * **Water-quality and consumption datasets:** Existing datasets collected online provide readings such as pH, TDS, turbidity, temperature, or water consumption, depending on what each dataset contains.
    * **EPANET/WNTR outputs:** Hydraulic simulations provide simulated hydraulic measurements such as flow and pressure under normal and leak conditions.

    Because no physical analog sensors are attached, the 8-channel ADC and sensor signal-conditioning / voltage-level converter circuits are not required.

## 2. Component Connections and Data Flow

The most important difference between the original and updated architecture is that the Raspberry Pi 4 receives sensor readings through a **dataset-replay layer** rather than physical sensor connections (and without an 8-channel ADC). Two data sources feed this replay layer: existing water-quality and consumption datasets (providing pH, TDS, turbidity, temperature, or water-consumption readings depending on the dataset) and EPANET/WNTR hydraulic simulation outputs (providing simulated flow and pressure under normal and leak conditions).

The RTC is connected to the Raspberry Pi to timestamp sensor readings and detected events; its connection interface will be determined after the RTC model is finalized (I²C or direct). The microSD card stores:
- The Raspberry Pi operating system and application.
- Locally available datasets used for replay.
- The trained TinyML model used for local inference.
- Timestamped readings and detection-event logs, which are kept locally and re-sent when the LoRa link becomes available.

The buzzer connects directly to the Raspberry Pi GPIO to provide audible alerts when a configured anomaly is detected. The Raspberry Pi is powered directly via a 5 V USB cable without a Battery HAT or battery cells.

The Raspberry Pi processes the replayed sensor readings locally through the composable `EdgeRuntime` (`firmware/aqua_edge.py`), which preserves the physical deployment split (`FlatNode` for flat flow+pressure → leak & abnormal consumption; `TankNode` for tank level/flow+water-quality probes → TinyML quality & tank monitoring) while also supporting a `combined` mode for joint three-pipeline resource validation (`firmware/test_unified_runtime.py`: `78.67 KB` code + model JSONs, `185.51 KB` peak Python `tracemalloc` RAM, `907.81 us/step` on Windows-11 AMD64 host; physical Pi 4 measurement pending). It can continue detecting anomalies and generating local buzzer alerts without Internet connectivity, while locally storing readings and relevant event information for later use. The Pi transmits sensor readings, detected events, and node status through LoRa transceiver module 1. At the gateway, LoRa transceiver module 2 receives these messages and passes them to the ESP32, which relays the data to the laptop over USB serial. The laptop runs the gateway application and performs cross-node analysis by combining information received from monitoring nodes, assessing risk, and prioritizing maintenance. It also displays the collected data and analysis results. When Internet connectivity is available, the laptop can optionally synchronize data with a cloud service for remote monitoring and historical analysis. When Internet access is unavailable, local detection and alerts at the monitoring node continue, and gateway-level analysis can continue on the laptop as long as the LoRa link and local gateway hardware remain operational.

## 3. References and Methodological Inspiration

Paper [1] uses pH, TDS, turbidity, and temperature sensors to obtain four water-quality features, applies sampling and averaging to reduce measurement noise, and performs on-device classification using a quantized neural network. Its use of local storage and timestamped readings also provides a methodological basis for including a microSD card and RTC in our project, supporting offline data buffering and event reconstruction.

Paper [2] uses water-meter readings and pressure measurements to investigate leakage-related conditions, with detection based on a pressure threshold. Its LoRaWAN communication performance is evaluated through simulation. This supports the selection of flow and pressure parameters alongside LoRa communication, although the paper does not validate our exact hardware configuration.

Paper [3] implements edge analytics on an Arduino101-based sensor node, using a customized Lempel–Ziv compression method and changes in compression rate to identify anomalous water-network behaviour. Data associated with anomalous conditions are transmitted over LoRa. This provides a methodological precedent for processing measurements at the monitoring node and transmitting relevant events rather than relying entirely on remote analysis.

Paper [4] combines pH, TDS, turbidity, and temperature sensing with LoRa transmission, an ESP32 gateway, and a DS3231 RTC. Its timestamped local buffering and store-and-forward approach informs our use of an RTC, local storage, and a separate gateway receiver. Its forecasting component is outside our project's scope.

Paper [5] uses EPANET to model water-distribution behaviour and NS-3 to simulate LoRaWAN communication, evaluating leakage detection, energy consumption, and network performance. It therefore informs our hydraulic simulation, dataset-driven evaluation, and network-level leakage-monitoring rationale rather than requiring a specific physical sensor or ADC design.

Paper [6] develops a physical water-distribution prototype with flow and water-quality sensors arranged across network nodes, synchronizes measurements, and compares main-pipe and branch-flow measurements to identify leakage conditions. Its flow-balance methodology informs our leakage-detection approach, while its cloud-oriented data pipeline provides the baseline against which our project's offline-first design can be evaluated.

Taken together, these methodologies support the selection of water-quality and pipeline parameters, local processing, LoRa communication, timestamping, and local storage. Using direct USB power for the Raspberry Pi, replaying real datasets and EPANET/WNTR outputs in place of physical sensors and an 8-channel ADC, and connecting a local buzzer and ESP32 USB-serial bridge are practical implementation choices needed to build and evaluate our chosen hardware setup; they should not be presented as components specifically validated by all six papers. The reviewed papers provide methodological precedents for individual functions, while our project's proposed integration of water-quality anomaly detection, leakage detection, and abnormal-consumption detection in an offline-first architecture is what we intend to evaluate.
