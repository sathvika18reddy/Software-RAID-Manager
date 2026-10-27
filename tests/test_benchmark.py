"""Unit test for benchmark runner and workload generator."""

import pytest
from raid.workload import generate_workload
from raid.benchmark import run_full_benchmark, plot_throughput, plot_latency, plot_scheduler_movement


def test_workload_generator():
    for kind in ["sequential", "random", "mixed"]:
        reqs = generate_workload(kind, n_requests=30, max_lbn=20, read_ratio=0.7, seed=42)
        assert len(reqs) == 30
        for r in reqs:
            assert r["op"] in ("r", "w")
            assert 0 <= r["lbn"] < 20


def test_benchmark_run_and_plots():
    df_perf, df_sched, df_penalty, df_comp = run_full_benchmark(n_requests=20, num_blocks=16, seed=123)

    assert len(df_perf) == 9  # 3 levels * 3 workloads
    assert set(df_perf["level"]) == {"RAID 0", "RAID 1", "RAID 5"}
    assert set(df_perf["workload"]) == {"sequential", "random", "mixed"}

    assert len(df_sched) == 4  # FCFS, SCAN, C-SCAN, LOOK
    assert set(df_sched["policy"]) == {"FCFS", "SCAN", "C-SCAN", "LOOK"}
    scan_row = df_sched[df_sched["policy"] == "SCAN"].iloc[0]
    fcfs_row = df_sched[df_sched["policy"] == "FCFS"].iloc[0]
    assert scan_row["total_head_movement"] <= fcfs_row["total_head_movement"]

    assert len(df_penalty) == 3
    assert len(df_comp) == 3

    # Ensure plot functions produce Figures without throwing
    fig1 = plot_throughput(df_perf)
    fig2 = plot_latency(df_perf)
    fig3 = plot_scheduler_movement(df_sched)
    assert fig1 is not None
    assert fig2 is not None
    assert fig3 is not None
