"""Unit tests for RAID 1 (Mirroring)."""

import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid1 import Raid1
from raid.config import BLOCK_SIZE


def test_raid1_mirror_consistency():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid1(disks)
    assert raid.capacity_blocks() == 8

    raid.write_block(2, b"mirrored payload")

    # Verify that physical block 2 on ALL disks contains the data
    for disk in disks:
        assert disk.read(2).startswith(b"mirrored payload")


def test_raid1_survives_n_minus_one_failures():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid1(disks)

    raid.write_block(0, b"resilient block")

    # Fail Disk 0
    raid.fail_disk(0)
    assert raid.is_degraded() is True
    assert raid.read_block(0).startswith(b"resilient block")

    # Fail Disk 1 (only Disk 2 remains)
    raid.fail_disk(1)
    assert raid.is_degraded() is True
    assert raid.read_block(0).startswith(b"resilient block")

    # Fail Disk 2 (all failed)
    raid.fail_disk(2)
    with pytest.raises(DiskFailedError):
        raid.read_block(0)

    with pytest.raises(DiskFailedError):
        raid.write_block(0, b"new write fails")


def test_raid1_replace_and_rebuild():
    disks = [VirtualDisk(i, num_blocks=4, block_size=BLOCK_SIZE) for i in range(2)]
    raid = Raid1(disks)

    for lbn in range(4):
        raid.write_block(lbn, f"Block {lbn}".encode())

    # Fail Disk 1
    raid.fail_disk(1)
    assert raid.is_degraded() is True

    # Replace Disk 1 with a blank disk
    raid.replace_disk(1)
    assert disks[1].needs_rebuild is True
    # Disk 1 data is blank right now
    assert disks[1].read(0) == b"\x00" * BLOCK_SIZE

    # Rebuild Disk 1
    progress_recorded = []
    success = raid.rebuild(1, progress_cb=lambda done, total: progress_recorded.append((done, total)))
    assert success is True
    assert len(progress_recorded) == 4
    assert progress_recorded[-1] == (4, 4)
    assert disks[1].needs_rebuild is False
    assert raid.is_degraded() is False

    # Check data on restored Disk 1
    for lbn in range(4):
        assert disks[1].read(lbn).startswith(f"Block {lbn}".encode())
