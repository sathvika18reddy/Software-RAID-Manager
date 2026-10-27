"""RAID 10 (Striping across Mirrored Pairs) implementation."""

import time
import threading
from raid.raid_base import RaidArray
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.oplog import OperationLog


class Raid10(RaidArray):
    """RAID 10: Block-level striping across N >= 4 mirrored disk pairs (RAID 1+0)."""

    def __init__(
        self,
        disks: list[VirtualDisk],
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager=None,
        scheduler_policy: str = "FCFS",
        chunk_size: int = 1,
    ):
        if len(disks) < 4 or len(disks) % 2 != 0:
            raise ValueError(f"RAID 10 requires an even number of at least 4 disks (got {len(disks)})")
        if chunk_size < 1:
            raise ValueError("chunk_size must be >= 1")
        super().__init__(disks, spares, oplog, lock_manager, scheduler_policy)
        self.chunk_size = chunk_size
        self.num_pairs = self.num_disks // 2
        self._pair_rr = [0] * self.num_pairs
        self._rr_lock = threading.Lock()

    def capacity_blocks(self) -> int:
        """Usable capacity is (N / 2) * disk_blocks."""
        return self.num_pairs * self.disk_blocks

    def map_lbn(self, lbn: int) -> dict:
        """Map logical block number (LBN) to mirrored pair, primary/mirror disks, and PBN."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")

        stripe_data_blocks = self.num_pairs * self.chunk_size
        stripe = lbn // stripe_data_blocks
        offset = lbn % stripe_data_blocks
        pair_idx = offset // self.chunk_size
        chunk_offset = offset % self.chunk_size
        pbn = stripe * self.chunk_size + chunk_offset

        primary_disk_idx = 2 * pair_idx
        mirror_disk_idx = 2 * pair_idx + 1

        return {
            "lbn": lbn,
            "stripe": stripe,
            "pbn": pbn,
            "pair": pair_idx,
            "primary_disk": primary_disk_idx,
            "mirror_disk": mirror_disk_idx,
            "disks": [primary_disk_idx, mirror_disk_idx],
            "role": "data",
        }

    def read_block(self, lbn: int) -> bytes:
        """Read logical block from any healthy disk in the target mirrored pair."""
        mapping = self.map_lbn(lbn)
        pair_idx = mapping["pair"]
        pair_disks = [self.disks[i] for i in mapping["disks"]]
        healthy = [d for d in pair_disks if not d.failed and not d.needs_rebuild]

        if not healthy:
            self.oplog.add(f"RAID10 Read failed for LBN {lbn}: both disks in pair {pair_idx} failed", level="error")
            raise DiskFailedError(f"RAID10 data loss: mirrored pair {pair_idx} completely failed")

        with self._rr_lock:
            chosen_disk = healthy[self._pair_rr[pair_idx] % len(healthy)]
            self._pair_rr[pair_idx] += 1

        data = chosen_disk.read(mapping["pbn"])
        self.oplog.add(f"RAID10 Read LBN {lbn} from Disk {chosen_disk.disk_id} (pair {pair_idx} PBN {mapping['pbn']})", level="info")
        return data

    def write_block(self, lbn: int, data: bytes) -> None:
        """Write logical block to all healthy mirror disks in the target pair."""
        mapping = self.map_lbn(lbn)
        pair_idx = mapping["pair"]
        pair_disks = [self.disks[i] for i in mapping["disks"]]
        healthy = [d for d in pair_disks if not d.failed]

        if not healthy:
            self.oplog.add(f"RAID10 Write failed for LBN {lbn}: both disks in pair {pair_idx} failed", level="error")
            raise DiskFailedError(f"RAID10 data loss: mirrored pair {pair_idx} completely failed")

        for disk in healthy:
            disk.write(mapping["pbn"], data)

        self.oplog.add(
            f"RAID10 Write LBN {lbn} mirrored to {len(healthy)} disk(s) in pair {pair_idx} PBN {mapping['pbn']}",
            level="info",
        )

    def rebuild(self, disk_id: int, progress_cb=None, rate_limit_delay_ms: float = 0.0) -> bool:
        """Rebuild target disk by copying from its mirrored partner."""
        target_idx = None
        for i, d in enumerate(self.disks):
            if d.disk_id == disk_id:
                target_idx = i
                break

        if target_idx is None:
            self.oplog.add(f"RAID10 Rebuild failed: Disk {disk_id} not found", level="error")
            return False

        target_disk = self.disks[target_idx]
        partner_idx = target_idx ^ 1  # 0->1, 1->0, 2->3, 3->2
        partner_disk = self.disks[partner_idx]

        if partner_disk.failed or partner_disk.needs_rebuild:
            self.oplog.add(f"RAID10 Rebuild failed: partner Disk {partner_disk.disk_id} is unavailable", level="error")
            return False

        self.oplog.add(
            f"RAID10 Rebuilding Disk {disk_id} from mirror partner Disk {partner_disk.disk_id}...",
            level="info",
        )

        for pbn in range(self.disk_blocks):
            if rate_limit_delay_ms > 0:
                time.sleep(rate_limit_delay_ms / 1000.0)
            block_data = partner_disk.read(pbn)
            target_disk.write(pbn, block_data)
            if progress_cb:
                progress_cb(pbn + 1, self.disk_blocks)

        target_disk.needs_rebuild = False
        self.oplog.add(f"RAID10 Rebuild for Disk {disk_id} completed successfully.", level="info")
        return True

    def is_degraded(self) -> bool:
        """True if any member disk is failed or needs rebuild."""
        return any(d.failed or d.needs_rebuild for d in self.disks)

    def io_count_for_write(self) -> dict:
        """RAID 10 write: 0 reads, 2 writes (mirrored write)."""
        return {"reads": 0, "writes": 2}

    def layout_snapshot(self) -> list[list[dict]]:
        """Return 2D grid [disk_idx][pbn] with block metadata."""
        grid = []
        for d_idx, disk in enumerate(self.disks):
            pair_idx = d_idx // 2
            is_mirror = (d_idx % 2 == 1)
            kind = "mirror" if is_mirror else "data"
            disk_blocks = []

            for pbn in range(self.disk_blocks):
                stripe = pbn // self.chunk_size
                chunk_offset = pbn % self.chunk_size
                lbn = (stripe * self.num_pairs + pair_idx) * self.chunk_size + chunk_offset

                if disk.failed:
                    state = "failed"
                elif disk.needs_rebuild:
                    state = "rebuilt"
                else:
                    state = "ok"

                disk_blocks.append({
                    "kind": kind,
                    "state": state,
                    "lbn": lbn,
                    "stripe": stripe,
                    "pbn": pbn,
                    "pair": pair_idx,
                })
            grid.append(disk_blocks)
        return grid
