"""Unit tests for scrubbing, write-hole crash simulation, silent corruption, and repair."""

import pytest
from raid.virtual_disk import VirtualDisk
from raid.raid5 import Raid5, xor_bytes
from raid.raid1 import Raid1
from raid.scrub import scrub, write_with_crash, SimulatedCrashError, checksum_array
from raid.config import BLOCK_SIZE


def test_scrub_detects_and_repairs_injected_corruption():
    """Scrub detects and repairs manually injected parity corruption in RAID 5."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks=disks)

    # Initialize data
    for lbn in range(12):
        raid.write_block(lbn, f"Block-{lbn}".encode())

    # Pre-check: clean array has 0 inconsistencies
    res_clean = scrub(raid, repair=False)
    assert len(res_clean["inconsistent"]) == 0

    # Inject corruption: corrupt parity block on stripe 2
    # In 4-disk RAID 5, stripe 2 parity disk = 3 - (2 % 4) = 1
    parity_disk_stripe2 = disks[1]
    parity_disk_stripe2.write(2, b"CORRUPTED_PARITY_BYTES")

    # Scrub without repair
    res_corrupt = scrub(raid, repair=False)
    assert 2 in res_corrupt["inconsistent"]
    assert res_corrupt["repaired"] == 0

    # Scrub with repair
    res_repaired = scrub(raid, repair=True)
    assert 2 in res_repaired["inconsistent"]
    assert res_repaired["repaired"] == 1

    # Post-repair scrub: clean
    res_post = scrub(raid, repair=False)
    assert len(res_post["inconsistent"]) == 0


def test_write_hole_produces_inconsistency_and_silent_corruption():
    """Write-hole crash leaves parity stale, causing silent data corruption during degraded read."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks=disks)

    # Initialize stripe 0 with known data
    # Stripe 0 data disks: 0 (LBN 0), 1 (LBN 1), 2 (LBN 2); parity on Disk 3
    raid.write_block(0, b"Initial A")
    raid.write_block(1, b"Initial B")
    raid.write_block(2, b"Initial C")

    # Verify clean scrub
    assert len(scrub(raid, repair=False)["inconsistent"]) == 0

    # Inject crash mid-write on LBN 0
    with pytest.raises(SimulatedCrashError):
        write_with_crash(raid, lbn=0, data=b"Updated A (Crash)")

    # Parity is now stale: scrub should find stripe 0 inconsistent!
    res_crash = scrub(raid, repair=False)
    assert 0 in res_crash["inconsistent"]

    # Demonstrate Silent Corruption:
    # Disk 0 has the new data ("Updated A"), but parity on Disk 3 was NOT updated.
    # Now suppose Disk 1 fails!
    disks[1].fail()

    # Degraded read of LBN 1 (which lives on Disk 1) will XOR Disk 0, Disk 2, and Disk 3.
    # Because Disk 0 has new data while Disk 3 has OLD parity, the reconstructed LBN 1 will be WRONG!
    corrupted_read_lbn1 = raid.read_block(1)
    assert not corrupted_read_lbn1.startswith(b"Initial B"), "Expected silent corruption due to write-hole!"


def test_write_hole_repaired_by_scrub_prevents_corruption():
    """If scrub repairs the write-hole before a second disk fails, degraded reads recover correctly."""
    disks = [VirtualDisk(i, num_blocks=8, block_size=BLOCK_SIZE) for i in range(4)]
    raid = Raid5(disks=disks)

    raid.write_block(0, b"Initial A")
    raid.write_block(1, b"Initial B")
    raid.write_block(2, b"Initial C")

    with pytest.raises(SimulatedCrashError):
        write_with_crash(raid, lbn=0, data=b"Updated A")

    # Repair parity via scrub BEFORE any disk fails
    scrub_res = scrub(raid, repair=True)
    assert 0 in scrub_res["inconsistent"]
    assert scrub_res["repaired"] == 1

    # Now fail Disk 1
    disks[1].fail()

    # Degraded read of LBN 1 should successfully recover the true data!
    recovered_lbn1 = raid.read_block(1)
    assert recovered_lbn1.startswith(b"Initial B")


def test_raid1_scrub_and_repair():
    """Test scrub detection and repair for RAID 1 mirror mismatch."""
    disks = [VirtualDisk(i, num_blocks=4, block_size=BLOCK_SIZE) for i in range(3)]
    raid = Raid1(disks=disks)

    for lbn in range(4):
        raid.write_block(lbn, f"Block-{lbn}".encode())

    # Corrupt mirror on Disk 2
    disks[2].write(1, b"Corrupted mirror on disk 2")

    res = scrub(raid, repair=False)
    assert 1 in res["inconsistent"]

    res_rep = scrub(raid, repair=True)
    assert 1 in res_rep["inconsistent"]
    assert res_rep["repaired"] == 1

    res_clean = scrub(raid, repair=False)
    assert len(res_clean["inconsistent"]) == 0
    assert disks[2].read(1).startswith(b"Block-1")
