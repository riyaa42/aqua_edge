"""Control panel: start leaks, bad water or radio cuts while the live simulation runs.

    uv run streamlit run live/control_panel.py

Every button appends one command to run/commands.jsonl; the timekeeper applies it at the next
simulated minute. This panel never touches sensor values: the water model works those out.
"""
import json
import os

import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = os.environ.get("AQUA_RUN", os.path.join(HERE, "run"))


def lines(name):
    path = os.path.join(RUN, name)
    return open(path).read().split() if os.path.exists(path) else []


def jsonl(name, last=10):
    path = os.path.join(RUN, name)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(x) for x in f if x.strip()][-last:]


def send(**cmd):
    with open(os.path.join(RUN, "commands.jsonl"), "a") as f:
        f.write(json.dumps(cmd) + "\n")
    st.toast(f"sent: {cmd}")


def hhmm(t):
    return f"day {int(t // 86400) + 1}, {int(t % 86400 // 3600):02d}:{int(t % 3600 // 60):02d}"


st.set_page_config(page_title="Aqua Edge control panel", layout="wide")
st.title("Aqua Edge control panel")

if not os.path.exists(os.path.join(RUN, "commands.jsonl")):
    st.warning("No simulation running. Start one with: sh live/run_local.sh")
    st.stop()


@st.fragment(run_every=1)
def status():
    try:
        c = json.load(open(os.path.join(RUN, "clock.json")))
    except (OSError, ValueError):
        st.info("waiting for the timekeeper ...")
        return
    a, b, d, q = st.columns(4)
    a.metric("Simulated time", hhmm(c["t"]), "paused" if c["paused"] else None)
    b.metric("Speed", "as fast as possible" if c["delay_s"] == 0 else f"{c['delay_s']} s per minute")
    d.metric("Active leaks", ", ".join(f"{k} ({v} L/s)" for k, v in c["leaks"].items()) or "none")
    q.metric("Tank water", c["quality"])
    left, right = st.columns(2)
    left.caption("Applied events (ground truth)")
    left.dataframe([{**e, "t": hhmm(e["t"])} for e in jsonl("events_applied.jsonl")], hide_index=True)
    right.caption("Latest alerts that reached the gateway")
    right.dataframe([{"node": m["n"], "alert": m["a"], "created": hhmm(m["t"]), "received": hhmm(m["rx"])}
                     for m in jsonl("gateway_log.jsonl", last=10_000) if m["k"] == "ALERT"][-10:], hide_index=True)


status()

leak, water, radio, clock = st.tabs(["Leak", "Water quality", "Radio", "Clock"])
with leak:
    sites = lines("leak_sites.txt")
    where = st.selectbox("Where", sites, help="Flats (B-<floor><flat>) or the downtake pipe (B-D<floor>)")
    size = st.radio("Size", ["small", "medium", "large"], horizontal=True,
                    captions=["0.05 L/s, running toilet", "0.5 L/s, cracked pipe", "1.5 L/s, burst"])
    a, b = st.columns(2)
    if a.button("Start leak", type="primary"):
        send(do="leak", where=where, size=size)
    if b.button("Fix leak"):
        send(do="fix", where=where)
with water:
    kind = st.radio("Event", ["runoff", "chemical"], horizontal=True,
                    captions=["rainwater-like: acidic, low TDS, cloudier", "bleach-like: high pH, high TDS"])
    minutes = st.slider("How long (simulated minutes)", 5, 180, 60)
    if st.button("Send into the tank", type="primary"):
        send(do="quality", kind=kind, minutes=minutes)
with radio:
    who = st.selectbox("Whose radio", ["all"] + lines("nodes.txt"))
    minutes = st.slider("Cut for (simulated minutes)", 5, 300, 60, key="cut")
    if st.button("Cut radio", type="primary"):
        send(do="radio_cut", who=who, minutes=minutes)
with clock:
    delay = st.select_slider("Real seconds per simulated minute", [0.0, 0.05, 0.1, 0.5, 1.0, 60.0], value=0.1,
                             help="0 = as fast as possible, 60 = real time")
    a, b, c = st.columns(3)
    if a.button("Set speed"):
        send(do="speed", delay_s=delay)
    if b.button("Pause"):
        send(do="pause")
    if c.button("Resume"):
        send(do="resume")
