# Aqua Edge: Q&A Preparation (CSD457 First Presentation)

Short answers to questions the lecturer may ask. Keep each answer to two or three sentences when speaking. Say "to the best of our search", never "no one has done this".

---

## A. Problem and motivation

**1. Why offline-first? Isn't the internet usually available?**
Rural sites, underground pump rooms and outages all lose connectivity, and that is exactly when a leak or contamination does the most damage. Detection shouldn't depend on the link.

**2. Why not just add a 4G backup connection?**
A backup is still a link that can fail, and it costs money every month. Deciding on the device also gives faster alerts even when the link is up.

**3. Who would use this?**
Housing complexes, campuses, small town water schemes and rural water supplies: anywhere with long pipes and patchy connectivity.

---

## B. Literature and gap

**4. Why only six papers? Are they from good venues?**
The brief asked for 3 to 5. We read six in depth, from Sensors, Scientific Reports, ISA Transactions, Internet of Things and an arXiv paper. We also found an IEEE paper on edge leak detection (Garlisi et al., IEEE MSN 2022) that supports the same direction.

**5. How are you different from Ali et al. (2022)?**
They detect in the cloud. We detect on the device and gateway, and we measure what happens as outages get longer.

**6. Why does Ali et al. do its detection in the cloud?**
Their leak method compares flow from several nodes at the same moment (water in vs water out), so they gathered everything in one central place, a cloud database (Firebase), which also gave users and admins a dashboard. It was a small home prototype on reliable Wi-Fi, so outages weren't their focus. Their nodes store data offline, but the leak decision waits for the cloud. We move that decision onto the devices and the gateway, so it keeps working when the link is down.

**7. So how do you compare flows from several nodes without the cloud?**
The gateway does it. It's local and receives data from all the nodes over LoRa, with no internet needed.

**8. Are you sure nobody has done this?**
To the best of our search, no. We don't claim nobody has; we claim we didn't find it, and our contribution is the measurement.

**9. Isn't this just combining existing methods?**
The methods are borrowed on purpose. What's new is testing them together on one small device under outages: the trade-offs between accuracy, alert speed and battery haven't been measured.

**10. What is the challenge in combining all three? What's the actual gap?**

*20-second answer:* Combining them isn't just stacking three detectors. They compete for one small device's memory and battery, they work on different time scales, the same signal like a pressure drop can mean a leak or a supply problem, and some checks need data from other nodes, which normally comes from the cloud. Nobody has measured how a combined system handles these trade-offs when offline. That's our gap.

*The five challenges, if they ask for detail:*
1. **Limited device resources:** one ESP32 has little memory, a slow processor and a small battery, and now runs three detectors plus storage and radio. Each paper ran only one task.
2. **Different time scales:** leaks need checks every few seconds, water quality changes over minutes, and abnormal use needs days of history. Faster readings catch leaks sooner but use more battery.
3. **The same signal can mean different things:** a pressure drop could be a leak, heavy normal use, or the whole supply weakening. A usage spike could be a leak or someone filling a tank. Detectors running together can contradict each other or raise false alarms.
4. **Some checks need other nodes:** the flow-balance check and the supply-event check need data from several nodes. Offline, that job moves to a local gateway.
5. **Surviving outages:** during a long outage the SD card fills up and the radio can only send small amounts, so the system must decide what to keep, what to send first (alerts before routine data), and keep timestamps correct.

*The gap in one sentence:* Nobody has measured whether one low-cost device can run all three detections on its own, and how well it still performs as internet outages get longer, compared with a cloud-based system.

**11. Is Alghamdi et al. a real paper if it's "only a simulation"?**
Yes, it's published in Sensors (2022). "Simulation" describes how they tested it: a virtual housing complex in the FLoRa simulator on OMNeT++, with 72 to 1,800 nodes, instead of real hardware.

---

## C. Design

**12. How do you detect each problem?**
- **Water quality:** a tiny neural network on pH, TDS, turbidity and temperature.
- **Leaks:** a pressure drop, or water in not matching water out.
- **Abnormal use:** comparing current flow with that node's normal pattern, such as flow at 3 am or a sudden spike.

**13. How do you tell a leak from normal heavy use or a supply problem?**
A leak shows at one node or a few close together. Heavy use matches the time-of-day pattern. A supply problem hits every node at once, and the gateway checks across nodes to call it a supply event, not a leak, so users don't get false leak alerts everywhere.

**14. Does each node have an ESP32?**
Yes. Each node has its own ESP32, so every node makes its own decisions and keeps working offline. One node is: ESP32 + sensors (pH, TDS, turbidity, temperature, flow, pressure) + LoRa radio module + SD card + battery. The ESP32 has no LoRa built in, so we add a LoRa module (or use a combined LoRa32 board). The gateway is a separate device. We're doing simulation first, so most nodes will be virtual; hardware, if built, would be one or two real nodes.

**15. Why an ESP32?**
It's cheap, low-power, has enough memory to run the small water-quality model, and the paper we build on already showed the model runs on it.

**16. Why LoRa?**
Long range and low power for small sensor messages, and it works without internet, which fits an offline-first water network.

**17. Where is the neural network trained?**
On a laptop, using the data. It's then shrunk (quantized) and copied onto the ESP32, which only runs it.

**18. Will all three detectors fit on one ESP32?**
The quality model in Sharma et al. needs about 14.5 KB of flash and under 2 KB of RAM, and the leak and usage checks are simple rules. So yes, and measuring memory use is part of our evaluation. We avoid heavy models such as a TinyML leak model needing 255 KB of RAM.

**19. What if LoRa or the gateway is down too?**
The node still detects on its own and stores readings and alerts on its SD card, then sends them when the link returns. LoRa doesn't need the internet anyway.

**20. How do timestamps stay correct offline?**
A small real-time clock module on the node, as in Mariadhason et al.

**21. What about battery life?**
The node sleeps between readings (duty cycling). Energy per node and estimated battery life are among our metrics.

**22. What about security?**
It's not our focus. LoRaWAN already encrypts messages. We'll mention it as a limitation.

**23. What about sensor drift and calibration?**
Low-cost sensors drift, which Sharma et al. also note. We'll assume periodic calibration and state it as a limitation.

---

## D. Data and evaluation

**24. Where does your data come from?**
EPANET simulates leaks and supply drops, we inject abnormal-use patterns into flow data, and water quality comes from public or small collected datasets. We'll clearly say it's synthetic.

**25. Why simulation and not hardware?**
Outages must be repeated exactly, at many lengths, to compare fairly, which is hard on real hardware. A hardware prototype is a stretch goal.

**26. How will you simulate outages?**
In NS-3 we cut the link for set lengths of time, from short to long, and measure what each design still detects and delivers.

**27. What are your baselines and metrics?**
- **Baselines:** a cloud-only design like Ali et al., and each detector running alone.
- **Metrics:** precision, recall and F1 per task, alert latency, alerts delivered vs outage length, data lost, energy and memory.

**28. How is your hypothesis testable? What result would prove you wrong?**
If the edge system loses a lot of accuracy, or doesn't deliver alerts faster during outages, our hypothesis is wrong.

**29. What does "comparable accuracy" mean?**
We'll set an acceptable accuracy difference once our evaluation plan is approved.

**30. Your hypothesis doesn't mention energy. Why is it in the question?**
We'll measure energy to see the cost of running detection on the device.

---

## E. Project management (fill these in as a team)

**31. How is the work divided?**
[Who does node logic, simulation, evaluation and report]

**32. What's your timeline?**
[For example: design approval, then simulation setup, detectors, outage experiments, report]
