"""Unit tests for concurrency control and race condition reproduction."""

import threading
import time
import pytest
from raid.virtual_disk import VirtualDisk
from raid.raid5 import Raid5
from raid.locks import StripeLockManager
from raid.scrub import scrub
from raid.config import BLOCK_SIZE


def test_concurrency_race_condition_without_locks():
    """Unsafe mode: Concurrent writes to the same stripe without locks produce corrupted parity."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    lock_mgr = StripeLockManager(num_stripes=8, enabled=False)  # Locks DISABLED
    raid = Raid5(disks=disks, lock_manager=lock_mgr, artificial_delay_ms=25.0)

    # In stripe 0: LBN 0 is on Disk 0, LBN 1 is on Disk 1, Parity is on Disk 3
    t1 = threading.Thread(target=raid.write_block, args=(0, b"Payload from Thread 1"))
    t2 = threading.Thread(target=raid.write_block, args=(1, b"Payload from Thread 2"))

    t1.start()
    time.sleep(0.005)  # Start t2 while t1 is sleeping inside artificial_delay
    t2.start()

    t1.join()
    t2.join()

    # Scrub should report stripe 0 as inconsistent due to lost parity update
    result = scrub(raid)
    assert 0 in result["inconsistent"], "Expected stripe 0 to have inconsistent parity due to race condition!"


def test_concurrency_safe_mode_with_locks():
    """Safe mode: Concurrent writes to the same stripe with locks enabled preserve parity consistency."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    lock_mgr = StripeLockManager(num_stripes=8, enabled=True)  # Locks ENABLED
    raid = Raid5(disks=disks, lock_manager=lock_mgr, artificial_delay_ms=25.0)

    t1 = threading.Thread(target=raid.write_block, args=(0, b"Payload from Thread 1"))
    t2 = threading.Thread(target=raid.write_block, args=(1, b"Payload from Thread 2"))

    t1.start()
    time.sleep(0.005)
    t2.start()

    t1.join()
    t2.join()

    # Scrub should report ZERO inconsistent stripes
    result = scrub(raid)
    assert len(result["inconsistent"]) == 0, f"Expected 0 inconsistent stripes, but found: {result['inconsistent']}"

    # Also verify that both blocks read back their respective data
    assert raid.read_block(0).startswith(b"Payload from Thread 1")
    assert raid.read_block(1).startswith(b"Payload from Thread 2")
