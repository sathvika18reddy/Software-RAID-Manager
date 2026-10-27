"""LRU Buffer Cache Layer for RAID Storage Stack.

Demonstrates write-back vs write-through caching, dirty block eviction,
and read latency reduction.
"""

from collections import OrderedDict
import threading
from raid.raid_base import RaidArray


class CacheBlock:
    """Represents a single block in the buffer cache."""

    def __init__(self, lbn: int, data: bytes, dirty: bool = False):
        self.lbn = lbn
        self.data = bytearray(data)
        self.dirty = dirty


class LruBlockCache:
    """Least-Recently-Used (LRU) block buffer cache over a RaidArray."""

    def __init__(self, array: RaidArray, capacity_blocks: int = 8, write_back: bool = True):
        self.array = array
        self.capacity = capacity_blocks
        self.write_back = write_back
        self._cache: OrderedDict[int, CacheBlock] = OrderedDict()
        self._lock = threading.Lock()
        self.stats = {
            "hits": 0,
            "misses": 0,
            "writes": 0,
            "dirty_evictions": 0,
        }

    def read_block(self, lbn: int) -> bytes:
        """Read block through cache. Returns cached data on hit, loads from RAID on miss."""
        with self._lock:
            if lbn in self._cache:
                self.stats["hits"] += 1
                block = self._cache[lbn]
                self._cache.move_to_end(lbn)
                return bytes(block.data)

            self.stats["misses"] += 1

        # Miss: read from underlying array outside cache lock
        disk_data = self.array.read_block(lbn)

        with self._lock:
            # Check again in case another thread loaded it
            if lbn in self._cache:
                return bytes(self._cache[lbn].data)

            if len(self._cache) >= self.capacity:
                self._evict_one()

            self._cache[lbn] = CacheBlock(lbn=lbn, data=disk_data, dirty=False)
            return bytes(disk_data)

    def write_block(self, lbn: int, data: bytes) -> None:
        """Write block through cache. Handles write-back buffering or write-through commit."""
        with self._lock:
            self.stats["writes"] += 1

            if len(self._cache) >= self.capacity and lbn not in self._cache:
                self._evict_one()

            if self.write_back:
                # Buffer in cache as dirty block
                self._cache[lbn] = CacheBlock(lbn=lbn, data=data, dirty=True)
                self._cache.move_to_end(lbn)
                return
            else:
                # Write-through: cache and write immediately to RAID
                self._cache[lbn] = CacheBlock(lbn=lbn, data=data, dirty=False)
                self._cache.move_to_end(lbn)

        # Write-through commits directly to RAID
        self.array.write_block(lbn, data)

    def _evict_one(self) -> None:
        """Evict oldest block. If dirty, flush it to the underlying RAID array."""
        oldest_lbn, block = self._cache.popitem(last=False)
        if block.dirty:
            self.stats["dirty_evictions"] += 1
            self.array.write_block(oldest_lbn, bytes(block.data))

    def flush(self) -> int:
        """Flush all dirty blocks to the underlying RAID array. Returns count of flushed blocks."""
        flushed = 0
        with self._lock:
            for lbn, block in self._cache.items():
                if block.dirty:
                    self.array.write_block(lbn, bytes(block.data))
                    block.dirty = False
                    flushed += 1
        return flushed

    def clear(self) -> None:
        """Clear the cache, discarding non-flushed entries."""
        with self._lock:
            self._cache.clear()

    @property
    def hit_rate(self) -> float:
        """Cache hit percentage."""
        total = self.stats["hits"] + self.stats["misses"]
        return (self.stats["hits"] / total) if total > 0 else 0.0

    @property
    def dirty_count(self) -> int:
        """Number of modified blocks currently uncommitted to RAID."""
        with self._lock:
            return sum(1 for b in self._cache.values() if b.dirty)
