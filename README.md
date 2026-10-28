# Software RAID Manager: Fault-Tolerant Storage Simulator

A comprehensive Operating System storage stack simulator implementing software-defined RAID architectures over virtual mechanical disks. It faithfully models physical disk latency (cylinder seek distance, rotational delay, transfer time), elevator disk request schedulers (FCFS, SCAN, C-SCAN, LOOK), stripe-level concurrency mutual exclusion, crash consistency with write-intent journaling, ZFS-style per-block cryptographic checksum self-healing, LRU buffer caching, SMART predictive failure migration, Monte Carlo data-loss reliability modeling with URE risk, and an interactive 8-tab Streamlit dashboard.

---

## 1. Five-Layer Storage Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 1: Logical Volume Layer & LRU Buffer Cache                       │
│  - Linear block addressing (LBN 0..N-1)                                │
│  - LRU Block Cache (Write-Back dirty flushes / Write-Through)          │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 2: RAID Engine (RAID 0, 1, 5, 6, 10)                             │
│  - RAID 0: Pure Striping (K-block chunks, zero redundancy)             │
│  - RAID 1: Mirroring (N-1 failure survivability)                       │
│  - RAID 5: Distributed Rotating Parity (Single-disk fault tolerance)   │
│  - RAID 6: Dual Galois-Field GF(2^8) Reed-Solomon Parity (2 failures)  │
│  - RAID 10: Striped Mirrored Pairs (Up to N/2 independent failures)    │
│  - Hot spare auto-activation, online rebuild, and rate limiting        │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 3: Integrity, Concurrency & Crash Consistency                    │
│  - StripeLockManager: Per-stripe critical sections for concurrent RMW  │
│  - WriteIntentJournal: Write-ahead logging closing Write-Hole syndrome │
│  - BlockChecksumManager: Per-block SHA-256 self-healing verification   │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 4: Request Queue & Disk Schedulers                               │
│  - DiskQueue per spindle with FCFS, SCAN, C-SCAN, and LOOK policies    │
│  - Seek distance minimization and cylinder travel accounting           │
├────────────────────────────────────────────────────────────────────────┤
│ Layer 5: Virtual Disks & Hardware Telemetry                            │
│  - Cylinder head positioning, rotational delay, and transfer cost      │
│  - Failure injection, sector corruption, and SMART wear indicators     │
│  - Proactive hot-spare data migration upon threshold degradation       │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Operating System Concepts Demonstrated

| OS Concept | Simulator Implementation | Real-World OS Equivalent |
|---|---|---|
| **Mass-Storage Structure** | Block mapping, striping, rotating parity, chunk units | Linux `mdadm`, FreeBSD `geom_raid`, Windows LDM |
| **Disk I/O Scheduling** | FCFS, SCAN (Elevator), C-SCAN (Circular), LOOK | Linux `mq-deadline`, `bfq`, `kyber` I/O schedulers |
| **Critical Sections & Synchronization** | `StripeLockManager` serializing RMW update paths | Kernel stripe mutexes, spinlocks in RAID drivers |
| **Crash Consistency & Write-Ahead Logging** | `WriteIntentJournal` logging stripes before parity write | Linux MD Write-Intent Bitmap, WAL, ZFS ZIL |
| **Silent Bit-Rot & Self-Healing** | Per-block SHA-256 checksums with parity reconstruction | ZFS Merkle tree data integrity, Btrfs scrub |
| **Buffer Cache & Replacement** | `LruBlockCache` with dirty block tracking and flush | OS Page Cache, Buffer Cache, `bdflush` / `pdflush` |
| **Failure Prediction & Reliability** | SMART wear signals + Monte Carlo Markov MTTDL modeling | Drive SMART monitoring (`smartd`), Google/Backblaze models |
| **Workload Trace Analysis** | Synthetic generators + real-world CSV trace replay | `blktrace`, `fio`, block-layer replay utilities |

---

## 3. Supported RAID Architectures

| Level | Min Disks | Usable Capacity | Fault Tolerance | Read IOs | Write IOs (Small Write) | Primary Trade-Off |
|:---:|:---:|:---:|:---:|:---:|:---:|---|
| **RAID 0** | 2 | $N \times S$ | 0 Disks (None) | 1 | 1 (Direct write) | Maximum performance; 0 fault tolerance |
| **RAID 1** | 2 | $S$ | $N - 1$ Disks | 1 (Round-robin) | $N$ (Mirror writes) | Simple redundancy; 50% capacity overhead |
| **RAID 5** | 3 | $(N - 1) \times S$ | 1 Disk | 1 | 4 ($2\text{R} + 2\text{W}$ RMW) | Balanced storage efficiency; RMW write penalty |
| **RAID 6** | 4 | $(N - 2) \times S$ | 2 Disks | 1 | 6 ($3\text{R} + 3\text{W}$ P+Q RMW) | Enterprise safety (Galois math); high write penalty |
| **RAID 10** | 4 (Even) | $\frac{N}{2} \times S$ | 1 to $N/2$ Disks | 1 | 2 (Mirrored pair) | High random write IOPs; 50% capacity cost |

---

## 4. Project Structure

```
RAID_MANAGER/
├── README.md                           # System documentation and architecture guide
├── PRESENTATION_SCRIPT.md              # Defense script for project presentation
├── requirements.txt                    # Project dependencies (Streamlit, Matplotlib, Pandas, Pytest)
├── app.py                              # Interactive 8-tab Streamlit dashboard
├── raid/
│   ├── __init__.py
│   ├── config.py                       # Timing parameters and physical disk constants
│   ├── virtual_disk.py                 # VirtualDisk, DiskFailedError, and SMART telemetry
│   ├── scheduler.py                    # DiskQueue: FCFS, SCAN, C-SCAN, and LOOK policies
│   ├── raid_base.py                    # Abstract RaidArray, hot spares, checksum self-healing
│   ├── raid0.py                        # RAID 0 striping with chunk size support
│   ├── raid1.py                        # RAID 1 mirroring and round-robin dispatch
│   ├── raid5.py                        # RAID 5 distributed rotating parity & RMW cycles
│   ├── raid6.py                        # RAID 6 dual parity via Galois Field GF(2^8) arithmetic
│   ├── raid10.py                       # RAID 10 striped mirrored pairs
│   ├── galois.py                       # Galois Field GF(2^8) tables and polynomial multiplication
│   ├── locks.py                        # StripeLockManager per-stripe mutual exclusion
│   ├── journal.py                      # WriteIntentJournal write-ahead logging
│   ├── cache.py                        # LruBlockCache write-back and write-through cache
│   ├── scrub.py                        # Scrubbing engine, silent corruption, and write-hole demo
│   ├── oplog.py                        # Thread-safe operation logger
│   ├── workload.py                     # Synthetic workload generator and CSV trace parser
│   ├── benchmark.py                    # Performance benchmark runner and matplotlib plots
│   └── reliability.py                  # Monte Carlo reliability engine with URE risk modeling
├── scripts/
│   └── run_reliability.py              # CLI batch runner for Monte Carlo sweeps
├── tests/
│   ├── test_virtual_disk.py            # Latency, failure states, and out-of-bounds checks
│   ├── test_raid0.py                   # Mapping, chunking, and failure data loss
│   ├── test_raid1.py                   # Mirroring, N-1 survivability, and rebuild
│   ├── test_raid5.py                   # Parity rotation, degraded read/write, and rebuild
│   ├── test_raid6.py                   # GF(2^8) math, dual-disk recovery, and rebuild
│   ├── test_raid10.py                  # Mirror pairing, partner survivability, trace replay
│   ├── test_scheduler.py               # FCFS, SCAN, C-SCAN, and LOOK head movement sweeps
│   ├── test_concurrency.py             # Multithreaded race condition vs stripe locks
│   ├── test_scrub_writehole.py         # Crash mid-write, degraded scrub refusal, parity repair
│   ├── test_journal_cache.py           # Journal recovery, LRU cache hit/flush, checksum heal
│   ├── test_rebuild_hotspare_checksum.py # Spare auto-activation and checksum verification
│   ├── test_smart.py                   # SMART telemetry thresholds and proactive spare migration
│   ├── test_benchmark.py               # Workload metrics and multi-level plot verification
│   └── test_reliability.py             # Monte Carlo simulations and analytic MTTDL formulas
├── results/                            # Generated reliability experiment CSVs and charts
└── report/                             # Academic report, slides, and screenshot assets
```

---

## 5. Installation & Quickstart

### Prerequisites
- Python 3.10+ (tested on Python 3.11)
- `pip`

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Run Test Suite
Run all 64 automated tests covering unit logic, concurrency races, crash recovery, and mathematical invariants:
```bash
pytest -v
```

### 3. Run Batch Reliability Sweeps (CLI)
```bash
python scripts/run_reliability.py --trials 20000 --mttf 100000 --rebuild 24 --out results/
```

### 4. Launch Interactive Streamlit UI
```bash
streamlit run app.py
```

---

## 6. Interactive Dashboard Walkthrough (8 Tabs)

1. **Tab 1: Physical Array View & Layout Matrix**
   - Live visual grid of all disks and blocks with state-aware coloring (Data, Parity P, Parity Q, Mirrors, Failed, Rebuilt).
   - Shows diagonal parity distribution across RAID 5 and RAID 6.
   - Interactive I/O console to write and read logical blocks with live block-mapping breakdown.

2. **Tab 2: Failure Injection & Online Recovery**
   - Inject disk failures on any spindle.
   - Test on-the-fly degraded reads via parity reconstruction.
   - Execute online rebuild with real-time rate limiting delay controls.
   - Hot-spare pool management with pre-failure vs post-rebuild SHA-256 verification.

3. **Tab 3: Concurrency Control & Race Condition Demonstration**
   - Demonstrates the classic lost parity update bug: two threads concurrently write to blocks in the same stripe with artificial timing jitter.
   - **Locks Disabled**: Unsynchronized RMW cycles overwrite each other's parity updates, corrupting the stripe.
   - **Locks Enabled**: `StripeLockManager` serializes access, guaranteeing atomic updates and zero inconsistencies.

4. **Tab 4: Crash Consistency, Write-Intent Journal & Scrubbing**
   - **Simulate Mid-Write Crash**: Induces the Write-Hole syndrome by updating data without parity.
   - **Write-Intent Log**: Logs stripe updates before execution; on crash recovery, only logged stripes are re-synced instead of a full disk scan.
   - **Self-Healing Per-Block Checksums**: Detects silent data corruption and repairs corrupt sectors directly from redundant parity.

5. **Tab 5: Disk Schedulers (FCFS, SCAN, C-SCAN, LOOK)**
   - Head movement and latency comparison across 4 industry-standard elevator policies.
   - Visual step-by-step actuator trajectory graph showing dramatic seek travel reductions.

6. **Tab 6: Performance Benchmarks & Trace Replay**
   - Evaluates all 5 RAID architectures (`RAID 0, 1, 5, 6, 10`) under Sequential, Random, and Mixed workloads.
   - Comparative throughput, average latency, and P95 latency charts.
   - Small-write penalty table ($2\text{R}+2\text{W}$ for RAID 5, $3\text{R}+3\text{W}$ for RAID 6).
   - Replay custom block I/O trace files (`CSV` format).
   - LRU Block Cache acceleration demo with Write-Back dirty block amortization.

7. **Tab 7: Live Operation Subsystem Log**
   - Thread-safe ring buffer logger showing timestamped micro-operations (read, write, lock acquire, parity calculate, spare claim, rebuild).

8. **Tab 8: Monte Carlo Reliability & URE Simulation**
   - Simulates 20,000+ stochastic mission lifetimes with exponential disk failure intervals.
   - Compares data loss probabilities across all 5 architectures.
   - Evaluates rebuild window sensitivity and Unrecoverable Read Error (URE) risks on surviving disks.
