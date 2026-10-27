"""RAID 1 (Mirroring) implementation."""

import threading
from raid.raid_base import RaidArray
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.oplog import OperationLog


class Raid1(RaidArray):
    """RAID 1: Mirroring across N >= 2 disks with full redundancy."""

    def __init__(
        self,
        disks: list[VirtualDisk],
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager=None,
        scheduler_policy: str = "FCFS",
    ):
        if len(disks) < 2:
            raise ValueError("RAID 1 requires at least 2 disks")
        super().__init__(disks, spares, oplog, lock_manager, scheduler_policy)
        self._read_rr_idx = 0
        self._rr_lock = threading.Lock()

    def capacity_blocks(self) -> int:
        """Usable capacity equals the capacity of a single disk."""
        return self.disk_blocks

    def map_lbn(self, lbn: int) -> dict:
        """Map logical block number (LBN) to physical block number across all mirrors."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")
        return {
            "lbn": lbn,
            "pbn": lbn,
            "disks": [d.disk_id for d in self.disks],
            "role": "mirror",
            "stripe": lbn,
        }

    def read_block(self, lbn: int) -> bytes:
        """Read logical block from any healthy mirror disk using round-robin."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")

        healthy_disks = [d for d in self.disks if not d.failed and not d.needs_rebuild]
        if not healthy_disks:
            self.oplog.add(f"RAID1 Read failed for LBN {lbn}: all mirror disks failed", level="error")
            raise DiskFailedError("All mirror disks failed")

        with self._rr_lock:
            chosen_disk = healthy_disks[self._read_rr_idx % len(healthy_disks)]
            self._read_rr_idx += 1

        data = chosen_disk.read(lbn)
        self.oplog.add(f"RAID1 Read LBN {lbn} from mirror Disk {chosen_disk.disk_id}", level="info")
        return data

    def write_block(self, lbn: int, data: bytes) -> None:
        """Write logical block to all healthy mirror disks."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")

        healthy_disks = [d for d in self.disks if not d.failed]
        if not healthy_disks:
            self.oplog.add(f"RAID1 Write failed for LBN {lbn}: all mirror disks failed", level="error")
            raise DiskFailedError("All mirror disks failed")

        for disk in healthy_disks:
            disk.write(lbn, data)

        self.oplog.add(
            f"RAID1 Write LBN {lbn} mirrored to {len(healthy_disks)} healthy disk(s)",
            level="info",
        )

    def rebuild(self, disk_id: int, progress_cb=None, rate_limit_delay_ms: float = 0.0) -> bool:
        """Rebuild replaced disk by copying all blocks from any healthy mirror disk."""
        import time

        target_disk = None
        for d in self.disks:
            if d.disk_id == disk_id:
                target_disk = d
                break

        if target_disk is None:
            self.oplog.add(f"RAID1 Rebuild failed: Disk {disk_id} not found", level="error")
            return False

        healthy_sources = [d for d in self.disks if not d.failed and not d.needs_rebuild and d.disk_id != disk_id]
        if not healthy_sources:
            self.oplog.add(f"RAID1 Rebuild failed: no healthy mirror source available", level="error")
            return False

        source_disk = healthy_sources[0]
        self.oplog.add(f"RAID1 starting rebuild of Disk {disk_id} from source Disk {source_disk.disk_id}", level="info")

        for pbn in range(self.disk_blocks):
            if rate_limit_delay_ms > 0:
                time.sleep(rate_limit_delay_ms / 1000.0)
            block_data = source_disk.read(pbn)
            target_disk.write(pbn, block_data)
            if progress_cb:
                progress_cb(pbn + 1, self.disk_blocks)

        target_disk.needs_rebuild = False
        self.oplog.add(f"RAID1 Rebuild of Disk {disk_id} complete.", level="info")
        return True

    def is_degraded(self) -> bool:
        """True if any mirror disk is failed or needs rebuild."""
        return any(d.failed or d.needs_rebuild for d in self.disks)

    def io_count_for_write(self) -> dict:
        """RAID 1 write requires 0 reads and N writes (1 write per mirror disk)."""
        return {"reads": 0, "writes": self.num_disks}

    def layout_snapshot(self) -> list[list[dict]]:
        """Return 2D grid [disk_idx][pbn] with block metadata."""
        grid = []
        for d_idx, disk in enumerate(self.disks):
            disk_blocks = []
            for pbn in range(self.disk_blocks):
                if disk.failed:
                    state = "failed"
                elif disk.needs_rebuild:
                    state = "rebuilt"
                else:
                    state = "ok"

                disk_blocks.append({
                    "kind": "data" if d_idx == 0 else "mirror",
                    "state": state,
                    "lbn": pbn,
                    "stripe": pbn,
                })
            grid.append(disk_blocks)
        return grid
