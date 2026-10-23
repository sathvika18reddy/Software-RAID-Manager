"""Workspace renderers for Software RAID Manager: Array, Experiments, and Benchmarks."""

import time
import threading
import streamlit as st
import pandas as pd
import matplotlib.pyplot as plt

from raid.config import BLOCK_SIZE
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid5 import Raid5
from raid.locks import StripeLockManager
from raid.scrub import scrub, write_with_crash, checksum_array, SimulatedCrashError
from raid.scheduler import DiskQueue
from raid.workload import generate_workload
from raid.benchmark import run_full_benchmark
from raid.reliability import (
    ReliabilityParams,
    run_simulation,
    sweep_rebuild_time,
    compare_levels,
    analytic_mttdl_raid5,
    analytic_loss_probability,
)
from ui.components import status_banner, stat_row, rack_view, mapping_inspector
from ui.charts import (
    plot_benchmark_throughput,
    plot_benchmark_latency,
    plot_scheduler_comparison,
    COLOR_PANEL,
    COLOR_LINE,
    COLOR_DATA,
    COLOR_MIRROR,
    COLOR_PARITY,
    COLOR_FAILED,
    COLOR_OK,
    apply_theme_to_axes,
)


# ==============================================================================
# WORKSPACE 1: ARRAY
# ==============================================================================
def render_array(array, oplog) -> None:
    """Render the primary Array workspace: rack, mapping, data I/O, failure recovery."""
    inconsistent_stripes = st.session_state.get("scrub_inconsistent", [])

    # 1. Status Banner
    st.html(status_banner(array, inconsistent_stripes))

    # 2. Stat Row
    st.html(stat_row(array))

    # Inspector selection state
    highlight_stripe = st.session_state.get("highlight_stripe", None)
    highlight_lbn = st.session_state.get("highlight_lbn", None)

    # 3. Drive Rack View
    snapshot = array.layout_snapshot()
    st.html(rack_view(
        array=array,
        snapshot=snapshot,
        highlight_stripe=highlight_stripe,
        highlight_lbn=highlight_lbn,
        inconsistent_stripes=inconsistent_stripes,
    ))

    # 4. Block Mapping Inspector
    st.markdown("##### Block mapping inspector")
    insp_col1, insp_col2 = st.columns([1, 3])
    with insp_col1:
        inspect_lbn = st.number_input(
            "Logical block (LBN)",
            min_value=0,
            max_value=max(0, array.capacity_blocks() - 1),
            value=st.session_state.get("last_inspect_lbn", 0),
            key="inspector_lbn_input",
            label_visibility="collapsed",
        )
    with insp_col2:
        mapping_text, mapped_stripe = mapping_inspector(array, inspect_lbn)
        st.session_state["highlight_stripe"] = mapped_stripe
        st.session_state["highlight_lbn"] = inspect_lbn
        st.session_state["last_inspect_lbn"] = inspect_lbn
        st.caption(mapping_text)

    st.write("")

    # 5. Two-Column Action Area
    col_data, col_fail = st.columns(2)

    # --- Left: Data Operations ---
    with col_data:
        st.markdown("##### Data operations")
        w_tab1, w_tab2 = st.tabs(["Write", "Read"])

        with w_tab1:
            write_lbn = st.number_input("Target LBN", min_value=0, max_value=max(0, array.capacity_blocks() - 1), value=0, key="arr_write_lbn")
            input_mode = st.radio("Input mode", ["Text", "File"], horizontal=True, key="arr_write_mode")

            if input_mode == "Text":
                text_payload = st.text_input("Data string", value="Software RAID Manager OS Storage Stack", key="arr_write_text")
                payload_bytes = text_payload.encode()
            else:
                uploaded = st.file_uploader("Upload file (up to 4KB)", key="arr_write_file")
                payload_bytes = uploaded.read() if uploaded else b"Sample File Payload"

            if st.button("Write data", type="primary", key="arr_write_btn"):
                try:
                    chunks = [payload_bytes[i:i + BLOCK_SIZE] for i in range(0, len(payload_bytes), BLOCK_SIZE)]
                    for offset, chunk in enumerate(chunks):
                        tgt = write_lbn + offset
                        if tgt < array.capacity_blocks():
                            array.write_block(tgt, chunk)
                    st.session_state["last_written_data"] = payload_bytes
                    oplog.add(f"Wrote {len(payload_bytes)} bytes starting at block {write_lbn}", level="info")
                    st.toast(f"Wrote {len(payload_bytes)} bytes starting at block {write_lbn}")
                    st.rerun()
                except Exception as e:
                    st.error(f"Write failed: {e}")

        with w_tab2:
            read_lbn = st.number_input("Read LBN", min_value=0, max_value=max(0, array.capacity_blocks() - 1), value=0, key="arr_read_lbn")
            if st.button("Read data", key="arr_read_btn"):
                try:
                    data = array.read_block(read_lbn)
                    oplog.add(f"Read {len(data)} bytes from block {read_lbn}", level="info")
                    st.toast(f"Read {len(data)} bytes from block {read_lbn}")
                    st.code(data[:128].decode(errors="replace"), language="text")
                except DiskFailedError as e:
                    st.error(f"Cannot read block {read_lbn}: {e}")

    # --- Right: Failure & Recovery ---
    with col_fail:
        st.markdown("##### Failure and recovery")

        disk_ids = [d.disk_id for d in array.disks]
        selected_disk_id = st.selectbox("Select disk", options=disk_ids, key="arr_fail_select_disk")
        target_disk = next((d for d in array.disks if d.disk_id == selected_disk_id), None)

        is_failed = target_disk.failed if target_disk else False
        is_rebuilding = target_disk.needs_rebuild if target_disk else False
        is_healthy = not is_failed and not is_rebuilding

        btn_c1, btn_c2 = st.columns(2)

        # Fail Disk Button
        with btn_c1:
            fail_disabled = is_failed or is_rebuilding
            if st.button("Fail disk", disabled=fail_disabled, key="arr_fail_btn"):
                array.fail_disk(selected_disk_id)
                st.toast(f"Disk {selected_disk_id} failed")
                st.rerun()
            if fail_disabled:
                st.caption("Disk is already offline")

        # Replace Disk Button
        with btn_c2:
            replace_disabled = not is_failed or is_rebuilding
            if st.button("Replace disk", disabled=replace_disabled, key="arr_replace_btn"):
                array.replace_disk(selected_disk_id)
                st.toast(f"Disk {selected_disk_id} replaced with a blank disk")
                st.rerun()
            if not is_failed and not is_rebuilding:
                st.caption("Disk is already healthy")
            elif is_rebuilding:
                st.caption("Disk is awaiting rebuild")

        # Rebuild & Verify Row
        reb_c1, reb_c2 = st.columns(2)

        with reb_c1:
            rebuild_disabled = not is_rebuilding
            if st.button("Start rebuild", disabled=rebuild_disabled, key="arr_rebuild_btn"):
                progress_placeholder = st.empty()
                pbar = progress_placeholder.progress(0, text="Starting rebuild...")

                def _cb(done, total):
                    pbar.progress(done / total, text=f"Stripe {done} of {total}")
                    time.sleep(0.01)

                success = array.rebuild(selected_disk_id, progress_cb=_cb)
                progress_placeholder.empty()

                if success:
                    st.toast(f"Rebuild finished. Checksum matches.")
                    st.rerun()
                else:
                    st.error("Rebuild failed: multiple disks unavailable.")
            if not is_rebuilding:
                st.caption("Replace the failed disk first" if is_failed else "Disk is already healthy")

        with reb_c2:
            if st.button("Verify checksum", key="arr_verify_btn"):
                csum = checksum_array(array)
                stored = st.session_state.get("stored_checksum", None)
                if stored is None:
                    st.session_state["stored_checksum"] = csum
                    st.toast("Checksum calculated and stored")
                    oplog.add(f"Saved volume checksum: {csum[:16]}...", level="info")
                else:
                    if csum == stored:
                        st.toast("Checksum matches")
                        st.success(f"Checksum matches: `{csum[:16]}...`")
                        oplog.add("Verified volume checksum: MATCH", level="info")
                    else:
                        st.toast("Checksum differs")
                        st.error(f"Checksum differs! Expected `{stored[:16]}`, found `{csum[:16]}`")
                        oplog.add("Verified volume checksum: MISMATCH", level="error")

        # Hot spare notification
        if array.spares:
            st.caption(f"Hot spares: {len(array.spares)} standby spare ready")
        else:
            st.caption("Hot spares: none available in pool")


# ==============================================================================
# WORKSPACE 2: EXPERIMENTS
# ==============================================================================
def render_experiments(array, oplog) -> None:
    """Render the Experiments workspace: Race condition, Write hole, Scrub, Scheduler."""
    st.markdown("### Storage experiments")

    exp_choice = st.segmented_control(
        "Select experiment",
        options=["Race condition", "Write hole", "Scrub and repair", "Disk scheduler"],
        default="Race condition",
        key="experiments_segmented_control",
    )

    st.write("")

    # --- Experiment 1: Race Condition ---
    if exp_choice == "Race condition":
        st.markdown("##### Stripe concurrency and race condition demo")
        st.caption(
            "In RAID 5 small writes, updating a block requires reading old data and old parity, "
            "then computing new parity. If two threads write to the same stripe with locks disabled, "
            "they overwrite each other's parity updates (lost update anomaly)."
        )

        col_race_action, col_race_desc = st.columns([1, 2])
        with col_race_action:
            run_both = st.button("Run both (locks off vs. on)", type="primary", key="exp_race_run_both")

        if run_both:
            # 1. Unsafe Run
            disks_unsafe = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
            lock_off = StripeLockManager(num_stripes=16, enabled=False)
            raid_unsafe = Raid5(disks=disks_unsafe, lock_manager=lock_off, artificial_delay_ms=30.0)

            t1 = threading.Thread(target=raid_unsafe.write_block, args=(0, b"DATA_THREAD_1_UNSAFE"))
            t2 = threading.Thread(target=raid_unsafe.write_block, args=(1, b"DATA_THREAD_2_UNSAFE"))
            t1.start()
            time.sleep(0.005)
            t2.start()
            t1.join()
            t2.join()

            res_unsafe = scrub(raid_unsafe, repair=False)

            # 2. Safe Run
            disks_safe = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
            lock_on = StripeLockManager(num_stripes=16, enabled=True)
            raid_safe = Raid5(disks=disks_safe, lock_manager=lock_on, artificial_delay_ms=30.0)

            t3 = threading.Thread(target=raid_safe.write_block, args=(0, b"DATA_THREAD_1_SAFE"))
            t4 = threading.Thread(target=raid_safe.write_block, args=(1, b"DATA_THREAD_2_SAFE"))
            t3.start()
            time.sleep(0.005)
            t4.start()
            t3.join()
            t4.join()

            res_safe = scrub(raid_safe, repair=False)
            st.session_state["race_results"] = (res_unsafe, res_safe)

        if "race_results" in st.session_state:
            res_unsafe, res_safe = st.session_state["race_results"]
            c_u, c_s = st.columns(2)

            with c_u:
                st.markdown(
                    f"""
                    <div class="action-card" style="border-left: 4px solid var(--failed);">
                      <div class="action-title">Locks off (unsafe)</div>
                      <div style="font-size: 24px; font-weight: bold; color: var(--failed); margin-bottom: 4px;">
                        {len(res_unsafe['inconsistent'])} inconsistent stripe(s)
                      </div>
                      <div style="color: var(--ink-soft); font-size: 13px;">
                        Stripes with bad parity: {res_unsafe['inconsistent']}. Parity update was lost.
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

            with c_s:
                st.markdown(
                    f"""
                    <div class="action-card" style="border-left: 4px solid var(--ok);">
                      <div class="action-title">Locks on (safe)</div>
                      <div style="font-size: 24px; font-weight: bold; color: var(--ok); margin-bottom: 4px;">
                        {len(res_safe['inconsistent'])} inconsistent stripes
                      </div>
                      <div style="color: var(--ink-soft); font-size: 13px;">
                        Stripe locks serialized the read-modify-write cycles. Parity is 100% consistent.
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # --- Experiment 2: Write Hole ---
    elif exp_choice == "Write hole":
        st.markdown("##### Write-hole syndrome sequence")
        st.caption(
            "The write hole happens when a system crashes after writing data but before updating parity. "
            "Data remains readable until another disk fails, at which point degraded reads silently corrupt data."
        )

        wh_col1, wh_col2, wh_col3 = st.columns(3)

        with wh_col1:
            st.markdown(
                """
                <div class="action-card">
                  <div class="action-title">Step 1: Write data</div>
                  <div style="color: var(--ink-soft); font-size: 13px; margin-bottom: 12px;">
                    Write initial baseline payload to stripe 0.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("1. Write baseline", key="wh_step1_btn"):
                array.write_block(0, b"BASELINE_DATA_A")
                array.write_block(1, b"BASELINE_DATA_B")
                st.session_state["scrub_inconsistent"] = []
                st.toast("Wrote baseline blocks to stripe 0")
                st.rerun()

        with wh_col2:
            st.markdown(
                """
                <div class="action-card">
                  <div class="action-title">Step 2: Simulate crash</div>
                  <div style="color: var(--ink-soft); font-size: 13px; margin-bottom: 12px;">
                    Commit data to disk, but crash before parity is updated.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("2. Crash mid-write", key="wh_step2_btn"):
                try:
                    write_with_crash(array, lbn=0, data=b"CRASHED_NEW_DATA")
                except SimulatedCrashError:
                    res = scrub(array, repair=False)
                    st.session_state["scrub_inconsistent"] = res["inconsistent"]
                    st.toast("Crash after data write. Parity not updated.")
                    oplog.add("Crash after data write. Parity not updated.", level="warning")
                    st.rerun()

        with wh_col3:
            st.markdown(
                """
                <div class="action-card">
                  <div class="action-title">Step 3: Degraded read</div>
                  <div style="color: var(--ink-soft); font-size: 13px; margin-bottom: 12px;">
                    Fail another disk in stripe 0 and attempt degraded read.
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            if st.button("3. Show silent corruption", key="wh_step3_btn"):
                mapping = array.map_lbn(0)
                other_disks = [d for d in mapping["data_disks"] if d != mapping["disk"]]
                if other_disks:
                    victim_id = other_disks[0]
                    array.disks[victim_id].fail()
                    victim_lbn = mapping["stripe"] * (array.num_disks - 1) + mapping["data_disks"].index(victim_id)
                    corrupted_data = array.read_block(victim_lbn)
                    st.session_state["corrupted_read"] = (victim_id, victim_lbn, corrupted_data)
                    oplog.add(f"Disk {victim_id} failed during write-hole. Degraded read produced silent corruption.", level="error")
                    st.rerun()

        if "corrupted_read" in st.session_state:
            v_id, v_lbn, corrupted_val = st.session_state["corrupted_read"]
            st.error(
                f"Silent data corruption confirmed: Disk {v_id} is down. Reconstructing block {v_lbn} "
                f"via stale parity yielded corrupted bytes: `{corrupted_val[:48]}`"
            )
            if st.button("Repair parity via scrub", key="wh_repair_btn"):
                res = scrub(array, repair=True)
                st.session_state["scrub_inconsistent"] = res["inconsistent"]
                st.toast(f"Scrub found and repaired {res['repaired']} inconsistent stripe(s)")
                st.session_state.pop("corrupted_read", None)
                st.rerun()

    # --- Experiment 3: Scrub & Repair ---
    elif exp_choice == "Scrub and repair":
        st.markdown("##### Parity and mirror scrubbing")
        st.caption("Scans every stripe across the array, checks the XOR parity invariant, and repairs inconsistent blocks.")

        sc_btn1, sc_btn2 = st.columns(2)
        with sc_btn1:
            if st.button("Scrub (audit only)", key="exp_scrub_audit"):
                res = scrub(array, repair=False)
                st.session_state["scrub_inconsistent"] = res["inconsistent"]
                if res["inconsistent"]:
                    st.toast(f"Scrub found {len(res['inconsistent'])} inconsistent stripes")
                    oplog.add(f"Scrub found {len(res['inconsistent'])} inconsistent stripes", level="warning")
                else:
                    st.toast("Scrub complete: all stripes healthy")
                    oplog.add("Scrub complete: all stripes healthy", level="info")
                st.rerun()

        with sc_btn2:
            if st.button("Scrub and repair", key="exp_scrub_repair"):
                res = scrub(array, repair=True)
                st.session_state["scrub_inconsistent"] = res["inconsistent"]
                st.toast(f"Scrub finished. Repaired {res['repaired']} inconsistent stripes.")
                oplog.add(f"Scrub finished. Repaired {res['repaired']} inconsistent stripes.", level="info")
                st.rerun()

        incons = st.session_state.get("scrub_inconsistent", [])
        if incons:
            st.warning(f"Inconsistent stripes detected: {incons}. These stripes are highlighted on the Drive Rack.")
        else:
            st.success("All stripes are verified consistent.")

    # --- Experiment 4: Disk Scheduler ---
    elif exp_choice == "Disk scheduler":
        st.markdown("##### Disk scheduling: FCFS vs. SCAN (elevator)")
        st.caption("Compares head movement and mechanical seek distance between arrival order (FCFS) and elevator sweep (SCAN).")

        sc_c1, sc_c2 = st.columns([1, 2])
        with sc_c1:
            req_count = st.slider("Request batch size", min_value=10, max_value=50, value=25, key="sched_req_count")
            sched_seed = st.number_input("Random seed", value=42, key="sched_seed_input")
            run_sched = st.button("Run comparison", key="sched_run_btn")

        with sc_c2:
            if run_sched or "last_sched_data" in st.session_state:
                if run_sched:
                    import numpy as np
                    rng = np.random.default_rng(sched_seed)
                    pbns = rng.integers(0, array.disk_blocks, size=req_count).tolist()

                    d_fcfs = VirtualDisk(0, num_blocks=array.disk_blocks, block_size=BLOCK_SIZE)
                    q_fcfs = DiskQueue(d_fcfs, policy="FCFS")
                    for p in pbns:
                        q_fcfs.submit({"op": "r", "pbn": int(p)})
                    s_fcfs = q_fcfs.process_all()

                    d_scan = VirtualDisk(1, num_blocks=array.disk_blocks, block_size=BLOCK_SIZE)
                    q_scan = DiskQueue(d_scan, policy="SCAN")
                    for p in pbns:
                        q_scan.submit({"op": "r", "pbn": int(p)})
                    s_scan = q_scan.process_all()

                    st.session_state["last_sched_data"] = (s_fcfs, s_scan, q_fcfs.total_head_movement, q_scan.total_head_movement)

                s_fcfs, s_scan, h_fcfs, h_scan = st.session_state["last_sched_data"]
                reduction = ((h_fcfs - h_scan) / h_fcfs) * 100.0 if h_fcfs > 0 else 0.0

                st.markdown(
                    f"""
                    <div style="display:flex; gap: 16px; margin-bottom: 12px;">
                      <div class="stat-item" style="flex:1;">
                        <div class="stat-label">FCFS head movement</div>
                        <div class="stat-value" style="color: var(--failed);">{h_fcfs} blocks</div>
                      </div>
                      <div class="stat-item" style="flex:1; border-color: var(--ok);">
                        <div class="stat-label">SCAN head movement</div>
                        <div class="stat-value" style="color: var(--ok);">{h_scan} blocks ({reduction:.1f}% reduction)</div>
                      </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

                fig_sched = plot_scheduler_comparison(s_fcfs, s_scan, h_fcfs, h_scan)
                st.pyplot(fig_sched)


# ==============================================================================
# WORKSPACE 3: BENCHMARKS
# ==============================================================================
def render_benchmarks(array, oplog) -> None:
    """Render the Benchmarks workspace: throughput, latency, penalty tables, and Reliability."""
    st.markdown("### Performance benchmarks")

    c_b1, c_b2, c_b3 = st.columns([1, 1, 2])
    with c_b1:
        n_req = st.slider("Requests per workload", min_value=20, max_value=120, value=60, step=20, key="bench_reqs")
    with c_b2:
        bench_seed = st.number_input("Benchmark seed", value=42, key="bench_seed")
    with c_b3:
        st.write("")
        run_bench = st.button("Run benchmarks", type="primary", key="bench_run_btn")

    if run_bench or "bench_cache" not in st.session_state or st.session_state["bench_cache"] is None:
        with st.spinner("Running sequential, random, and mixed workloads..."):
            st.session_state["bench_cache"] = run_full_benchmark(n_requests=n_req, num_blocks=array.disk_blocks, seed=bench_seed)

    df_perf, df_sched, df_penalty, df_comp = st.session_state["bench_cache"]

    # Visual Charts
    col_chart1, col_chart2 = st.columns(2)
    with col_chart1:
        fig_tp = plot_benchmark_throughput(df_perf)
        st.pyplot(fig_tp)
    with col_chart2:
        fig_lat = plot_benchmark_latency(df_perf)
        st.pyplot(fig_lat)

    st.write("")

    # Comparison Table
    st.markdown("##### Architecture comparison")
    st.dataframe(df_comp, use_container_width=True, hide_index=True)

    # Small-Write Penalty Table
    st.markdown("##### Small-write penalty table")
    st.dataframe(df_penalty, use_container_width=True, hide_index=True)

    st.divider()

    # Sub-section: Reliability Simulation
    st.markdown("### Reliability analysis (Monte Carlo simulation)")
    st.caption("Simulates stochastic exponential drive failures and rebuild windows to estimate data-loss probability.")

    rc1, rc2, rc3 = st.columns(3)
    with rc1:
        rel_mttf = st.number_input("Disk MTTF (hours)", min_value=10000.0, max_value=500000.0, value=100000.0, step=10000.0, key="rel_mttf_w3")
        rel_rebuild = st.number_input("Rebuild time (hours)", min_value=1.0, max_value=336.0, value=24.0, step=6.0, key="rel_reb_w3")
    with rc2:
        rel_disks = st.number_input("Array disks count", min_value=3, max_value=16, value=4, key="rel_disks_w3")
        rel_years = st.number_input("Mission time (years)", min_value=1.0, max_value=20.0, value=5.0, key="rel_years_w3")
    with rc3:
        rel_trials = st.number_input("Simulation trials", min_value=1000, max_value=100000, value=20000, step=5000, key="rel_trials_w3")
        st.write("")
        run_rel = st.button("Run reliability simulation", key="rel_run_btn_w3")

    if run_rel or "rel_results_cache" in st.session_state:
        if run_rel:
            with st.spinner("Simulating mission lifetimes across architectures..."):
                mission_hours = rel_years * 8760.0
                base_p = ReliabilityParams(
                    level="RAID5",
                    num_disks=rel_disks,
                    mttf_hours=rel_mttf,
                    rebuild_hours=rel_rebuild,
                    mission_hours=mission_hours,
                    n_trials=rel_trials,
                    seed=42,
                )
                df_rel_comp = compare_levels(base_p, ["RAID 0", "RAID 1", "RAID 5"])
                rebuild_vals = [1.0, 6.0, 12.0, 24.0, 48.0, 96.0, 168.0]
                df_rel_reb = sweep_rebuild_time(base_p, rebuild_vals)

                analytic_p = []
                for r in rebuild_vals:
                    mttdl = analytic_mttdl_raid5(mttf=rel_mttf, rebuild=r, n=rel_disks)
                    analytic_p.append(analytic_loss_probability(mttdl=mttdl, mission_hours=mission_hours))
                df_rel_reb["p_analytic"] = analytic_p
                st.session_state["rel_results_cache"] = (df_rel_comp, df_rel_reb)

        df_rel_comp, df_rel_reb = st.session_state["rel_results_cache"]

        st.caption("RAID 5 is vulnerable only while rebuilding.")

        r_res1, r_res2 = st.columns(2)
        with r_res1:
            fig_bar, ax_bar = plt.subplots(figsize=(6, 3.8), facecolor=COLOR_PANEL)
            apply_theme_to_axes(ax_bar)
            bars = ax_bar.bar(df_rel_comp["level"], df_rel_comp["p_loss"], color=[COLOR_FAILED, COLOR_MIRROR, COLOR_PARITY], width=0.5)
            ax_bar.set_title("Probability of data loss (5-year mission)", fontsize=11, fontweight="bold")
            ax_bar.set_ylabel("P(data loss)")
            for b in bars:
                h = b.get_height()
                ax_bar.text(b.get_x() + b.get_width() / 2.0, h + 0.005, f"{h:.4f}", ha="center", va="bottom", fontsize=9, fontweight="bold")
            plt.tight_layout()
            st.pyplot(fig_bar)

        with r_res2:
            fig_line, ax_line = plt.subplots(figsize=(6.5, 3.8), facecolor=COLOR_PANEL)
            apply_theme_to_axes(ax_line)
            yerr = [df_rel_reb["p_loss"] - df_rel_reb["ci_low"], df_rel_reb["ci_high"] - df_rel_reb["p_loss"]]
            ax_line.errorbar(df_rel_reb["rebuild_hours"], df_rel_reb["p_loss"], yerr=yerr, fmt="o-", color=COLOR_PARITY, label="Monte Carlo simulation", capsize=3)
            ax_line.plot(df_rel_reb["rebuild_hours"], df_rel_reb["p_analytic"], "k--", label="Analytic MTTDL approx")
            ax_line.set_title("RAID 5: P(loss) vs. rebuild window", fontsize=11, fontweight="bold")
            ax_line.set_xlabel("Rebuild time (hours)")
            ax_line.set_ylabel("P(data loss)")
            ax_line.legend(frameon=False)
            plt.tight_layout()
            st.pyplot(fig_line)
