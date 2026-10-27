"""Unit tests for Write-Intent Journal, LRU Block Cache, and Per-Block Checksums."""

import pytest
from raid.virtual_disk import VirtualDisk
from raid.raid5 import Raid5
from raid.journal import WriteIntentJournal
from raid.cache import LruBlockCache
from raid.scrub import BlockChecksumManager, scrub, write_with_crash, SimulatedCrashError


def test_write_intent_journal_lifecycle():
    """Verify write-intent logging, commit, and recovery."""
    journal = WriteIntentJournal(enabled=True)
    assert journal.pending_stripes() == set()

    # Log intent
    journal.log_intent(stripe=2, lbn=6)
    assert journal.pending_stripes() == {2}

    # Second intent
    journal.log_intent(stripe=3, lbn=9)
    assert journal.pending_stripes() == {2, 3}

    # Commit first
    journal.log_commit(stripe=2)
    assert journal.pending_stripes() == {3}


def test_write_intent_journal_recovers_unclean_stripes():
    """Verify recovery audits and repairs only the logged pending stripes."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(4)]
    array = Raid5(disks=disks)
    journal = WriteIntentJournal(enabled=True)

    # Log intent on stripe 1, then simulate crash mid-write
    journal.log_intent(stripe=1, lbn=3)
    try:
        write_with_crash(array, lbn=3, data=b"CRASHED_IN_FLIGHT")
    except SimulatedCrashError:
        pass

    # Array now has an inconsistent stripe
    res_before = scrub(array, repair=False)
    assert 1 in res_before["inconsistent"]

    # Journal recovery should audit and repair ONLY pending stripe 1
    repaired_stripes = journal.recover(array)
    assert 1 in repaired_stripes
    assert len(journal.pending_stripes()) == 0

    # Verification: array is now fully consistent
    res_after = scrub(array, repair=False)
    assert len(res_after["inconsistent"]) == 0


def test_lru_block_cache_hit_and_eviction():
    """Verify cache hits, misses, LRU eviction, and stats."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(4)]
    array = Raid5(disks=disks)
    cache = LruBlockCache(array=array, capacity_blocks=3, write_back=False)

    cache.write_block(lbn=1, data=b"BLOCK_1")
    cache.write_block(lbn=2, data=b"BLOCK_2")
    cache.write_block(lbn=3, data=b"BLOCK_3")

    # Read block 1 and 2 (cached)
    assert cache.read_block(1).startswith(b"BLOCK_1")
    assert cache.read_block(2).startswith(b"BLOCK_2")

    # Put block 4 -> should evict block 3
    cache.write_block(lbn=4, data=b"BLOCK_4")

    # Stats: reads produced hits
    assert cache.stats["hits"] >= 2
    assert cache.hit_rate > 0.0


def test_lru_block_cache_write_back_flush():
    """Verify write-back dirty marking and flush callback."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(4)]
    array = Raid5(disks=disks)
    cache = LruBlockCache(array=array, capacity_blocks=4, write_back=True)

    cache.write_block(lbn=10, data=b"DIRTY_DATA_10")
    cache.write_block(lbn=11, data=b"DIRTY_DATA_11")

    assert cache.dirty_count == 2

    # Flush all dirty blocks to RAID
    flushed_count = cache.flush()
    assert flushed_count == 2
    assert cache.dirty_count == 0

    # Verify directly from underlying RAID array
    assert array.read_block(10).startswith(b"DIRTY_DATA_10")
    assert array.read_block(11).startswith(b"DIRTY_DATA_11")


def test_per_block_checksum_self_healing():
    """Verify ZFS-style per-block checksum detection of bit rot and self-healing."""
    disks = [VirtualDisk(i, num_blocks=16) for i in range(4)]
    array = Raid5(disks=disks)

    # Write initial data to LBN 0
    test_data = b"INTEGRITY_PROTECTED_DATA"
    array.write_block(0, test_data)

    # Initialize checksum manager and populate
    c_mgr = BlockChecksumManager()
    c_mgr.populate_from_array(array)

    mapping = array.map_lbn(0)
    target_disk = array.disks[mapping["disk"]]
    pbn = mapping["pbn"]

    # Verify initial block matches checksum
    assert c_mgr.verify(target_disk.disk_id, pbn, target_disk.read(pbn)) is True

    # Inject silent bit rot into disk directly (simulating disk bit rot without OS awareness)
    corrupted_data = bytearray(target_disk.read(pbn))
    corrupted_data[0] ^= 0xFF
    target_disk.blocks[pbn] = bytearray(corrupted_data)

    # Verification: checksum manager catches the corrupted block
    assert c_mgr.verify(target_disk.disk_id, pbn, target_disk.read(pbn)) is False

    # Scrub with repair=True and checksum_mgr
    scrub_res = scrub(array, repair=True, checksum_mgr=c_mgr)
    assert scrub_res["healed"] >= 1
    assert len(scrub_res["corrupted_blocks"]) >= 1

    # Block on disk must be restored to original test_data
    assert target_disk.read(pbn)[:len(test_data)] == test_data
    # Re-scrub should now be 100% clean
    clean_scrub = scrub(array, repair=False, checksum_mgr=c_mgr)
    assert len(clean_scrub["inconsistent"]) == 0
