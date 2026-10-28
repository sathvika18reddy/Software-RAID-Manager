# Software RAID Manager: Fault-Tolerant Storage Simulator
## Operating Systems Final Project Technical Report

**Authors:** System Architecture Team  
**Date:** October 2026  
**Language:** Python 3.11  
**Key Technologies:** Streamlit, Matplotlib, Pandas, Pytest  

---

## 1. Introduction and Motivation

Modern operating systems interface with diverse storage hardware where individual physical disks are prone to mechanical wear, sector corruption, latency spikes, and catastrophic hardware failures. To guarantee high availability, high throughput, and data resilience, storage subsystems deploy **Redundant Arrays of Independent Disks (RAID)**.

While hardware RAID controllers offload array management to dedicated microcontrollers, **Software RAID** operates directly inside the operating system kernel storage stack (e.g., Linux `mdadm`). Managing RAID in software highlights quintessential operating systems challenges:
1. **Abstraction and Layering:** Translating abstract logical block numbers (LBNs) into physical cylinders and sectors across independent spindles.
2. **Crash Consistency & Integrity:** Addressing the infamous **RAID Write-Hole syndrome**, where power failure mid-update leaves parity inconsistent with data.
3. **Concurrency Control:** Serializing concurrent Read-Modify-Write (RMW) operations within shared stripes without crippling multi-core I/O parallelism.
4. **I/O Scheduling:** Minimizing physical seek latency through elevator algorithms (SCAN) across queued requests.

The goal of this project is to construct a modular, educational, and high-fidelity simulator of an OS storage stack supporting **5 RAID architectures** (RAID 0, RAID 1, RAID 5, RAID 6 with $GF(2^8)$ Galois Field dual parity, and RAID 10 striped mirrors). The system demonstrates real-time fault injection, degraded on-the-fly reconstruction, background rebuilds, hot-spare failover, race-condition reproduction, write-intent journaling (WAL), ZFS-style self-healing block checksums, LRU buffer caching, SMART failure prediction, and empirical performance benchmarking.

---

## 2. OS Concept Mapping

The simulator mirrors core concepts across the operating systems syllabus:

| Feature / Module | Operating Systems Concept | Real-World System Equivalent |
|---|---|---|
| **RAID 0, 1, 5, 6, 10** | Mass-Storage Structure & Block Layer | Linux `mdadm`, FreeBSD `geom_raid` |
| **VirtualDisk Latency Accounting** | Disk Physics & Mechanical Access Modeling | Hard Drive Actuator / Cylinder Physics |
| **DiskQueue (FCFS, SCAN, C-SCAN, LOOK)** | Disk Request Scheduling & Seek Reduction | Linux Elevator Schedulers (`mq-deadline`, `bfq`, `kyber`) |
| **StripeLockManager** | Synchronization, Mutual Exclusion, Critical Sections | Kernel Mutexes, Stripe Locks in MD RAID |
| **WriteIntentJournal** | Crash Consistency & Recovery (Write-Ahead Logging) | Linux MD Write-Intent Bitmap, WAL, ZFS ZIL |
| **Parity Scrubbing & SHA-256 Checksums** | Background Auditing & Self-Healing Data Integrity | Linux MD Scrubbing, ZFS Scrub, Btrfs Scrub |
| **LruBlockCache (Write-Back / Write-Through)** | Page Cache, Write Buffering, Dirty Flush | OS Page Cache, Buffer Cache, `bdflush` / `pdflush` |
| **SMART Telemetry & Proactive Migration** | Predictive Hardware Maintenance & Availability | `smartd`, Backblaze Drive Reliability Analytics |
| **Logical Volume Interface** | Block Device Driver & VFS Translation | Block Layer (`struct bio`, `request_queue`) |
| **Reliability Simulation & URE Modeling** | Reliability and Fault Tolerance in Mass Storage | Monte Carlo Availability & URE Modeling |

---

## 3. Design and Architecture

The simulator is structured into a rigorous five-layer storage stack:

```
 ┌─────────────────────────────────────────────────────────────┐
 │ Layer 1: Logical Volume Layer (read/write LBN, files)       │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 2: RAID Layer (RAID 0, RAID 1, RAID 5, Rebuild)       │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 3: Request Queue & Disk Scheduler (FCFS, SCAN)        │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 4: Virtual Disks (blocks, latency, failure state)     │
 ├─────────────────────────────────────────────────────────────┤
 │ Layer 5: Concurrency Control (stripe locking & safety)      │
 └─────────────────────────────────────────────────────────────┘
```

### Component Details
1. **VirtualDisk (`raid/virtual_disk.py`):**
   - Simulates physical sectors (`BLOCK_SIZE = 512` bytes).
   - Models head seek distance: $\text{Seek Cost} = \Delta \text{Cylinder} \times 0.5\,\text{ms}$.
   - Adds simulated rotational delay ($2.0\,\text{ms}$) and transfer delay ($0.1\,\text{ms/block}$).
   - Tracks per-disk statistics: reads, writes, total seek distance, and accumulated simulated latency.
2. **Scheduler (`raid/scheduler.py`):**
   - Implements `DiskQueue` for per-disk request buffering.
   - **FCFS:** Serves requests strictly in arrival order.
   - **SCAN:** Sweeps across cylinders monotonically in one direction serving requests along the path before reversing, minimizing total actuator travel.
   - **C-SCAN:** Circular sweep providing uniform wait times across cylinders.
   - **LOOK:** Optimizes elevator sweeps by reversing immediately when no further pending requests lie ahead.
3. **RAID Layer (`raid/raid_base.py`, `raid/raid0.py`, `raid/raid1.py`, `raid/raid5.py`, `raid/raid6.py`, `raid/raid10.py`):**
   - **RAID 0 (Striping):** Distributes blocks cyclically: $\text{disk} = \text{lbn} \pmod N$, $\text{pbn} = \text{lbn} // N$. Usable capacity is $N \times S$. Supports multi-block chunk units.
   - **RAID 1 (Mirroring):** Duplicates every write across all $N$ disks at $\text{pbn} = \text{lbn}$. Reads utilize round-robin scheduling across healthy disks. Survives up to $N - 1$ disk failures.
   - **RAID 5 (Rotating Parity):** Distributes parity dynamically to prevent bottlenecking a single parity spindle:
     $$\text{data\_per\_stripe} = N - 1$$
     $$\text{stripe} = \text{lbn} // \text{data\_per\_stripe}$$
     $$\text{parity\_disk} = (N - 1) - (\text{stripe} \pmod N)$$
     Parity is maintained such that for every stripe:
     $$\bigoplus_{i=0}^{N-1} \text{Block}_{i,\text{stripe}} = 0$$
   - **RAID 6 (Dual GF(2^8) Parity):** Dual fault tolerance using Galois Field $GF(2^8)$ Reed-Solomon arithmetic ($P$ syndrome via XOR, $Q$ syndrome via polynomial powers $g^i$). Survives any 2 simultaneous disk failures.
   - **RAID 10 (Striped Mirrors):** Aggregates mirrored pairs into a striped logical volume. Survives up to $N/2$ failures provided no two failures hit the same mirrored pair.
4. **Concurrency & Integrity Subsystems:**
   - **Concurrency Control (`raid/locks.py`):** Per-stripe mutexes (`StripeLockManager`) wrapping the RMW cycle:
     $$\text{New Parity} = \text{Old Parity} \oplus \text{Old Data} \oplus \text{New Data}$$
   - **Write-Intent Journal (`raid/journal.py`):** Pre-logs stripe updates to non-volatile journal storage before committing blocks, eliminating the Write-Hole syndrome.
   - **Self-Healing Checksums (`raid/raid_base.py`):** Per-block SHA-256 integrity verification that detects silent bit corruption and reconstructs corrupted data from parity.
   - **LRU Block Cache (`raid/cache.py`):** High-speed buffer cache with dirty-block tracking and configurable Write-Back vs. Write-Through policies.

---

## 4. Implementation Details

### 4.1 Small-Write Penalty in RAID 5
Writing a partial stripe in RAID 5 requires a **Read-Modify-Write (RMW)** cycle:
1. Read the old data block from target disk.
2. Read the old parity block from the parity disk.
3. Compute the differential parity: $\text{Parity}_{\text{new}} = \text{Parity}_{\text{old}} \oplus \text{Data}_{\text{old}} \oplus \text{Data}_{\text{new}}$.
4. Write the new data block.
5. Write the new parity block.

Hence, a single logical write incurs **2 physical disk reads + 2 physical disk writes** (an amplification factor of 4).

### 4.2 Degraded Reads & Rebuilds
- **Degraded Read:** If a disk holding requested data has failed, the RAID 5 layer reads all surviving data blocks and the parity block in that stripe, reconstructing the missing block on-the-fly:
  $$\text{Target Data} = \text{Parity} \oplus \bigoplus_{d \in \text{surviving data}} \text{Data}_d$$
- **Online Rebuild:** When a failed disk is replaced, the system iterates over each stripe $0 \dots S-1$, computes the missing block via parity XOR, commits it to the replacement disk, and invokes a progress callback.

### 4.3 Hot Spare Failover
The `RaidArray` base class holds a spare disk pool. When `fail_disk(i)` is called and a spare is available:
1. The spare is immediately mapped into disk slot $i$.
2. The array transitions to rebuilding state.
3. A background daemon thread executes `rebuild(i)`.
4. Upon completion, the array automatically returns to healthy status.

---

## 5. Experiments and Results

### 5.1 Storage Architecture Comparison

| Architecture | Minimum Disks | Usable Capacity Ratio | Fault Tolerance | Read Perf | Write Perf | Small-Write Penalty |
|---|---|---|---|---|---|---|
| **RAID 0** | 2 | $1.00$ ($100\%$) | 0 Disks (None) | High | High | None (0R + 1W) |
| **RAID 1** | 2 | $1/N$ ($25.0\%$ for $N=4$) | $N - 1$ Disks | High | Moderate | $N$ writes per block |
| **RAID 5** | 3 | $(N - 1)/N$ ($75.0\%$ for $N=4$) | 1 Disk | High | Moderate | 2 Reads + 2 Writes |
| **RAID 6** | 4 | $(N - 2)/N$ ($50.0\%$ for $N=4$) | 2 Disks | High | Moderate | 3 Reads + 3 Writes (P+Q) |
| **RAID 10** | 4 | $1/2$ ($50.0\%$) | 1 to $N/2$ Disks | High | High | 2 Writes (Mirrored pair) |

### 5.2 Throughput and Latency Benchmarks (60 Requests, 24 Blocks/Disk, N=4)

| RAID Level | Workload | Throughput (req/s) | Avg Latency (ms) | P95 Latency (ms) | Storage Efficiency |
|---|---|---|---|---|---|
| **RAID 0** | Sequential | **156.4** | 5.2 | 8.1 | $100.0\%$ |
| **RAID 0** | Random | **112.8** | 7.9 | 14.3 | $100.0\%$ |
| **RAID 0** | Mixed | **131.2** | 6.5 | 11.2 | $100.0\%$ |
| **RAID 1** | Sequential | **84.2** | 10.4 | 16.2 | $25.0\%$ |
| **RAID 1** | Random | **61.8** | 14.8 | 25.1 | $25.0\%$ |
| **RAID 1** | Mixed | **72.1** | 12.3 | 20.7 | $25.0\%$ |
| **RAID 5** | Sequential | **71.5** | 12.6 | 19.4 | $75.0\%$ |
| **RAID 5** | Random | **52.3** | 17.5 | 28.9 | $75.0\%$ |
| **RAID 5** | Mixed | **60.9** | 15.1 | 24.0 | $75.0\%$ |
| **RAID 6** | Sequential | **58.4** | 15.8 | 24.2 | $50.0\%$ |
| **RAID 6** | Random | **41.2** | 22.4 | 36.5 | $50.0\%$ |
| **RAID 6** | Mixed | **48.7** | 19.1 | 30.8 | $50.0\%$ |
| **RAID 10** | Sequential | **134.6** | 6.8 | 11.0 | $50.0\%$ |
| **RAID 10** | Random | **98.5** | 9.4 | 16.2 | $50.0\%$ |
| **RAID 10** | Mixed | **112.3** | 8.1 | 13.5 | $50.0\%$ |

*Observations:*
- **RAID 0** achieves maximum throughput and minimum latency due to lack of write amplification.
- **RAID 10** delivers near-RAID 0 random write speeds because mirrored writes bypass parity calculation completely.
- **RAID 6** incurs the highest write penalty ($3\text{R}+3\text{W}$) but guarantees survivability against any two concurrent disk failures.

### 5.3 Scheduler Evaluation: FCFS vs. SCAN, C-SCAN, and LOOK
Across randomized cylinder request batches:
- **FCFS:** High head thrashing traversing across cylinder boundaries.
- **SCAN:** Sweeps across cylinders, yielding **>85% reduction** in actuator head travel.
- **C-SCAN:** Provides bounded variance in service wait times by eliminating bias towards middle tracks.
- **LOOK:** Eliminates wasteful travel to disk edges when no further cylinder requests exist.

---

## 6. Testing and Verification

The test suite consists of **64 automated pytest test cases** spanning 15 specialized test modules:

```
tests/test_virtual_disk.py ................. [6 tests]
tests/test_raid0.py ........................ [4 tests]
tests/test_raid1.py ........................ [3 tests]
tests/test_raid5.py ........................ [5 tests]
tests/test_raid6.py ........................ [7 tests]
tests/test_raid10.py ....................... [5 tests]
tests/test_scheduler.py .................... [6 tests]
tests/test_concurrency.py .................. [2 tests]
tests/test_scrub_writehole.py .............. [6 tests]
tests/test_journal_cache.py ................ [5 tests]
tests/test_rebuild_hotspare_checksum.py .... [3 tests]
tests/test_smart.py ........................ [2 tests]
tests/test_benchmark.py .................... [2 tests]
tests/test_reliability.py .................. [8 tests]
======================= 64 passed in ~18s =======================
```

Key verification milestones:
1. **Galois Field GF(2^8) Dual-Parity:** Proved exact reconstruction when any two disks fail simultaneously in RAID 6 (two data disks, or one data + one parity disk).
2. **Rotating Parity Invariant:** Proved that for any random write pattern on RAID 5 and 6, syndromes remain strictly consistent.
3. **Write-Ahead Intent Journaling:** Validated that interrupted writes leave journal entries that enable fast post-crash stripe re-sync without full-array scrubs.
4. **Self-Healing Checksums:** Proved that injected silent bit corruption is detected by SHA-256 block digests and automatically healed using parity.
5. **LRU Buffer Cache:** Verified clean write-back dirty block accumulation, LRU eviction order, and asynchronous flush to disk.

---

## 7. Future Work

1. **Distributed Consensus Storage:** Expanding local software RAID primitives to network-attached distributed clusters using Raft or Paxos replicated state machines.
2. **NVMe-oF & Kernel Block Driver:** Porting Python simulation abstractions to a native C/Rust Linux kernel block driver (`/dev/sraid`) over NVMe over Fabrics.

## 8. Live Demo Screenshots

- **RAID 5 rotating parity:** [Array View](./screenshots/array_view_raid5_rotating_parity.png)
- **Degraded read recovery:** [Parity reconstruction](./screenshots/failure_recovery_degraded_read.png)
- **Hot spare rebuild:** [Rebuild operation log](./screenshots/failure_recovery_hot_spare_rebuild.png)
- **Concurrency race without locks:** [Unsafe writes](./screenshots/concurrency_race_locks_disabled.png)
- **Concurrency with stripe locks:** [Consistent writes](./screenshots/concurrency_locks_enabled.png)
- **Write-hole repair:** [Scrub and repair operation log](./screenshots/write_hole_parity_repair.png)
