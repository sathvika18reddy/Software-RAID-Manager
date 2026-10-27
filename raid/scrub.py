"""Parity scrubbing, integrity verification, and write-hole crash simulation."""

import hashlib


class SimulatedCrashError(Exception):
    """Raised to simulate an abrupt power loss or crash mid-write."""
    pass


class ScrubUnavailableError(Exception):
    """Raised when parity scrub cannot run because a member disk is failed."""
    pass


class BlockChecksumManager:
    """ZFS-style block-level cryptographic checksum tracking.
    
    Tracks independent SHA-256 checksums per physical block (disk_id, pbn) to detect
    silent data corruption (bit rot). When parity in a stripe is inconsistent,
    the checksum manager pinpoints the exact corrupted block, allowing targeted
    self-healing reconstruction from surviving disks + parity.
    """

    def __init__(self):
        self._checksums: dict[tuple[int, int], str] = {}

    def record(self, disk_id: int, pbn: int, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        self._checksums[(disk_id, pbn)] = digest
        return digest

    def verify(self, disk_id: int, pbn: int, data: bytes) -> bool:
        expected = self._checksums.get((disk_id, pbn))
        if expected is None:
            return True
        return hashlib.sha256(data).hexdigest() == expected

    def get(self, disk_id: int, pbn: int) -> str | None:
        return self._checksums.get((disk_id, pbn))

    def clear(self) -> None:
        self._checksums.clear()

    def populate_from_array(self, array) -> None:
        """Hash all physical blocks across all healthy online disks."""
        for disk in array.disks:
            if not disk.failed and not disk.needs_rebuild:
                for pbn in range(disk.num_blocks):
                    data = disk.read(pbn)
                    self.record(disk.disk_id, pbn, data)


def checksum_array(array, num_blocks: int | None = None) -> str:
    """Compute SHA-256 digest over logical blocks 0..num_blocks-1."""
    if num_blocks is None:
        num_blocks = array.capacity_blocks()
    hasher = hashlib.sha256()
    for lbn in range(num_blocks):
        block_data = array.read_block(lbn)
        hasher.update(block_data)
    return hasher.hexdigest()


def scrub(array, repair: bool = False, checksum_mgr: BlockChecksumManager | None = None) -> dict:
    """Scrub the array for parity or mirror inconsistencies. Optionally repair detected errors."""
    from raid.raid5 import Raid5
    from raid.raid1 import Raid1
    from raid.raid10 import Raid10

    stripes_checked = 0
    inconsistent = []
    repaired = 0
    corrupted_blocks = []
    healed = 0

    if array.is_degraded():
        unavailable_disks = [d for d in array.disks if d.failed or d.needs_rebuild]
        ids = ", ".join(str(d.disk_id) for d in unavailable_disks) if unavailable_disks else "unknown"
        msg = (
            f"Scrub cannot run on a degraded array. Scrub needs all disks online. "
            f"Replace and rebuild the failed disk first (offline/rebuilding disk(s): {ids})."
        )
        array.oplog.add(msg, level="warning")
        raise ScrubUnavailableError(msg)

    from raid.virtual_disk import DiskFailedError

    try:
        if isinstance(array, Raid5):
            from raid.raid5 import xor_bytes

            chunk_k = getattr(array, "chunk_size", 1)
            stripes_checked = array.disk_blocks // chunk_k
            for pbn in range(array.disk_blocks):
                stripe = pbn // chunk_k
                parity_disk_idx = (array.num_disks - 1) - (stripe % array.num_disks)
                parity_disk = array.disks[parity_disk_idx]

                # Recompute expected parity by XORing all data blocks in the stripe
                data_disks = [d for i, d in enumerate(array.disks) if i != parity_disk_idx]
                expected_parity = bytes(array.block_size)
                for d in data_disks:
                    expected_parity = xor_bytes(expected_parity, d.read(pbn))

                stored_parity = parity_disk.read(pbn)

                # Checksum-assisted silent bit-rot verification
                corrupt_in_stripe = []
                if checksum_mgr:
                    for d in array.disks:
                        bdata = d.read(pbn)
                        if not checksum_mgr.verify(d.disk_id, pbn, bdata):
                            corrupt_in_stripe.append(d)
                            corrupted_blocks.append({"disk_id": d.disk_id, "pbn": pbn, "stripe": stripe})

                if stored_parity != expected_parity or corrupt_in_stripe:
                    if stripe not in inconsistent:
                        inconsistent.append(stripe)
                    if repair:
                        if checksum_mgr and corrupt_in_stripe:
                            for bad_disk in corrupt_in_stripe:
                                if bad_disk.disk_id == parity_disk.disk_id:
                                    # Parity disk is the corrupted block
                                    parity_disk.write(pbn, expected_parity)
                                    checksum_mgr.record(parity_disk.disk_id, pbn, expected_parity)
                                    repaired += 1
                                    healed += 1
                                    array.oplog.add(f"Scrub self-healed parity on Disk {parity_disk.disk_id} PBN {pbn}", level="info")
                                else:
                                    # Data disk is corrupted: reconstruct via XOR of all other disks + parity
                                    healed_data = stored_parity
                                    for od in data_disks:
                                        if od.disk_id != bad_disk.disk_id:
                                            healed_data = xor_bytes(healed_data, od.read(pbn))
                                    bad_disk.write(pbn, healed_data)
                                    checksum_mgr.record(bad_disk.disk_id, pbn, healed_data)
                                    repaired += 1
                                    healed += 1
                                    array.oplog.add(f"Scrub self-healed bit-rot on Data Disk {bad_disk.disk_id} PBN {pbn}", level="info")
                        else:
                            # Standard scrub repair: update parity to match data
                            parity_disk.write(pbn, expected_parity)
                            repaired += 1
                            if checksum_mgr:
                                checksum_mgr.record(parity_disk.disk_id, pbn, expected_parity)
                            array.oplog.add(f"Scrub repaired inconsistent parity on stripe {stripe} PBN {pbn}", level="info")

        elif isinstance(array, Raid1):
            stripes_checked = array.disk_blocks
            for pbn in range(array.disk_blocks):
                healthy = [d for d in array.disks if not d.failed and not d.needs_rebuild]
                if len(healthy) > 1:
                    ref_disk = healthy[0]
                    ref_data = ref_disk.read(pbn)
                    mismatch = False
                    for other in healthy[1:]:
                        if other.read(pbn) != ref_data:
                            mismatch = True
                            break
                    if mismatch:
                        inconsistent.append(pbn)
                        if repair:
                            # If checksum_mgr is available, identify healthy disk
                            target_good_data = ref_data
                            if checksum_mgr:
                                for cand in healthy:
                                    if checksum_mgr.verify(cand.disk_id, pbn, cand.read(pbn)):
                                        target_good_data = cand.read(pbn)
                                        break
                            for other in healthy:
                                if other.read(pbn) != target_good_data:
                                    other.write(pbn, target_good_data)
                                    if checksum_mgr:
                                        checksum_mgr.record(other.disk_id, pbn, target_good_data)
                            repaired += 1
                            healed += 1
                            array.oplog.add(f"Scrub repaired mirror mismatch on block {pbn}", level="info")

        elif isinstance(array, Raid10):
            stripes_checked = array.disk_blocks
            for pbn in range(array.disk_blocks):
                for p_idx in range(array.num_pairs):
                    d0 = array.disks[2 * p_idx]
                    d1 = array.disks[2 * p_idx + 1]
                    b0 = d0.read(pbn)
                    b1 = d1.read(pbn)
                    if b0 != b1:
                        inconsistent.append(pbn)
                        if repair:
                            # Pick valid disk via checksum if available, else d0
                            good = b0
                            if checksum_mgr:
                                if checksum_mgr.verify(d1.disk_id, pbn, b1) and not checksum_mgr.verify(d0.disk_id, pbn, b0):
                                    good = b1
                                    d0.write(pbn, good)
                                    checksum_mgr.record(d0.disk_id, pbn, good)
                                else:
                                    d1.write(pbn, good)
                                    checksum_mgr.record(d1.disk_id, pbn, good)
                            else:
                                d1.write(pbn, good)
                            repaired += 1
                            healed += 1
                            array.oplog.add(f"Scrub repaired RAID 10 pair {p_idx} mismatch on PBN {pbn}", level="info")

    except DiskFailedError as e:
        msg = f"Scrub needs all disks online. Replace and rebuild the failed disk first: {e}"
        array.oplog.add(msg, level="warning")
        raise ScrubUnavailableError(msg) from e

    result = {
        "stripes_checked": stripes_checked,
        "inconsistent": inconsistent,
        "repaired": repaired,
        "corrupted_blocks": corrupted_blocks,
        "healed": healed,
    }
    array.oplog.add(
        f"Scrub completed: {stripes_checked} checked, {len(inconsistent)} inconsistent, {repaired} repaired.",
        level="warning" if inconsistent else "info",
    )
    return result


def write_with_crash(array, lbn: int, data: bytes, crash_point: str = "after_data") -> None:
    """Perform a RAID 5 write but abruptly crash after writing the data block and before writing parity."""
    from raid.raid5 import xor_bytes

    mapping = array.map_lbn(lbn)
    data_disk_idx = mapping["disk"]
    parity_disk_idx = mapping["parity_disk"]
    stripe = mapping["stripe"]
    pbn = mapping["pbn"]

    data_disk = array.disks[data_disk_idx]
    parity_disk = array.disks[parity_disk_idx]

    # Acquire lock if present
    lock_mgr = getattr(array, "lock_manager", None)
    lock_ctx = lock_mgr.lock(stripe) if lock_mgr else None

    def _execute():
        # Step 1: Write data block
        data_disk.write(pbn, data)
        array.oplog.add(
            f"Write-with-crash: Data written to Disk {data_disk.disk_id} PBN {pbn}. Simulating crash now!",
            level="warning",
        )
        if crash_point == "after_data":
            raise SimulatedCrashError(f"System crashed mid-write at LBN {lbn} before parity update (Write-Hole injected)")

        # Step 2: Write parity (only reached if crash_point != 'after_data')
        old_data = data_disk.read(pbn)
        old_parity = parity_disk.read(pbn)
        new_parity = xor_bytes(old_parity, xor_bytes(old_data, data))
        parity_disk.write(pbn, new_parity)

    if lock_ctx:
        with lock_ctx:
            _execute()
    else:
        _execute()
