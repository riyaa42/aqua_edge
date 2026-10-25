# JalRakshak: Components, Connections, and References

## 1. Components Required

### A. Monitoring Node

1. Raspberry Pi Zero 2 W
2. Battery HAT for battery power management and power multiplexing
3. LoRa transceiver module 1 with compatible antenna
4. pH sensor
5. TDS sensor
6. Turbidity sensor
7. Water temperature sensor
8. Water flow sensor
9. Water pressure sensor
10. 8-channel ADC (Analog-to-Digital Converter)
11. RTC (Real-Time Clock) module
12. microSD card
13. Buzzer module for local alerts
14. 18650 Li-ion battery cell(s) compatible with the Battery HAT
15. USB cables, jumper wires, and compatible connectors

### B. Gateway Node

16. LoRa transceiver module 2 with compatible antenna
17. ESP32 development board
18. USB cable to connect the ESP32 to the laptop
19. Laptop for gateway processing, cross-node analysis, maintenance prioritization, and dashboard

### C. Additional Requirements

20. Sensor signal-conditioning circuits or voltage-level converters, if required by the selected hardware.

## 2. Component Connections and Data Flow

The pH, TDS, turbidity, temperature, flow, and pressure sensors are connected to the monitoring node built around the Raspberry Pi Zero 2 W. The analog-output sensors connect to the 8-channel ADC, which converts their analog signals into digital readings for processing by the Pi. The ADC communicates with the Raspberry Pi through the I²C interface. Flow sensors that produce pulse outputs may require a GPIO input rather than an ADC channel, depending on the selected sensor model. The RTC is used to timestamp sensor readings and detected events; its connection interface will be determined after the RTC model is finalized (I²C or direct). The microSD card holds the operating system, the TinyML model used for local inference, and a timestamped log of sensor readings and detected events, which are kept locally and re-sent when the LoRa link becomes available. The buzzer connects directly to the Raspberry Pi to provide audible alerts when a configured anomaly is detected. The Battery HAT manages the monitoring node's battery-based power arrangement and power multiplexing using compatible 18650 cells.

The Raspberry Pi processes sensor readings locally and runs the configured detection logic for water-quality anomalies, pipeline leakage, and abnormal consumption. It can continue detecting anomalies and generating local buzzer alerts without Internet connectivity, while locally storing readings and relevant event information for later use. The Pi transmits sensor readings, detected events, and node status through LoRa transceiver module 1. At the gateway, LoRa transceiver module 2 receives these messages and passes them to the ESP32, which relays the data to the laptop over USB serial. The laptop runs the gateway application and performs cross-node analysis by combining information received from monitoring nodes, assessing risk, and prioritizing maintenance. It also displays the collected data and analysis results. When Internet connectivity is available, the laptop can optionally synchronize data with a cloud service for remote monitoring and historical analysis. When Internet access is unavailable, local detection and alerts at the monitoring node continue, and gateway-level analysis can continue on the laptop as long as the LoRa link and local gateway hardware remain operational.

## 3. References and Methodological Inspiration

Paper [1] uses pH, TDS, turbidity, and temperature sensors to obtain four water-quality features, applies sampling and averaging to reduce measurement noise, and performs on-device classification using a quantized neural network. Its use of local storage and timestamped readings also provides a methodological basis for including a microSD card and RTC in our project, supporting offline data buffering and event reconstruction.

Paper [2] uses water-meter readings and pressure measurements to investigate leakage-related conditions, with detection based on a pressure threshold. Its LoRaWAN communication performance is evaluated through simulation. This supports the selection of flow and pressure sensors alongside LoRa communication, although the paper does not validate our exact hardware configuration.

Paper [3] implements edge analytics on an Arduino101-based sensor node, using a customized Lempel–Ziv compression method and changes in compression rate to identify anomalous water-network behaviour. Data associated with anomalous conditions are transmitted over LoRa. This provides a methodological precedent for processing measurements at the monitoring node and transmitting relevant events rather than relying entirely on remote analysis.

Paper [4] combines pH, TDS, turbidity, and temperature sensing with LoRa transmission, an ESP32 gateway, and a DS3231 RTC. Its timestamped local buffering and store-and-forward approach informs our use of an RTC, local storage, and a separate gateway receiver. Its forecasting component is outside our project's scope.

Paper [5] uses EPANET to model water-distribution behaviour and NS-3 to simulate LoRaWAN communication, evaluating leakage detection, energy consumption, and network performance. It therefore informs the network-level evaluation and leakage-monitoring rationale rather than providing a specific physical sensor or ADC design.

Paper [6] develops a physical water-distribution prototype with flow and water-quality sensors arranged across network nodes, synchronizes measurements, and compares main-pipe and branch-flow measurements to identify leakage conditions. Its flow-balance methodology informs our selection of flow sensors and our leakage-detection approach, while its cloud-oriented data pipeline provides the baseline against which our project's offline-first design can be evaluated.

Taken together, these methodologies support the selection of water-quality and pipeline sensors, local processing, LoRa communication, timestamping, and local storage. The 8-channel ADC, Battery HAT, 18650 cells, buzzer, and USB connections are implementation choices needed to interface and power our chosen hardware and support local alerts; they should not be presented as components specifically validated by all six papers. The reviewed papers provide methodological precedents for individual functions, while our project's proposed integration of water-quality anomaly detection, leakage detection, and abnormal-consumption detection in an offline-first architecture is what we intend to evaluate.
