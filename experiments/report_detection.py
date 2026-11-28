"""Tables and figures for experiment 4, from detection.py's trials, and the investigation of the two
false alarms on sensors.csv.

    uv run python -m experiments.report_detection
"""
import collections
import csv
import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import LinearSegmentedColormap

from detection.quality.quality_model import TemporalModel
from edge.sensors import DATASET
from experiments.detection import CONSUMPTION_RULES, FLAT_LEAK_RULES, SCENARIOS, first_delay, raised
from experiments.style import INK, MUTED, SERIES, SURFACE, finish, new_figure, write_table

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "results")
BLUES = LinearSegmentedColormap.from_list("blues", [SURFACE, "#cde2fb", "#6da7ec", "#2a78d6", "#0d366b"])
NOISES = ("steady", "replay")
TEMPORAL = ("QUALITY_TEMPORAL_ANOMALY",)
LIMIT = ("QUALITY_LIMIT_VIOLATION",)
CLASSES = ("QUALITY_RUNOFF", "QUALITY_CHEMICAL")
USE_CODES = tuple(sorted(CONSUMPTION_RULES | {"PROLONGED_ZERO_FLOW"}))
LEAK_CODES = tuple(sorted(FLAT_LEAK_RULES))
SHORT = {"LEAK_NIGHT_FLOW": "night flow", "LEAK_CONTINUOUS_FLOW": "continuous flow",
         "LEAK_PRESSURE_DROP": "pressure drop", "ABNORMAL_CONSUMPTION": "high use",
         "SUDDEN_CONSUMPTION_CHANGE": "sudden change", "UNUSUAL_NIGHT_CONSUMPTION": "unusual night",
         "PROLONGED_ZERO_FLOW": "zero flow", "QUALITY_RUNOFF": "runoff", "QUALITY_CHEMICAL": "chemical",
         "QUALITY_LIMIT_VIOLATION": "limit violation", "QUALITY_TEMPORAL_ANOMALY": "temporal anomaly"}


def scores(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * p * r / (p + r) if tp else (0.0 if tp + fp + fn else float("nan"))
    return p, r, f


def fmt(x):
    return "n/a" if x != x else f"{x:.2f}"


def binary(rows, codes, positive):
    """Trial-level counts. positive(row): the event should be detected. A positive trial is a hit if
    the code is raised during its event; any other raising of it, in any trial, is a false positive."""
    tp = fn = fp = tn = 0
    for r in rows:
        if positive(r):
            hit = raised(r, codes, during_event_only=True)
            tp, fn = tp + hit, fn + (not hit)
            fp += any(a["code"] in codes and not a["inside"] for a in r["alerts"])
        elif raised(r, codes):
            fp += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def first_code(row, codes, any_time):
    got = [a for a in row["alerts"] if a["code"] in codes and (a["inside"] or any_time)]
    return min(got, key=lambda a: a["at_min"])["code"] if got else None


def matrix_figure(ax, matrix, row_labels, col_labels, title):
    m = np.array(matrix)
    share = m / np.maximum(m.sum(axis=1, keepdims=True), 1)
    ax.imshow(share, cmap=BLUES, vmin=0, vmax=1, aspect="auto")
    for i in range(m.shape[0]):
        for j in range(m.shape[1]):
            ax.text(j, i, int(m[i, j]) if m[i, j] else "·", ha="center", va="center", fontsize=11,
                    color="#ffffff" if share[i, j] > 0.55 else INK)
    ax.set_xticks(range(len(col_labels)), col_labels, rotation=30, ha="right", fontsize=9)
    ax.set_yticks(range(len(row_labels)), row_labels, fontsize=9)
    ax.set_xlabel("first alert raised")
    ax.set_title(title, fontsize=10.5, loc="left", color=INK)
    ax.set_xticks(np.arange(-0.5, len(col_labels)), minor=True)
    ax.set_yticks(np.arange(-0.5, len(row_labels)), minor=True)
    ax.grid(which="minor", color=SURFACE, lw=2)
    ax.tick_params(which="minor", length=0)
    ax.tick_params(length=0)
    for s in ax.spines.values():
        s.set_visible(False)


def save_matrix(fig, name, title):
    fig.suptitle(title, x=0.01, ha="left", fontsize=13.5, fontweight="bold")
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    fig.savefig(os.path.join(OUT, "figures", f"{name}.png"), dpi=200)
    plt.close(fig)


def module(rows, scenario_codes, codes, matrix_name):
    """Multi-class confusion matrix (first alert) and per-class precision, recall and F1."""
    labels = [label for label, _, _ in scenario_codes]
    columns = [*codes, None]
    matrix = [[0] * len(columns) for _ in scenario_codes]
    for i, (_, scenario, expected) in enumerate(scenario_codes):
        for r in rows:
            if r["scenario"] == scenario:
                matrix[i][columns.index(first_code(r, codes, any_time=not expected))] += 1
    out, totals = [], [0, 0, 0]
    names = {s for _, s, _ in scenario_codes}
    mine = [r for r in rows if r["scenario"] in names]
    for label, scenario, expected in scenario_codes:
        if not expected:
            continue
        tp, fp, fn, _ = binary(mine, expected, lambda r: r["scenario"] == scenario)
        totals = [a + b for a, b in zip(totals, (tp, fp, fn))]
        delays = [d for r in mine if r["scenario"] == scenario for d in [first_delay(r, expected)] if d is not None]
        out.append([label, " or ".join(SHORT[c] for c in expected), tp + fn, tp, fn, fp,
                    *map(fmt, scores(tp, fp, fn)), int(np.median(delays)) if delays else "n/a"])
    out.append(["All (micro-average)", "", totals[0] + totals[2], totals[0], totals[2], totals[1],
                *map(fmt, scores(*totals)), ""])
    return matrix, labels, [SHORT[c] for c in codes] + ["none"], out


HEADER = ["Scenario", "Expected", "Events", "Detected", "Missed", "False positives (trials)", "Precision",
          "Recall", "F1", "Median delay (data min)"]


def experiment4(rows):
    os.makedirs(os.path.join(OUT, "figures"), exist_ok=True)
    flat = [r for r in rows if "noise" not in r]
    water = [r for r in rows if "noise" in r]

    leak_set = [("normal use", "leak_normal", ()), ("night-flow leak", "leak_night_flow", ("LEAK_NIGHT_FLOW",)),
                ("continuous-flow leak", "leak_continuous_flow", ("LEAK_CONTINUOUS_FLOW",)),
                ("high flow + pressure drop", "leak_flow_pressure_drop", ("LEAK_PRESSURE_DROP",))]
    use_set = [("normal use", "use_normal", ()),
               ("sudden spike", "use_sudden_spike", ("ABNORMAL_CONSUMPTION", "SUDDEN_CONSUMPTION_CHANGE")),
               ("unusual night use", "use_unusual_night", ("UNUSUAL_NIGHT_CONSUMPTION",)),
               ("prolonged zero flow", "use_prolonged_zero", ("PROLONGED_ZERO_FLOW",))]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.8))
    for ax, name, spec, codes, title in ((axes[0], "leak", leak_set, LEAK_CODES, "Flat leakage"),
                                         (axes[1], "consumption", use_set, USE_CODES, "Abnormal consumption")):
        matrix, labels, cols, table = module(flat, spec, codes, name)
        matrix_figure(ax, matrix, labels, cols, f"{title} (30 trials per row)")
        write_table(OUT, f"exp4_{name}_scores", HEADER, table)
    save_matrix(fig, "exp4_confusion_flat", "Experiment 4: leakage and consumption rules")

    # water: the three layers kept apart
    limit_rows, temporal_rows, class_rows, sample_rows = [], [], [], []
    violating = {"wq_ph_low", "wq_ph_high", "wq_tds_high", "wq_turbidity_high", "wq_runoff_like", "wq_chemical_like"}
    fig, axes = plt.subplots(2, 3, figsize=(15, 7.6), gridspec_kw={"width_ratios": [1, 1.25, 1]})
    for row, noise in enumerate(NOISES):
        mine = [r for r in water if r["noise"] == noise]
        for layer, codes, positive, table, column, title in (
                ("limits", LIMIT, lambda r: r["scenario"] in violating, limit_rows, 0, "IS 10500 limit check"),
                ("temporal", TEMPORAL, lambda r: r["scenario"] != "wq_normal", temporal_rows, 2,
                 "Temporal autoencoder")):
            tp, fp, fn, tn = binary(mine, codes, positive)
            delays = [d for r in mine if positive(r) for d in [first_delay(r, codes)] if d is not None]
            table.append([noise, tp + fn, tp, fn, fp, tn, *map(fmt, scores(tp, fp, fn)),
                          int(np.median(delays)) if delays else "n/a"])
            ax = axes[row][column]
            truth = [[r for r in mine if positive(r)], [r for r in mine if not positive(r)]]
            m = [[sum(raised(r, codes, True) for r in truth[0]), sum(not raised(r, codes, True) for r in truth[0])],
                 [sum(raised(r, codes) for r in truth[1]), sum(not raised(r, codes) for r in truth[1])]]
            matrix_figure(ax, m, ["event" if layer == "temporal" else "out of limits",
                                  "normal" if layer == "temporal" else "within limits"],
                          ["alert", "no alert"], f"{title} ({noise} noise)")
            ax.set_xlabel("")
        spec = [("NORMAL", "wq_normal"), ("RUNOFF", "wq_runoff_like"), ("CHEMICAL", "wq_chemical_like")]
        columns = ["NORMAL", "RUNOFF", "CHEMICAL"]
        events = [[0] * 3 for _ in spec]
        samples = [[0] * 3 for _ in spec]
        for i, (truth, scenario) in enumerate(spec):
            for r in mine:
                if r["scenario"] != scenario:
                    continue
                got = first_code(r, CLASSES, any_time=scenario == "wq_normal")
                events[i][columns.index(got.removeprefix("QUALITY_") if got else "NORMAL")] += 1
                for c in r["sample_classes"]:
                    samples[i][columns.index(c)] += 1
        matrix_figure(axes[row][1], events, [f"true {t.lower()}" for t, _ in spec], [c.lower() for c in columns],
                      f"Neural classifier, per event ({noise} noise)")
        axes[row][1].set_xlabel("")
        for i, c in enumerate(columns):
            tp = events[i][i]
            fn = sum(events[i]) - tp
            fp = sum(events[j][i] for j in range(3) if j != i)
            if c != "NORMAL":
                class_rows.append([noise, c.lower(), sum(events[i]), tp, fn, fp, *map(fmt, scores(tp, fp, fn))])
        # reading by reading, on readings inside an injected event (normal trials have none)
        for truth, scenario in spec[1:]:
            i = columns.index(truth)
            total = sum(samples[i])
            sample_rows.append([noise, truth.lower(), total, samples[i][i], f"{100 * samples[i][i] / total:.1f}%"])
    save_matrix(fig, "exp4_confusion_water", "Experiment 4: water quality, three layers scored separately")
    binary_header = ["Water noise", "Events", "Detected", "Missed", "False positives (trials)", "True negatives",
                     "Precision", "Recall", "F1", "Median delay (data min)"]
    write_table(OUT, "exp4_water_limit_check_scores", binary_header, limit_rows)
    write_table(OUT, "exp4_water_temporal_scores", binary_header, temporal_rows)
    write_table(OUT, "exp4_water_classifier_event_scores",
                ["Water noise", "Class", "Events", "Correct", "Missed", "False positives", "Precision", "Recall", "F1"],
                class_rows)
    write_table(OUT, "exp4_water_classifier_reading_accuracy",
                ["Water noise", "True class", "Event readings", "Classified correctly", "Share"], sample_rows)

    # every scenario: expected against actual
    table, identified = [], collections.Counter()
    for scenario, (group, expected, main_set) in SCENARIOS.items():
        for noise in (NOISES if group == "water" else (None,)):
            mine = [r for r in rows if r["scenario"] == scenario and r.get("noise") == noise]
            during = collections.Counter(c for r in mine for c in {a["code"] for a in r["alerts"] if a["inside"]})
            outside = sum(any(not a["inside"] for a in r["alerts"]) for r in mine)
            delays = [d for r in mine for d in [first_delay(r, expected)] if expected and d is not None]
            right = sum(any(n == r["node"] and c in expected for n, c in r["gateway_alerts"]) for r in mine
                        if expected and first_delay(r, expected) is not None)
            identified["right"] += right
            identified["detected"] += len(delays)
            table.append([scenario, noise or "", "main" if main_set else "boundary",
                          " or ".join(SHORT[c] for c in expected) or ("no dedicated rule" if scenario ==
                          "wq_temperature_high" else "no alert"), len(mine),
                          f"{len(delays)}/{len(mine)}" if expected else "n/a",
                          "; ".join(f"{SHORT[c]} {n}" for c, n in during.most_common()) or ("-" if scenario.endswith(
                              "normal") else "none"), outside,
                          f"{int(np.median(delays))} ({min(delays)}-{max(delays)})" if delays else "n/a",
                          f"{right}/{len(delays)}" if delays else "n/a"])
    write_table(OUT, "exp4_expected_vs_actual",
                ["Scenario", "Water noise", "Set", "Expected", "Trials", "Expected alert detected",
                 "Alerts during the event (trials)", "Trials with an alert outside the event",
                 "Delay, median (range), data min", "Gateway named the right node"], table)

    bystander_days = sum(r["bystander_days"] for r in flat)
    by_code = collections.Counter(c for r in flat for _, c in r["other_node_alerts"])
    write_table(OUT, "exp4_node_identification", ["Measure", "Value"], [
        ["Detected events where the gateway recorded the injected node",
         f"{identified['right']}/{identified['detected']}"],
        ["Normal flats watched alongside injected ones (node-days)", bystander_days],
        ["Alerts raised by those flats", sum(by_code.values())],
        ["By type", "; ".join(f"{c} {n}" for c, n in by_code.items()) or "none"]])

    # latency: one dot per detected event
    spec = [(s, e, g) for s, (g, e, m) in SCENARIOS.items() if m and e and s != "use_prolonged_zero"]
    fig, ax = new_figure(13, 6.2)
    rng = np.random.default_rng(0)
    color = {"water": SERIES[0], "leak": SERIES[2], "use": SERIES[1]}
    for i, (s, expected, group) in enumerate(spec):
        y = len(spec) - 1 - i
        mine = [r for r in rows if r["scenario"] == s and r.get("noise") in (None, "steady")]
        delays = [max(d, 1) for r in mine for d in [first_delay(r, expected)] if d is not None]
        ax.scatter(delays, y + rng.uniform(-0.17, 0.17, len(delays)), s=26, color=color[group], alpha=0.75, zorder=3)
        if delays:
            med = np.median(delays)
            ax.plot([med, med], [y - 0.32, y + 0.32], color=INK, lw=2, zorder=4)
            ax.annotate(f"{med:g} min", (max(delays), y), xytext=(10, -3), textcoords="offset points", fontsize=9)
    ax.set_yticks(range(len(spec)), [s.split("_", 1)[1].replace("_", " ") for s, _, _ in spec][::-1])
    ax.set_xscale("log")
    ax.set_xticks([1, 2, 5, 10, 20, 30, 60, 120, 180, 300], ["1", "2", "5", "10", "20", "30", "60", "120", "180", "300"])
    ax.set_xlim(0.8, 600)
    ax.set_xlabel("minutes of data from the start of the event to the expected alert (log scale); black bar: median")
    for g, label in (("water", "water quality (limit check)"), ("leak", "flat leakage"), ("use", "consumption")):
        ax.scatter([], [], color=color[g], label=label)
    ax.legend(frameon=False, ncols=3, loc="upper right", bbox_to_anchor=(1, 1.08))
    zero = [d for r in rows if r["scenario"] == "use_prolonged_zero" for d in [first_delay(r, ("PROLONGED_ZERO_FLOW",))]
            if d is not None]
    ax.text(0.99, 0.02, f"not shown: prolonged zero flow, median {np.median(zero) / 60:.1f} h", transform=ax.transAxes,
            ha="right", fontsize=9, color=MUTED)
    finish(fig, ax, OUT, "exp4_detection_latency", "Experiment 4: detection delay per injected event")


# --- why sensors.csv raises its two false alarms ---
def investigate():
    with open(DATASET, newline="") as f:
        rows = list(csv.DictReader(f))
    t = np.array([float(r["t_s"]) for r in rows])
    col = lambda name: np.array([float(r[name]) for r in rows])
    flow = col("B-101:flow_lps")
    longest = []
    for day in (0, 1):
        best = run = 0
        for v in flow[day * 1440:(day + 1) * 1440]:
            run = run + 1 if v > 0.005 else 0
            best = max(best, run)
        longest.append(best)

    def temporal(day, turbidity=None):
        model, hours, score = TemporalModel(), [], []
        for r in rows[day * 1440:(day + 1) * 1440]:
            ready, s, _ = model.step(float(r["B-TANK:temp_c"]), float(r["B-TANK:ph"]), float(r["B-TANK:tds_ppm"]),
                                     turbidity if turbidity is not None else float(r["B-TANK:turbidity_ntu"]),
                                     ts_s=float(r["t_s"]))
            if ready:
                hours.append(float(r["t_s"]) % 86400 / 3600)
                score.append(s)
        return np.array(hours), np.array(score), model.threshold

    fig, axes = plt.subplots(1, 2, figsize=(14, 4.6))
    window = (t >= 86400 + 5 * 3600) & (t < 86400 + 12 * 3600)
    axes[0].plot((t[window] - 86400) / 3600, flow[window], color=SERIES[1], lw=1.4, label="B-101 flow in sensors.csv")
    axes[0].axhline(0.005, color=INK, lw=1.2, ls="--", label="'water is off' limit, 0.005 L/s")
    axes[0].set_title("Flats: the water never stops for 3 h", fontsize=10.5, loc="left")
    axes[0].set_xlabel("hour of day 2")
    axes[0].set_ylabel("L/s")
    axes[0].set_ylim(0, None)
    axes[0].legend(frameon=False, fontsize=9)
    stats = {}
    for label, fix, color in (("as in sensors.csv", None, SERIES[0]), ("turbidity noise removed", 0.5, SERIES[2])):
        hours, score, threshold = temporal(0, fix)
        early = hours < 6
        axes[1].plot(hours[early], score[early], color=color, lw=1.2, label=label)
        stats[label] = (float(np.median(score[early])), float(100 * np.mean(score[early] > threshold)))
    axes[1].axhline(threshold, color=INK, lw=1.2, ls="--", label=f"alert threshold {threshold:.1f}")
    axes[1].set_yscale("log")
    axes[1].set_title("Tank: temporal score, 00:00-06:00 of the normal day", fontsize=10.5, loc="left")
    axes[1].set_xlabel("hour of day 1")
    axes[1].set_ylabel("anomaly score (log scale)")
    axes[1].legend(frameon=False, fontsize=9)
    finish(fig, axes, OUT, "exp1_false_alarm_causes", "Why sensors.csv raises false alarms")

    model = TemporalModel()
    turb = np.log1p(col("B-TANK:turbidity_ntu")[:360])
    write_table(OUT, "exp1_false_alarm_causes", ["Finding", "Value"], [
        ["B-101 longest run above 0.005 L/s, normal day 1 / day 2 (rule alerts at 180 min)",
         f"{longest[0]} min / {longest[1]} min"],
        ["Temporal score 00-06h day 1 as recorded: median, share above threshold",
         f"{stats['as in sensors.csv'][0]:.0f}, {stats['as in sensors.csv'][1]:.0f}%"],
        ["Same with turbidity held at 0.5 NTU",
         f"{stats['turbidity noise removed'][0]:.1f}, {stats['turbidity noise removed'][1]:.0f}%"],
        ["Spread of log1p turbidity in sensors.csv / model's training spread of its short-term change",
         f"{turb.std():.4f} / {model.sd[3]:.5f} ({turb.std() / model.sd[3]:.0f} times)"]])


def main():
    with open(os.path.join(OUT, "detection_trials.json")) as f:
        rows = json.load(f)
    experiment4(rows)
    investigate()
    print("done")


if __name__ == "__main__":
    main()
