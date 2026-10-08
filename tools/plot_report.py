"""Optional static scientific figure from an already computed report."""
import argparse
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

def plot(directory):
    directory = Path(directory)
    report = json.loads((directory / "report.json").read_text())
    names = ("persistence", "microprice", "ridge")
    colors = ("#718198", "#3676db", "#db7a36")
    conditions = sorted({row["condition"] for row in report["by_market_condition"]})
    fig, axes = plt.subplots(1, 2, figsize=(12, 5), layout="constrained")
    overall = {row["model"]: row for row in report["overall"]}
    axes[0].bar(names, [overall[name]["mae"] for name in names], color=colors)
    axes[0].set(ylabel="Mean absolute error (USD)", title="One-second midpoint forecast")
    x = np.arange(len(conditions))
    for i, (name, color) in enumerate(zip(names, colors)):
        # Pool by condition only for this visual; full by-day detail remains in CSV.
        vals = []
        for condition in conditions:
            rows = [r for r in report["by_market_condition"] if r["condition"] == condition and r["model"] == name]
            vals.append(sum(r["mae"] * r["samples"] for r in rows) / sum(r["samples"] for r in rows))
        axes[1].bar(x + (i - 1) * .24, vals, width=.24, label=name, color=color)
    counts = {c: sum(r["samples"] for r in report["by_market_condition"] if r["condition"] == c and r["model"] == "persistence") for c in conditions}
    axes[1].set_xticks(x, [c.replace("/", "\n").replace("_", " ") + f"\nn={counts[c]:,}" for c in conditions], fontsize=9)
    axes[1].set(title="Error by training-defined market condition", ylabel="Mean absolute error (USD)")
    axes[1].legend(frameon=False)
    for axis in axes:
        axis.spines[["top", "right"]].set_visible(False)
        axis.grid(axis="y", alpha=.15); axis.set_axisbelow(True)
    fig.suptitle(f"{report['product']} · {report['provenance'].upper()} · exploratory observations", fontweight="bold")
    fig.savefig(directory / "forecast_errors.png", dpi=160)
    plt.close(fig)

if __name__ == "__main__":
    p = argparse.ArgumentParser(); p.add_argument("report_directory"); a = p.parse_args(); plot(a.report_directory)
