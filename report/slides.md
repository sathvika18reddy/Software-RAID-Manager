# Software RAID Manager: Fault-Tolerant Storage Simulator
## Presentation Slide Deck (6-Minute Project Presentation)

---

### Slide 1: Title Slide
- **Title:** Software RAID Manager: Fault-Tolerant Storage Simulator in an OS Storage Stack
- **Subtitle:** Modeling Striping, Mirroring, Rotating Parity, Schedulers, and Concurrency Control
- **Presenters:** System Architecture Team
- **Course:** Operating Systems & Storage Architecture
- **Tech Stack:** Python 3.11, Streamlit, Matplotlib, Pandas, Pytest

---

### Slide 2: Motivation & Background
- **The Storage Problem:** Physical disks suffer from mechanical seek overhead, bad blocks, and unannounced hardware failure.
- **Why Software RAID?**
  - Implemented inside OS kernel storage stack (e.g. Linux `mdadm`).
  - No proprietary controller lock-in.
  - Exposes fundamental OS problems: crash consistency, race conditions, request scheduling, and block virtualization.
- **Project Goal:** Build an end-to-end interactive storage simulator modeling layers 1 through 5 of the OS storage stack.

---

### Slide 3: The 5-Layer Storage Architecture
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
- **Layer 5 cuts across Layer 2:** wraps Read-Modify-Write (RMW) cycles to ensure thread safety across concurrent stripe updates.

---

### Slide 4: RAID Mapping & Distributed Parity
- **RAID 0 (Striping):**
  - Max throughput, 0 fault tolerance ($C = N \times S$).
- **RAID 1 (Mirroring):**
  - Full redundancy, survives up to $N - 1$ failures ($C = S$).
- **RAID 5 (Rotating Parity):**
  - Capacity: $(N - 1) \times S$. Tolerates 1 disk failure.
  - Left-symmetric rotating parity:
    $$\text{parity\_disk} = (N - 1) - (\text{stripe} \pmod N)$$
  - Prevents a single parity disk from becoming an I/O bottleneck.

---

### Slide 5: RAID 5 Small-Write Penalty (RMW)
- Writing a single logical block requires Read-Modify-Write:
  1. Read old data block ($1\text{R}$)
  2. Read old parity block ($1\text{R}$)
  3. Compute $\text{new\_parity} = \text{old\_parity} \oplus \text{old\_data} \oplus \text{new\_data}$
  4. Write new data block ($1\text{W}$)
  5. Write new parity block ($1\text{W}$)
- **Penalty:** **4 physical disk I/Os** for 1 logical write!

---

### Slide 6: Concurrency Control & The Race Condition Demo
- **The Problem:** Two threads writing to different blocks in the *same stripe* read the same old parity and overwrite each other's parity updates (**Lost Update**).
- **Demo Results:**
  - **Locks DISABLED:** Scrubbing detects corrupted stripes ($100\%$ reproducible race condition).
  - **Locks ENABLED:** `StripeLockManager` serializes stripe updates, yielding **0 inconsistencies**.

---

### Slide 7: Write-Hole Syndrome & Scrubbing
- **The Write-Hole:** System power cuts after writing the data block, before writing parity.
- **The Silent Corruption Danger:**
  - If another disk fails later, degraded read computes wrong data using the stale parity!
- **Solution: Scrubbing & Self-Healing:**
  - Audits XOR invariant $\bigoplus \text{Blocks} = 0$.
  - Auto-repairs parity before secondary failures occur.

---

### Slide 8: Disk Scheduling: FCFS vs. SCAN
- **Physics of Disk Latency:**
  $$\text{Latency} = \Delta \text{Cylinder} \times 0.5\,\text{ms} + 2.0\,\text{ms (Rotational)} + 0.1\,\text{ms (Transfer)}$$
- **FCFS:** High seek thrashing across random cylinder requests.
- **SCAN (Elevator Algorithm):** Sweeps cylinders in order, reversing at boundaries.
- **Empirical Result:** **$87\%$ reduction** in actuator travel distance!

---

### Slide 9: Benchmark Performance Summary
- **Throughput:**
  - RAID 0: $112.8\,\text{req/s}$ (no overhead)
  - RAID 1: $61.8\,\text{req/s}$ (mirror write amplification)
  - RAID 5: $52.3\,\text{req/s}$ (RMW $2\text{R} + 2\text{W}$ overhead)
- **Storage Efficiency:**
  - RAID 0: $100\%$ | RAID 1: $33.3\%$ | RAID 5: $75.0\%$
- **Trade-off:** RAID 5 achieves high resilience and high efficiency at the cost of the small-write penalty.

---

### Slide 10: Conclusion & Key Takeaways
- Successfully simulated a complete 5-layer OS software storage stack.
- Verified fault tolerance with 32 passing automated pytest tests.
- Recreated and resolved classical OS storage hazards:
  - Lost update race conditions $\rightarrow$ Per-stripe locks.
  - Write-hole silent corruption $\rightarrow$ Parity scrubbing & repair.
  - Seek latency bottlenecks $\rightarrow$ Elevator disk scheduling.
- Fully functional interactive Streamlit interface ready for live presentation!
