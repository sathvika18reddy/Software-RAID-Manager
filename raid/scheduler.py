"""Disk Request Queue and Scheduling Algorithms (FCFS, SCAN)."""

import threading
from concurrent.futures import Future
from raid.virtual_disk import VirtualDisk


class DiskQueue:
    """Manages an I/O request queue for a VirtualDisk with FCFS and SCAN scheduling."""

    def __init__(self, disk: VirtualDisk, policy: str = "FCFS"):
        if policy not in ("FCFS", "SCAN"):
            raise ValueError(f"Unknown scheduling policy: {policy}")
        self.disk = disk
        self.policy = policy
        self._queue: list[tuple[dict, Future]] = []
        self._lock = threading.Lock()
        self.total_head_movement: int = 0
        self.served_order: list[dict] = []
        self.direction: int = 1  # 1 for UP (increasing PBN), -1 for DOWN

    def submit(self, request: dict) -> Future:
        """Submit an I/O request: {'op': 'r'|'w', 'pbn': int, 'data': bytes|None}. Returns a Future."""
        future = Future()
        with self._lock:
            self._queue.append((request, future))
        return future

    def schedule(self, items: list[tuple[dict, Future]]) -> list[tuple[dict, Future]]:
        """Order queued requests according to the selected policy."""
        if not items or self.policy == "FCFS":
            return list(items)

        if self.policy == "SCAN":
            current_head = self.disk.head_position
            # Partition requests: those >= current head and those < current head
            up = [item for item in items if item[0]["pbn"] >= current_head]
            down = [item for item in items if item[0]["pbn"] < current_head]

            # Upward sweep: increasing PBN
            up.sort(key=lambda item: item[0]["pbn"])
            # Downward sweep: decreasing PBN
            down.sort(key=lambda item: item[0]["pbn"], reverse=True)

            if self.direction >= 0:
                # Sweep UP first, then reverse DOWN
                ordered = up + down
                if down:
                    self.direction = -1
            else:
                # Sweep DOWN first, then reverse UP
                ordered = down + up
                if up:
                    self.direction = 1
            return ordered

        return list(items)

    def process_all(self) -> list[dict]:
        """Process all queued requests in policy order, execute on disk, and return served order."""
        with self._lock:
            items_to_process = list(self._queue)
            self._queue.clear()

        ordered_items = self.schedule(items_to_process)
        served = []

        for req, future in ordered_items:
            op = req.get("op", "r")
            pbn = req["pbn"]
            prev_head = self.disk.head_position
            seek_dist = abs(pbn - prev_head)
            self.total_head_movement += seek_dist

            try:
                if op == "r":
                    result = self.disk.read(pbn)
                elif op == "w":
                    data = req.get("data", b"")
                    self.disk.write(pbn, data)
                    result = None
                else:
                    raise ValueError(f"Unknown operation: {op}")

                future.set_result(result)
                served_item = {
                    "op": op,
                    "pbn": pbn,
                    "prev_head": prev_head,
                    "new_head": self.disk.head_position,
                    "seek_dist": seek_dist,
                    "result": result,
                }
                served.append(served_item)
                self.served_order.append(served_item)
            except Exception as e:
                future.set_exception(e)
                served.append({"op": op, "pbn": pbn, "error": str(e)})

        return served
