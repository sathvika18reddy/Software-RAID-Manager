"""Unit tests for RAID 10 (Striped Mirrors) and trace replay."""

import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid10 import Raid10
from raid.workload import parse_trace_csv, get_sample_trace
from raid.config import BLOCK_SIZE


def test_raid10_initialization_and_capacity():
    """RAID 10 requires even disk count >= 4 and has 50% storage capacity."""
    # Invalid disk counts
    disks3 = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(3)]
    with pytest.raises(ValueError):
        Raid10(disks3)

    disks5 = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(5)]
    with pytest.raises(ValueError):
        Raid10(disks5)

    # Valid 4 disks, 16 blocks each
    disks4 = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
    r10 = Raid10(disks4, chunk_size=1)
    assert r10.capacity_blocks() == 32  # 2 pairs * 16 blocks
    assert r10.num_pairs == 2


def test_raid10_read_write_roundtrip():
    """Verify write mirroring and round-robin read consistency."""
    disks = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
    r10 = Raid10(disks, chunk_size=1)

    # Write LBN 0 (lives in pair 0: Disk 0 and Disk 1)
    r10.write_block(0, b"Data-Pair-0")
    # Verify physical blocks on both mirrors are identical
    assert disks[0].read(0).startswith(b"Data-Pair-0")
    assert disks[1].read(0).startswith(b"Data-Pair-0")

    # Read back through logical interface
    assert r10.read_block(0).startswith(b"Data-Pair-0")


def test_raid10_survives_independent_failures():
    """RAID 10 can survive multiple disk failures as long as no single pair loses both disks."""
    disks = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
    r10 = Raid10(disks, chunk_size=1)

    r10.write_block(0, b"Data-LBN-0")
    r10.write_block(1, b"Data-LBN-1")

    # Fail Disk 0 (in pair 0) and Disk 2 (in pair 1) - both pairs still have 1 healthy disk!
    disks[0].fail()
    disks[2].fail()

    assert r10.is_degraded()
    # Should read back successfully from Disk 1 and Disk 3
    assert r10.read_block(0).startswith(b"Data-LBN-0")
    assert r10.read_block(1).startswith(b"Data-LBN-1")


def test_raid10_rebuild_from_partner():
    """Rebuild reconstructs data directly from the mirror partner."""
    disks = [VirtualDisk(i, num_blocks=16, block_size=BLOCK_SIZE) for i in range(4)]
    r10 = Raid10(disks, chunk_size=1)

    for lbn in range(10):
        r10.write_block(lbn, f"Block-{lbn}".encode())

    # Fail Disk 1 (partner of Disk 0)
    disks[1].fail()
    r10.replace_disk(1)
    assert disks[1].needs_rebuild

    # Rebuild Disk 1 from Disk 0
    success = r10.rebuild(1)
    assert success
    assert not disks[1].needs_rebuild
    assert not r10.is_degraded()

    # Verify physical data on Disk 1 matches Disk 0
    for pbn in range(16):
        assert disks[1].read(pbn) == disks[0].read(pbn)


def test_trace_parsing_and_replay():
    """Test CSV trace parser and sample workload traces."""
    csv_text = """
    # Sample Trace
    op,lbn
    r,0
    w,5
    r,12
    w,3
    """
    reqs = parse_trace_csv(csv_text, max_lbn=16)
    assert len(reqs) == 4
    assert reqs[0] == {"op": "r", "lbn": 0}
    assert reqs[1] == {"op": "w", "lbn": 5}

    oltp_trace = get_sample_trace("OLTP", max_lbn=20, n_requests=25)
    assert len(oltp_trace) == 25
