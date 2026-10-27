"""Write-Intent Log (Journaling) for RAID Crash Consistency.

Closes the RAID Write-Hole syndrome by logging in-flight stripes before
modifying data or parity. After an abrupt crash, only logged active stripes
need to be checked/re-synchronized instead of a full array scrub.
"""

import threading
import time
from typing import Set


class WriteIntentJournal:
    """Manages an in-memory or simulated non-volatile write-ahead journal."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._active_stripes: dict[int, dict] = {}
        self._lock = threading.Lock()
        self.log_history: list[dict] = []

    def log_intent(self, stripe: int, lbn: int) -> None:
        """Record intent to modify stripe before initiating Read-Modify-Write."""
        if not self.enabled:
            return
        with self._lock:
            entry = {
                "stripe": stripe,
                "lbn": lbn,
                "timestamp": time.time(),
                "status": "ACTIVE",
            }
            self._active_stripes[stripe] = entry
            self.log_history.append(entry)

    def log_commit(self, stripe: int) -> None:
        """Mark stripe update as successfully committed to data and parity disks."""
        if not self.enabled:
            return
        with self._lock:
            if stripe in self._active_stripes:
                self._active_stripes.pop(stripe, None)
                self.log_history.append({
                    "stripe": stripe,
                    "status": "COMMITTED",
                    "timestamp": time.time(),
                })

    def pending_stripes(self) -> set[int]:
        """Return stripes that were active (in-flight) and never committed before crash."""
        with self._lock:
            return set(self._active_stripes.keys())

    def recover(self, array) -> list[int]:
        """Fast post-crash recovery: inspect and re-sync only the journaled pending stripes.
        
        Returns:
            list of repaired stripe numbers.
        """
        pending = self.pending_stripes()
        if not pending:
            return []

        repaired = []
        from raid.raid5 import Raid5, xor_bytes

        if isinstance(array, Raid5):
            for stripe in pending:
                parity_disk_idx = (array.num_disks - 1) - (stripe % array.num_disks)
                parity_disk = array.disks[parity_disk_idx]

                # Recalculate parity across this specific stripe
                chunk_size = getattr(array, "chunk_size", 1)
                for chunk_offset in range(chunk_size):
                    pbn = stripe * chunk_size + chunk_offset
                    expected_parity = bytes(array.block_size)
                    for i, d in enumerate(array.disks):
                        if i != parity_disk_idx:
                            expected_parity = xor_bytes(expected_parity, d.read(pbn))
                    parity_disk.write(pbn, expected_parity)

                repaired.append(stripe)
                array.oplog.add(f"Journal recovery: Re-synced in-flight stripe {stripe}", level="info")

        with self._lock:
            self._active_stripes.clear()

        return repaired
