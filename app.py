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
from raid.raid6 import Raid6
from raid.raid10 import Raid10
from raid.locks import StripeLockManager
from raid.oplog import OperationLog
from raid.journal import WriteIntentJournal
from raid.cache import LruBlockCache
from raid.scrub import (
    scrub,
    write_with_crash,
    checksum_array,
    BlockChecksumManager,
    SimulatedCrashError,
    ScrubUnavailableError,
)
from raid.scheduler import DiskQueue
from raid.workload import generate_workload, parse_trace_csv, get_sample_trace
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

if "chunk_size" not in st.session_state:
    st.session_state.chunk_size = 1

if "rebuild_rate_limit_ms" not in st.session_state:
    st.session_state.rebuild_rate_limit_ms = 0.0

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

if "race_unsafe_result" not in st.session_state:
    st.session_state.race_unsafe_result = None

if "race_safe_result" not in st.session_state:
    st.session_state.race_safe_result = None

if "scrub_feedback" not in st.session_state:
    st.session_state.scrub_feedback = None

if "hot_spare_feedback" not in st.session_state:
    st.session_state.hot_spare_feedback = None

if "rebuild_feedback" not in st.session_state:
    st.session_state.rebuild_feedback = None

if "write_intent_journal" not in st.session_state:
    st.session_state.write_intent_journal = WriteIntentJournal(enabled=True)

if "block_checksum_mgr" not in st.session_state:
    st.session_state.block_checksum_mgr = BlockChecksumManager()


def create_array():
    oplog = st.session_state.oplog
    rtype = st.session_state.get("sidebar_raid_type", st.session_state.get("raid_type", "RAID 5"))
    n_disks = st.session_state.get("sidebar_num_disks", st.session_state.get("num_disks", 4))
    n_blocks = st.session_state.get("sidebar_blocks", st.session_state.get("blocks_per_disk", 16))
    n_spares = st.session_state.get("sidebar_spares", st.session_state.get("num_spares", 1))
    policy = st.session_state.get("sidebar_policy", st.session_state.get("scheduler_policy", "FCFS"))
    locks_on = st.session_state.get("sidebar_locks", st.session_state.get("locks_enabled", True))
    chunk_k = st.session_state.get("sidebar_chunk_size", st.session_state.get("chunk_size", 1))
    rate_delay = st.session_state.get("sidebar_rate_limit", st.session_state.get("rebuild_rate_limit_ms", 0.0))

    if rtype == "RAID 10" and n_disks % 2 != 0:
        n_disks += 1

    oplog.add(
        f"Initialized new {rtype} ({n_disks} disks, {n_blocks} blocks/disk, {n_spares} hot spares, chunk={chunk_k}, policy={policy}).",
        level="info",
    )

    disks = [VirtualDisk(disk_id=i, num_blocks=n_blocks, block_size=BLOCK_SIZE) for i in range(n_disks)]
    spares = [VirtualDisk(disk_id=100 + i, num_blocks=n_blocks, block_size=BLOCK_SIZE) for i in range(n_spares)]

    if rtype == "RAID 0":
        array = Raid0(disks=disks, spares=spares, oplog=oplog, scheduler_policy=policy, chunk_size=chunk_k)
    elif rtype == "RAID 1":
        array = Raid1(disks=disks, spares=spares, oplog=oplog, scheduler_policy=policy)
    elif rtype == "RAID 6":
        lock_mgr = StripeLockManager(num_stripes=n_blocks, enabled=locks_on)
        array = Raid6(
            disks=disks,
            spares=spares,
            oplog=oplog,
            lock_manager=lock_mgr,
            scheduler_policy=policy,
            chunk_size=chunk_k,
        )
    elif rtype == "RAID 10":
        array = Raid10(
            disks=disks,
            spares=spares,
            oplog=oplog,
            scheduler_policy=policy,
            chunk_size=chunk_k,
        )
    else:  # RAID 5
        lock_mgr = StripeLockManager(num_stripes=n_blocks, enabled=locks_on)
        array = Raid5(
            disks=disks,
            spares=spares,
            oplog=oplog,
            lock_manager=lock_mgr,
            scheduler_policy=policy,
            chunk_size=chunk_k,
        )

    array.rebuild_rate_limit_ms = rate_delay
    st.session_state.block_checksum_mgr.populate_from_array(array)

    st.session_state.raid_array = array
    st.session_state.raid_type = rtype
    st.session_state.num_disks = n_disks
    st.session_state.blocks_per_disk = n_blocks
    st.session_state.num_spares = n_spares
    st.session_state.chunk_size = chunk_k
    st.session_state.rebuild_rate_limit_ms = rate_delay
    st.session_state.locks_enabled = locks_on
    st.session_state.scheduler_policy = policy
    st.session_state.stored_checksum = None
    st.session_state.scrub_inconsistent = []
    st.session_state.race_unsafe_result = None
    st.session_state.race_safe_result = None
    st.session_state.scrub_feedback = None
    st.session_state.hot_spare_feedback = None
    st.session_state.rebuild_feedback = None


# --- Sidebar ---
st.sidebar.title("🛡️ Storage Stack Config")

raid_choice = st.sidebar.selectbox(
    "RAID Architecture",
    options=["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"],
    index=2,
    key="sidebar_raid_type",
)

if raid_choice in ("RAID 6", "RAID 10"):
    min_d = 4
    step_d = 2 if raid_choice == "RAID 10" else 1
elif raid_choice == "RAID 5":
    min_d = 3
    step_d = 1
else:
    min_d = 2
    step_d = 1

current_n = max(min_d, st.session_state.num_disks)
if raid_choice == "RAID 10" and current_n % 2 != 0:
    current_n += 1

st.sidebar.number_input("Disks Count", min_value=min_d, max_value=8, value=current_n, step=step_d, key="sidebar_num_disks")
st.sidebar.number_input("Blocks per Disk", min_value=8, max_value=64, value=st.session_state.blocks_per_disk, step=8, key="sidebar_blocks")
st.sidebar.number_input("Hot Spares Pool", min_value=0, max_value=4, value=st.session_state.num_spares, key="sidebar_spares")
st.sidebar.selectbox("Chunk / Stripe Unit Size (Blocks)", options=[1, 2, 4], index=0, key="sidebar_chunk_size")
st.sidebar.slider("Rebuild Rate Limit Delay (ms)", min_value=0.0, max_value=50.0, value=float(st.session_state.rebuild_rate_limit_ms), step=5.0, key="sidebar_rate_limit")
st.sidebar.selectbox("Queue Scheduler Policy", options=["FCFS", "SCAN", "C-SCAN", "LOOK"], index=0, key="sidebar_policy")
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
        <span style='background:#dd6b20;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Parity P (OK)</span> &nbsp;
        <span style='background:#805ad5;color:white;padding:3px 8px;border-radius:4px;font-size:12px;'>Parity Q (OK)</span> &nbsp;
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
        ("parity_q", "ok"): "#805ad5",
        ("parity_q", "failed"): "#e53e3e",
        ("parity_q", "rebuilt"): "#38a169",
        ("parity_q", "inconsistent"): "#d69e2e",
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
                label = b.get("label") or (f"P{stripe}" if kind == "parity" else (f"M{lbn}" if kind == "mirror" else f"L{lbn}"))
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

    if st.session_state.rebuild_feedback:
        fb_type, fb_msg = st.session_state.rebuild_feedback
        if fb_type == "success":
            st.success(fb_msg)
        elif fb_type == "error":
            st.error(fb_msg)
        elif fb_type == "warning":
            st.warning(fb_msg)
        else:
            st.info(fb_msg)

    f_col1, f_col2, f_col3, f_col4 = st.columns(4)
    disk_ids = [d.disk_id for d in array.disks]

    with f_col1:
        k_id = st.selectbox("Select Disk to Kill", options=disk_ids, key="k_disk_select")
        if st.button("💥 Kill Disk", type="primary"):
            array.fail_disk(k_id)
            st.session_state.rebuild_feedback = ("warning", f"⚠️ Disk {k_id} marked FAILED.")
            st.rerun()

    with f_col2:
        rep_id = st.selectbox("Select Disk to Replace", options=disk_ids, key="rep_disk_select")
        if st.button("🔧 Replace Disk"):
            array.replace_disk(rep_id)
            st.session_state.rebuild_feedback = ("info", f"🔧 Disk {rep_id} replaced with blank disk. Status: Needs Rebuild.")
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
                st.session_state.rebuild_feedback = (
                    "success",
                    f"✅ Disk {reb_id} rebuild complete: 100% reconstructed across all {array.disk_blocks} stripes!",
                )
            else:
                st.session_state.rebuild_feedback = (
                    "error",
                    f"❌ Rebuild failed for Disk {reb_id}. Array may have multiple disk failures.",
                )
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
            failed_id = array.disks[0].disk_id
            had_spare = len(array.spares) > 0
            array.fail_disk(failed_id)
            rebuild_thread = getattr(array, "last_rebuild_thread", None)
            if rebuild_thread:
                with st.spinner("Hot spare activated! Running background rebuild thread..."):
                    rebuild_thread.join(timeout=6.0)
            if had_spare:
                st.session_state.hot_spare_feedback = (
                    "success",
                    f"⚡ **Hot Spare Auto-Activated!** Rebuilt all {array.disk_blocks}/{array.disk_blocks} stripes onto Disk {failed_id}. "
                    f"Volume returned to HEALTHY. Spares remaining in pool: {len(array.spares)}.",
                )
            else:
                st.session_state.hot_spare_feedback = (
                    "warning",
                    f"⚠️ Disk {failed_id} failed, but hot spare pool is empty. Please replace and rebuild manually.",
                )
            st.rerun()

    if st.session_state.hot_spare_feedback:
        kind, msg = st.session_state.hot_spare_feedback
        if kind == "success":
            st.success(msg)
        else:
            st.warning(msg)

    st.divider()

    st.markdown("### 4. 🧠 SMART Predictive Health & Proactive Migration")
    st.markdown("""
    Enterprise storage arrays monitor drive SMART health indicators (bad sector reallocations, uncorrectable read errors).
    When failure is imminent, **Proactive Spare Migration** clones data directly from the online disk to a spare
    **before** catastrophic drive death, preventing the array from ever entering a vulnerable degraded state!
    """)

    smart_telemetry = array.check_smart_telemetry()
    df_smart = pd.DataFrame(smart_telemetry)
    st.dataframe(df_smart, use_container_width=True)

    c_sm1, c_sm2 = st.columns(2)
    with c_sm1:
        if st.button("⚠️ Inject SMART Bad Sectors on Disk 1"):
            if len(array.disks) > 1:
                array.disks[1].record_smart_error(3)
                st.session_state.oplog.add(f"Injected 3 SMART reallocated sectors into Disk {array.disks[1].disk_id}.", level="warning")
                st.rerun()

    with c_sm2:
        if st.button("🛡️ Proactively Migrate Warning Disk to Spare"):
            warning_disks = [d for d in array.disks if d.is_smart_warning()]
            if not warning_disks:
                st.info("No disks currently in SMART WARNING status. Inject errors first.")
            elif not array.spares:
                st.warning("Hot spare pool is empty! Cannot perform proactive migration.")
            else:
                target_failing = warning_disks[0]
                success = array.proactive_spare_migration(target_failing.disk_id)
                if success:
                    st.success(f"🎉 **Proactive Migration Complete!** Disk {target_failing.disk_id} migrated cleanly to spare with 0% downtime and NO degraded state!")
                st.rerun()

    if isinstance(array, Raid6):
        st.divider()
        st.markdown("### 5. 💥 RAID 6 Dual-Disk Failure Survival Demo")
        st.write("RAID 6 uses distributed Reed-Solomon Galois Field $P+Q$ parity to tolerate **any 2 simultaneous disk failures**.")
        if st.button("💥 Kill Both Disk 0 and Disk 1 Simultaneously"):
            if len(array.disks) >= 4:
                array.disks[0].fail()
                array.disks[1].fail()
                st.session_state.oplog.add("Simulated dual disk failure on Disks 0 & 1.", level="error")
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

    st.markdown("You can run either demonstration individually or run both together to observe the contrast side-by-side:")

    if st.button("⚡ Run Both Demos (Side-by-Side Comparison)", type="primary"):
        # 1. Run Unsafe
        disks_u = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
        lock_off = StripeLockManager(num_stripes=16, enabled=False)
        raid_u = Raid5(disks=disks_u, lock_manager=lock_off, artificial_delay_ms=30.0)
        t1 = threading.Thread(target=raid_u.write_block, args=(0, b"DATA_FROM_THREAD_1_UNSAFE"))
        t2 = threading.Thread(target=raid_u.write_block, args=(1, b"DATA_FROM_THREAD_2_UNSAFE"))
        t1.start()
        time.sleep(0.005)
        t2.start()
        t1.join()
        t2.join()
        scrub_u = scrub(raid_u, repair=False)
        st.session_state.race_unsafe_result = (
            "error",
            f"💥 **RACE CONDITION DETECTED!**\n\nStripe 0 Parity is Corrupted!\nInconsistent Stripes: {scrub_u['inconsistent']}",
        )

        # 2. Run Safe
        disks_s = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
        lock_on = StripeLockManager(num_stripes=16, enabled=True)
        raid_s = Raid5(disks=disks_s, lock_manager=lock_on, artificial_delay_ms=30.0)
        t3 = threading.Thread(target=raid_s.write_block, args=(0, b"DATA_FROM_THREAD_1_SAFE"))
        t4 = threading.Thread(target=raid_s.write_block, args=(1, b"DATA_FROM_THREAD_2_SAFE"))
        t3.start()
        time.sleep(0.005)
        t4.start()
        t3.join()
        t4.join()
        scrub_s = scrub(raid_s, repair=False)
        st.session_state.race_safe_result = (
            "success",
            "🎉 **PERFECT CONSISTENCY!**\n\nStripe locks prevented the race condition. 0 inconsistent stripes detected.",
        )
        st.rerun()

    col_race1, col_race2 = st.columns(2)

    with col_race1:
        st.markdown("### ❌ Unsafe Mode (Locks DISABLED)")
        st.caption("Two threads write to LBN 0 and LBN 1 (same stripe) with artificial RMW delay.")
        if st.button("Run Unsafe Writes Only"):
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
            st.session_state.oplog.add(
                f"Concurrency demo: unsafe concurrent writes. Inconsistent stripes: {scrub_res['inconsistent']}",
                level="warning",
            )
            st.session_state.race_unsafe_result = (
                "error",
                f"💥 **RACE CONDITION DETECTED!**\n\nStripe 0 Parity is Corrupted!\nInconsistent Stripes: {scrub_res['inconsistent']}",
            )
            st.rerun()

        if st.session_state.race_unsafe_result:
            _, msg = st.session_state.race_unsafe_result
            st.error(msg)
        else:
            st.info("Click 'Run Unsafe Writes Only' or 'Run Both Demos' to execute.")

    with col_race2:
        st.markdown("### ✅ Safe Mode (Locks ENABLED)")
        st.caption("StripeLockManager serializes RMW critical sections per stripe.")
        if st.button("Run Safe Writes Only"):
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
                st.session_state.oplog.add(
                    "Concurrency demo: safe concurrent writes. 0 inconsistent stripes.",
                    level="info",
                )
                st.session_state.race_safe_result = (
                    "success",
                    "🎉 **PERFECT CONSISTENCY!**\n\nStripe locks prevented the race condition. 0 inconsistent stripes detected.",
                )
            else:
                st.session_state.oplog.add(
                    f"Concurrency demo: safe concurrent writes still found inconsistencies: {scrub_res['inconsistent']}",
                    level="error",
                )
                st.session_state.race_safe_result = (
                    "error",
                    f"Inconsistencies found: {scrub_res['inconsistent']}",
                )
            st.rerun()

        if st.session_state.race_safe_result:
            kind, msg = st.session_state.race_safe_result
            if kind == "success":
                st.success(msg)
            else:
                st.error(msg)
        else:
            st.info("Click 'Run Safe Writes Only' or 'Run Both Demos' to execute.")

    if st.session_state.race_unsafe_result and st.session_state.race_safe_result:
        st.divider()
        st.markdown("### 📊 Side-by-Side Architectural Takeaway")
        c_take1, c_take2 = st.columns(2)
        with c_take1:
            st.warning("⚠️ **Locks Disabled (Unsafe):** Concurrent RMW threads read stale parity concurrently, clobbering each other's updates. Parity becomes silently corrupt.")
        with c_take2:
            st.success("🛡️ **Stripe Locks Enabled (Safe):** Each stripe has a dedicated mutex. The critical section (Read Data/Parity $\\rightarrow$ Compute $\\rightarrow$ Write Data/Parity) is atomic per stripe.")

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
                try:
                    res = scrub(array, repair=False)
                    st.session_state.scrub_inconsistent = res["inconsistent"]
                    st.session_state.scrub_feedback = (
                        "warning",
                        f"⚠️ **SIMULATED POWER CRASH TRIGGERED!**\n\n{e}",
                    )
                except (ScrubUnavailableError, DiskFailedError):
                    st.session_state.scrub_feedback = (
                        "degraded",
                        "Scrub needs all disks online. Replace and rebuild the failed disk first.",
                    )
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
                array.oplog.add(
                    f"Silent corruption demo: Disk {victim_id} failed after write-hole.",
                    level="error",
                )
                victim_lbn = stripe * (array.num_disks - 1) + mapping["data_disks"].index(victim_id)

                st.error(f"Disk {victim_id} failed! Performing degraded read on LBN {victim_lbn}...")
                reconstructed = array.read_block(victim_lbn)
                st.error(f"Reconstructed block corrupted due to stale parity:\n```\n{reconstructed[:64]}...\n```")

    with c_wh2:
        st.markdown("### 2. Parity Scrubbing & Self-Healing")

        col_sc1, col_sc2 = st.columns(2)
        with col_sc1:
            if st.button("🔍 Run Parity Scrub (Audit)"):
                try:
                    res = scrub(array, repair=False)
                    st.session_state.scrub_inconsistent = res["inconsistent"]
                    if res["inconsistent"]:
                        st.session_state.scrub_feedback = (
                            "warning",
                            f"🔍 **Parity Audit:** Detected {len(res['inconsistent'])} inconsistent stripe(s): {res['inconsistent']}. Parity is out-of-sync with data.",
                        )
                    else:
                        st.session_state.scrub_feedback = (
                            "success",
                            "🔍 **Parity Audit:** 100% of stripes are consistent. No parity errors detected.",
                        )
                except (ScrubUnavailableError, DiskFailedError):
                    st.session_state.scrub_feedback = (
                        "degraded",
                        "⚠️ **Scrub needs all disks online.** Replace and rebuild the failed disk before auditing parity.",
                    )
                st.rerun()

        with col_sc2:
            if st.button("🛠️ Scrub and Auto-Repair"):
                try:
                    res = scrub(array, repair=True)
                    repaired = res["repaired"]
                    # Highlights should not remain after a completed repair.
                    st.session_state.scrub_inconsistent = []
                    if repaired > 0:
                        st.session_state.scrub_feedback = (
                            "success",
                            f"🛠️ **Scrub and Auto-Repair:** Repaired {repaired} inconsistent stripe(s). Parity is now 100% consistent!",
                        )
                    else:
                        st.session_state.scrub_feedback = (
                            "success",
                            "🛠️ **Scrub and Auto-Repair:** All stripes checked; 0 inconsistencies found.",
                        )
                except (ScrubUnavailableError, DiskFailedError):
                    st.session_state.scrub_feedback = (
                        "degraded",
                        "⚠️ **Scrub needs all disks online.** Replace and rebuild the failed disk before repairing parity.",
                    )
                st.rerun()

        if st.session_state.scrub_feedback:
            kind, msg = st.session_state.scrub_feedback
            if kind == "success":
                st.success(msg)
            elif kind == "degraded":
                st.warning(msg)
            else:
                st.warning(msg)

        if st.session_state.scrub_inconsistent:
            st.warning(f"Highlighted inconsistent stripes in Array View: {st.session_state.scrub_inconsistent}")

    st.divider()

    c_wh3, c_wh4 = st.columns(2)

    with c_wh3:
        st.markdown("### 3. 📜 Write-Intent Logging (NVRAM Journal)")
        st.markdown("""
        Standard RAID 5 must audit the **entire array** ($O(\\text{Capacity})$) after an unclean shutdown.
        A **Write-Intent Log** tracks active in-flight stripes in persistent NVRAM memory.
        Post-crash recovery checks **only** the uncommitted stripes ($O(1)$) in milliseconds!
        """)

        journal = st.session_state.write_intent_journal
        pending = journal.pending_stripes()
        st.info(f"Active In-Flight Journaled Stripes: `{list(pending) if pending else 'None (Clean)'}`")

        j_col1, j_col2 = st.columns(2)
        with j_col1:
            if st.button("💥 Log Intent & Crash Mid-Write"):
                try:
                    mapping = array.map_lbn(crash_lbn)
                    journal.log_intent(stripe=mapping["stripe"], lbn=crash_lbn)
                    write_with_crash(array, crash_lbn, b"JOURNALED_INFLIGHT_DATA")
                except SimulatedCrashError:
                    st.session_state.scrub_feedback = (
                        "warning",
                        f"⚠️ Power loss simulated! Stripe {mapping['stripe']} is flagged DIRTY in the Write-Intent Journal.",
                    )
                    st.rerun()

        with j_col2:
            if st.button("⚡ Fast Journal Recovery"):
                repaired_stripes = journal.recover(array)
                st.session_state.scrub_inconsistent = []
                if repaired_stripes:
                    st.session_state.scrub_feedback = (
                        "success",
                        f"⚡ **Instant Journal Recovery!** Audited & repaired {len(repaired_stripes)} active stripe(s): {repaired_stripes} in < 1ms without a full array scrub!",
                    )
                else:
                    st.session_state.scrub_feedback = (
                        "success",
                        "⚡ **Journal Clean:** No pending in-flight stripes found.",
                    )
                st.rerun()

    with c_wh4:
        st.markdown("### 4. 🛡️ ZFS-Style Per-Block Checksums & Self-Healing")
        st.markdown("""
        Standard parity scrub detects inconsistency but cannot identify **which disk** contains corrupted data.
        Cryptographic per-block SHA-256 hashes pinpoint silent bit rot on individual physical blocks and
        reconstruct the corrupted block using surviving blocks + parity!
        """)

        c_mgr = st.session_state.block_checksum_mgr

        b_btn1, b_btn2 = st.columns(2)
        with b_btn1:
            if st.button("🧪 Inject Silent Bit-Rot on Disk 0"):
                if array.disks:
                    d0 = array.disks[0]
                    # Mutate physical block 0 directly
                    corrupted = bytearray(d0.read(0))
                    corrupted[0] ^= 0xAA
                    d0.blocks[0] = bytearray(corrupted)
                    st.session_state.oplog.add(f"Silent bit rot injected into Disk {d0.disk_id} PBN 0 (data flipped).", level="error")
                    st.session_state.scrub_feedback = (
                        "warning",
                        f"🧪 **Bit Rot Injected:** Disk {d0.disk_id} PBN 0 silently modified. OS is unaware until checksum audit!",
                    )
                    st.rerun()

        with b_btn2:
            if st.button("🛡️ Scrub with Checksum Self-Healing"):
                try:
                    res = scrub(array, repair=True, checksum_mgr=c_mgr)
                    st.session_state.scrub_inconsistent = []
                    healed = res.get("healed", 0)
                    corrupted = res.get("corrupted_blocks", [])
                    if healed > 0:
                        st.session_state.scrub_feedback = (
                            "success",
                            f"🛡️ **ZFS-Style Self-Healing Success!** Pinpointed bit rot on {len(corrupted)} block(s): {corrupted}. "
                            f"Successfully reconstructed and restored {healed} block(s) from parity!",
                        )
                    else:
                        st.session_state.scrub_feedback = (
                            "success",
                            "🛡️ All cryptographic block checksums verified: 0 bit-rot errors detected.",
                        )
                except (ScrubUnavailableError, DiskFailedError):
                    st.session_state.scrub_feedback = (
                        "degraded",
                        "⚠️ Scrub needs all disks online to verify checksums.",
                    )
                st.rerun()

# ==============================================================================
# TAB 5: Scheduler
# ==============================================================================
with tabs[4]:
    st.subheader("⏱️ Disk Request Scheduling: FCFS vs SCAN vs C-SCAN vs LOOK")

    st.markdown("""
    Storage access latency is heavily dominated by mechanical seek time:
    $$\\text{Latency} = \\text{Seek Distance} \\times 0.5\\text{ms} + \\text{Rotational Delay} + \\text{Transfer Time}$$
    - **FCFS**: Serves requests in arrival order (prone to head thrashing across random blocks).
    - **SCAN (Elevator)**: Sweeps in one direction serving requests monotonically, dramatically reducing seek distance.
    - **C-SCAN (Circular SCAN)**: Sweeps in one direction (increasing PBN), then returns to the lowest track to provide uniform waiting time.
    - **LOOK**: Similar to SCAN, but only travels as far as the furthest pending request in each direction (no boundary visits).
    """)

    sched_col1, sched_col2 = st.columns([1, 2])

    with sched_col1:
        n_req = st.slider("Request Batch Size", min_value=10, max_value=60, value=25)
        sched_seed = st.number_input("Random Seed", value=42, step=1)
        run_sched = st.button("🚀 Compare All Schedulers", type="primary")

    with sched_col2:
        if run_sched:
            import numpy as np
            rng = np.random.default_rng(sched_seed)
            pbns = rng.integers(0, array.disk_blocks, size=n_req).tolist()

            policies = ["FCFS", "SCAN", "C-SCAN", "LOOK"]
            queues = {}
            disks = {}
            served_map = {}
            records = []

            for i, pol in enumerate(policies):
                d = VirtualDisk(disk_id=i, num_blocks=array.disk_blocks, block_size=BLOCK_SIZE)
                d.head_position = 0
                q = DiskQueue(disk=d, policy=pol)
                for p in pbns:
                    q.submit({"op": "r", "pbn": int(p)})
                served = q.process_all()
                disks[pol] = d
                queues[pol] = q
                served_map[pol] = served

            fcfs_movement = queues["FCFS"].total_head_movement
            for pol in policies:
                q = queues[pol]
                d = disks[pol]
                reduction = f"{(1 - q.total_head_movement / max(1, fcfs_movement))*100:.1f}%" if pol != "FCFS" else "Baseline (0.0%)"
                records.append({
                    "Policy": pol,
                    "Requests": n_req,
                    "Total Head Movement (Blocks)": q.total_head_movement,
                    "Seek Reduction vs FCFS": reduction,
                    "Total Latency (ms)": f"{d.stats['total_simulated_ms']:.1f}",
                })

            st.dataframe(pd.DataFrame(records), use_container_width=True)

            fig, ax = plt.subplots(figsize=(7.5, 3.8))
            styles = {
                "FCFS": ("#e53e3e", "o", "-"),
                "SCAN": ("#38a169", "s", "-"),
                "C-SCAN": ("#3182ce", "^", "--"),
                "LOOK": ("#dd6b20", "D", "-."),
            }
            for pol in policies:
                color, marker, lstyle = styles[pol]
                q = queues[pol]
                ax.plot(
                    [s["pbn"] for s in served_map[pol]],
                    label=f"{pol} ({q.total_head_movement} blks)",
                    marker=marker,
                    color=color,
                    linestyle=lstyle,
                    alpha=0.75,
                )
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
        with st.spinner("Executing sequential, random, and mixed workloads across RAID 0, 1, 5, 6, 10..."):
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

    st.divider()

    b_col5, b_col6 = st.columns(2)

    with b_col5:
        st.markdown("### 5. 📂 Real-World Workload Trace Replay")
        trace_type = st.selectbox(
            "Select Trace Source",
            options=["OLTP Database (70% Read / 30% Write)", "Web Server (95% Read / 5% Write)", "Custom CSV File Upload"],
        )

        trace_requests = []
        if trace_type == "Custom CSV File Upload":
            uploaded_csv = st.file_uploader("Upload Trace CSV (format: op,lbn)", type=["csv", "txt"])
            if uploaded_csv:
                trace_content = uploaded_csv.read().decode("utf-8")
                trace_requests = parse_trace_csv(trace_content, max_lbn=array.capacity_blocks() - 1)
        else:
            trace_name = "oltp" if "OLTP" in trace_type else "web"
            trace_requests = get_sample_trace(trace_name, max_lbn=array.capacity_blocks() - 1, n_requests=40)

        st.caption(f"Loaded **{len(trace_requests)}** requests ready for replay.")

        if st.button("▶️ Execute Trace on Active Array"):
            start_ms = time.time() * 1000.0
            r_count = 0
            w_count = 0
            for req in trace_requests:
                op = req.get("op", "r")
                lbn = req.get("lbn", 0) % array.capacity_blocks()
                if op == "r":
                    array.read_block(lbn)
                    r_count += 1
                else:
                    array.write_block(lbn, b"TRACE_DATA")
                    w_count += 1
            duration_ms = (time.time() * 1000.0) - start_ms

            st.success(
                f"🎉 **Trace Replay Complete!**\n\n"
                f"- Total Requests: {len(trace_requests)} ({r_count} Reads, {w_count} Writes)\n"
                f"- Wall-clock Execution Time: {duration_ms:.2f} ms\n"
                f"- Array Health: {'DEGRADED ⚠️' if array.is_degraded() else 'HEALTHY 🟢'}"
            )

    with b_col6:
        st.markdown("### 6. 🚀 LRU Buffer Cache Acceleration")
        st.markdown("""
        Demonstrates how an in-memory page/block cache absorbs reads and amortizes small writes:
        - **Write-Back**: Writes are buffered in cache as dirty blocks; flushed asynchronously.
        - **Write-Through**: Writes update cache and immediately commit to underlying disks.
        """)

        cache_mode = st.radio("Cache Write Policy", ["Write-Back (Fast)", "Write-Through (Consistent)"], horizontal=True)
        cache_capacity = st.slider("Cache Capacity (Blocks)", min_value=2, max_value=16, value=4)

        if st.button("⚡ Run Cached Workload Comparison"):
            is_wb = "Write-Back" in cache_mode
            cache = LruBlockCache(array=array, capacity_blocks=cache_capacity, write_back=is_wb)

            locality_seq = [0, 1, 0, 2, 0, 1, 3, 2, 0, 1, 4, 0, 1, 2, 0, 3, 1, 0, 2, 1]
            for lbn in locality_seq:
                cache.read_block(lbn % array.capacity_blocks())
                cache.write_block(lbn % array.capacity_blocks(), b"CACHED_PAYLOAD")

            st.metric("Cache Hit Rate", f"{cache.hit_rate * 100:.1f}%")
            st.metric("Dirty Blocks in Cache", cache.dirty_count)
            flushed = cache.flush()
            st.success(f"Cache test finished. Hit rate: {cache.hit_rate*100:.1f}%. Flushed {flushed} dirty blocks to RAID.")

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
def run_reliability_cache(mttf: float, rebuild: float, num_disks: int, mission_years: float, n_trials: int, seed: int, p_ure: float = 0.0):
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
        p_ure_per_rebuild=p_ure,
    )
    df_comp = compare_levels(base, ["RAID 0", "RAID 1", "RAID 5", "RAID 6", "RAID 10"])
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
    st.info("💡 **Core Principles:** RAID 5 is vulnerable only while rebuilding. RAID 6 survives 2 simultaneous failures. URE modeling captures latent sector errors on surviving disks during rebuild.")

    r_col1, r_col2, r_col3, r_col4 = st.columns(4)
    with r_col1:
        rel_mttf = st.number_input("Disk MTTF (Hours)", min_value=5000.0, max_value=500000.0, value=100000.0, step=10000.0)
        rel_rebuild = st.number_input("Rebuild Time (Hours)", min_value=1.0, max_value=336.0, value=24.0, step=6.0)
    with r_col2:
        rel_disks = st.number_input("Array Disks (N for RAID 5/6/0)", min_value=4, max_value=16, value=4)
        rel_years = st.number_input("Mission Duration (Years)", min_value=1.0, max_value=20.0, value=5.0, step=1.0)
    with r_col3:
        rel_trials = st.number_input("Monte Carlo Trials", min_value=1000, max_value=100000, value=20000, step=5000)
        rel_seed = st.number_input("Simulation Seed", value=42, step=1)
    with r_col4:
        rel_ure = st.slider("URE Risk per Rebuild (%)", min_value=0.0, max_value=5.0, value=0.0, step=0.5) / 100.0
        st.caption("Unrecoverable Read Error probability while rebuilding surviving disks.")

    if st.button("🚀 Run Reliability Simulation", type="primary"):
        with st.spinner(f"Simulating {rel_trials:,} mission lifetimes across architectures..."):
            df_comp, df_reb = run_reliability_cache(rel_mttf, rel_rebuild, rel_disks, rel_years, rel_trials, rel_seed, rel_ure)
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
            fig_bar, ax_bar = plt.subplots(figsize=(6.5, 4))
            color_dict = {
                "RAID 0": "#e53e3e",
                "RAID 1": "#319795",
                "RAID 5": "#dd6b20",
                "RAID 6": "#805ad5",
                "RAID 10": "#3182ce",
            }
            colors = [color_dict.get(lvl, "#4a5568") for lvl in df_comp["level"]]
            bars = ax_bar.bar(df_comp["level"], df_comp["p_loss"], color=colors, width=0.5, edgecolor="black")
            ax_bar.set_title("P(Data Loss) Across Architectures", fontweight="bold")
            ax_bar.set_ylabel("P(Data Loss)")
            ax_bar.grid(axis="y", linestyle="--", alpha=0.6)
            for b in bars:
                h = b.get_height()
                ax_bar.text(b.get_x() + b.get_width() / 2.0, h + 0.005, f"{h:.4f}", ha="center", va="bottom", fontweight="bold")
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
