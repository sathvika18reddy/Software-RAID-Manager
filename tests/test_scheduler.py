"""Unit tests for DiskQueue and scheduling policies (FCFS, SCAN)."""

import random
import pytest
from raid.virtual_disk import VirtualDisk
from raid.scheduler import DiskQueue
from raid.config import BLOCK_SIZE


def test_fcfs_order_is_arrival_order():
    disk = VirtualDisk(disk_id=0, num_blocks=64, block_size=BLOCK_SIZE)
    queue = DiskQueue(disk=disk, policy="FCFS")

    sequence = [12, 45, 5, 23, 60, 2]
    futures = [queue.submit({"op": "w", "pbn": pbn, "data": f"block-{pbn}".encode()}) for pbn in sequence]

    served = queue.process_all()
    served_pbns = [s["pbn"] for s in served]

    assert served_pbns == sequence
    for f in futures:
        assert f.done()
        assert f.exception() is None


def test_scan_order_monotonic_per_sweep():
    # Head starts at 20
    disk = VirtualDisk(disk_id=1, num_blocks=64, block_size=BLOCK_SIZE)
    disk.head_position = 20

    queue = DiskQueue(disk=disk, policy="SCAN")
    # Mix of requests above and below 20
    requests = [10, 40, 5, 30, 50, 15]
    for pbn in requests:
        queue.submit({"op": "r", "pbn": pbn})

    served = queue.process_all()
    served_pbns = [s["pbn"] for s in served]

    # Starting at 20 sweeping UP: [30, 40, 50], then reversing DOWN: [15, 10, 5]
    assert served_pbns == [30, 40, 50, 15, 10, 5]


def test_scan_head_movement_less_or_equal_fcfs():
    """SCAN head movement should be <= FCFS on a random batch (fixed seed)."""
    random.seed(999)
    num_blocks = 128
    requests = [random.randint(0, num_blocks - 1) for _ in range(40)]

    # Run FCFS
    disk_fcfs = VirtualDisk(disk_id=2, num_blocks=num_blocks, block_size=BLOCK_SIZE)
    disk_fcfs.head_position = 0
    queue_fcfs = DiskQueue(disk=disk_fcfs, policy="FCFS")
    for pbn in requests:
        queue_fcfs.submit({"op": "r", "pbn": pbn})
    queue_fcfs.process_all()

    # Run SCAN
    disk_scan = VirtualDisk(disk_id=3, num_blocks=num_blocks, block_size=BLOCK_SIZE)
    disk_scan.head_position = 0
    queue_scan = DiskQueue(disk=disk_scan, policy="SCAN")
    for pbn in requests:
        queue_scan.submit({"op": "r", "pbn": pbn})
    queue_scan.process_all()

    assert queue_scan.total_head_movement <= queue_fcfs.total_head_movement
    # Typically SCAN provides a massive reduction in head movement
    assert queue_scan.total_head_movement < queue_fcfs.total_head_movement
