"""Matplotlib styling helpers matching Software RAID Manager design tokens."""

import matplotlib.pyplot as plt
import pandas as pd

# Color tokens for dark server console
COLOR_PANEL = "#131B2A"
COLOR_INK = "#F8FAFC"
COLOR_INK_SOFT = "#94A3B8"
COLOR_LINE = "#232F45"
COLOR_DATA = "#3B82F6"
COLOR_PARITY = "#F59E0B"
COLOR_MIRROR = "#14B8A6"
COLOR_FAILED = "#EF4444"
COLOR_OK = "#10B981"
COLOR_INCONSISTENT = "#A855F7"


COLOR_RAID6 = "#805AD5"
COLOR_RAID10 = "#0EA5E9"

LEVEL_COLORS = {
    "RAID 0": COLOR_DATA,
    "RAID 1": COLOR_MIRROR,
    "RAID 5": COLOR_PARITY,
    "RAID 6": COLOR_RAID6,
    "RAID 10": COLOR_RAID10,
}


def apply_theme_to_axes(ax: plt.Axes) -> None:
    """Apply minimal, projector-ready styling to a matplotlib Axes."""
    ax.set_facecolor(COLOR_PANEL)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color(COLOR_LINE)
    ax.spines["bottom"].set_color(COLOR_LINE)
    ax.tick_params(colors=COLOR_INK_SOFT, labelsize=10)
    ax.xaxis.label.set_color(COLOR_INK)
    ax.yaxis.label.set_color(COLOR_INK)
    ax.title.set_color(COLOR_INK)
    ax.grid(axis="y", linestyle="--", alpha=0.5, color=COLOR_LINE)


def plot_benchmark_throughput(df_perf: pd.DataFrame) -> plt.Figure:
    """Bar chart of throughput per RAID level per workload."""
    fig, ax = plt.subplots(figsize=(7.5, 3.8), facecolor=COLOR_PANEL)
    apply_theme_to_axes(ax)

    pivot = df_perf.pivot(index="workload", columns="level", values="throughput_req_per_sec")
    level_order = ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"]
    pivot = pivot[[c for c in level_order if c in pivot.columns]]
    colors = [LEVEL_COLORS.get(col, "#64748B") for col in pivot.columns]
    pivot.plot(kind="bar", ax=ax, width=0.75, color=colors)

    ax.set_title("Throughput by workload (req / simulated sec)", fontsize=12, fontweight="bold", pad=8)
    ax.set_xlabel("Workload pattern", fontsize=10)
    ax.set_ylabel("Throughput (req/s)", fontsize=10)
    ax.tick_params(axis="x", rotation=0)
    ax.legend(frameon=False, labelcolor=COLOR_INK)

    plt.tight_layout()
    return fig


def plot_benchmark_latency(df_perf: pd.DataFrame) -> plt.Figure:
    """Side-by-side bar chart of average and p95 latency."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 3.8), facecolor=COLOR_PANEL)
    apply_theme_to_axes(ax1)
    apply_theme_to_axes(ax2)

    pivot_avg = df_perf.pivot(index="workload", columns="level", values="avg_latency_ms")
    pivot_p95 = df_perf.pivot(index="workload", columns="level", values="p95_latency_ms")
    level_order = ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"]
    pivot_avg = pivot_avg[[c for c in level_order if c in pivot_avg.columns]]
    pivot_p95 = pivot_p95[[c for c in level_order if c in pivot_p95.columns]]
    colors = [LEVEL_COLORS.get(col, "#64748B") for col in pivot_avg.columns]

    pivot_avg.plot(kind="bar", ax=ax1, width=0.75, color=colors, legend=False)
    ax1.set_title("Average latency (ms)", fontsize=11, fontweight="bold")
    ax1.set_xlabel("Workload")
    ax1.set_ylabel("Simulated ms")
    ax1.tick_params(axis="x", rotation=0)

    pivot_p95.plot(kind="bar", ax=ax2, width=0.75, color=colors)
    ax2.set_title("P95 latency (ms)", fontsize=11, fontweight="bold")
    ax2.set_xlabel("Workload")
    ax2.set_ylabel("Simulated ms")
    ax2.tick_params(axis="x", rotation=0)
    ax2.legend(frameon=False, labelcolor=COLOR_INK)

    plt.tight_layout()
    return fig


def plot_scheduler_comparison(served_fcfs: list[dict], served_scan: list[dict], head_fcfs: int, head_scan: int) -> plt.Figure:
    """Trace plot of head position over requests comparing FCFS and SCAN."""
    fig, ax = plt.subplots(figsize=(7, 3.5), facecolor=COLOR_PANEL)
    apply_theme_to_axes(ax)

    pbns_fcfs = [s["pbn"] for s in served_fcfs]
    pbns_scan = [s["pbn"] for s in served_scan]

    ax.plot(pbns_fcfs, label=f"FCFS ({head_fcfs} blocks)", marker="o", markersize=4, color=COLOR_FAILED, linewidth=1.5, alpha=0.8)
    ax.plot(pbns_scan, label=f"SCAN ({head_scan} blocks)", marker="s", markersize=4, color=COLOR_OK, linewidth=1.5, alpha=0.8)

    ax.set_title("Actuator head movement: FCFS vs. SCAN", fontsize=11, fontweight="bold", pad=8)
    ax.set_xlabel("Request step", fontsize=10)
    ax.set_ylabel("Physical block (cylinder)", fontsize=10)
    ax.legend(frameon=False, labelcolor=COLOR_INK)

    plt.tight_layout()
    return fig
