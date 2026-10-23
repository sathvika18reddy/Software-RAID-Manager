"""RAID 0 (Striping) implementation."""

from raid.raid_base import RaidArray
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.oplog import OperationLog


class Raid0(RaidArray):
    """RAID 0: Block-level striping across N >= 2 disks without redundancy."""

    def __init__(
        self,
        disks: list[VirtualDisk],
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager=None,
        scheduler_policy: str = "FCFS",
    ):
        if len(disks) < 2:
            raise ValueError("RAID 0 requires at least 2 disks")
        super().__init__(disks, spares, oplog, lock_manager, scheduler_policy)

    def capacity_blocks(self) -> int:
        """Usable capacity is N * disk_blocks."""
        return self.num_disks * self.disk_blocks

    def map_lbn(self, lbn: int) -> dict:
        """Map logical block number (LBN) to physical disk and PBN."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")
        disk_idx = lbn % self.num_disks
        pbn = lbn // self.num_disks
        return {
            "lbn": lbn,
            "disk": disk_idx,
            "pbn": pbn,
            "role": "data",
            "stripe": pbn,
        }

    def read_block(self, lbn: int) -> bytes:
        """Read logical block. Raises DiskFailedError if target disk has failed."""
        mapping = self.map_lbn(lbn)
        disk = self.disks[mapping["disk"]]
        try:
            data = disk.read(mapping["pbn"])
            self.oplog.add(f"RAID0 Read LBN {lbn} from Disk {disk.disk_id} PBN {mapping['pbn']}", level="info")
            return data
        except DiskFailedError as e:
            self.oplog.add(f"RAID0 Read failed for LBN {lbn}: Disk {disk.disk_id} is failed (data lost)", level="error")
            raise e

    def write_block(self, lbn: int, data: bytes) -> None:
        """Write logical block. Raises DiskFailedError if target disk has failed."""
        mapping = self.map_lbn(lbn)
        disk = self.disks[mapping["disk"]]
        try:
            disk.write(mapping["pbn"], data)
            self.oplog.add(f"RAID0 Write LBN {lbn} to Disk {disk.disk_id} PBN {mapping['pbn']}", level="info")
        except DiskFailedError as e:
            self.oplog.add(f"RAID0 Write failed for LBN {lbn}: Disk {disk.disk_id} is failed", level="error")
            raise e

    def rebuild(self, disk_id: int, progress_cb=None) -> bool:
        """RAID 0 has no redundancy and cannot rebuild."""
        self.oplog.add(f"RAID0 Rebuild attempted on Disk {disk_id}: not supported (no redundancy)", level="warning")
        return False

    def is_degraded(self) -> bool:
        """True if any disk has failed (which causes immediate partial data loss in RAID 0)."""
        return any(d.failed for d in self.disks)

    def io_count_for_write(self) -> dict:
        """RAID 0 write: 0 reads, 1 write per logical block."""
        return {"reads": 0, "writes": 1}

    def layout_snapshot(self) -> list[list[dict]]:
        """Return 2D grid [disk_idx][pbn] with block metadata."""
        grid = []
        for d_idx, disk in enumerate(self.disks):
            disk_blocks = []
            for pbn in range(self.disk_blocks):
                lbn = pbn * self.num_disks + d_idx
                if disk.failed:
                    state = "failed"
                elif disk.needs_rebuild:
                    state = "rebuilt"
                else:
                    state = "ok"
                disk_blocks.append({
                    "kind": "data",
                    "state": state,
                    "lbn": lbn,
                    "stripe": pbn,
                })
            grid.append(disk_blocks)
        return grid
