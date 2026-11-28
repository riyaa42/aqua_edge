"""Shared look of the figures (light surface, one fixed colour per node) and table writing."""
import csv
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]       # fixed order; checked for colour blindness
NODE_COLOR = dict(zip(("B-TANK", "B-101", "B-203", "B-301"), SERIES))

plt.rcParams.update({"figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
                     "text.color": INK, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
                     "axes.edgecolor": GRID, "font.size": 10.5})


def new_figure(width, height):
    return plt.subplots(figsize=(width, height))


def finish(fig, axes, folder, name, title):
    for ax in (axes.flat if hasattr(axes, "flat") else [axes]):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(True, axis="both", color=GRID, lw=0.8)
        ax.set_axisbelow(True)
    fig.suptitle(title, x=0.01, ha="left", fontsize=13.5, fontweight="bold", color=INK)
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    os.makedirs(os.path.join(folder, "figures"), exist_ok=True)
    fig.savefig(os.path.join(folder, "figures", f"{name}.png"), dpi=200)
    plt.close(fig)


def write_table(folder, name, header, rows):
    """The same table as CSV and as Markdown."""
    os.makedirs(os.path.join(folder, "tables"), exist_ok=True)
    with open(os.path.join(folder, "tables", f"{name}.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    with open(os.path.join(folder, "tables", f"{name}.md"), "w") as f:
        f.write("| " + " | ".join(header) + " |\n|" + "---|" * len(header) + "\n")
        f.writelines("| " + " | ".join(str(c) for c in row) + " |\n" for row in rows)
