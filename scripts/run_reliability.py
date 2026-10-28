import os
import sys
import argparse

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from raid.reliability import (
    ReliabilityParams,
    run_simulation,
    sweep_rebuild_time,
    sweep_num_disks,
    compare_levels,
    analytic_mttdl_raid5,
    analytic_loss_probability,
)


def run_experiments(
    trials: int = 20000,
    mttf: float = 100000.0,
    rebuild: float = 24.0,
    mission: float = 43800.0,
    seed: int = 42,
    out_dir: str = "results",
) -> None:
    """Execute all four reliability experiments and save charts and CSVs."""
    os.makedirs(out_dir, exist_ok=True)
    base_params = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=mttf,
        rebuild_hours=rebuild,
        mission_hours=mission,
        n_trials=trials,
        seed=seed,
    )

    print(f"==================================================================")
    print(f" RAID RELIABILITY MONTE CARLO EXPERIMENTS")
    print(f" Trials: {trials:,} | MTTF: {mttf:,.0f}h | Rebuild: {rebuild}h | Mission: {mission:,.0f}h")
    print(f" Output Directory: {out_dir}")
    print(f"==================================================================\n")

    # -------------------------------------------------------------------------
    # Experiment 1: Compare Architectures (RAID 0, 1, 5, 6, 10)
    # -------------------------------------------------------------------------
    print("[1/4] Running Experiment 1: Level Comparison (RAID 0, 1, 5, 6, 10)...")
    levels = ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"]
    df_exp1 = compare_levels(base_params, levels)
    csv1 = os.path.join(out_dir, "exp1_level_comparison.csv")
    df_exp1.to_csv(csv1, index=False)

    fig1, ax1 = plt.subplots(figsize=(8, 4.5))
    yerr = [df_exp1["p_loss"] - df_exp1["ci_low"], df_exp1["ci_high"] - df_exp1["p_loss"]]
    bars = ax1.bar(
        df_exp1["level"],
        df_exp1["p_loss"],
        yerr=yerr,
        capsize=5,
        color=["#e53e3e", "#319795", "#dd6b20", "#805ad5", "#0ea5e9"],
        edgecolor="black",
        width=0.55,
    )
    ax1.set_title("Probability of Data Loss by RAID Level (5-Year Mission)", fontsize=12, fontweight="bold")
    ax1.set_xlabel("Architecture", fontsize=11)
    ax1.set_ylabel("P(Data Loss)", fontsize=11)
    ax1.grid(axis="y", linestyle="--", alpha=0.6)

    for bar, p in zip(bars, df_exp1["p_loss"]):
        ax1.text(
            bar.get_x() + bar.get_width() / 2.0,
            bar.get_height() + 0.01,
            f"{p:.4f}",
            ha="center",
            va="bottom",
            fontweight="bold",
            fontsize=10,
        )

    plt.tight_layout()
    png1 = os.path.join(out_dir, "exp1_level_comparison.png")
    fig1.savefig(png1, dpi=300)
    plt.close(fig1)

    # -------------------------------------------------------------------------
    # Experiment 2: Rebuild-Time Sweep (RAID 5) with Analytic Overlay
    # -------------------------------------------------------------------------
    print("[2/4] Running Experiment 2: Rebuild Time Sweep on RAID 5...")
    rebuild_values = [1.0, 6.0, 12.0, 24.0, 48.0, 96.0, 168.0]
    df_exp2 = sweep_rebuild_time(base_params, rebuild_values)

    # Calculate analytic curve
    analytic_p = []
    for r in rebuild_values:
        mttdl = analytic_mttdl_raid5(mttf=base_params.mttf_hours, rebuild=r, n=base_params.num_disks)
        analytic_p.append(analytic_loss_probability(mttdl=mttdl, mission_hours=base_params.mission_hours))
    df_exp2["p_analytic"] = analytic_p

    csv2 = os.path.join(out_dir, "exp2_rebuild_sweep.csv")
    df_exp2.to_csv(csv2, index=False)

    fig2, ax2 = plt.subplots(figsize=(8, 4.5))
    yerr2 = [df_exp2["p_loss"] - df_exp2["ci_low"], df_exp2["ci_high"] - df_exp2["p_loss"]]
    ax2.errorbar(
        df_exp2["rebuild_hours"],
        df_exp2["p_loss"],
        yerr=yerr2,
        fmt="o-",
        capsize=4,
        color="#dd6b20",
        label="Monte Carlo Simulation",
        linewidth=2,
    )
    ax2.plot(
        rebuild_values,
        analytic_p,
        "k--",
        label="Analytic MTTDL Approx",
        linewidth=1.8,
    )
    ax2.set_title("RAID 5: P(Data Loss) vs. Rebuild Time", fontsize=12, fontweight="bold")
    ax2.set_xlabel("Rebuild Time (Hours)", fontsize=11)
    ax2.set_ylabel("P(Data Loss)", fontsize=11)
    ax2.legend()
    ax2.grid(True, linestyle="--", alpha=0.6)

    plt.tight_layout()
    png2 = os.path.join(out_dir, "exp2_rebuild_sweep.png")
    fig2.savefig(png2, dpi=300)
    plt.close(fig2)

    # -------------------------------------------------------------------------
    # Experiment 3: Disk-Count Sweep (RAID 5)
    # -------------------------------------------------------------------------
    print("[3/4] Running Experiment 3: Disk Count Sweep on RAID 5...")
    disk_counts = [3, 4, 5, 6, 8]
    df_exp3 = sweep_num_disks(base_params, disk_counts)
    csv3 = os.path.join(out_dir, "exp3_disk_count_sweep.csv")
    df_exp3.to_csv(csv3, index=False)

    fig3, ax3 = plt.subplots(figsize=(7, 4.5))
    yerr3 = [df_exp3["p_loss"] - df_exp3["ci_low"], df_exp3["ci_high"] - df_exp3["p_loss"]]
    ax3.errorbar(
        df_exp3["num_disks"],
        df_exp3["p_loss"],
        yerr=yerr3,
        fmt="s-",
        capsize=4,
        color="#2b6cb0",
        label="RAID 5 Monte Carlo",
        linewidth=2,
    )
    ax3.set_title("RAID 5: P(Data Loss) vs. Number of Disks", fontsize=12, fontweight="bold")
    ax3.set_xlabel("Array Disk Count (N)", fontsize=11)
    ax3.set_ylabel("P(Data Loss)", fontsize=11)
    ax3.set_xticks(disk_counts)
    ax3.legend()
    ax3.grid(True, linestyle="--", alpha=0.6)

    plt.tight_layout()
    png3 = os.path.join(out_dir, "exp3_disk_count_sweep.png")
    fig3.savefig(png3, dpi=300)
    plt.close(fig3)

    # -------------------------------------------------------------------------
    # Experiment 4: Mission Time Sweep (Years 1 to 10)
    # -------------------------------------------------------------------------
    print("[4/4] Running Experiment 4: Mission Time View (Years 1 to 10)...")
    years = list(range(1, 11))
    exp4_records = []

    arch_configs = [
        ("RAID 0", "#e53e3e", "o", 4),
        ("RAID 1", "#319795", "^", 2),
        ("RAID 5", "#dd6b20", "s", 4),
        ("RAID 6", "#805ad5", "D", 4),
        ("RAID 10", "#0ea5e9", "v", 4),
    ]

    for yr in years:
        hrs = yr * 8760.0
        for lvl, color, marker, n_d in arch_configs:
            p = ReliabilityParams(
                level=lvl,
                num_disks=n_d,
                mttf_hours=mttf,
                rebuild_hours=rebuild,
                mission_hours=hrs,
                n_trials=trials // 2,
                seed=seed + yr,
            )
            res = run_simulation(p)
            exp4_records.append({"year": yr, "level": lvl, "p_loss": res.p_loss, "ci_low": res.ci_low, "ci_high": res.ci_high})

    df_exp4 = pd.DataFrame(exp4_records)
    csv4 = os.path.join(out_dir, "exp4_mission_time_sweep.csv")
    df_exp4.to_csv(csv4, index=False)

    fig4, ax4 = plt.subplots(figsize=(8.5, 4.5))
    for lvl, color, marker, _ in arch_configs:
        sub = df_exp4[df_exp4["level"] == lvl]
        ax4.plot(sub["year"], sub["p_loss"], marker=marker, label=lvl, color=color, linewidth=2)

    ax4.set_title("Probability of Data Loss over Mission Duration (1 to 10 Years)", fontsize=12, fontweight="bold")
    ax4.set_xlabel("Mission Duration (Years)", fontsize=11)
    ax4.set_ylabel("P(Data Loss)", fontsize=11)
    ax4.set_xticks(years)
    ax4.legend()
    ax4.grid(True, linestyle="--", alpha=0.6)

    plt.tight_layout()
    png4 = os.path.join(out_dir, "exp4_mission_time_sweep.png")
    fig4.savefig(png4, dpi=300)
    plt.close(fig4)

    print("\nAll experiments completed successfully!")
    print(f"Results saved in '{out_dir}':")
    print(f" - {png1} | {csv1}")
    print(f" - {png2} | {csv2}")
    print(f" - {png3} | {csv3}")
    print(f" - {png4} | {csv4}\n")

    print("Summary of Architecture Comparison (5-Year Mission):")
    print(df_exp1.to_string(index=False))


def main():
    parser = argparse.ArgumentParser(description="Monte Carlo Reliability Simulation for RAID Arrays")
    parser.add_argument("--trials", type=int, default=20000, help="Number of Monte Carlo trials per configuration")
    parser.add_argument("--mttf", type=float, default=100000.0, help="Mean Time to Failure per disk in hours")
    parser.add_argument("--rebuild", type=float, default=24.0, help="Rebuild time in hours")
    parser.add_argument("--mission", type=float, default=43800.0, help="Mission duration in hours (default 5 years)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--out", type=str, default="results", help="Output directory for CSVs and PNGs")
    args = parser.parse_args()

    run_experiments(
        trials=args.trials,
        mttf=args.mttf,
        rebuild=args.rebuild,
        mission=args.mission,
        seed=args.seed,
        out_dir=args.out,
    )


if __name__ == "__main__":
    main()
