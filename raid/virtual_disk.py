"""Virtual disk simulation with head movement, latency accounting, and failure injection."""

import threading
from raid.config import (
    BLOCK_SIZE,
    DEFAULT_DISK_BLOCKS,
    SEEK_COST_PER_BLOCK,
    ROTATIONAL_DELAY,
    TRANSFER_TIME,
)


class DiskFailedError(Exception):
    """Raised when an I/O operation is attempted on a failed disk."""
    pass


class VirtualDisk:
    """Simulates a physical hard disk drive with block storage and simulated latency."""

    def __init__(self, disk_id: int, num_blocks: int = DEFAULT_DISK_BLOCKS, block_size: int = BLOCK_SIZE):
        self.disk_id = disk_id
        self.num_blocks = num_blocks
        self.block_size = block_size
        self.failed = False
        self.head_position = 0
        self.needs_rebuild = False
        self.blocks = [bytearray(self.block_size) for _ in range(self.num_blocks)]
        self.stats = {
            "reads": 0,
            "writes": 0,
            "total_seek_distance": 0,
            "total_simulated_ms": 0.0,
        }
        # SMART Telemetry
        self.reallocated_sectors = 0
        self.read_errors = 0
        self.temperature = 35
        self.health_score = 100.0
        self._lock = threading.Lock()

    def record_smart_error(self, count: int = 1) -> None:
        """Inject SMART bad sectors / read errors, lowering health score."""
        with self._lock:
            self.reallocated_sectors += count
            self.read_errors += count
            self.health_score = max(0.0, self.health_score - count * 15.0)

    def is_smart_warning(self) -> bool:
        """Return True if drive health indicates imminent failure."""
        return self.health_score < 70.0 or self.reallocated_sectors >= 3

    def smart_report(self) -> dict:
        """Return structured SMART health report."""
        status = "FAILED" if self.failed else ("WARNING" if self.is_smart_warning() else "OK")
        return {
            "disk_id": self.disk_id,
            "status": status,
            "health_score": round(self.health_score, 1),
            "reallocated_sectors": self.reallocated_sectors,
            "read_errors": self.read_errors,
            "temperature_c": self.temperature,
        }

    def access_cost(self, pbn: int) -> float:
        """Calculate simulated access cost in ms, moving the head and updating stats."""
        seek_dist = abs(pbn - self.head_position)
        cost = (seek_dist * SEEK_COST_PER_BLOCK) + ROTATIONAL_DELAY + TRANSFER_TIME
        self.head_position = pbn
        self.stats["total_seek_distance"] += seek_dist
        self.stats["total_simulated_ms"] += cost
        return cost

    def read(self, pbn: int) -> bytes:
        """Read data from physical block number (PBN). Raises DiskFailedError if failed."""
        with self._lock:
            if self.failed:
                raise DiskFailedError(f"Disk {self.disk_id} has failed")
            if pbn < 0 or pbn >= self.num_blocks:
                raise IndexError(f"PBN {pbn} out of bounds (0..{self.num_blocks - 1}) on Disk {self.disk_id}")
            self.access_cost(pbn)
            self.stats["reads"] += 1
            return bytes(self.blocks[pbn])

    def write(self, pbn: int, data: bytes) -> None:
        """Write data to physical block number (PBN). Raises DiskFailedError if failed."""
        with self._lock:
            if self.failed:
                raise DiskFailedError(f"Disk {self.disk_id} has failed")
            if pbn < 0 or pbn >= self.num_blocks:
                raise IndexError(f"PBN {pbn} out of bounds (0..{self.num_blocks - 1}) on Disk {self.disk_id}")
            # Pad or truncate to block_size
            if len(data) < self.block_size:
                data = data.ljust(self.block_size, b'\x00')
            elif len(data) > self.block_size:
                data = data[:self.block_size]
            self.access_cost(pbn)
            self.stats["writes"] += 1
            self.blocks[pbn] = bytearray(data)

    def fail(self) -> None:
        """Mark the disk as failed. Existing data is retained in memory but unreadable."""
        with self._lock:
            self.failed = True

    def replace(self) -> None:
        """Replace the disk with a fresh blank disk, failed=False, flagged needs_rebuild."""
        with self._lock:
            self.failed = False
            self.needs_rebuild = True
            self.head_position = 0
            self.blocks = [bytearray(self.block_size) for _ in range(self.num_blocks)]
