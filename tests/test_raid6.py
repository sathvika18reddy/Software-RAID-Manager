"""Unit tests for Galois Field arithmetic and RAID 6 dual parity recovery."""

import pytest
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.galois import gf_add, gf_mul, gf_div, gf_inv, gf_pow, compute_p_parity, compute_q_parity, solve_two_disk_failures
from raid.raid6 import Raid6


def test_galois_field_properties():
    # Commutativity and identity
    assert gf_mul(1, 42) == 42
    assert gf_mul(0, 42) == 0
    assert gf_mul(7, 9) == gf_mul(9, 7)

    # Inversion
    for val in [1, 2, 3, 17, 128, 255]:
        inv = gf_inv(val)
        assert gf_mul(val, inv) == 1

    # Division
    assert gf_div(42, 6) == gf_mul(42, gf_inv(6))


def test_raid6_initialization_and_capacity():
    with pytest.raises(ValueError, match="at least 4 disks"):
        Raid6(disks=[VirtualDisk(i, num_blocks=8) for i in range(3)])

    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)
    # 5 disks: 3 data, 2 parity (P+Q) -> capacity = 3 * 16 = 48
    assert array.capacity_blocks() == 48


def test_raid6_read_write_roundtrip():
    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)

    payload = b"RAID6_DUAL_PARITY_VERIFICATION"
    for lbn in range(10):
        array.write_block(lbn, payload + f"_{lbn}".encode())

    for lbn in range(10):
        data = array.read_block(lbn)
        expected = payload + f"_{lbn}".encode()
        assert data[:len(expected)] == expected


def test_raid6_single_disk_failure():
    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)
    array.write_block(0, b"DATA_SINGLE_FAILURE_TEST")

    # Fail disk 0 (which holds LBN 0)
    mapping = array.map_lbn(0)
    failed_disk_idx = mapping["disk"]
    array.disks[failed_disk_idx].fail()

    assert array.is_degraded() is True
    # Reconstructed read
    recovered = array.read_block(0)
    assert recovered[:24] == b"DATA_SINGLE_FAILURE_TEST"


def test_raid6_dual_disk_failure_two_data_disks():
    """Verify simultaneous failure of 2 data disks in the same stripe."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(6)]
    array = Raid6(disks=disks)

    # 6 disks: 4 data, 2 parity (P and Q)
    array.write_block(0, b"DATA_BLOCK_0_DUAL_TEST")
    array.write_block(1, b"DATA_BLOCK_1_DUAL_TEST")

    # Fail the disks containing LBN 0 and LBN 1
    d0 = array.map_lbn(0)["disk"]
    d1 = array.map_lbn(1)["disk"]
    assert d0 != d1

    array.disks[d0].fail()
    array.disks[d1].fail()

    assert array.is_degraded() is True
    # Both data blocks must be correctly reconstructed via P+Q Galois field syndrome inversion
    rec0 = array.read_block(0)
    rec1 = array.read_block(1)
    assert rec0.startswith(b"DATA_BLOCK_0_DUAL_TEST")
    assert rec1.startswith(b"DATA_BLOCK_1_DUAL_TEST")


def test_raid6_dual_disk_failure_data_and_parity():
    """Verify simultaneous failure of 1 data disk and 1 parity disk."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)
    array.write_block(0, b"DATA_BLOCK_DATA_AND_PARITY_TEST")

    mapping = array.map_lbn(0)
    data_disk = mapping["disk"]
    p_disk = mapping["p_disk"]

    # Fail data disk AND P disk -> reconstructs via Q disk!
    array.disks[data_disk].fail()
    array.disks[p_disk].fail()

    rec = array.read_block(0)
    assert rec[:31] == b"DATA_BLOCK_DATA_AND_PARITY_TEST"


def test_raid6_rebuild_from_failure():
    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)
    array.write_block(0, b"REBUILD_VERIFICATION_BLOCK")

    mapping = array.map_lbn(0)
    victim_id = array.disks[mapping["disk"]].disk_id
    array.fail_disk(victim_id)

    # Replace with blank disk
    array.replace_disk(victim_id)
    assert array.is_degraded() is True

    # Rebuild
    success = array.rebuild(victim_id)
    assert success is True
    assert array.is_degraded() is False

    # Verify disk content is restored
    assert array.read_block(0)[:26] == b"REBUILD_VERIFICATION_BLOCK"


def test_raid6_three_failures_exceeds_tolerance():
    disks = [VirtualDisk(i, num_blocks=16) for i in range(5)]
    array = Raid6(disks=disks)
    array.disks[0].fail()
    array.disks[1].fail()
    array.disks[2].fail()

    with pytest.raises(DiskFailedError, match="cannot recover"):
        array.read_block(0)
