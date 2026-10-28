# Software RAID Manager: Fault-Tolerant Storage Simulator
## Presentation Slide Deck (6-to-8 Minute Project Presentation)

---

### Slide 1: Title Slide
- **Title:** Software RAID Manager: Fault-Tolerant Storage Simulator in an OS Storage Stack
- **Subtitle:** Modeling Striping, Mirroring, Rotating Parity, GF(2^8) Reed-Solomon, Schedulers, and Concurrency Control
- **Presenters:** System Architecture Team
- **Course:** Operating Systems & Storage Architecture
- **Tech Stack:** Python 3.11, Streamlit, Matplotlib, Pandas, Pytest (64 Automated Tests)

---

### Slide 2: Motivation & Background
- **The Storage Problem:** Physical disks suffer from mechanical seek overhead, bad blocks, and unannounced hardware failure.
- **Why Software RAID?**
  - Implemented inside OS kernel storage stack (e.g. Linux `mdadm`).
  - No proprietary controller lock-in.
  - Exposes fundamental OS problems: crash consistency, race conditions, request scheduling, and block virtualization.
- **Project Goal:** Build an end-to-end interactive storage simulator modeling layers 1 through 5 of the OS storage stack across 5 RAID architectures.

---

### Slide 3: The 5-Layer Storage Architecture
```
 ┌─────────────────────────────────────────────────────────────┐
 │ Layer 1: Logical Volume Layer & LRU Buffer Cache            │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 2: RAID Layer (RAID 0, 1, 5, 6, 10, Online Rebuild)   │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 3: Concurrency, Journaling & Checksum Self-Healing    │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 4: Request Queue & Schedulers (FCFS, SCAN, C-SCAN, LOOK)│
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 5: Virtual Disks & Hardware SMART Telemetry           │
 └─────────────────────────────────────────────────────────────┘
```

---

### Slide 4: Supported RAID Architectures (5 Levels)
- **RAID 0 (Striping):** Maximum throughput, 0 redundancy. Configurable chunk size.
- **RAID 1 (Mirroring):** Duplicates across disks; survives $N-1$ failures ($C = S$).
- **RAID 5 (Rotating Parity):** Single-disk tolerance; distributed parity eliminates bottleneck.
- **RAID 6 (Dual Parity):** Dual fault tolerance using Galois Field $GF(2^8)$ arithmetic. Survives **any 2 simultaneous failures**.
- **RAID 10 (Striped Mirrors):** Aggregates mirrored pairs; high random write performance, survives up to $N/2$ failures.

---

### Slide 5: Small-Write Penalty Comparison
- **RAID 0:** $0\text{R} + 1\text{W}$ (1 IO per write)
- **RAID 10:** $0\text{R} + 2\text{W}$ (2 IOs per write to mirrored pair)
- **RAID 5 (RMW):** $2\text{R} + 2\text{W}$ (4 IOs per write: read old data+parity, write new data+parity)
- **RAID 6 (P+Q RMW):** $3\text{R} + 3\text{W}$ (6 IOs per write: read old data+P+Q, write new data+P+Q)

---

### Slide 6: Concurrency Control & Race Condition Demonstration
- **The Problem:** Two threads writing concurrently to different blocks in the same stripe read identical stale parity, triggering a **lost update**.
- **Live Demo Results:**
  - **Locks DISABLED:** Scrubbing detects corrupted stripes ($100\%$ reproducible race condition).
  - **Locks ENABLED:** `StripeLockManager` serializes stripe updates, yielding **0 inconsistencies**.

---

### Slide 7: Crash Consistency: Write-Hole & Write-Intent Journal
- **The Write-Hole Syndrome:** Power fails after data write but before parity write. A future disk failure uses stale parity to reconstruct corrupted data.
- **The Modern Fix (Journaling / WAL):**
  - Logs stripe updates to a non-volatile Write-Intent Journal before touching disk blocks.
  - On reboot/crash recovery, only logged stripes are repaired instead of hours of full-array scrubbing.
- **Self-Healing Checksums:** SHA-256 per block detects bit-rot and triggers on-the-fly parity reconstruction.

---

### Slide 8: Disk Schedulers: FCFS, SCAN, C-SCAN, and LOOK
- **Physics of Disk Latency:**
  $$\text{Latency} = \Delta \text{Cylinder} \times 0.5\,\text{ms} + 2.0\,\text{ms (Rotational)} + 0.1\,\text{ms (Transfer)}$$
- **FCFS:** High seek thrashing across random cylinder requests.
- **SCAN:** Sweeps cylinders in order; **>85% reduction** in actuator travel.
- **C-SCAN:** Circular sweep providing uniform bounded wait times.
- **LOOK:** Optimizes elevator sweeps by avoiding unused track edges.

---

### Slide 9: Benchmark Performance & LRU Cache
- **Throughput & Latency:** Evaluates all 5 RAID levels across Sequential, Random, and Mixed workloads.
- **LRU Block Cache:**
  - **Write-Back:** Buffers dirty blocks in RAM, amortizing disk writes and boosting burst throughput.
  - **Write-Through:** Synchronously commits blocks to underlying disks for maximum consistency.

---

### Slide 10: Monte Carlo Reliability & URE Risk Modeling
- Simulates 20,000+ stochastic mission lifetimes with exponential disk failure intervals.
- Proves RAID 6 provides orders-of-magnitude lower failure probability than RAID 5 during long rebuilds.
- Incorporates **Unrecoverable Read Error (URE)** risks on surviving disks during rebuilds.

---

### Slide 11: Conclusion & Project Summary
- Full-stack OS software storage stack implemented from scratch in Python 3.11.
- **64 passing automated tests** across 15 test modules.
- Solved classic OS storage hazards:
  - Lost update races $\rightarrow$ Per-stripe mutexes.
  - Write-hole crashes $\rightarrow$ Write-ahead journaling & scrubbing.
  - Bit-rot $\rightarrow$ Self-healing SHA-256 checksums.
  - Head seek bottlenecks $\rightarrow$ Elevator disk scheduling.
- Interactive 8-tab Streamlit dashboard ready for live defense and demonstration!
