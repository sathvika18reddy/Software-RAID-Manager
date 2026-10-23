"""Stripe-level concurrency control for RAID read-modify-write synchronization."""

import threading
from contextlib import contextmanager


class StripeLockManager:
    """Manages per-stripe locks to protect read-modify-write critical sections."""

    def __init__(self, num_stripes: int, enabled: bool = True):
        self.num_stripes = num_stripes
        self.enabled = enabled
        self._locks = [threading.Lock() for _ in range(num_stripes)]

    @contextmanager
    def lock(self, stripe: int):
        """Context manager to acquire lock for a given stripe if locking is enabled."""
        if self.enabled and 0 <= stripe < len(self._locks):
            with self._locks[stripe]:
                yield
        else:
            yield
