"""Unit tests for Phase 4: Rebuild, hot spare auto-activation, and checksum verification."""

import time
import pytest
from raid.virtual_disk import VirtualDisk
from raid.raid1 import Raid1
from raid.raid5 import Raid5
from raid.scrub import checksum_array
from raid.config import BLOCK_SIZE


def test_raid1_hot_spare_auto_rebuild_and_checksum():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(2)]
    spares = [VirtualDisk(100, num_blocks=8, block_size=BLOCK_SIZE)]
    raid = Raid1(disks=disks, spares=spares)

    # Write distinct data
    for lbn in range(raid.capacity_blocks()):
        raid.write_block(lbn, f"Raid1-Data-{lbn}".encode())

    pre_checksum = checksum_array(raid)

    # Fail Disk 0 -> should auto-activate hot spare and start rebuild
    assert len(raid.spares) == 1
    raid.fail_disk(0)
    assert len(raid.spares) == 0
    assert raid.last_rebuild_thread is not None

    raid.last_rebuild_thread.join(timeout=5.0)
    assert not raid.last_rebuild_thread.is_alive()

    post_checksum = checksum_array(raid)
    assert post_checksum == pre_checksum
    assert raid.is_degraded() is False


def test_raid5_hot_spare_auto_rebuild_and_checksum():
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    spares = [VirtualDisk(200, num_blocks=8, block_size=BLOCK_SIZE)]
    raid = Raid5(disks=disks, spares=spares)

    # Write distinct data to all logical blocks
    for lbn in range(raid.capacity_blocks()):
        raid.write_block(lbn, f"Raid5-Payload-{lbn}".encode())

    pre_checksum = checksum_array(raid)

    # Fail Disk 2 -> triggers hot spare replacement and auto-rebuild
    assert len(raid.spares) == 1
    raid.fail_disk(2)
    assert len(raid.spares) == 0
    assert raid.last_rebuild_thread is not None

    raid.last_rebuild_thread.join(timeout=5.0)
    assert not raid.last_rebuild_thread.is_alive()

    post_checksum = checksum_array(raid)
    assert post_checksum == pre_checksum
    assert raid.is_degraded() is False


def test_manual_rebuild_progress_callback():
    disks = [VirtualDisk(i, num_blocks=6, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid5(disks=disks)

    for lbn in range(raid.capacity_blocks()):
        raid.write_block(lbn, f"ManualRebuild-{lbn}".encode())

    pre_checksum = checksum_array(raid)

    # Fail and manual replace
    raid.fail_disk(1)
    raid.replace_disk(1)
    assert disks[1].needs_rebuild is True

    progress_ticks = []
    success = raid.rebuild(1, progress_cb=lambda done, total: progress_ticks.append((done, total)))
    assert success is True
    assert len(progress_ticks) == 6
    assert progress_ticks[-1] == (6, 6)

    post_checksum = checksum_array(raid)
    assert post_checksum == pre_checksum
