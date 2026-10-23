"""Streamlit UI for Software RAID Manager Simulator.

Fault-tolerant storage stack with RAID 0, 1, 5, request scheduling,
concurrency control, failure injection, rebuild, write-hole demo, benchmarking,
and Monte Carlo reliability simulation.
"""

import io
import time
import threading
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

from raid.config import BLOCK_SIZE, DEFAULT_DISK_BLOCKS
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid0 import Raid0
from raid.raid1 import Raid1
from raid.raid5 import Raid5
from raid.locks import StripeLockManager
from raid.oplog import OperationLog
from raid.scrub import scrub, write_with_crash, checksum_array, SimulatedCrashError
from raid.scheduler import DiskQueue
from raid.workload import generate_workload
from raid.benchmark import (
    run_full_benchmark,
    plot_throughput,
    plot_latency,
    plot_scheduler_movement,
)
from raid.reliability import (
    ReliabilityParams,
    run_simulation,
    sweep_rebuild_time,
    compare_levels,
    analytic_mttdl_raid5,
    analytic_loss_probability,
)

# Page configuration
st.set_page_config(
    page_title="Software RAID Manager Simulator",
    page_icon="🛡️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Initialize Session State
if "oplog" not in st.session_state:
    st.session_state.oplog = OperationLog()

if "raid_array" not in st.session_state:
    st.session_state.raid_array = None

if "raid_type" not in st.session_state:
    st.session_state.raid_type = "RAID 5"

if "num_disks" not in st.session_state:
    st.session_state.num_disks = 4

if "blocks_per_disk" not in st.session_state:
    st.session_state.blocks_per_disk = 16

if "num_spares" not in st.session_state:
    st.session_state.num_spares = 1

if "locks_enabled" not in st.session_state:
    st.session_state.locks_enabled = True

if "scheduler_policy" not in st.session_state:
    st.session_state.scheduler_policy = "FCFS"

if "stored_checksum" not in st.session_state:
    st.session_state.stored_checksum = None

if "scrub_inconsistent" not in st.session_state:
    st.session_state.scrub_inconsistent = []

if "bench_cache" not in st.session_state:
    st.session_state.bench_cache = None


def create_array():
    oplog = st.session_state.oplog
    rtype = st.session_state.get("sidebar_raid_type", st.session_state.get("raid_type", "RAID 5"))
    n_disks = st.session_state.get("sidebar_num_disks", st.session_state.get("num_disks", 4))
    n_blocks = st.session_state.get("sidebar_blocks", st.session_state.get("blocks_per_disk", 16))
    n_spares = st.session_state.get("sidebar_spares", st.session_state.get("num_spares", 1))
    policy = st.session_state.get("sidebar_policy", st.session_state.get("scheduler_policy", "FCFS"))
    locks_on = st.session_state.get("sidebar_locks", st.session_state.get("locks_enabled", True))

    oplog.add(f"Initialized new {rtype} ({n_disks} disks, {n_blocks} blocks/disk, {n_spares} hot spares, policy={policy}).", level="info")

    disks = [VirtualDisk(disk_id=i, num_blocks=n_blocks, block_size=BLOCK_SIZE) for i in range(n_disks)]
    spares = [VirtualDisk(disk_id=100 + i, num_blocks=n_blocks, block_size=BLOCK_SIZE) for i in range(n_spares)]

    if rtype == "RAID 0":
        array = Raid0(disks=disks, spares=spares, oplog=oplog, scheduler_policy=policy)
    elif rtype == "RAID 1":
        array = Raid1(disks=disks, spares=spares, oplog=oplog, scheduler_policy=policy)
    else:
        lock_mgr = StripeLockManager(num_stripes=n_blocks, enabled=locks_on)
        array = Raid5(
            disks=disks,
            spares=spares,
            oplog=oplog,
            lock_manager=lock_mgr,
            scheduler_policy=policy,
        )

    st.session_state.raid_array = array
    st.session_state.raid_type = rtype
    st.session_state.num_disks = n_disks
    st.session_state.blocks_per_disk = n_blocks
    st.session_state.num_spares = n_spares
    st.session_state.locks_enabled = locks_on
    st.session_state.scheduler_policy = policy
    st.session_state.stored_checksum = None
    st.session_state.scrub_inconsistent = []


# --- Sidebar ---
st.sidebar.title("🛡️ Storage Stack Config")

raid_choice = st.sidebar.selectbox(
    "RAID Architecture",
    options=["RAID 0", "RAID 1", "RAID 5"],
    index=2,
    key="sidebar_raid_type",
)

min_d = 3 if raid_choice == "RAID 5" else 2
current_n = max(min_d, st.session_state.num_disks)
st.sidebar.number_input("Disks Count", min_value=min_d, max_value=8, value=current_n, key="sidebar_num_disks")
st.sidebar.number_input("Blocks per Disk", min_value=8, max_value=64, value=st.session_state.blocks_per_disk, step=8, key="sidebar_blocks")
st.sidebar.number_input("Hot Spares Pool", min_value=0, max_value=4, value=st.session_state.num_spares, key="sidebar_spares")
st.sidebar.selectbox("Queue Scheduler Policy", options=["FCFS", "SCAN"], index=0, key="sidebar_policy")
st.sidebar.checkbox("Stripe-Level Locking", value=st.session_state.locks_enabled, key="sidebar_locks")

if st.sidebar.button("🔄 Create / Reset Array", type="primary") or st.session_state.raid_array is None:
    create_array()

# Active array instance
array = st.session_state.raid_array

# --- Top Header Status Metrics ---
st.title("🛡️ Software RAID Storage Simulator")
st.caption("OS Storage Stack Simulator: Logical Volumes, Striping, Mirroring, Rotating Parity, Schedulers, and Concurrency.")

m_col1, m_col2, m_col3, m_col4, m_col5, m_col6 = st.columns(6)
with m_col1:
    st.metric("RAID Level", st.session_state.raid_type)
with m_col2:
    st.metric("Disks / Spares", f"{array.num_disks} / {len(array.spares)}")
with m_col3:
    st.metric("Usable Capacity", f"{array.capacity_blocks()} blks ({array.capacity_blocks() * BLOCK_SIZE / 1024:.1f} KB)")
with m_col4:
    degraded = array.is_degraded()
    st.metric("Array Health", "⚠️ DEGRADED" if degraded else "🟢 HEALTHY")
with m_col5:
    io_c = array.io_count_for_write()
    st.metric("Write Amplification", f"{io_c['reads']}R + {io_c['writes']}W")
with m_col6:
    eff = (array.capacity_blocks() / (array.num_disks * array.disk_blocks)) * 100
    st.metric("Storage Efficiency", f"{eff:.1f}%")

# --- Tabs ---
tabs = st.tabs([
    "1. Array View",
    "2. Failure & Recovery",
    "3. Concurrency Demo",
    "4. Write-Hole & Scrub",
    "5. Scheduler",
    "6. Benchmarks",
    "7. Operation Log",
    "8. Reliability",
])

# ==============================================================================
# TAB 1: Array View
# ==============================================================================
with tabs[0]:
    st.subheader("Physical Disk Layout & Block Matrix")

    st.markdown("""
    <div style='margin-bottom:12px;'>
        <b>Legend:</b> &nbsp;
        <span style='background:#2b6cb0;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Data (OK)</span> &nbsp;
        <span style='background:#dd6b20;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Parity (OK)</span> &nbsp;
        <span style='background:#319795;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Mirror (OK)</span> &nbsp;
        <span style='background:#e53e3e;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Failed</span> &nbsp;
        <span style='background:#38a169;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Rebuilt</span> &nbsp;
        <span style='background:#d69e2e;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Inconsistent</span>
    </div>
    """, unsafe_allow_html=True)

    snapshot = array.layout_snapshot()
    cols = st.columns(len(array.disks))

    color_map = {
        ("data", "ok"): "#2b6cb0",
        ("data", "failed"): "#e53e3e",
        ("data", "rebuilt"): "#38a169",
        ("data", "inconsistent"): "#d69e2e",
        ("parity", "ok"): "#dd6b20",
        ("parity", "failed"): "#e53e3e",
        ("parity", "rebuilt"): "#38a169",
        ("parity", "inconsistent"): "#d69e2e",
        ("mirror", "ok"): "#319795",
        ("mirror", "failed"): "#e53e3e",
        ("mirror", "rebuilt"): "#38a169",
        ("mirror", "inconsistent"): "#d69e2e",
        ("empty", "ok"): "#718096",
    }

    inconsistent_stripes = set(st.session_state.scrub_inconsistent)

    for d_idx, disk in enumerate(array.disks):
        with cols[d_idx]:
            status_text = "❌ FAILED" if disk.failed else ("🔄 REBUILDING" if disk.needs_rebuild else "✅ HEALTHY")
            st.markdown(f"**Disk {disk.disk_id}** ({status_text})")
            st.caption(f"Head: {disk.head_position} | R:{disk.stats['reads']} W:{disk.stats['writes']}")

            disk_blocks = snapshot[d_idx]
            html_blocks = []
            for b in disk_blocks:
                kind = b.get("kind", "data")
                state = b.get("state", "ok")
                lbn = b.get("lbn")
                pbn = b.get("pbn", 0)
                stripe = b.get("stripe", 0)

                if stripe in inconsistent_stripes and not disk.failed and not disk.needs_rebuild:
                    state = "inconsistent"

                bg = color_map.get((kind, state), "#4a5568")
                label = f"P{stripe}" if kind == "parity" else (f"M{lbn}" if kind == "mirror" else f"L{lbn}")
                tooltip = f"Disk {disk.disk_id} | PBN {pbn} | Stripe {stripe} | Kind: {kind} | State: {state}"
                html_blocks.append(
                    f"<div title='{tooltip}' style='background-color:{bg};color:white;text-align:center;font-size:11px;margin:2px 0;padding:4px;border-radius:3px;font-family:monospace;'>"
                    f"{label}</div>"
                )
            st.markdown("".join(html_blocks), unsafe_allow_html=True)

    st.divider()

    # Read / Write Controls
    rw1, rw2 = st.columns(2)
    with rw1:
        st.markdown("#### ✍️ Logical Volume Write")
        write_lbn = st.number_input("Target LBN", min_value=0, max_value=array.capacity_blocks() - 1, value=0)
        write_mode = st.radio("Input Format", ["Text String", "File Upload"], horizontal=True)

        if write_mode == "Text String":
            payload_text = st.text_input("Payload", value="Software RAID Manager OS Storage Demo")
            payload_bytes = payload_text.encode()
        else:
            uploaded = st.file_uploader("Upload File (up to 4KB)", type=["txt", "png", "jpg", "bin"])
            if uploaded is not None:
                payload_bytes = uploaded.read()
            else:
                payload_bytes = b"Sample File Payload"

        if st.button("🚀 Write to Logical Volume"):
            try:
                # Support multi-block writes if payload exceeds BLOCK_SIZE
                chunks = [payload_bytes[i:i + BLOCK_SIZE] for i in range(0, len(payload_bytes), BLOCK_SIZE)]
                for offset, chunk in enumerate(chunks):
                    tgt = write_lbn + offset
                    if tgt < array.capacity_blocks():
                        array.write_block(tgt, chunk)
                st.success(f"Wrote {len(chunks)} block(s) starting at LBN {write_lbn}.")
                st.rerun()
            except Exception as e:
                st.error(f"Write operation failed: {e}")

    with rw2:
        st.markdown("#### 📖 Logical Volume Read")
        read_lbn = st.number_input("Read LBN", min_value=0, max_value=array.capacity_blocks() - 1, value=0, key="read_lbn_t1")
        if st.button("🔍 Read Logical Block"):
            try:
                data = array.read_block(read_lbn)
                st.info(f"Retrieved Block ({len(data)} bytes):\n```\n{data[:128]}...\n```")
                mapping = array.map_lbn(read_lbn)
                st.json(mapping)
            except Exception as e:
                st.error(f"Read operation failed: {e}")

    st.divider()

    # Fault Injection Controls
    st.markdown("#### ⚡ Disk Management Actions")
    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    disk_ids = [d.disk_id for d in array.disks]

    with f_col1:
        k_id = st.selectbox("Select Disk to Kill", options=disk_ids, key="k_disk_select")
        if st.button("💥 Kill Disk", type="primary"):
            array.fail_disk(k_id)
            st.rerun()

    with f_col2:
        rep_id = st.selectbox("Select Disk to Replace", options=disk_ids, key="rep_disk_select")
        if st.button("🔧 Replace Disk"):
            array.replace_disk(rep_id)
            st.rerun()

    with f_col3:
        reb_id = st.selectbox("Select Disk to Rebuild", options=disk_ids, key="reb_disk_select")
        if st.button("🔄 Online Rebuild"):
            pbar = st.progress(0, text="Initiating rebuild...")
            success = array.rebuild(
                reb_id,
                progress_cb=lambda done, total: pbar.progress(done / total, text=f"Rebuilding stripe {done}/{total}...")
            )
            if success:
                st.success(f"Disk {reb_id} rebuild complete!")
            else:
                st.error(f"Rebuild failed for Disk {reb_id}")
            time.sleep(0.5)
            st.rerun()

    with f_col4:
        st.markdown("**Array Checksum**")
        if st.button("🔒 Compute Checksum"):
            csum = checksum_array(array)
            st.code(csum[:16] + "...")
            st.session_state.stored_checksum = csum

# ==============================================================================
# TAB 2: Failure and Recovery
# ==============================================================================
with tabs[1]:
    st.subheader("🛡️ Fault Tolerance, Degraded Modes & Rebuild")

    st.markdown("""
    This tab evaluates how the RAID layer handles disk outages:
    - **RAID 0**: Any disk failure leads to immediate permanent data loss.
    - **RAID 1**: Operates seamlessly in degraded mode with up to $N - 1$ disk failures.
    - **RAID 5**: Recovers any missing block on-the-fly using distributed parity XOR reconstruction.
    """)

    c_deg1, c_deg2 = st.columns(2)

    with c_deg1:
        st.markdown("### 1. Degraded Read Verification")
        deg_lbn = st.number_input("Target LBN to Read", min_value=0, max_value=array.capacity_blocks() - 1, value=0, key="deg_lbn")
        mapping = array.map_lbn(deg_lbn)
        tgt_disk_id = mapping["disk"] if "disk" in mapping else mapping["disks"][0]
        tgt_disk = next((d for d in array.disks if d.disk_id == tgt_disk_id), None)
        is_target_failed = tgt_disk.failed if tgt_disk else False

        st.caption(f"LBN {deg_lbn} is physically hosted on **Disk {tgt_disk_id}** (Status: {'FAILED ❌' if is_target_failed else 'HEALTHY ✅'})")

        if st.button("Read Block in Current State"):
            try:
                res = array.read_block(deg_lbn)
                if is_target_failed:
                    st.success(f"🎉 **DEGRADED RECONSTRUCTION SUCCESS!**\n\nDisk {tgt_disk_id} is failed, but parity XOR recovered data:\n```\n{res[:96]}\n```")
                else:
                    st.info(f"Read from healthy disk:\n```\n{res[:96]}\n```")
            except Exception as e:
                st.error(f"Read failed: {e}")

    with c_deg2:
        st.markdown("### 2. Checksum Verification (Pre vs Post Rebuild)")
        st.markdown("Verify that catastrophic disk failure and subsequent rebuild restore 100% bit-exact data.")

        c_btn1, c_btn2 = st.columns(2)
        with c_btn1:
            if st.button("📸 1. Save Pre-Failure Checksum"):
                st.session_state.stored_checksum = checksum_array(array)
                st.success("Pre-failure SHA-256 snapshot saved!")

        with c_btn2:
            if st.button("🧪 2. Verify Post-Rebuild Checksum"):
                if st.session_state.stored_checksum is None:
                    st.warning("Save pre-failure checksum first!")
                else:
                    curr_csum = checksum_array(array)
                    if curr_csum == st.session_state.stored_checksum:
                        st.success(f"✅ **PASS**: Checksums match perfectly!\nSHA: `{curr_csum[:24]}...`")
                    else:
                        st.error(f"❌ **FAIL**: Checksum mismatch!\nExpected: `{st.session_state.stored_checksum[:16]}`\nActual: `{curr_csum[:16]}`")

        if st.session_state.stored_checksum:
            st.code(f"Stored Checksum: {st.session_state.stored_checksum}")

    st.divider()

    st.markdown("### 3. Hot Spare Auto-Activation Demo")
    st.write(f"Current available hot spares in pool: **{len(array.spares)}**")
    if st.button("🔥 Simulate Surprise Failure on Disk 0"):
        if array.disks:
            array.fail_disk(array.disks[0].disk_id)
            if array.last_rebuild_thread:
                with st.spinner("Hot spare activated! Running background rebuild thread..."):
                    array.last_rebuild_thread.join(timeout=6.0)
            st.rerun()

# ==============================================================================
# TAB 3: Concurrency Demo
# ==============================================================================
with tabs[2]:
    st.subheader("⚡ Concurrency Control: Stripe Locking vs Race Condition")

    st.markdown("""
    In RAID 5 small writes, updating a block requires a **Read-Modify-Write (RMW)** cycle:
    $$\\text{New Parity} = \\text{Old Parity} \\oplus \\text{Old Data} \\oplus \\text{New Data}$$
    When two threads write to different data blocks in the **same stripe** concurrently without locking, they read the same stale parity and overwrite each other's parity updates (Lost Update Anomaly).
    """)

    col_race1, col_race2 = st.columns(2)

    with col_race1:
        st.markdown("### ❌ Unsafe Mode (Locks DISABLED)")
        st.caption("Two threads write to LBN 0 and LBN 1 (same stripe) with artificial RMW delay.")
        if st.button("Run Unsafe Concurrent Writes"):
            disks_unsafe = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
            lock_mgr_off = StripeLockManager(num_stripes=16, enabled=False)
            raid_unsafe = Raid5(disks=disks_unsafe, lock_manager=lock_mgr_off, artificial_delay_ms=30.0)

            t1 = threading.Thread(target=raid_unsafe.write_block, args=(0, b"DATA_FROM_THREAD_1_UNSAFE"))
            t2 = threading.Thread(target=raid_unsafe.write_block, args=(1, b"DATA_FROM_THREAD_2_UNSAFE"))

            t1.start()
            time.sleep(0.005)
            t2.start()
            t1.join()
            t2.join()

            scrub_res = scrub(raid_unsafe, repair=False)
            st.error(f"💥 **RACE CONDITION DETECTED!**\n\nStripe 0 Parity is Corrupted!\nInconsistent Stripes: {scrub_res['inconsistent']}")

    with col_race2:
        st.markdown("### ✅ Safe Mode (Locks ENABLED)")
        st.caption("StripeLockManager serializes RMW critical sections per stripe.")
        if st.button("Run Safe Concurrent Writes"):
            disks_safe = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
            lock_mgr_on = StripeLockManager(num_stripes=16, enabled=True)
            raid_safe = Raid5(disks=disks_safe, lock_manager=lock_mgr_on, artificial_delay_ms=30.0)

            t1 = threading.Thread(target=raid_safe.write_block, args=(0, b"DATA_FROM_THREAD_1_SAFE"))
            t2 = threading.Thread(target=raid_safe.write_block, args=(1, b"DATA_FROM_THREAD_2_SAFE"))

            t1.start()
            time.sleep(0.005)
            t2.start()
            t1.join()
            t2.join()

            scrub_res = scrub(raid_safe, repair=False)
            if len(scrub_res["inconsistent"]) == 0:
                st.success("🎉 **PERFECT CONSISTENCY!**\n\nStripe locks prevented the race condition. 0 inconsistent stripes detected.")
            else:
                st.error(f"Inconsistencies found: {scrub_res['inconsistent']}")

# ==============================================================================
# TAB 4: Write-Hole and Scrubbing
# ==============================================================================
with tabs[3]:
    st.subheader("🕳️ RAID Write-Hole Syndrome & Parity Scrubbing")

    st.markdown("""
    The **Write-Hole** occurs when a system crashes after updating a data block but before the corresponding parity block is committed to disk.
    This creates silent inconsistency:
    1. The array continues operating normally until a subsequent disk fails.
    2. During degraded read, the stale parity causes **silent data corruption** on reconstructed blocks.
    3. Periodic **Scrubbing** audits all stripes and repairs inconsistent parities.
    """)

    c_wh1, c_wh2 = st.columns(2)

    with c_wh1:
        st.markdown("### 1. Write-Hole Crash Injection")
        crash_lbn = st.number_input("Crash Target LBN", min_value=0, max_value=array.capacity_blocks() - 1, value=0, key="crash_lbn")

        if st.button("💥 Simulate Crash Mid-Write"):
            try:
                write_with_crash(array, crash_lbn, b"CRASHED_WRITE_DATA")
            except SimulatedCrashError as e:
                st.warning(f"⚠️ **SIMULATED POWER CRASH TRIGGERED!**\n\n{e}")
                # Re-audit
                res = scrub(array, repair=False)
                st.session_state.scrub_inconsistent = res["inconsistent"]
                st.rerun()

        if st.button("🕵️ Demonstrate Silent Corruption"):
            mapping = array.map_lbn(crash_lbn)
            stripe = mapping["stripe"]
            # Fail another data disk in this stripe
            other_data_disks = [d for d in mapping["data_disks"] if d != mapping["disk"]]
            if other_data_disks:
                victim_id = other_data_disks[0]
                victim_disk = array.disks[victim_id]
                victim_disk.fail()
                victim_lbn = stripe * (array.num_disks - 1) + mapping["data_disks"].index(victim_id)

                st.error(f"Disk {victim_id} failed! Performing degraded read on LBN {victim_lbn}...")
                reconstructed = array.read_block(victim_lbn)
                st.error(f"Reconstructed block corrupted due to stale parity:\n```\n{reconstructed[:64]}...\n```")

    with c_wh2:
        st.markdown("### 2. Parity Scrubbing & Self-Healing")

        col_sc1, col_sc2 = st.columns(2)
        with col_sc1:
            if st.button("🔍 Run Parity Scrub (Audit)"):
                res = scrub(array, repair=False)
                st.session_state.scrub_inconsistent = res["inconsistent"]
                if res["inconsistent"]:
                    st.warning(f"Audit: {len(res['inconsistent'])} inconsistent stripes: {res['inconsistent']}")
                else:
                    st.success("Audit: All stripes 100% consistent!")
                st.rerun()

        with col_sc2:
            if st.button("🛠️ Scrub and Auto-Repair"):
                res = scrub(array, repair=True)
                st.session_state.scrub_inconsistent = res["inconsistent"]
                st.success(f"Scrub complete: repaired {res['repaired']} stripe(s).")
                st.rerun()

        if st.session_state.scrub_inconsistent:
            st.warning(f"Highlighted inconsistent stripes in Array View: {st.session_state.scrub_inconsistent}")

# ==============================================================================
# TAB 5: Scheduler
# ==============================================================================
with tabs[4]:
    st.subheader("⏱️ Disk Request Scheduling: FCFS vs SCAN (Elevator)")

    st.markdown("""
    Storage access latency is heavily dominated by mechanical seek time:
    $$\\text{Latency} = \\text{Seek Distance} \\times 0.5\\text{ms} + \\text{Rotational Delay} + \\text{Transfer Time}$$
    - **FCFS**: Serves requests in arrival order, causing high head travel across random blocks.
    - **SCAN (Elevator)**: Sweeps in one direction serving requests monotonically, dramatically reducing head movement.
    """)

    sched_col1, sched_col2 = st.columns([1, 2])

    with sched_col1:
        n_req = st.slider("Request Batch Size", min_value=10, max_value=60, value=25)
        sched_seed = st.number_input("Random Seed", value=42, step=1)
        run_sched = st.button("🚀 Compare FCFS vs SCAN")

    with sched_col2:
        if run_sched:
            import numpy as np
            rng = np.random.default_rng(sched_seed)
            pbns = rng.integers(0, array.disk_blocks, size=n_req).tolist()

            # FCFS
            d_fcfs = VirtualDisk(disk_id=0, num_blocks=array.disk_blocks, block_size=BLOCK_SIZE)
            q_fcfs = DiskQueue(disk=d_fcfs, policy="FCFS")
            for p in pbns:
                q_fcfs.submit({"op": "r", "pbn": int(p)})
            served_fcfs = q_fcfs.process_all()

            # SCAN
            d_scan = VirtualDisk(disk_id=1, num_blocks=array.disk_blocks, block_size=BLOCK_SIZE)
            q_scan = DiskQueue(disk=d_scan, policy="SCAN")
            for p in pbns:
                q_scan.submit({"op": "r", "pbn": int(p)})
            served_scan = q_scan.process_all()

            df_res = pd.DataFrame([
                {"Policy": "FCFS", "Requests": n_req, "Total Head Movement (Blocks)": q_fcfs.total_head_movement, "Total Latency (ms)": f"{d_fcfs.stats['total_simulated_ms']:.1f}"},
                {"Policy": "SCAN", "Requests": n_req, "Total Head Movement (Blocks)": q_scan.total_head_movement, "Total Latency (ms)": f"{d_scan.stats['total_simulated_ms']:.1f}"},
            ])
            st.dataframe(df_res, use_container_width=True)

            fig, ax = plt.subplots(figsize=(7, 3.5))
            ax.plot([s["pbn"] for s in served_fcfs], label=f"FCFS ({q_fcfs.total_head_movement} blks)", marker="o", color="#e53e3e", alpha=0.7)
            ax.plot([s["pbn"] for s in served_scan], label=f"SCAN ({q_scan.total_head_movement} blks)", marker="s", color="#38a169", alpha=0.7)
            ax.set_title("Head Position Over Request Sequence", fontweight="bold")
            ax.set_xlabel("Request Step")
            ax.set_ylabel("PBN (Cylinder)")
            ax.legend()
            ax.grid(True, linestyle="--", alpha=0.5)
            st.pyplot(fig)

# ==============================================================================
# TAB 6: Benchmarks
# ==============================================================================
with tabs[5]:
    st.subheader("📊 Performance Benchmarks & Storage Trade-offs")

    if st.button("🚀 Run Full Benchmark Suite", type="primary") or st.session_state.bench_cache is None:
        with st.spinner("Executing sequential, random, and mixed workloads across RAID 0, 1, 5..."):
            st.session_state.bench_cache = run_full_benchmark(n_requests=60, num_blocks=24, seed=42)

    df_perf, df_sched, df_penalty, df_comp = st.session_state.bench_cache

    st.markdown("### 1. Comparative Architecture Overview")
    st.dataframe(df_comp, use_container_width=True)

    st.markdown("### 2. Throughput & Latency Across Workload Patterns")
    b_col1, b_col2 = st.columns(2)
    with b_col1:
        fig_tp = plot_throughput(df_perf)
        st.pyplot(fig_tp)
    with b_col2:
        fig_lat = plot_latency(df_perf)
        st.pyplot(fig_lat)

    st.divider()

    b_col3, b_col4 = st.columns(2)
    with b_col3:
        st.markdown("### 3. Small-Write Penalty Matrix")
        st.dataframe(df_penalty, use_container_width=True)
    with b_col4:
        st.markdown("### 4. Scheduler Seek Reduction")
        fig_sched = plot_scheduler_movement(df_sched)
        st.pyplot(fig_sched)

# ==============================================================================
# TAB 7: Operation Log
# ==============================================================================
with tabs[6]:
    st.subheader("📜 Live Storage Subsystem Operation Log")

    col_l1, col_l2, col_l3 = st.columns([3, 1, 1])
    with col_l2:
        filter_lvl = st.selectbox("Filter Level", ["all", "info", "warning", "error"])
    with col_l3:
        if st.button("🗑️ Clear Log"):
            st.session_state.oplog.clear()
            st.rerun()

    entries = st.session_state.oplog.entries()
    if filter_lvl != "all":
        entries = [e for e in entries if e.get("level") == filter_lvl]

    if not entries:
        st.info("No log entries found.")
    else:
        for e in reversed(entries):
            lvl = e.get("level", "info")
            t = e.get("time", "")
            msg = e.get("message", "")
            if lvl == "error":
                st.error(f"[{t}] {msg}")
            elif lvl == "warning":
                st.warning(f"[{t}] {msg}")
            else:
                st.info(f"[{t}] {msg}")

# ==============================================================================
# TAB 8: Reliability (Monte Carlo Simulation)
# ==============================================================================
@st.cache_data
def run_reliability_cache(mttf: float, rebuild: float, num_disks: int, mission_years: float, n_trials: int, seed: int):
    """Run cached reliability sweeps."""
    mission_hours = mission_years * 8760.0
    base = ReliabilityParams(
        level="RAID5",
        num_disks=num_disks,
        mttf_hours=mttf,
        rebuild_hours=rebuild,
        mission_hours=mission_hours,
        n_trials=n_trials,
        seed=seed,
    )
    df_comp = compare_levels(base, ["RAID 0", "RAID 1", "RAID 5"])
    rebuild_values = [1.0, 6.0, 12.0, 24.0, 48.0, 96.0, 168.0]
    df_reb = sweep_rebuild_time(base, rebuild_values)

    analytic_p = []
    for r in rebuild_values:
        mttdl = analytic_mttdl_raid5(mttf=mttf, rebuild=r, n=num_disks)
        analytic_p.append(analytic_loss_probability(mttdl=mttdl, mission_hours=mission_hours))
    df_reb["p_analytic"] = analytic_p

    return df_comp, df_reb


with tabs[7]:
    st.subheader("🎲 Monte Carlo RAID Reliability & Data-Loss Simulation")
    st.markdown("""
    Estimate the probability of **unrecoverable data loss** over a mission lifetime given stochastic disk failures and finite rebuild durations.
    """)
    st.info("💡 **Core Principle:** RAID 5 is vulnerable only while rebuilding. A longer rebuild time or larger array size increases the probability that a second disk fails before the first is rebuilt.")

    r_col1, r_col2, r_col3 = st.columns(3)
    with r_col1:
        rel_mttf = st.number_input("Disk MTTF (Hours)", min_value=5000.0, max_value=500000.0, value=100000.0, step=10000.0)
        rel_rebuild = st.number_input("Rebuild Time (Hours)", min_value=1.0, max_value=336.0, value=24.0, step=6.0)
    with r_col2:
        rel_disks = st.number_input("Array Disks (N for RAID 5/0)", min_value=3, max_value=16, value=4)
        rel_years = st.number_input("Mission Duration (Years)", min_value=1.0, max_value=20.0, value=5.0, step=1.0)
    with r_col3:
        rel_trials = st.number_input("Monte Carlo Trials", min_value=1000, max_value=100000, value=20000, step=5000)
        rel_seed = st.number_input("Simulation Seed", value=42, step=1)

    if st.button("🚀 Run Reliability Simulation", type="primary"):
        with st.spinner(f"Simulating {rel_trials:,} mission lifetimes across architectures..."):
            df_comp, df_reb = run_reliability_cache(rel_mttf, rel_rebuild, rel_disks, rel_years, rel_trials, rel_seed)
            st.session_state["rel_results"] = (df_comp, df_reb)

    if "rel_results" in st.session_state:
        df_comp, df_reb = st.session_state["rel_results"]

        st.markdown("### 1. Data-Loss Probability by Architecture")
        df_display = df_comp.copy()
        df_display["p_loss_pct"] = df_display["p_loss"].apply(lambda x: f"{x*100:.3f}%")
        df_display["ci_95"] = df_display.apply(lambda r: f"[{r['ci_low']*100:.3f}%, {r['ci_high']*100:.3f}%]", axis=1)
        st.dataframe(df_display[["level", "num_disks", "p_loss_pct", "ci_95", "losses"]], use_container_width=True)

        rel_p1, rel_p2 = st.columns(2)
        with rel_p1:
            fig_bar, ax_bar = plt.subplots(figsize=(6, 4))
            bars = ax_bar.bar(df_comp["level"], df_comp["p_loss"], color=["#e53e3e", "#319795", "#dd6b20"], width=0.5, edgecolor="black")
            ax_bar.set_title("P(Data Loss) Across Architectures", fontweight="bold")
            ax_bar.set_ylabel("P(Data Loss)")
            ax_bar.grid(axis="y", linestyle="--", alpha=0.6)
            for b in bars:
                h = b.get_height()
                ax_bar.text(b.get_x() + b.get_width() / 2.0, h + 0.01, f"{h:.4f}", ha="center", va="bottom", fontweight="bold")
            plt.tight_layout()
            st.pyplot(fig_bar)

        with rel_p2:
            fig_line, ax_line = plt.subplots(figsize=(6.5, 4))
            yerr = [df_reb["p_loss"] - df_reb["ci_low"], df_reb["ci_high"] - df_reb["p_loss"]]
            ax_line.errorbar(df_reb["rebuild_hours"], df_reb["p_loss"], yerr=yerr, fmt="o-", color="#dd6b20", label="Monte Carlo Simulation", capsize=4)
            ax_line.plot(df_reb["rebuild_hours"], df_reb["p_analytic"], "k--", label="Analytic MTTDL Approx")
            ax_line.set_title("RAID 5: P(Loss) vs. Rebuild Window", fontweight="bold")
            ax_line.set_xlabel("Rebuild Time (Hours)")
            ax_line.set_ylabel("P(Data Loss)")
            ax_line.legend()
            ax_line.grid(True, linestyle="--", alpha=0.6)
            plt.tight_layout()
            st.pyplot(fig_line)
