"""UI Components for Software RAID Manager: The Drive Rack."""

import html
from raid.virtual_disk import VirtualDisk
from raid.config import BLOCK_SIZE


def status_banner(array, inconsistent_stripes: list[int] | None = None) -> str:
    """Generate the single-line status banner with LED indicator and plain-language message."""
    inconsistent_stripes = inconsistent_stripes or []
    failed_disks = [d for d in array.disks if d.failed]
    rebuilding_disks = [d for d in array.disks if d.needs_rebuild]

    # Check for catastrophic data loss
    is_data_lost = False
    loss_reason = ""
    level_name = array.__class__.__name__.upper()

    if "RAID0" in level_name and len(failed_disks) > 0:
        is_data_lost = True
        loss_reason = f"Data lost on Disk {failed_disks[0].disk_id}. RAID 0 has no redundancy and cannot recover it."
    elif "RAID5" in level_name and len(failed_disks) + len(rebuilding_disks) > 1:
        is_data_lost = True
        loss_reason = f"Multiple disks unavailable in RAID 5. Parity cannot reconstruct data."
    elif "RAID1" in level_name and len(failed_disks) == len(array.disks):
        is_data_lost = True
        loss_reason = "All mirror disks failed. Volume is inaccessible."

    if is_data_lost:
        led_cls = "failed"
        text = loss_reason
    elif inconsistent_stripes:
        led_cls = "inconsistent"
        text = f"{len(inconsistent_stripes)} stripe(s) have inconsistent parity. Run a scrub to repair."
    elif rebuilding_disks:
        led_cls = "rebuilding"
        reb_id = rebuilding_disks[0].disk_id
        text = f"Rebuilding Disk {reb_id}. Reconstruction in progress."
    elif failed_disks:
        led_cls = "degraded"
        f_id = failed_disks[0].disk_id
        text = f"Disk {f_id} failed. Data is readable in degraded mode but not fully protected."
    else:
        led_cls = "ok"
        text = "All disks healthy. Data is fully protected."

    return f"""
    <div class="status-banner" role="status" aria-live="polite">
      <div class="status-led {led_cls}"></div>
      <div>{html.escape(text)}</div>
    </div>
    """


def stat_row(array) -> str:
    """Render key array health and architecture metrics."""
    healthy_count = sum(1 for d in array.disks if not d.failed and not d.needs_rebuild)
    total_disks = len(array.disks)
    cap_blocks = array.capacity_blocks()
    cap_kb = (cap_blocks * BLOCK_SIZE) / 1024.0
    io_c = array.io_count_for_write()
    eff = (cap_blocks / (total_disks * array.disk_blocks)) * 100.0 if total_disks > 0 else 0.0

    return f"""
    <div class="stat-row-container">
      <div class="stat-item">
        <div class="stat-label">Usable capacity</div>
        <div class="stat-value">{cap_blocks} blks ({cap_kb:.1f} KB)</div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Disk health</div>
        <div class="stat-value">{healthy_count} of {total_disks} online</div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Write I/O cost</div>
        <div class="stat-value">{io_c['reads']}R + {io_c['writes']}W</div>
      </div>
      <div class="stat-item">
        <div class="stat-label">Storage efficiency</div>
        <div class="stat-value">{eff:.1f}%</div>
      </div>
    </div>
    """


def rack_view(
    array,
    snapshot: list[list[dict]],
    highlight_stripe: int | None = None,
    highlight_lbn: int | None = None,
    inconsistent_stripes: list[int] | None = None,
) -> str:
    """Render physical drive rack bays with hardware-like block columns."""
    inconsistent_stripes = set(inconsistent_stripes or [])
    level_name = array.__class__.__name__.upper()

    bays_html = []

    # Active member disks
    for d_idx, disk in enumerate(array.disks):
        if disk.failed:
            led_class = "failed"
            status_desc = "Failed"
        elif disk.needs_rebuild:
            led_class = "rebuilding"
            status_desc = "Rebuilding"
        else:
            led_class = "ok"
            status_desc = "Healthy"

        if "RAID5" in level_name:
            role_desc = "data and parity"
        elif "RAID1" in level_name:
            role_desc = "primary data" if d_idx == 0 else "mirror copy"
        else:
            role_desc = "striped data"

        disk_blocks = snapshot[d_idx]
        blocks_html = []

        for b in disk_blocks:
            kind = b.get("kind", "data")
            state = b.get("state", "ok")
            lbn = b.get("lbn")
            pbn = b.get("pbn", 0)
            stripe = b.get("stripe", 0)

            # Detect inconsistent stripe
            is_inconsistent = stripe in inconsistent_stripes and not disk.failed and not disk.needs_rebuild
            is_highlighted = (highlight_stripe is not None and stripe == highlight_stripe) or \
                             (highlight_lbn is not None and lbn == highlight_lbn)

            classes = ["block-cell"]
            if is_inconsistent:
                classes.append("inconsistent")
            elif disk.failed:
                classes.append("failed")
            elif disk.needs_rebuild:
                classes.append("rebuilt")
            elif kind == "parity":
                classes.append("parity")
            elif kind == "mirror":
                classes.append("mirror")
            elif kind == "data":
                classes.append("data")
            else:
                classes.append("empty")

            if is_highlighted:
                classes.append("highlighted")

            # Glyph / label
            if disk.failed:
                label = ""
            elif disk.needs_rebuild:
                label = "✓"
            elif kind == "parity":
                label = "P"
            elif lbn is not None:
                label = str(lbn)
            else:
                label = ""

            tooltip = f"Disk {disk.disk_id} · block {pbn} · stripe {stripe} · {kind}"
            if lbn is not None:
                tooltip += f" · LBN {lbn}"
            if is_inconsistent:
                tooltip += " · INCONSISTENT"

            blocks_html.append(
                f'<div class="{" ".join(classes)}" title="{html.escape(tooltip)}" aria-label="{html.escape(tooltip)}">{label}</div>'
            )

        bay_html = f"""
        <div class="drive-bay" aria-label="Disk {disk.disk_id}, status {status_desc}">
          <div class="bay-header">
            <div class="bay-title-row">
              <span class="status-led {led_class}"></span>
              <span>Disk {disk.disk_id}</span>
            </div>
            <div class="bay-role">{html.escape(role_desc)}</div>
          </div>
          <div class="bay-blocks-col">
            {"".join(blocks_html)}
          </div>
        </div>
        """
        bays_html.append(bay_html)

    # Hot spares (dimmer presentation)
    for sp in array.spares:
        sp_blocks = []
        for pbn in range(array.disk_blocks):
            sp_blocks.append('<div class="block-cell empty" title="Spare block"></div>')

        spare_bay = f"""
        <div class="drive-bay spare" aria-label="Hot Spare {sp.disk_id}, status ready">
          <div class="bay-header">
            <div class="bay-title-row">
              <span class="status-led ok" style="opacity:0.6;"></span>
              <span>Spare {sp.disk_id}</span>
            </div>
            <div class="bay-role">standby</div>
          </div>
          <div class="bay-blocks-col">
            {"".join(sp_blocks)}
          </div>
        </div>
        """
        bays_html.append(spare_bay)

    return f"""
    <div class="rack-enclosure">
      <div class="rack-bays-container">
        {"".join(bays_html)}
      </div>
      {legend()}
    </div>
    """


def legend() -> str:
    """Render compact swatch and pattern legend row."""
    return """
    <div class="rack-legend-row">
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--data);"></div>
        <span>Data</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--parity); display:flex; align-items:center; justify-content:center; color:#fff; font-size:10px; font-weight:bold;">P</div>
        <span>Parity</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--mirror);"></div>
        <span>Mirror</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background: repeating-linear-gradient(45deg, #D64545, #D64545 3px, #B53838 3px, #B53838 6px);"></div>
        <span>Failed (hatched)</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--ok); display:flex; align-items:center; justify-content:center; color:#fff; font-size:11px;">✓</div>
        <span>Rebuilt</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--inconsistent); outline: 2px dotted #4A1D80; outline-offset:-2px;"></div>
        <span>Inconsistent (dotted)</span>
      </div>
      <div class="legend-item">
        <div class="legend-swatch" style="background-color: var(--empty);"></div>
        <span>Empty</span>
      </div>
    </div>
    """


def activity_log(entries: list[dict]) -> str:
    """Render persistent, scrollable activity log with colored status dots."""
    if not entries:
        return """
        <div class="activity-log-card">
          <div class="activity-log-header">
            <span class="activity-log-title">Activity log</span>
          </div>
          <div style="color: var(--ink-soft); font-size: 13px; padding-top: 10px;">No events recorded yet.</div>
        </div>
        """

    items_html = []
    # Newest entry on top, max 50
    for e in reversed(entries[-50:]):
        lvl = e.get("level", "info")
        msg = e.get("message", "")
        time_str = e.get("time", "")

        # Map level to dot class
        if "rebuild complete" in msg.lower() or "matches" in msg.lower() or "success" in msg.lower():
            dot_cls = "success"
        elif lvl == "error" or "failed" in msg.lower():
            dot_cls = "error"
        elif "inconsistent" in msg.lower() or "crash" in msg.lower():
            dot_cls = "inconsistent"
        elif lvl == "warning":
            dot_cls = "warning"
        else:
            dot_cls = "info"

        items_html.append(f"""
        <div class="log-item">
          <div class="log-dot {dot_cls}"></div>
          <div>
            <div class="log-time">{html.escape(time_str)}</div>
            <div class="log-msg">{html.escape(msg)}</div>
          </div>
        </div>
        """)

    return f"""
    <div class="activity-log-card">
      <div class="activity-log-header">
        <span class="activity-log-title">Activity log</span>
        <span style="font-size: 11px; color: var(--ink-soft);">{len(entries)} events</span>
      </div>
      <div class="activity-log-list">
        {"".join(items_html)}
      </div>
    </div>
    """


def mapping_inspector(array, lbn: int) -> tuple[str, int]:
    """Generate plain-language mapping explanation and return (text, stripe_number)."""
    mapping = array.map_lbn(lbn)
    stripe = mapping["stripe"]
    level_name = array.__class__.__name__.upper()

    if "RAID5" in level_name:
        disk_id = array.disks[mapping["disk"]].disk_id
        parity_id = array.disks[mapping["parity_disk"]].disk_id
        pbn = mapping["pbn"]
        text = f"Logical block {lbn} lives on Disk {disk_id}, block {pbn}. Parity for this stripe is on Disk {parity_id}."
    elif "RAID1" in level_name:
        pbn = mapping["pbn"]
        text = f"Logical block {lbn} is mirrored across all disks at physical block {pbn}."
    else:  # RAID0
        disk_id = array.disks[mapping["disk"]].disk_id
        pbn = mapping["pbn"]
        text = f"Logical block {lbn} is striped onto Disk {disk_id}, physical block {pbn}. No parity exists."

    return text, stripe
