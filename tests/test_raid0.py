"""Unit tests for RAID 0 (Striping)."""

import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid0 import Raid0
from raid.config import BLOCK_SIZE


def test_raid0_mapping():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid0(disks)
    assert raid.capacity_blocks() == 24

    # Check mapping
    m0 = raid.map_lbn(0)
    assert m0 == {"lbn": 0, "disk": 0, "pbn": 0, "role": "data", "stripe": 0}

    m1 = raid.map_lbn(1)
    assert m1 == {"lbn": 1, "disk": 1, "pbn": 0, "role": "data", "stripe": 0}

    m2 = raid.map_lbn(2)
    assert m2 == {"lbn": 2, "disk": 2, "pbn": 0, "role": "data", "stripe": 0}

    m3 = raid.map_lbn(3)
    assert m3 == {"lbn": 3, "disk": 0, "pbn": 1, "role": "data", "stripe": 1}

    with pytest.raises(IndexError):
        raid.map_lbn(24)


def test_raid0_read_write_roundtrip():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(2)]
    raid = Raid0(disks)

    payloads = {
        0: b"Block zero data",
        1: b"Block one data",
        5: b"Block five data",
    }
    for lbn, data in payloads.items():
        raid.write_block(lbn, data)

    for lbn, data in payloads.items():
        read_back = raid.read_block(lbn)
        assert read_back.startswith(data)


def test_raid0_failure_data_loss():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(2)]
    raid = Raid0(disks)

    raid.write_block(0, b"disk0 block0")
    raid.write_block(1, b"disk1 block0")

    assert raid.is_degraded() is False
    raid.fail_disk(1)
    assert raid.is_degraded() is True

    # Reading disk0's block (LBN 0) should still succeed
    assert raid.read_block(0).startswith(b"disk0 block0")

    # Reading disk1's block (LBN 1) must raise DiskFailedError
    with pytest.raises(DiskFailedError):
        raid.read_block(1)

    # Rebuild is not possible in RAID 0
    assert raid.rebuild(1) is False


def test_raid0_layout_snapshot():
    disks = [VirtualDisk(i, num_blocks=4, block_size=BLOCK_SIZE) for i in range(2)]
    raid = Raid0(disks)
    snap = raid.layout_snapshot()
    assert len(snap) == 2  # 2 disks
    assert len(snap[0]) == 4  # 4 blocks each
    assert snap[0][0]["kind"] == "data"
    assert snap[0][0]["state"] == "ok"
    assert snap[0][0]["lbn"] == 0
    assert snap[1][0]["lbn"] == 1
