"""Parity scrubbing, integrity verification, and write-hole crash simulation."""

import hashlib
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.raid5 import xor_bytes


class SimulatedCrashError(Exception):
    """Raised to simulate an abrupt power loss or crash mid-write."""
    pass


def checksum_array(array, num_blocks: int | None = None) -> str:
    """Compute SHA-256 digest over logical blocks 0..num_blocks-1."""
    if num_blocks is None:
        num_blocks = array.capacity_blocks()
    hasher = hashlib.sha256()
    for lbn in range(num_blocks):
        block_data = array.read_block(lbn)
        hasher.update(block_data)
    return hasher.hexdigest()


def scrub(array, repair: bool = False) -> dict:
    """Scrub the array for parity or mirror inconsistencies. Optionally repair detected errors."""
    from raid.raid5 import Raid5
    from raid.raid1 import Raid1

    stripes_checked = 0
    inconsistent = []
    repaired = 0

    if isinstance(array, Raid5):
        stripes_checked = array.disk_blocks
        for stripe in range(array.disk_blocks):
            parity_disk_idx = (array.num_disks - 1) - (stripe % array.num_disks)
            parity_disk = array.disks[parity_disk_idx]

            # Recompute expected parity by XORing all data blocks in the stripe
            data_disks = [d for i, d in enumerate(array.disks) if i != parity_disk_idx]
            expected_parity = bytes(array.block_size)
            for d in data_disks:
                expected_parity = xor_bytes(expected_parity, d.read(stripe))

            stored_parity = parity_disk.read(stripe)
            if stored_parity != expected_parity:
                inconsistent.append(stripe)
                if repair:
                    parity_disk.write(stripe, expected_parity)
                    repaired += 1
                    array.oplog.add(f"Scrub repaired inconsistent parity on stripe {stripe}", level="info")

    elif isinstance(array, Raid1):
        stripes_checked = array.disk_blocks
        for pbn in range(array.disk_blocks):
            healthy = [d for d in array.disks if not d.failed and not d.needs_rebuild]
            if len(healthy) > 1:
                ref_data = healthy[0].read(pbn)
                mismatch = False
                for other in healthy[1:]:
                    if other.read(pbn) != ref_data:
                        mismatch = True
                        break
                if mismatch:
                    inconsistent.append(pbn)
                    if repair:
                        for other in healthy[1:]:
                            other.write(pbn, ref_data)
                        repaired += 1
                        array.oplog.add(f"Scrub repaired mirror mismatch on block {pbn}", level="info")

    result = {
        "stripes_checked": stripes_checked,
        "inconsistent": inconsistent,
        "repaired": repaired,
    }
    array.oplog.add(
        f"Scrub completed: {stripes_checked} checked, {len(inconsistent)} inconsistent, {repaired} repaired.",
        level="warning" if inconsistent else "info",
    )
    return result


def write_with_crash(array, lbn: int, data: bytes, crash_point: str = "after_data") -> None:
    """Perform a RAID 5 write but abruptly crash after writing the data block and before writing parity."""
    mapping = array.map_lbn(lbn)
    data_disk_idx = mapping["disk"]
    parity_disk_idx = mapping["parity_disk"]
    stripe = mapping["stripe"]

    data_disk = array.disks[data_disk_idx]
    parity_disk = array.disks[parity_disk_idx]

    # Acquire lock if present
    lock_mgr = getattr(array, "lock_manager", None)
    lock_ctx = lock_mgr.lock(stripe) if lock_mgr else None

    def _execute():
        # Step 1: Write data block
        data_disk.write(stripe, data)
        array.oplog.add(
            f"Write-with-crash: Data written to Disk {data_disk.disk_id} PBN {stripe}. Simulating crash now!",
            level="warning",
        )
        if crash_point == "after_data":
            raise SimulatedCrashError(f"System crashed mid-write at LBN {lbn} before parity update (Write-Hole injected)")

        # Step 2: Write parity (only reached if crash_point != 'after_data')
        old_data = data_disk.read(stripe)
        old_parity = parity_disk.read(stripe)
        new_parity = xor_bytes(old_parity, xor_bytes(old_data, data))
        parity_disk.write(stripe, new_parity)

    if lock_ctx:
        with lock_ctx:
            _execute()
    else:
        _execute()
