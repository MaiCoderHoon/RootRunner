# makes all the graphs (exp 1-5) from the csv files in experiments/results
# saves png files to experiments/plots
#
# run from project folder:  python -m experiments.make_plots

import csv
import glob
import statistics
from collections import Counter
from pathlib import Path

import matplotlib
matplotlib.use("Agg")   # no window, just save files
import matplotlib.pyplot as plt

res_dir = Path("experiments/results")
plot_dir = Path("experiments/plots")

BLUE, ORANGE, AQUA, YELLOW = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"
INK, GREY = "#0b0b0b", "#52514e"

plt.rcParams.update({
    "font.size": 10, "axes.edgecolor": GREY, "axes.labelcolor": INK,
    "xtick.color": GREY, "ytick.color": GREY, "axes.spines.top": False,
    "axes.spines.right": False, "axes.grid": True, "grid.color": "#e6e5e1",
    "grid.linewidth": 0.6, "axes.axisbelow": True, "figure.dpi": 130,
})


def read(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def save(fig, name):
    plot_dir.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(plot_dir / name)
    plt.close(fig)
    print("saved", plot_dir / name)


def plot_cold_warm():
    rows = []
    for p in sorted(glob.glob(str(res_dir / "exp1_cold_warm_*.csv"))):
        rows += read(p)
    if not rows:
        print("no exp1 files, skipping")
        return
    cold = [float(r["latency_ms"]) for r in rows if r["outcome"] == "MISS"]
    warm = [float(r["latency_ms"]) for r in rows if r["outcome"] == "HIT"]

    fig, ax = plt.subplots(figsize=(6, 4))
    # spread the dots sideways a bit so they dont sit on top of each other
    for x, vals, col in [(0, cold, BLUE), (1, warm, ORANGE)]:
        xs = [x + (i % 9 - 4) * 0.03 for i in range(len(vals))]
        ax.scatter(xs, vals, s=22, color=col, alpha=0.8, edgecolors="white", linewidths=0.8)
        med = statistics.median(vals)
        ax.hlines(med, x - 0.25, x + 0.25, color=INK, linewidth=2)
        ax.annotate(f"median {med:.3g} ms", (x + 0.28, med), va="center", color=INK, fontsize=9)
    ax.set_yscale("log")
    ax.set_xticks([0, 1], [f"cold (cache miss)\nn={len(cold)}", f"warm (cache hit)\nn={len(warm)}"])
    ax.set_xlim(-0.5, 1.9)
    ax.set_ylabel("lookup time (ms, log scale)")
    ax.set_title("Cold vs warm lookup latency", loc="left", color=INK)
    save(fig, "exp1_cold_vs_warm.png")


def plot_queries():
    runs = {"answer cache only": "exp2_answer_only.csv",
            "with delegation cache": "exp2_with_delegation.csv"}
    counts = {}
    for label, fname in runs.items():
        p = res_dir / fname
        if not p.exists():
            print("missing", p, "skipping exp2 plot")
            return
        q = [int(r["queries_sent"]) for r in read(p) if r["outcome"] == "MISS"]
        counts[label] = Counter(q)

    top = max(max(c) for c in counts.values())
    xs = list(range(1, top + 1))
    w = 0.38
    fig, ax = plt.subplots(figsize=(7, 4))
    for i, (label, c) in enumerate(counts.items()):
        mean = sum(k * v for k, v in c.items()) / sum(c.values())
        offset = (i - 0.5) * (w + 0.02)
        ax.bar([x + offset for x in xs], [c.get(x, 0) for x in xs], width=w,
               color=[BLUE, ORANGE][i], label=f"{label} (mean {mean:.2f})")
    ax.set_xticks(xs)
    ax.set_xlabel("queries sent for one lookup")
    ax.set_ylabel("number of lookups")
    ax.grid(axis="x", visible=False)
    ax.legend(frameon=False, ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0))
    ax.set_title("Queries per lookup: 114 successful of 115 sites", loc="left", color=INK, pad=28)
    save(fig, "exp2_queries_per_lookup.png")


def plot_hit_ratio():
    p = res_dir / "exp3_replay.csv"
    if not p.exists():
        print("missing", p, "skipping exp3 plot")
        return
    rows = read(p)
    hits, ratio, best, seen = 0, [], [], set()
    for i, r in enumerate(rows, 1):
        if r["outcome"] in ("HIT", "NEGATIVE_HIT"):
            hits += 1
        seen.add(r["name"])
        ratio.append(hits / i)
        best.append((i - len(seen)) / i)   # if only first lookups ever missed

    xs = range(1, len(rows) + 1)
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.plot(xs, best, color=GREY, linewidth=1.5, linestyle="--", label="best possible")
    ax.plot(xs, ratio, color=BLUE, linewidth=2, label="our cache")
    ax.annotate(f"{ratio[-1]:.2f}", (len(rows), ratio[-1]), xytext=(6, -4),
                textcoords="offset points", color=INK)
    ax.set_ylim(0, 1)
    ax.set_xlabel("lookups so far")
    ax.set_ylabel("hit ratio")
    ax.legend(frameon=False, loc="lower right")
    ax.set_title("Cache hit ratio over a replayed trace (500 lookups)", loc="left", color=INK)
    save(fig, "exp3_hit_ratio.png")


def plot_dead_server():
    p = res_dir / "exp4_dead_server.csv"
    if not p.exists():
        print("missing", p, "skipping exp4 plot")
        return
    rows = read(p)
    sites = list(dict.fromkeys(r["site"] for r in rows))
    scen = ["normal", "auth_dead", "half_roots", "all_roots"]
    colors = [BLUE, ORANGE, AQUA, YELLOW]
    w = 0.2

    fig, ax = plt.subplots(figsize=(8, 4.5))
    for j, (s, col) in enumerate(zip(scen, colors)):
        for i, site in enumerate(sites):
            r = next((r for r in rows if r["site"] == site and r["scenario"] == s), None)
            if r is None:
                continue
            ms = float(r["latency_ms"])
            x = i + (j - 1.5) * (w + 0.02)
            ax.bar(x, ms / 1000, width=w, color=col, label=s if i == 0 else None)
            tag = "fail" if r["status"] == "failed" else f"{ms / 1000:.1f}"
            ax.annotate(tag, (x, ms / 1000), xytext=(0, 2), textcoords="offset points",
                        ha="center", fontsize=7, color=INK)
    ax.set_xticks(range(len(sites)), sites)
    ax.set_ylabel("lookup time (s)")
    ax.grid(axis="x", visible=False)
    ax.set_ylim(0, 7)
    # legend above the plot so it doesnt cover the tall bars
    ax.legend(frameon=False, ncol=4, loc="lower left", bbox_to_anchor=(0, 1.0))
    ax.set_title("Lookup time when servers are unreachable (1 s timeout)", loc="left", color=INK, pad=28)
    save(fig, "exp4_dead_server.png")


def plot_vs_dig():
    p = res_dir / "exp5_vs_dig.csv"
    if not p.exists():
        print("missing", p, "skipping exp5 plot")
        return
    rows = read(p)
    sites = [r["site"] for r in rows]
    dig_q = [int(r["dig_queries"]) for r in rows]
    our_q = [int(r["our_queries"]) for r in rows]
    h = 0.38
    ys = range(len(rows))

    fig, ax = plt.subplots(figsize=(7, 0.32 * len(rows) + 1.4))
    ax.barh([y - h / 2 - 0.01 for y in ys], dig_q, height=h, color=BLUE, label="dig +trace")
    ax.barh([y + h / 2 + 0.01 for y in ys], our_q, height=h, color=ORANGE, label="our resolver")
    for y, (d, o) in enumerate(zip(dig_q, our_q)):
        if o != d:
            ax.annotate(f"{o}", (o, y + h / 2), xytext=(3, 0), textcoords="offset points",
                        va="center", fontsize=8, color=INK)
    ax.set_yticks(list(ys), sites, fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("queries sent")
    ax.grid(axis="y", visible=False)
    ax.legend(frameon=False, ncol=2, loc="lower left", bbox_to_anchor=(0, 1.0))
    ax.set_title("Queries per lookup: our resolver vs dig +trace", loc="left", color=INK, pad=28)
    save(fig, "exp5_vs_dig.png")


def main():
    plot_cold_warm()
    plot_queries()
    plot_hit_ratio()
    plot_dead_server()
    plot_vs_dig()


if __name__ == "__main__":
    main()