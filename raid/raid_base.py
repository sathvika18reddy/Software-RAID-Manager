"""Abstract base class for RAID implementations."""

from abc import ABC, abstractmethod
import threading
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.oplog import OperationLog
from raid.config import BLOCK_SIZE, DEFAULT_DISK_BLOCKS


class RaidArray(ABC):
    """Abstract base class representing a software RAID array."""

    def __init__(
        self,
        disks: list[VirtualDisk],
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager=None,
        scheduler_policy: str = "FCFS",
    ):
        if not disks:
            raise ValueError("RAID array requires at least one disk")
        self.disks = disks
        self.spares = list(spares) if spares else []
        self.oplog = oplog if oplog is not None else OperationLog()
        self.lock_manager = lock_manager
        self.scheduler_policy = scheduler_policy
        self.num_disks = len(disks)
        self.disk_blocks = disks[0].num_blocks
        self.block_size = disks[0].block_size
        self._array_lock = threading.Lock()
        self.oplog.add(f"Initialized {self.__class__.__name__} with {self.num_disks} disks, {len(self.spares)} spares.")

    # --- Logical Volume Interface ---

    @abstractmethod
    def read_block(self, lbn: int) -> bytes:
        """Read a logical block by LBN."""
        pass

    @abstractmethod
    def write_block(self, lbn: int, data: bytes) -> None:
        """Write a logical block by LBN."""
        pass

    @abstractmethod
    def capacity_blocks(self) -> int:
        """Return total usable capacity in logical blocks."""
        pass

    # --- Failure Management ---

    def fail_disk(self, disk_id: int) -> None:
        """Fail a disk by ID. If a hot spare is available, replace and rebuild automatically."""
        target_idx = None
        for i, d in enumerate(self.disks):
            if d.disk_id == disk_id:
                target_idx = i
                break

        if target_idx is None:
            self.oplog.add(f"Disk {disk_id} not found in array", level="warning")
            return

        self.disks[target_idx].fail()
        self.oplog.add(f"Disk {disk_id} failed!", level="error")

        # Check for available hot spare
        with self._array_lock:
            if self.spares:
                spare = self.spares.pop(0)
                spare.disk_id = disk_id
                spare.needs_rebuild = True
                self.disks[target_idx] = spare
                self.oplog.add(
                    f"Hot spare activated for Disk {disk_id}. Remaining spares: {len(self.spares)}. Starting background rebuild...",
                    level="info",
                )
                self.last_rebuild_thread = threading.Thread(
                    target=self._run_background_rebuild,
                    args=(disk_id,),
                    daemon=True,
                )
                self.last_rebuild_thread.start()
            else:
                self.last_rebuild_thread = None

    def checksum(self, num_blocks: int | None = None) -> str:
        """Compute SHA-256 over logical blocks."""
        from raid.scrub import checksum_array
        return checksum_array(self, num_blocks)

    def _run_background_rebuild(self, disk_id: int) -> None:
        """Helper to run rebuild in a background thread."""
        success = self.rebuild(disk_id)
        if success:
            self.oplog.add(f"Background rebuild completed successfully for Disk {disk_id}.", level="info")
        else:
            self.oplog.add(f"Background rebuild failed for Disk {disk_id}.", level="error")

    def replace_disk(self, disk_id: int) -> None:
        """Manually replace a failed disk with a blank disk ready for rebuild."""
        target_disk = None
        for d in self.disks:
            if d.disk_id == disk_id:
                target_disk = d
                break

        if target_disk is None:
            self.oplog.add(f"Cannot replace Disk {disk_id}: not found", level="warning")
            return

        target_disk.replace()
        self.oplog.add(f"Disk {disk_id} replaced with blank disk. Status: needs rebuild.", level="info")

    @abstractmethod
    def rebuild(self, disk_id: int, progress_cb=None) -> bool:
        """Reconstruct data onto replaced disk. Returns True on success, False on failure."""
        pass

    @abstractmethod
    def is_degraded(self) -> bool:
        """Return True if one or more disks have failed or need rebuild."""
        pass

    # --- Helpers & UI Visualizations ---

    @abstractmethod
    def map_lbn(self, lbn: int) -> dict:
        """Return logical block mapping metadata: disk, pbn, stripe, role, etc."""
        pass

    @abstractmethod
    def io_count_for_write(self) -> dict:
        """Return simulated disk I/O operations per logical write: {'reads': n, 'writes': m}."""
        pass

    @abstractmethod
    def layout_snapshot(self) -> list[list[dict]]:
        """Return 2D grid [disk_index][pbn] with block metadata for UI rendering."""
        pass
