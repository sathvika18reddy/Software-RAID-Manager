# Software RAID Manager: Fault-Tolerant Storage Simulator

An operating system storage stack simulator implementing software RAID (RAID 0, 1, and 5) over virtual disks. It models physical disk latency (seek distance, rotational latency, transfer delay), request queues and scheduling policies (FCFS, SCAN), stripe-level concurrency control, failure injection, degraded read/write recovery, online rebuild, hot spare auto-activation, parity scrubbing, write-hole syndrome demonstration, and a full Streamlit dashboard.

---

## 1. Five-Layer Architecture

```
 ┌─────────────────────────────────────────────────────────┐
 │ Layer 1: Logical Volume Layer (read/write LBN, files)   │
 ├─────────────────────────────────────────────────────────┤
 │ Layer 2: RAID Layer (RAID 0, RAID 1, RAID 5, Rebuild)   │
 ├─────────────────────────────────────────────────────────┤
 │ Layer 3: Request Queue & Disk Scheduler (FCFS, SCAN)    │
 ├─────────────────────────────────────────────────────────┤
 │ Layer 4: Virtual Disks (blocks, latency, failure state) │
 ├─────────────────────────────────────────────────────────┤
 │ Layer 5: Concurrency Control (stripe locking & safety)  │
 └─────────────────────────────────────────────────────────┘
```

- **Layer 1 (Logical Volume Layer):** Provides a uniform block-device abstraction (`read_block(lbn)`, `write_block(lbn, data)`).
- **Layer 2 (RAID Layer):** Coordinates striping, mirroring, rotating parity, Read-Modify-Write (RMW), and online XOR reconstruction.
- **Layer 3 (Request Queue & Disk Scheduler):** Manages per-disk I/O queues and optimizes head movement using FCFS or the SCAN (elevator) algorithm.
- **Layer 4 (Virtual Disks):** Simulates cylinder head positions, rotational delay (2.0 ms), transfer time (0.1 ms/block), seek cost (0.5 ms/block), failure states, and statistics.
- **Layer 5 (Concurrency Control):** Enforces per-stripe critical sections (`StripeLockManager`) to eliminate lost parity updates during concurrent RMW small writes.

---

## 2. OS Concept Mapping

| Feature | OS Storage Stack Concept |
|---|---|
| RAID levels (0, 1, 5), striping, rotating parity | Mass-storage structure, block management |
| Request queues, FCFS vs. SCAN policies | Disk I/O scheduling and seek optimization |
| Stripe locks, race condition reproduction | Synchronization, mutual exclusion, critical sections |
| Write-hole syndrome, parity scrubbing, rebuild | Reliability, crash consistency, integrity auditing |
| Logical volume layer | Device driver and file system block interface |

---

## 3. Project Structure

```
RAID_MANAGER/
├── README.md                   # System documentation and instructions
├── requirements.txt            # Python dependencies
├── app.py                      # Interactive Streamlit dashboard
├── raid/
│   ├── __init__.py
│   ├── config.py               # Constants (BLOCK_SIZE, timing parameters)
│   ├── virtual_disk.py         # VirtualDisk & DiskFailedError
│   ├── scheduler.py            # DiskQueue, FCFS, and SCAN scheduling
│   ├── raid_base.py            # Abstract RaidArray base class & hot spares
│   ├── raid0.py                # RAID 0 implementation
│   ├── raid1.py                # RAID 1 mirroring implementation
│   ├── raid5.py                # RAID 5 rotating distributed parity & RMW
│   ├── locks.py                # StripeLockManager (per-stripe locking)
│   ├── oplog.py                # Thread-safe operation logger
│   ├── scrub.py                # Checksums, scrubbing, and write-hole demo
│   ├── workload.py             # Sequential, random, mixed workload generator
│   ├── benchmark.py            # Performance benchmark runner & matplotlib charts
│   └── reliability.py          # Monte Carlo data-loss reliability engine & sweeps
├── scripts/
│   └── run_reliability.py      # CLI runner for Monte Carlo reliability sweeps
├── tests/
│   ├── test_virtual_disk.py    # Disk I/O, failure, latency accounting
│   ├── test_raid0.py           # Striping, mapping, and data loss
│   ├── test_raid1.py           # Mirroring, N-1 survivability, rebuild
│   ├── test_raid5.py           # Rotating parity, degraded read/write, rebuild
│   ├── test_scheduler.py       # FCFS vs SCAN monotonic sweep & head movement
│   ├── test_concurrency.py     # Race condition reproduction & lock protection
│   ├── test_rebuild_hotspare_checksum.py  # Checksum matching & hot spares
│   ├── test_scrub_writehole.py # Silent corruption, write-hole crash & repair
│   ├── test_benchmark.py       # Benchmark metrics and plot generation
│   └── test_reliability.py     # Monte Carlo reliability, analytic MTTDL checks
└── report/
    ├── report.md               # Full academic/technical report
    ├── slides.md               # Presentation slide deck outline
    └── reliability_results.md  # Detailed Monte Carlo reliability analysis notes
```

---

## 4. Installation & Quickstart

### Prerequisites
- Python 3.10+ (tested on Python 3.11)
- `pip`

### Setup
```bash
pip install -r requirements.txt
```

### Running Tests
Execute the complete test suite (39 unit, integration, and reliability tests):
```bash
pytest -v
```

### Running Reliability Monte Carlo Experiments
```bash
python scripts/run_reliability.py --trials 20000 --mttf 100000 --rebuild 24 --out results/
```

### Launching the Streamlit UI
```bash
streamlit run app.py
```

---

## 5. Five-Minute Live Demo Script

1. **Array Creation & Initial I/O:**
   - In the sidebar, select **RAID 5**, 4 disks, 16 blocks/disk, 1 hot spare. Click **Create / Reset Array**.
   - In **Tab 1 (Array View)**, observe the rotating parity layout (P0..P15). Write the string `"Hello Software RAID!"` to LBN 0 and read it back.
2. **Failure Injection & Degraded Read:**
   - Under **Disk Operations**, select **Disk 0** and click **Kill Disk**.
   - Go to **Tab 2 (Failure & Recovery)**. Read LBN 0. Observe that degraded read executes an on-the-fly parity XOR across the remaining healthy disks and returns the correct string.
3. **Hot Spare Auto-Activation & Checksum Verification:**
   - In Tab 2, click **Save Pre-Failure Checksum**.
   - Click **Simulate Surprise Failure on Disk 0**. Observe the hot spare automatically claim the slot, start a background rebuild thread, and complete rebuild.
   - Click **Verify Post-Rebuild Checksum** to observe a green **PASS**.
4. **Concurrency & Race Condition Demo:**
   - Open **Tab 3 (Concurrency Demo)**.
   - Click **Run Unsafe Concurrent Writes (Locks DISABLED)**: Two threads write concurrently to LBN 0 and LBN 1 in the same stripe with an artificial delay. Parity is corrupted due to lost updates (scrub confirms inconsistency).
   - Click **Run Safe Concurrent Writes (Locks ENABLED)**: Stripe locks serialize the RMW critical sections. 0 inconsistencies found!
5. **Write-Hole Syndrome & Parity Scrubbing:**
   - Open **Tab 4 (Write-Hole & Scrub)**.
   - Click **Simulate Crash Mid-Write**: Data is written but the system crashes before updating parity.
   - Click **Demonstrate Silent Corruption**: Kill another disk in the stripe and read it back to observe corrupted output.
   - Click **Scrub and Auto-Repair** to re-sync parity and restore data integrity.
6. **Disk Scheduler & Benchmarks:**
   - Open **Tab 5 (Scheduler)** and run **Compare FCFS vs SCAN** to observe how SCAN reduces head movement.
   - Open **Tab 6 (Benchmarks)** and click **Run Full Benchmark Suite** to display comparative throughput, latency, small-write penalty tables, and storage efficiency across RAID 0, 1, and 5.

---

## 6. RAID Architectural Comparison

| Architecture | Min Disks | Usable Capacity | Fault Tolerance | Read Perf | Write Perf | Small-Write Penalty |
|---|---|---|---|---|---|---|
| **RAID 0** | 2 | $N \times S$ | None (0 disks) | High | High | None (0R + 1W) |
| **RAID 1** | 2 | $S$ | $N - 1$ disks | High | Moderate | $N$ writes per block |
| **RAID 5** | 3 | $(N - 1) \times S$ | 1 disk | High | Moderate | 2 Reads + 2 Writes (RMW) |
