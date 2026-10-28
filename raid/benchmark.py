"""Benchmark runner and visualization for RAID storage performance evaluation."""

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from raid.virtual_disk import VirtualDisk
from raid.raid0 import Raid0
from raid.raid1 import Raid1
from raid.raid5 import Raid5
from raid.scheduler import DiskQueue
from raid.workload import generate_workload
from raid.config import BLOCK_SIZE


from raid.raid6 import Raid6
from raid.raid10 import Raid10


def run_benchmark_for_array(array, requests: list[dict]) -> dict:
    """Execute requests on a RAID array and compute simulated performance metrics."""
    # Reset disk stats
    for d in array.disks:
        d.head_position = 0
        d.stats["reads"] = 0
        d.stats["writes"] = 0
        d.stats["total_seek_distance"] = 0
        d.stats["total_simulated_ms"] = 0.0

    op_latencies = []

    for req in requests:
        op = req["op"]
        lbn = req["lbn"]
        # Measure simulated ms increment across all disks for this logical operation
        prev_ms = sum(d.stats["total_simulated_ms"] for d in array.disks)
        if op == "r":
            array.read_block(lbn)
        elif op == "w":
            array.write_block(lbn, b"X" * array.block_size)
        curr_ms = sum(d.stats["total_simulated_ms"] for d in array.disks)
        op_latencies.append(curr_ms - prev_ms)

    total_sim_ms = sum(d.stats["total_simulated_ms"] for d in array.disks)
    total_head_move = sum(d.stats["total_seek_distance"] for d in array.disks)
    total_disk_reads = sum(d.stats["reads"] for d in array.disks)
    total_disk_writes = sum(d.stats["writes"] for d in array.disks)

    n_req = len(requests)
    throughput = (n_req / (total_sim_ms / 1000.0)) if total_sim_ms > 0 else 0.0
    avg_latency = float(np.mean(op_latencies)) if op_latencies else 0.0
    p95_latency = float(np.percentile(op_latencies, 95)) if op_latencies else 0.0

    usable_cap = array.capacity_blocks()
    raw_cap = array.num_disks * array.disk_blocks
    storage_efficiency = usable_cap / raw_cap if raw_cap > 0 else 0.0

    return {
        "num_requests": n_req,
        "total_simulated_ms": total_sim_ms,
        "throughput_req_per_sec": throughput,
        "avg_latency_ms": avg_latency,
        "p95_latency_ms": p95_latency,
        "total_head_movement": total_head_move,
        "disk_reads": total_disk_reads,
        "disk_writes": total_disk_writes,
        "reads_per_op": total_disk_reads / n_req if n_req > 0 else 0.0,
        "writes_per_op": total_disk_writes / n_req if n_req > 0 else 0.0,
        "storage_efficiency": storage_efficiency,
    }


def run_full_benchmark(
    n_requests: int = 100,
    num_blocks: int = 32,
    seed: int = 42,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Run full benchmark matrix across RAID levels, workloads, and schedulers."""
    workloads = ["sequential", "random", "mixed"]
    records = []

    for w_name in workloads:
        # RAID 0 (4 disks)
        d0 = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(4)]
        r0 = Raid0(d0)
        reqs0 = generate_workload(w_name, n_requests, max_lbn=r0.capacity_blocks(), seed=seed)
        m0 = run_benchmark_for_array(r0, reqs0)
        records.append({"level": "RAID 0", "workload": w_name, **m0})

        # RAID 1 (4 disks)
        d1 = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(4)]
        r1 = Raid1(d1)
        reqs1 = generate_workload(w_name, n_requests, max_lbn=r1.capacity_blocks(), seed=seed)
        m1 = run_benchmark_for_array(r1, reqs1)
        records.append({"level": "RAID 1", "workload": w_name, **m1})

        # RAID 5 (4 disks)
        d5 = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(4)]
        r5 = Raid5(d5)
        reqs5 = generate_workload(w_name, n_requests, max_lbn=r5.capacity_blocks(), seed=seed)
        m5 = run_benchmark_for_array(r5, reqs5)
        records.append({"level": "RAID 5", "workload": w_name, **m5})

        # RAID 6 (4 disks)
        d6 = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(4)]
        r6 = Raid6(d6)
        reqs6 = generate_workload(w_name, n_requests, max_lbn=r6.capacity_blocks(), seed=seed)
        m6 = run_benchmark_for_array(r6, reqs6)
        records.append({"level": "RAID 6", "workload": w_name, **m6})

        # RAID 10 (4 disks)
        d10 = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(4)]
        r10 = Raid10(d10)
        reqs10 = generate_workload(w_name, n_requests, max_lbn=r10.capacity_blocks(), seed=seed)
        m10 = run_benchmark_for_array(r10, reqs10)
        records.append({"level": "RAID 10", "workload": w_name, **m10})

    df_perf = pd.DataFrame(records)

    # Scheduler benchmark (FCFS, SCAN, C-SCAN, LOOK on identical batch)
    sched_records = []
    rng = np.random.default_rng(seed)
    pbn_batch = rng.integers(0, num_blocks, size=n_requests).tolist()

    for policy in ["FCFS", "SCAN", "C-SCAN", "LOOK"]:
        disk = VirtualDisk(disk_id=0, num_blocks=num_blocks, block_size=BLOCK_SIZE)
        q = DiskQueue(disk=disk, policy=policy)
        for pbn in pbn_batch:
            q.submit({"op": "r", "pbn": int(pbn)})
        q.process_all()
        sched_records.append({
            "policy": policy,
            "requests": n_requests,
            "total_head_movement": q.total_head_movement,
            "simulated_ms": disk.stats["total_simulated_ms"],
        })
    df_sched = pd.DataFrame(sched_records)

    # Small-write penalty table
    df_penalty = pd.DataFrame([
        {"Level": "RAID 0", "Logical Read (Disk Reads)": 1, "Logical Write (Disk Writes)": "1", "Disk Reads on Write": 0, "Disk Writes on Write": 1, "Total IOs per Write": 1},
        {"Level": "RAID 1", "Logical Read (Disk Reads)": 1, "Logical Write (Disk Writes)": "N (mirrors)", "Disk Reads on Write": 0, "Disk Writes on Write": 4, "Total IOs per Write": 4},
        {"Level": "RAID 5", "Logical Read (Disk Reads)": 1, "Logical Write (Disk Writes)": "2R + 2W (RMW)", "Disk Reads on Write": 2, "Disk Writes on Write": 2, "Total IOs per Write": 4},
        {"Level": "RAID 6", "Logical Read (Disk Reads)": 1, "Logical Write (Disk Writes)": "3R + 3W (P+Q RMW)", "Disk Reads on Write": 3, "Disk Writes on Write": 3, "Total IOs per Write": 6},
        {"Level": "RAID 10", "Logical Read (Disk Reads)": 1, "Logical Write (Disk Writes)": "2W (mirrored pair)", "Disk Reads on Write": 0, "Disk Writes on Write": 2, "Total IOs per Write": 2},
    ])

    # Comparison summary table (with measured throughput from random workload)
    rand_r0 = df_perf[(df_perf["level"] == "RAID 0") & (df_perf["workload"] == "random")].iloc[0]
    rand_r1 = df_perf[(df_perf["level"] == "RAID 1") & (df_perf["workload"] == "random")].iloc[0]
    rand_r5 = df_perf[(df_perf["level"] == "RAID 5") & (df_perf["workload"] == "random")].iloc[0]
    rand_r6 = df_perf[(df_perf["level"] == "RAID 6") & (df_perf["workload"] == "random")].iloc[0]
    rand_r10 = df_perf[(df_perf["level"] == "RAID 10") & (df_perf["workload"] == "random")].iloc[0]

    df_comp = pd.DataFrame([
        {
            "Level": "RAID 0",
            "Min disks": 2,
            "Usable capacity": "N",
            "Fault tolerance": "none",
            "Read perf": "high",
            "Write perf": "high",
            "Measured Throughput (req/s)": f"{rand_r0['throughput_req_per_sec']:.1f}",
            "Storage Efficiency": f"{rand_r0['storage_efficiency']*100:.1f}%",
        },
        {
            "Level": "RAID 1",
            "Min disks": 2,
            "Usable capacity": "1 (of N mirrors)",
            "Fault tolerance": "N - 1 disks",
            "Read perf": "high",
            "Write perf": "moderate",
            "Measured Throughput (req/s)": f"{rand_r1['throughput_req_per_sec']:.1f}",
            "Storage Efficiency": f"{rand_r1['storage_efficiency']*100:.1f}%",
        },
        {
            "Level": "RAID 5",
            "Min disks": 3,
            "Usable capacity": "N - 1",
            "Fault tolerance": "1 disk",
            "Read perf": "high",
            "Write perf": "write penalty (2R + 2W)",
            "Measured Throughput (req/s)": f"{rand_r5['throughput_req_per_sec']:.1f}",
            "Storage Efficiency": f"{rand_r5['storage_efficiency']*100:.1f}%",
        },
        {
            "Level": "RAID 6",
            "Min disks": 4,
            "Usable capacity": "N - 2",
            "Fault tolerance": "any 2 disks",
            "Read perf": "high",
            "Write perf": "double penalty (3R + 3W)",
            "Measured Throughput (req/s)": f"{rand_r6['throughput_req_per_sec']:.1f}",
            "Storage Efficiency": f"{rand_r6['storage_efficiency']*100:.1f}%",
        },
        {
            "Level": "RAID 10",
            "Min disks": "4 (even)",
            "Usable capacity": "N / 2 (50%)",
            "Fault tolerance": "up to N/2 (1 per pair)",
            "Read perf": "very high",
            "Write perf": "high (2 writes)",
            "Measured Throughput (req/s)": f"{rand_r10['throughput_req_per_sec']:.1f}",
            "Storage Efficiency": f"{rand_r10['storage_efficiency']*100:.1f}%",
        },
    ])

    return df_perf, df_sched, df_penalty, df_comp


# --- Plotting Helpers ---

def plot_throughput(df_perf: pd.DataFrame) -> plt.Figure:
    """Plot Throughput per RAID level per workload."""
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    pivot = df_perf.pivot(index="workload", columns="level", values="throughput_req_per_sec")
    level_order = ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"]
    pivot = pivot[[c for c in level_order if c in pivot.columns]]
    pivot.plot(kind="bar", ax=ax, width=0.75, colormap="viridis")
    ax.set_title("Throughput per RAID Level and Workload", fontsize=13, fontweight="bold", pad=10)
    ax.set_xlabel("Workload Pattern", fontsize=11)
    ax.set_ylabel("Throughput (Requests / Simulated Sec)", fontsize=11)
    ax.grid(axis="y", linestyle="--", alpha=0.6)
    plt.xticks(rotation=0)
    plt.tight_layout()
    return fig


def plot_latency(df_perf: pd.DataFrame) -> plt.Figure:
    """Plot Average and P95 Latency per RAID level per workload."""
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.5))
    pivot_avg = df_perf.pivot(index="workload", columns="level", values="avg_latency_ms")
    pivot_p95 = df_perf.pivot(index="workload", columns="level", values="p95_latency_ms")
    level_order = ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"]
    pivot_avg = pivot_avg[[c for c in level_order if c in pivot_avg.columns]]
    pivot_p95 = pivot_p95[[c for c in level_order if c in pivot_p95.columns]]

    pivot_avg.plot(kind="bar", ax=ax1, width=0.75, colormap="tab10")
    ax1.set_title("Average Latency (ms)", fontsize=12, fontweight="bold")
    ax1.set_xlabel("Workload")
    ax1.set_ylabel("Simulated ms")
    ax1.grid(axis="y", linestyle="--", alpha=0.6)
    ax1.tick_params(axis="x", rotation=0)

    pivot_p95.plot(kind="bar", ax=ax2, width=0.75, colormap="tab10")
    ax2.set_title("P95 Latency (ms)", fontsize=12, fontweight="bold")
    ax2.set_xlabel("Workload")
    ax2.set_ylabel("Simulated ms")
    ax2.grid(axis="y", linestyle="--", alpha=0.6)
    ax2.tick_params(axis="x", rotation=0)

    plt.tight_layout()
    return fig


def plot_scheduler_movement(df_sched: pd.DataFrame) -> plt.Figure:
    """Plot total head movement for FCFS vs SCAN vs C-SCAN vs LOOK."""
    fig, ax = plt.subplots(figsize=(6.5, 4))
    palette = {"FCFS": "#e53e3e", "SCAN": "#38a169", "C-SCAN": "#3182ce", "LOOK": "#dd6b20"}
    colors = [palette.get(p, "#4a5568") for p in df_sched["policy"]]
    bars = ax.bar(df_sched["policy"], df_sched["total_head_movement"], color=colors, width=0.5, edgecolor="black")
    ax.set_title("Disk Schedulers: Head Movement Comparison", fontsize=12, fontweight="bold", pad=10)
    ax.set_xlabel("Scheduling Policy", fontsize=11)
    ax.set_ylabel("Total Head Movement (Blocks)", fontsize=11)
    ax.grid(axis="y", linestyle="--", alpha=0.6)

    for bar in bars:
        yval = bar.get_height()
        ax.text(bar.get_x() + bar.get_width()/2.0, yval + 2, f"{int(yval)} blks", ha="center", va="bottom", fontweight="bold", fontsize=9)

    plt.tight_layout()
    return fig
