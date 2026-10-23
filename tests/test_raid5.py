"""Unit tests for RAID 5 (Distributed Rotating Parity)."""

import os
import random
import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid5 import Raid5, xor_bytes
from raid.config import BLOCK_SIZE


def test_raid5_mapping_and_parity_rotation():
    # 4 disks: data_per_stripe = 3
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks)
    assert raid.capacity_blocks() == 3 * 8  # 24 blocks

    # Stripe 0: parity_disk = 3 - (0 % 4) = 3. Data disks: [0, 1, 2]
    m0 = raid.map_lbn(0)
    assert m0["stripe"] == 0
    assert m0["parity_disk"] == 3
    assert m0["disk"] == 0

    m1 = raid.map_lbn(1)
    assert m1["disk"] == 1

    m2 = raid.map_lbn(2)
    assert m2["disk"] == 2

    # Stripe 1: parity_disk = 3 - (1 % 4) = 2. Data disks: [0, 1, 3]
    m3 = raid.map_lbn(3)
    assert m3["stripe"] == 1
    assert m3["parity_disk"] == 2
    assert m3["disk"] == 0

    m4 = raid.map_lbn(4)
    assert m4["disk"] == 1

    m5 = raid.map_lbn(5)
    assert m5["disk"] == 3


def test_raid5_parity_xor_consistency():
    random.seed(42)
    disks = [VirtualDisk(i, num_blocks=6, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks)

    # Write random data to all logical blocks
    for lbn in range(raid.capacity_blocks()):
        payload = f"BlockPayload-{lbn}-{random.randint(1000, 9999)}".encode()
        raid.write_block(lbn, payload)

    # Check for each stripe that XOR of all disks is 0
    for stripe in range(raid.disk_blocks):
        acc = bytes(BLOCK_SIZE)
        for disk in disks:
            block = disk.read(stripe)
            acc = xor_bytes(acc, block)
        assert acc == b"\x00" * BLOCK_SIZE, f"Stripe {stripe} parity is inconsistent!"


def test_raid5_degraded_read_each_disk_in_turn():
    """Phase 3 key criterion: For every disk in turn, write data, fail that disk, read all blocks back identical."""
    random.seed(123)
    num_disks = 4
    num_blocks = 8

    for failed_disk_id in range(num_disks):
        disks = [VirtualDisk(i, num_blocks=num_blocks, block_size=BLOCK_SIZE) for i in range(num_disks)]
        raid = Raid5(disks)

        original_data = {}
        for lbn in range(raid.capacity_blocks()):
            data = f"LBN-{lbn}-DiskFailTest-{failed_disk_id}-{random.random()}".encode()
            raid.write_block(lbn, data)
            original_data[lbn] = raid.read_block(lbn)

        # Fail disk
        raid.fail_disk(failed_disk_id)
        assert raid.is_degraded() is True

        # Read back all logical blocks and assert 100% fidelity
        for lbn in range(raid.capacity_blocks()):
            read_back = raid.read_block(lbn)
            assert read_back == original_data[lbn], f"Mismatch on LBN {lbn} when Disk {failed_disk_id} failed"


def test_raid5_degraded_write_and_rebuild():
    """Write data while degraded, then rebuild and ensure data is correct."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks)

    # Initial writes
    for lbn in range(12):
        raid.write_block(lbn, f"Init-{lbn}".encode())

    # Fail Disk 1
    raid.fail_disk(1)

    # Write new data to an LBN that normally lives on failed Disk 1 (e.g. LBN 1)
    mapping1 = raid.map_lbn(1)
    assert mapping1["disk"] == 1
    new_data = b"Overwritten during degraded mode!"
    raid.write_block(1, new_data)

    # Degraded read should return new data immediately
    assert raid.read_block(1).startswith(new_data)

    # Replace Disk 1 and rebuild
    raid.replace_disk(1)
    assert disks[1].needs_rebuild is True
    rebuild_success = raid.rebuild(1)
    assert rebuild_success is True
    assert raid.is_degraded() is False

    # Check physical block directly on rebuilt disk
    assert disks[1].read(mapping1["pbn"]).startswith(new_data)
    # Check all logical blocks read correctly
    assert raid.read_block(1).startswith(new_data)


def test_raid5_multiple_failures_raise():
    disks = [VirtualDisk(i, num_blocks=4, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid5(disks)
    raid.write_block(0, b"data")

    raid.fail_disk(0)
    raid.fail_disk(1)

    with pytest.raises(DiskFailedError):
        raid.read_block(0)

    with pytest.raises(DiskFailedError):
        raid.write_block(0, b"fail")
