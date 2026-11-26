# JalRakshak / Aqua Edge — Branch Reference & Experimentation Plan

> **Source-of-Truth Hierarchy:**
> 1. `presentation/JalRakshak_Presentation.pptx` (first presentation slides, architecture diagram, baselines, metrics, and speaker notes)
> 2. `circuit_plan.md` (updated from `prep_work/components_connections.md`) and `presentation/references.md`
> 3. Updated project hardware constraints (see Section 1)
> 4. `origin/live-simulation` branch (used as a secondary reference for simulation, firmware, and TinyML prototypes)

---

## 1. Updated Hardware Scope & Component Connections

Based on the current hardware availability and dataset-driven simulation plan, the physical hardware setup is updated as follows:

* **Power:** Monitoring nodes use a **5V USB cable** connected directly to the Raspberry Pi Zero 2 W (the Battery HAT and 18650 Li-ion battery cells are removed).
* **Sensing:** No physical water-quality, flow, or pressure sensors are connected; consequently, the **8-channel ADC** and analog signal-conditioning circuits are not required. Real public datasets and EPANET/WNTR hydraulic simulation outputs are stored locally and replayed row-by-row to each node.

### A. Monitoring Node (Edge Device)
1. **Raspberry Pi Zero 2 W** — Runs local TinyML water-quality inference, leakage detection, abnormal-consumption detection, and atomic store-and-forward buffering.
2. **5V USB Power Cable** — Direct power supply to the Raspberry Pi Zero 2 W.
3. **microSD Card** — Stores OS, quantized TinyML model, pre-loaded real/simulated sensor datasets, learned node baselines, and atomic store-and-forward queues.
4. **RTC Module (e.g., DS3231 via I²C)** — Provides accurate UTC/ISO-8601 timestamps for sensor readings and offline-buffered events.
5. **LoRa Transceiver Module 1 with Antenna (SPI + GPIO)** — Transmits alerts, flags, and periodic summaries independently of Wi-Fi/Internet.
6. **Buzzer Module (GPIO)** — Generates immediate local audible alerts when an anomaly is detected, even during connectivity loss.
7. **Jumper Wires & Connectors**

### B. Gateway Node
8. **LoRa Transceiver Module 2 with Antenna** — Receives node packets and sends signed ACKs.
9. **ESP32 Development Board** — Bridges LoRa Module 2 packets to the laptop over USB serial.
10. **USB Cable** — Connects the ESP32 to the laptop.
11. **Laptop (Gateway & Analysis Host)** — Runs cross-node mass balance, pressure-gradient correlation, supply-event disambiguation, risk analysis, maintenance prioritization, local dashboard, and optional cloud sync when Internet is available.

---

## 2. Alignment Review of `live-simulation` (Items to Update When Porting)

When migrating code incrementally from `origin/live-simulation` into the new `edge-simulation` branch, the following technical alignments should be made so the implementation matches the presentation and referenced papers:

### 2.1 Unified Edge Node Architecture (`firmware/aqua_edge.py`)
* **Current state in `live-simulation`:** Node firmware is split into `FlatNode` (flow-only) and `TankNode` (TinyML water quality + outflow/level). No single node runs all three detection tasks together.
* **Target alignment:** Unify the node pipeline so an edge node can jointly execute water-quality classification, leakage detection, abnormal-consumption detection, and store-and-forward buffering (as proposed in Slides 5, 7, and 8), allowing accurate measurement of combined RAM/CPU/flash footprint.

### 2.2 Water-Quality TinyML Model (`tinyml/train_quality.py`, `firmware/quality_model.py` — Paper [1])
* **Restore 4th input feature (`Temperature`):** Paper [1] (Sharma et al., 2026) and Slides 2, 7, and 8 specify 4 inputs (`Temperature, pH, TDS, Turbidity`) in a `4 → 16 → 8 → 3` MLP. `live-simulation` currently uses 3 inputs (`3 → 16 → 8 → 3`).
* **Add sampling/averaging filter:** Paper [1] applies sampling and averaging to reduce sensor noise prior to inference, rather than classifying single raw 1-minute samples.
* **Restore Stratified 5-Fold Cross-Validation:** Replace the single 70/30 split with Stratified 5-Fold CV as in Paper [1] (and the earlier `quality.py`).
* **Address threshold memorization & site calibration:** Training labels in `train_quality.py` are currently generated via hardcoded IS 10500 `if/else` thresholds on synthetic mixtures, which causes the model to flag ~80% of normal German utility water (GECCO 2019) as unsafe. Add site-relative baseline calibration or normalization alongside standard limits.

### 2.3 Leakage Detection & Supply-Event Disambiguation (`firmware/aqua_edge.py`, `firmware/gateway.py` — Papers [2, 6] & Slide 7)
* **Re-integrate pressure + flow correlation:** `live-simulation` removed pressure-based leak detection (`FLOW ONLY`) after an experiment where every flat triggered standalone `PRESSURE_LOW` alarms without checking local flow (causing neighbor false alarms during riser leaks). Instead, combine **node pressure drop + elevated/continuous flow** (Paper [2]) so neighbor flats do not false-alarm while retaining fast detection (~10 min vs. ~60 min).
* **Restore cross-node `SUPPLY_EVENT` rule:** Currently `gateway.py` triggers `SUPPLY_EVENT` solely from `B-TANK`'s `SUPPLY_LOW` level flag, which can misclassify a large leak draining the tank as a supply drop. Restore the Slide 7/11 rule: identify a supply drop when **pressure drops simultaneously across all nodes** alongside tank level/inflow drop.

### 2.4 Abnormal Consumption & Lempel–Ziv Edge Analytics (Paper [3] & Slide 2)
* **Add prolonged zero-flow detection:** Slide 2 explicitly includes *"prolonged periods of zero flow"* under Abnormal Use; `FlatNode.check()` currently has no zero-flow check.
* **Separate abnormal consumption from leak scoring:** `check_comms.py` and `compare_leaks.py` currently count `ABNORMAL_CONSUMPTION` as a leak alert (`FLAT_LEAK_ALERTS`). Score leakage and abnormal consumption as distinct tasks.
* **Implement Lempel–Ziv compression-rate anomaly detection:** Paper [3] (Babazadeh, 2019) detects anomalies via changes in the Lempel–Ziv compression rate of sensor streams on the node. Implement and evaluate this method alongside statistical threshold rules.

### 2.5 Store-and-Forward & Radio Protocol Fixes (`firmware/gateway.py`, `firmware/aqua_edge.py` — Paper [4])
* **Fix `SEEN_WINDOW = 1000` silent data-loss bug:** In `aqua_edge.py`, `Store.due()` sends newer high-priority `ALERT` packets ahead of older queued `SUM` packets. In `gateway.py`, any packet with `seq <= max(seen) - 1000` is acknowledged (causing the node to delete it from SD) and then silently dropped. After a long outage, a new alert advances `max(seen)` and discards older buffered summaries. Track acknowledged/seen sequences without dropping valid older buffered packets.
* **Add exponential retry backoff:** During a 3.5-hour outage, nodes currently retry every 2 minutes (~13,000 retries). Add exponential backoff to respect duty-cycle and airtime limits (Paper [4]).
* **Use UTC Unix / ISO-8601 timestamps:** Replace simulation-relative seconds (`0, 60, 120...`) with DS3231 RTC-backed UTC Unix seconds and ISO-8601 logs.

### 2.6 Hydraulic & Network Simulation Realism (`simulation/` — Paper [5])
* **Use realistic bursty household demand:** `build_society.py` gives all 20 flats the same smooth 24h `PATTERN` where flow never reaches zero for more than 6 hours (requiring `CONT_FLOW_MIN = 480` min = 8 hours). Replace with bursty residential demand (e.g., Cache County dataset) for realistic false-alarm and latency testing.
* **Restore full LoRaWAN MAC & SWI-FEED baseline scripts:** `aqua_lora.cc` currently bypasses the NS-3 LoRaWAN MAC (`SetReceiveOkCallback`) on a single EU868 channel. Evaluate with full LoRaWAN MAC / IN865 channels, and retain SWI-FEED `NET_6` reproduction scripts for reference.

---

## 3. Experimentation & Implementation Plan (Remaining Work)

The following components are planned for full implementation and evaluation to answer the Slide 6 Research Question and collect all Slide 10 Metrics:

### 3.1 Systems Under Comparison (Slide 9 Baselines)
1. **Baseline A — Cloud-Dependent Integrated Pipeline (Ali et al. [6]):**
   * Nodes stream raw/periodic sensor readings to the cloud/server; water-quality classification, leakage detection, and abnormal consumption detection are performed remotely.
   * Used as the primary comparison baseline to measure degradation during Internet/backhaul outages.
2. **Baseline B — Single-Task Edge Approaches (Papers [1, 2, 3, 5]):**
   * Isolated edge quality TinyML [1], pressure-only leak detection [2], and Lempel–Ziv compression anomaly detection [3].
3. **Proposed System — JalRakshak / Aqua Edge (Offline-First Joint Edge + Gateway):**
   * Joint on-node detection (TinyML quality + flow/pressure leak + abnormal consumption + store-and-forward) paired with gateway cross-node analysis, supply-event disambiguation, risk scoring, and maintenance prioritization.

### 3.2 Gateway Risk Analysis, Localization & Maintenance Prioritization (Slides 7 & 10)
* **Floor/Riser Localization:** Combine gateway mass-balance (`>10%` missing water, Paper [6]) with cross-floor pressure-drop gradients ($\Delta P$ per floor) to pinpoint which floor/riser section has a main-pipe leak.
* **Risk Priority Scoring:** Compute a composite risk score per location using event severity ($w_{\text{chemical}} > w_{\text{runoff}} > w_{\text{burst}} > w_{\text{leak}} > w_{\text{consumption}}$), anomaly magnitude, persistence duration, and downstream population affected, outputting a ranked maintenance queue (*"Which location should be inspected first?"*).

### 3.3 Controlled Outage Sweep & Slide 10 Evaluation Metrics
Run identical 48-hour multi-fault scenarios (contamination, flat leaks, riser bursts, consumption spikes, zero-flow faults, and supply drops) across **controlled Internet/backhaul outage durations: `0 min (online)`, `15 min`, `1 h`, `3.5 h`, `12 h`, and `24 h`**:

1. **Detection Accuracy:** Precision, Recall, and F1 separately for (a) water-quality anomalies, (b) leakage, and (c) abnormal consumption.
2. **Detection Latency:** Time from physical/dataset event onset to local detection decision.
3. **Alert Latency (Online vs. Offline):** Time from event onset to alert delivery under normal connectivity vs. across each outage duration (Edge-First vs. Cloud Baseline A).
4. **Data Loss / Recovery Rate (%):** Percentage of buffered alerts and summaries successfully recovered after link restoration.
5. **Maintenance-Priority Accuracy:** Top-1 / Top-$k$ accuracy in ranking the highest-risk fault location first across multi-fault scenarios.
6. **Edge Resource & Communication Footprint (Raspberry Pi Zero 2 W):** Measured RAM, SD flash usage, CPU time per sample (ms), and LoRa airtime / bytes transmitted (Edge-First vs. raw cloud streaming).
