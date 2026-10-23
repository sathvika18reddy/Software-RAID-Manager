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

The goal of this project is to construct a modular, educational, and high-fidelity simulator of an OS storage stack supporting RAID 0, RAID 1, and RAID 5. The system demonstrates real-time fault injection, degraded on-the-fly reconstruction, background rebuilds, hot-spare failover, race-condition reproduction, and empirical performance benchmarking.

---

## 2. OS Concept Mapping

The simulator mirrors core concepts across the operating systems syllabus:

| Feature / Module | Operating Systems Concept | Real-World System Equivalent |
|---|---|---|
| **RAID 0, 1, 5, Distributed Parity** | Mass-Storage Structure & Block Layer | Linux `mdadm`, FreeBSD `geom_raid` |
| **VirtualDisk Latency Accounting** | Disk Physics & Mechanical Access Modeling | Hard Drive Actuator / Cylinder Physics |
| **DiskQueue (FCFS vs. SCAN)** | Disk Request Scheduling & Seek Reduction | Linux Elevator Schedulers (`mq-deadline`, `bfq`) |
| **StripeLockManager** | Synchronization, Mutual Exclusion, Critical Sections | Kernel Mutexes, Stripe Locks in MD RAID |
| **Write-Hole & Crash Injection** | Crash Consistency & Recovery | WAL, Intent Logs, Battery-Backed Write Caches |
| **Parity Scrubbing & Self-Healing** | Background Auditing & Data Integrity | Linux MD Scrubbing, ZFS Scrub, Btrfs Scrub |
| **Logical Volume Interface** | Block Device Driver & VFS Translation | Block Layer (`struct bio`, `request_queue`) |
| **Reliability Simulation & MTTDL** | Reliability and Fault Tolerance in Mass Storage | Monte Carlo Availability Modeling |
| **Rebuild Window & Hot Spare Failover** | Recovery Time and System Availability | Hot Spare Daemon & Automated Failover |

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
3. **RAID Layer (`raid/raid_base.py`, `raid/raid0.py`, `raid/raid1.py`, `raid/raid5.py`):**
   - **RAID 0 (Striping):** Distributes blocks cyclically: $\text{disk} = \text{lbn} \pmod N$, $\text{pbn} = \text{lbn} // N$. Usable capacity is $N \times S$. No redundancy.
   - **RAID 1 (Mirroring):** Duplicates every write across all $N$ disks at $\text{pbn} = \text{lbn}$. Reads utilize round-robin scheduling across healthy disks. Survives up to $N - 1$ disk failures.
   - **RAID 5 (Rotating Parity):** Distributes parity dynamically to prevent bottlenecking a single parity spindle:
     $$\text{data\_per\_stripe} = N - 1$$
     $$\text{stripe} = \text{lbn} // \text{data\_per\_stripe}$$
     $$\text{parity\_disk} = (N - 1) - (\text{stripe} \pmod N)$$
     Parity is maintained such that for every stripe:
     $$\bigoplus_{i=0}^{N-1} \text{Block}_{i,\text{stripe}} = 0$$
4. **Concurrency Control (`raid/locks.py`):**
   - Manages an array of per-stripe locks (`StripeLockManager`).
   - Wraps the Read-Modify-Write cycle:
     $$\text{New Parity} = \text{Old Parity} \oplus \text{Old Data} \oplus \text{New Data}$$
   - When locking is disabled, concurrent writes to the same stripe trigger a lost parity update race condition.

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
| **RAID 1** | 2 | $1/N$ ($33.3\%$ for $N=3$) | $N - 1$ Disks | High | Moderate | $N$ writes per block |
| **RAID 5** | 3 | $(N - 1)/N$ ($75\%$ for $N=4$) | 1 Disk | High | Moderate | 2 Reads + 2 Writes |

### 5.2 Throughput and Latency Benchmarks (60 Requests, 24 Blocks/Disk)

| RAID Level | Workload | Throughput (req/s) | Avg Latency (ms) | P95 Latency (ms) | Storage Efficiency |
|---|---|---|---|---|---|
| **RAID 0** | Sequential | **156.4** | 5.2 | 8.1 | $100.0\%$ |
| **RAID 0** | Random | **112.8** | 7.9 | 14.3 | $100.0\%$ |
| **RAID 0** | Mixed | **131.2** | 6.5 | 11.2 | $100.0\%$ |
| **RAID 1** | Sequential | **84.2** | 10.4 | 16.2 | $33.3\%$ |
| **RAID 1** | Random | **61.8** | 14.8 | 25.1 | $33.3\%$ |
| **RAID 1** | Mixed | **72.1** | 12.3 | 20.7 | $33.3\%$ |
| **RAID 5** | Sequential | **71.5** | 12.6 | 19.4 | $75.0\%$ |
| **RAID 5** | Random | **52.3** | 17.5 | 28.9 | $75.0\%$ |
| **RAID 5** | Mixed | **60.9** | 15.1 | 24.0 | $75.0\%$ |

*Observations:*
- **RAID 0** achieves the highest throughput and lowest latency due to zero write overhead.
- **RAID 5** exhibits lower write throughput than RAID 0 due to the $2\text{R} + 2\text{W}$ penalty of RMW cycles.
- **Storage Efficiency:** RAID 5 ($75\%$ on 4 disks) offers more than double the storage efficiency of RAID 1 ($33.3\%$).

### 5.3 Scheduler Evaluation: FCFS vs. SCAN
On a randomized batch of 40 cylinder requests across 64 blocks:
- **FCFS Head Movement:** $912$ blocks traversed (head thrashing back and forth).
- **SCAN Head Movement:** $118$ blocks traversed.
- **Seek Reduction:** **$87.1\%$ reduction** in actuator head travel using SCAN.

---

## 6. Testing and Verification

The test suite consists of **32 automated pytest test cases** spanning unit, integration, concurrency, and crash scenarios:

```
tests/test_virtual_disk.py ................. [6 tests]
tests/test_raid0.py ........................ [4 tests]
tests/test_raid1.py ........................ [3 tests]
tests/test_raid5.py ........................ [5 tests]
tests/test_rebuild_hotspare_checksum.py .... [3 tests]
tests/test_scheduler.py .................... [3 tests]
tests/test_concurrency.py .................. [2 tests]
tests/test_scrub_writehole.py .............. [4 tests]
tests/test_benchmark.py .................... [2 tests]
======================= 32 passed in 2.47s =======================
```

Key verification milestones:
1. **Rotating Parity Invariant:** Proved that for any random write pattern on RAID 5, $\bigoplus_{d} \text{Block}_{d,s} = 0$ for all stripes.
2. **Degraded Invariance:** Tested every disk slot $0 \dots N-1$ in turn: wrote random data, killed disk $i$, and verified all logical blocks read back bit-identically.
3. **Rebuild SHA-256 Checksum Matching:** Computed pre-failure SHA-256 digests across all blocks; post-rebuild verification confirmed matching hashes.
4. **Race Condition Reproduction:** Proved that without locks, concurrent writes produce inconsistent stripes, while with `StripeLockManager` enabled, $0$ errors occur.
5. **Write-Hole Silent Corruption:** Proved that un-scrubbed crashes corrupt degraded reads, while scrub repair restores full recovery fidelity.

---

## 7. Limitations and Future Work

1. **Simulated Time vs Wall-Clock Time:** Latencies are simulated via math formulas ($0.5\,\text{ms}$ seek + $2.0\,\text{ms}$ rotation) to allow fast deterministic benchmarking. Real physical micro-benchmarking with disk spin-up would require OS-level direct I/O.
2. **RAID 6 & Double Parity:** The current implementation supports single-disk parity (RAID 5). Extending to dual-disk failure tolerance (RAID 6) would require Galois Field $GF(2^8)$ Reed-Solomon arithmetic.
3. **Journaling & WAL:** Implementing a non-volatile write-ahead log (WAL) for intent logging could permanently eliminate the write-hole syndrome without requiring periodic full-array scrubbing.
