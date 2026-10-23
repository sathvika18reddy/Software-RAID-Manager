"""Unit tests for VirtualDisk and OperationLog."""

import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.config import BLOCK_SIZE, SEEK_COST_PER_BLOCK, ROTATIONAL_DELAY, TRANSFER_TIME
from raid.oplog import OperationLog


def test_virtual_disk_read_write():
    disk = VirtualDisk(disk_id=0, num_blocks=16, block_size=BLOCK_SIZE)
    data = b"Hello Software RAID!"
    disk.write(2, data)
    read_back = disk.read(2)
    assert read_back.startswith(data)
    assert len(read_back) == BLOCK_SIZE
    assert read_back[len(data):] == b"\x00" * (BLOCK_SIZE - len(data))


def test_virtual_disk_failure():
    disk = VirtualDisk(disk_id=1, num_blocks=16, block_size=BLOCK_SIZE)
    disk.write(0, b"data before failure")
    disk.fail()
    assert disk.failed is True

    with pytest.raises(DiskFailedError):
        disk.read(0)

    with pytest.raises(DiskFailedError):
        disk.write(0, b"new data")


def test_virtual_disk_replace():
    disk = VirtualDisk(disk_id=2, num_blocks=16, block_size=BLOCK_SIZE)
    disk.write(4, b"important payload")
    disk.fail()
    disk.replace()

    assert disk.failed is False
    assert disk.needs_rebuild is True
    assert disk.head_position == 0
    # Data should be zeroed
    blank = disk.read(4)
    assert blank == b"\x00" * BLOCK_SIZE


def test_virtual_disk_latency_stats():
    disk = VirtualDisk(disk_id=3, num_blocks=32, block_size=BLOCK_SIZE)
    assert disk.head_position == 0
    assert disk.stats["reads"] == 0
    assert disk.stats["writes"] == 0
    assert disk.stats["total_seek_distance"] == 0
    assert disk.stats["total_simulated_ms"] == 0.0

    # Write at PBN 10
    disk.write(10, b"block 10")
    expected_cost1 = (10 * SEEK_COST_PER_BLOCK) + ROTATIONAL_DELAY + TRANSFER_TIME
    assert disk.head_position == 10
    assert disk.stats["writes"] == 1
    assert disk.stats["total_seek_distance"] == 10
    assert pytest.approx(disk.stats["total_simulated_ms"]) == expected_cost1

    # Read at PBN 4 (seek dist = 6)
    disk.read(4)
    expected_cost2 = (6 * SEEK_COST_PER_BLOCK) + ROTATIONAL_DELAY + TRANSFER_TIME
    assert disk.head_position == 4
    assert disk.stats["reads"] == 1
    assert disk.stats["total_seek_distance"] == 16
    assert pytest.approx(disk.stats["total_simulated_ms"]) == expected_cost1 + expected_cost2


def test_virtual_disk_out_of_bounds():
    disk = VirtualDisk(disk_id=4, num_blocks=8, block_size=BLOCK_SIZE)
    with pytest.raises(IndexError):
        disk.read(8)
    with pytest.raises(IndexError):
        disk.read(-1)
    with pytest.raises(IndexError):
        disk.write(10, b"bad")


def test_operation_log():
    log = OperationLog()
    log.add("First event", level="info")
    log.add("Warning occurred", level="warning")
    entries = log.entries()
    assert len(entries) == 2
    assert entries[0]["message"] == "First event"
    assert entries[0]["level"] == "info"
    assert "time" in entries[0]
    assert entries[1]["level"] == "warning"
