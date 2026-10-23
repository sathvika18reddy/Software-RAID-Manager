"""Thread-safe operation logger for tracking RAID events."""

from datetime import datetime
import threading


class OperationLog:
    """Thread-safe log capturing RAID operations, failures, rebuilds, and demos."""

    def __init__(self):
        self._entries: list[dict] = []
        self._lock = threading.Lock()

    def add(self, message: str, level: str = "info") -> None:
        """Add a log entry with current timestamp, level, and message."""
        now_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        entry = {
            "time": now_str,
            "level": level.lower(),
            "message": message,
        }
        with self._lock:
            self._entries.append(entry)

    def entries(self) -> list[dict]:
        """Return a copy of all log entries."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        """Clear all entries."""
        with self._lock:
            self._entries.clear()
