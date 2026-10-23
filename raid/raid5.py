"""RAID 5 (Distributed Rotating Parity) implementation."""

import time
from raid.raid_base import RaidArray
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.locks import StripeLockManager
from raid.oplog import OperationLog


def xor_bytes(a: bytes, b: bytes) -> bytes:
    """Bitwise XOR between two byte sequences, padded to the larger length."""
    length = max(len(a), len(b))
    pa = a.ljust(length, b'\x00')
    pb = b.ljust(length, b'\x00')
    return bytes(x ^ y for x, y in zip(pa, pb))


class Raid5(RaidArray):
    """RAID 5: Block-level striping with rotating distributed parity."""

    def __init__(
        self,
        disks: list[VirtualDisk],
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager: StripeLockManager | None = None,
        scheduler_policy: str = "FCFS",
        artificial_delay_ms: float = 0.0,
    ):
        if len(disks) < 3:
            raise ValueError("RAID 5 requires at least 3 disks")
        if lock_manager is None:
            lock_manager = StripeLockManager(num_stripes=disks[0].num_blocks, enabled=True)
        super().__init__(disks, spares, oplog, lock_manager, scheduler_policy)
        self.data_per_stripe = self.num_disks - 1
        self.artificial_delay_ms = artificial_delay_ms

    def capacity_blocks(self) -> int:
        """Usable capacity is (N - 1) * disk_blocks."""
        return self.data_per_stripe * self.disk_blocks

    def map_lbn(self, lbn: int) -> dict:
        """Map logical block number (LBN) to stripe, data disk, parity disk, and PBN."""
        if lbn < 0 or lbn >= self.capacity_blocks():
            raise IndexError(f"LBN {lbn} out of bounds (0..{self.capacity_blocks() - 1})")

        stripe = lbn // self.data_per_stripe
        idx = lbn % self.data_per_stripe
        parity_disk_idx = (self.num_disks - 1) - (stripe % self.num_disks)
        data_disk_indices = [d for d in range(self.num_disks) if d != parity_disk_idx]
        data_disk_idx = data_disk_indices[idx]

        return {
            "lbn": lbn,
            "stripe": stripe,
            "pbn": stripe,
            "disk": data_disk_idx,
            "parity_disk": parity_disk_idx,
            "role": "data",
            "data_disks": data_disk_indices,
        }

    def read_block(self, lbn: int) -> bytes:
        """Read logical block. Performs on-the-fly degraded reconstruction if target disk is failed."""
        mapping = self.map_lbn(lbn)
        data_disk_idx = mapping["disk"]
        stripe = mapping["stripe"]
        target_disk = self.disks[data_disk_idx]

        # Normal read path
        if not target_disk.failed and not target_disk.needs_rebuild:
            data = target_disk.read(stripe)
            self.oplog.add(f"RAID5 Read LBN {lbn} from Disk {target_disk.disk_id} (stripe {stripe})", level="info")
            return data

        # Degraded read path: target disk is failed or rebuilding
        failed_count = sum(1 for d in self.disks if d.failed or d.needs_rebuild)
        if failed_count > 1:
            self.oplog.add(f"RAID5 Degraded read failed for LBN {lbn}: {failed_count} disks failed", level="error")
            raise DiskFailedError(f"RAID5 cannot reconstruct LBN {lbn}: {failed_count} disks unavailable")

        self.oplog.add(
            f"RAID5 Degraded read for LBN {lbn}: Disk {target_disk.disk_id} failed, reconstructing via parity XOR",
            level="warning",
        )

        reconstructed = bytes(self.block_size)
        for i, disk in enumerate(self.disks):
            if i != data_disk_idx:
                block_data = disk.read(stripe)
                reconstructed = xor_bytes(reconstructed, block_data)

        return reconstructed

    def write_block(self, lbn: int, data: bytes) -> None:
        """Write logical block using Read-Modify-Write (RMW) under stripe-level concurrency lock."""
        mapping = self.map_lbn(lbn)
        data_disk_idx = mapping["disk"]
        parity_disk_idx = mapping["parity_disk"]
        stripe = mapping["stripe"]
        data_disk_indices = mapping["data_disks"]

        data_disk = self.disks[data_disk_idx]
        parity_disk = self.disks[parity_disk_idx]

        with self.lock_manager.lock(stripe):
            failed_disks = [d for d in self.disks if d.failed]
            if len(failed_disks) > 1:
                self.oplog.add(f"RAID5 Write failed for LBN {lbn}: multiple disks failed", level="error")
                raise DiskFailedError("RAID5 cannot write: multiple disks failed")

            # Case A: Normal path (both data disk and parity disk healthy)
            if not data_disk.failed and not parity_disk.failed:
                old_data = data_disk.read(stripe)
                old_parity = parity_disk.read(stripe)

                if self.artificial_delay_ms > 0:
                    time.sleep(self.artificial_delay_ms / 1000.0)

                # new_parity = old_parity XOR old_data XOR new_data
                new_parity = xor_bytes(old_parity, xor_bytes(old_data, data))

                data_disk.write(stripe, data)
                parity_disk.write(stripe, new_parity)

                self.oplog.add(
                    f"RAID5 RMW write LBN {lbn}: Data->Disk {data_disk.disk_id}, Parity->Disk {parity_disk.disk_id}",
                    level="info",
                )

            # Case B: Data disk failed, parity disk healthy (degraded write)
            elif data_disk.failed and not parity_disk.failed:
                # Recompute parity from all other surviving data blocks + new data
                new_parity = bytes(data).ljust(self.block_size, b'\x00')[:self.block_size]
                for other_idx in data_disk_indices:
                    if other_idx != data_disk_idx:
                        other_data = self.disks[other_idx].read(stripe)
                        new_parity = xor_bytes(new_parity, other_data)

                parity_disk.write(stripe, new_parity)
                self.oplog.add(
                    f"RAID5 Degraded write LBN {lbn}: Data disk failed, updated parity on Disk {parity_disk.disk_id}",
                    level="warning",
                )

            # Case C: Parity disk failed, data disk healthy (degraded write)
            elif not data_disk.failed and parity_disk.failed:
                data_disk.write(stripe, data)
                self.oplog.add(
                    f"RAID5 Degraded write LBN {lbn}: Parity disk failed, wrote data to Disk {data_disk.disk_id}",
                    level="warning",
                )

    def rebuild(self, disk_id: int, progress_cb=None) -> bool:
        """Rebuild target disk by XORing all surviving blocks in each stripe."""
        target_idx = None
        for i, d in enumerate(self.disks):
            if d.disk_id == disk_id:
                target_idx = i
                break

        if target_idx is None:
            self.oplog.add(f"RAID5 Rebuild failed: Disk {disk_id} not found", level="error")
            return False

        target_disk = self.disks[target_idx]

        # Ensure no other disks are failed
        other_failed = [d for d in self.disks if d != target_disk and (d.failed or d.needs_rebuild)]
        if other_failed:
            self.oplog.add(f"RAID5 Rebuild failed: multiple disks unavailable", level="error")
            return False

        self.oplog.add(f"RAID5 Rebuilding Disk {disk_id} across {self.disk_blocks} stripes...", level="info")

        for stripe in range(self.disk_blocks):
            reconstructed = bytes(self.block_size)
            for i, disk in enumerate(self.disks):
                if i != target_idx:
                    block_data = disk.read(stripe)
                    reconstructed = xor_bytes(reconstructed, block_data)

            target_disk.write(stripe, reconstructed)
            if progress_cb:
                progress_cb(stripe + 1, self.disk_blocks)

        target_disk.needs_rebuild = False
        self.oplog.add(f"RAID5 Rebuild for Disk {disk_id} completed successfully.", level="info")
        return True

    def is_degraded(self) -> bool:
        """True if any disk is failed or undergoing rebuild."""
        return any(d.failed or d.needs_rebuild for d in self.disks)

    def io_count_for_write(self) -> dict:
        """RAID 5 small-write penalty: 2 reads + 2 writes per logical write."""
        return {"reads": 2, "writes": 2}

    def layout_snapshot(self) -> list[list[dict]]:
        """Return 2D grid [disk_idx][pbn] with block metadata."""
        grid = []
        for d_idx, disk in enumerate(self.disks):
            disk_blocks = []
            for stripe in range(self.disk_blocks):
                parity_disk_idx = (self.num_disks - 1) - (stripe % self.num_disks)
                data_disk_indices = [d for d in range(self.num_disks) if d != parity_disk_idx]

                if d_idx == parity_disk_idx:
                    kind = "parity"
                    lbn = None
                else:
                    kind = "data"
                    sub_idx = data_disk_indices.index(d_idx)
                    lbn = stripe * self.data_per_stripe + sub_idx

                if disk.failed:
                    state = "failed"
                elif disk.needs_rebuild:
                    state = "rebuilt"
                else:
                    state = "ok"

                disk_blocks.append({
                    "kind": kind,
                    "state": state,
                    "lbn": lbn,
                    "stripe": stripe,
                    "pbn": stripe,
                })
            grid.append(disk_blocks)
        return grid
