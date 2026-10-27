"""RAID 6: Dual Distributed Parity (P + Q) using Galois Field GF(2^8).

Tolerates any 2 simultaneous disk failures with distributed Reed-Solomon style
dual parity. Uses Linux MD RAID 6 generator g=2 over GF(2^8) (polynomial 0x11d).
"""

import time
from raid.virtual_disk import VirtualDisk, DiskFailedError
from raid.oplog import OperationLog
from raid.raid_base import RaidBase
from raid.galois import (
    gf_pow,
    gf_inv,
    gf_mul,
    compute_p_parity,
    compute_q_parity,
    solve_two_disk_failures,
)


class Raid6(RaidBase):
    """RAID 6 array with rotating P and Q dual parity."""

    def __init__(
        self,
        disks: list[VirtualDisk] | None = None,
        spares: list[VirtualDisk] | None = None,
        oplog: OperationLog | None = None,
        lock_manager=None,
        scheduler_policy: str = "FCFS",
        chunk_size: int = 1,
    ):
        if disks is not None and len(disks) < 4:
            raise ValueError("RAID 6 requires at least 4 disks (2 data + 2 parity).")
        super().__init__(
            disks=disks,
            spares=spares,
            oplog=oplog,
            lock_manager=lock_manager,
            scheduler_policy=scheduler_policy,
        )
        self.chunk_size = max(1, chunk_size)

    def capacity_blocks(self) -> int:
        num_stripes = self.disk_blocks // self.chunk_size
        return num_stripes * (self.num_disks - 2) * self.chunk_size

    def is_degraded(self) -> bool:
        failed_count = sum(1 for d in self.disks if d.failed or d.needs_rebuild)
        return failed_count > 0

    def _stripe_layout(self, stripe: int) -> tuple[int, int, list[int]]:
        """Return (p_disk_idx, q_disk_idx, list_of_data_disk_indices)."""
        p_idx = (self.num_disks - 1) - (stripe % self.num_disks)
        q_idx = (self.num_disks - 2) - (stripe % self.num_disks)
        if q_idx < 0:
            q_idx += self.num_disks
        data_indices = [i for i in range(self.num_disks) if i != p_idx and i != q_idx]
        return p_idx, q_idx, data_indices

    def map_lbn(self, lbn: int) -> dict:
        num_data = self.num_disks - 2
        stripe = lbn // (num_data * self.chunk_size)
        stripe_offset = lbn % (num_data * self.chunk_size)
        data_disk_idx_in_stripe = stripe_offset // self.chunk_size
        chunk_offset = stripe_offset % self.chunk_size
        pbn = stripe * self.chunk_size + chunk_offset

        p_idx, q_idx, data_indices = self._stripe_layout(stripe)
        target_disk_idx = data_indices[data_disk_idx_in_stripe]

        return {
            "lbn": lbn,
            "stripe": stripe,
            "pbn": pbn,
            "disk": target_disk_idx,
            "data_disk_index": data_disk_idx_in_stripe,
            "p_disk": p_idx,
            "q_disk": q_idx,
            "data_disks": data_indices,
            "chunk_offset": chunk_offset,
        }

    def _reconstruct_stripe(self, stripe: int, chunk_offset: int) -> dict[int, bytes]:
        """Reconstruct any missing blocks in the stripe (supporting up to 2 failures)."""
        p_idx, q_idx, data_indices = self._stripe_layout(stripe)
        pbn = stripe * self.chunk_size + chunk_offset

        failed_disks = [i for i, d in enumerate(self.disks) if d.failed or d.needs_rebuild]
        if len(failed_disks) > 2:
            raise DiskFailedError(f"RAID 6 cannot recover: {len(failed_disks)} disks failed (>2 tolerance)")

        blocks: dict[int, bytes] = {}
        # Read all healthy disks
        for i, d in enumerate(self.disks):
            if i not in failed_disks:
                blocks[i] = d.read(pbn)

        if not failed_disks:
            return blocks

        # Case: 1 failure
        if len(failed_disks) == 1:
            failed_idx = failed_disks[0]
            if failed_idx == q_idx:
                # Recompute Q from data
                data_blks = [blocks[di] for di in data_indices]
                blocks[q_idx] = compute_q_parity(data_blks)
            elif failed_idx == p_idx:
                # Recompute P from data
                data_blks = [blocks[di] for di in data_indices]
                blocks[p_idx] = compute_p_parity(data_blks)
            else:
                # One data disk failed
                data_pos = data_indices.index(failed_idx)
                if p_idx not in failed_disks:
                    # Reconstruct via P parity (standard XOR)
                    p_val = bytearray(blocks[p_idx])
                    for di in data_indices:
                        if di != failed_idx:
                            other = blocks[di]
                            for b_i in range(self.block_size):
                                p_val[b_i] ^= other[b_i]
                    blocks[failed_idx] = bytes(p_val)
                else:
                    # P failed and Dx failed -> Reconstruct Dx via Q parity
                    # Q = sum(g^i * D_i) => g^x * Dx = Q ^ sum_{i!=x}(g^i * D_i)
                    gx = gf_pow(2, data_pos)
                    inv_gx = gf_inv(gx)
                    q_syn = bytearray(blocks[q_idx])
                    for pos, di in enumerate(data_indices):
                        if di != failed_idx:
                            coeff = gf_pow(2, pos)
                            bdata = blocks[di]
                            for b_i in range(self.block_size):
                                q_syn[b_i] ^= gf_mul(coeff, bdata[b_i])
                    # Multiply by inv_gx
                    dx = bytearray(self.block_size)
                    for b_i in range(self.block_size):
                        dx[b_i] = gf_mul(inv_gx, q_syn[b_i])
                    blocks[failed_idx] = bytes(dx)

        # Case: 2 failures
        elif len(failed_disks) == 2:
            f1, f2 = failed_disks
            # Subcase A: Both P and Q failed -> Data intact, just regenerate parities!
            if set(failed_disks) == {p_idx, q_idx}:
                data_blks = [blocks[di] for di in data_indices]
                blocks[p_idx] = compute_p_parity(data_blks)
                blocks[q_idx] = compute_q_parity(data_blks)

            # Subcase B: 1 data disk and 1 parity disk failed
            elif p_idx in failed_disks or q_idx in failed_disks:
                failed_data_idx = f1 if f1 not in (p_idx, q_idx) else f2
                failed_parity_idx = f1 if f1 in (p_idx, q_idx) else f2
                data_pos = data_indices.index(failed_data_idx)

                if failed_parity_idx == q_idx:
                    # Q failed, P healthy -> reconstruct data via P parity
                    p_val = bytearray(blocks[p_idx])
                    for di in data_indices:
                        if di != failed_data_idx:
                            other = blocks[di]
                            for b_i in range(self.block_size):
                                p_val[b_i] ^= other[b_i]
                    blocks[failed_data_idx] = bytes(p_val)
                    # Now recompute Q
                    data_blks = [blocks[di] for di in data_indices]
                    blocks[q_idx] = compute_q_parity(data_blks)
                else:
                    # P failed, Q healthy -> reconstruct data via Q parity
                    gx = gf_pow(2, data_pos)
                    inv_gx = gf_inv(gx)
                    q_syn = bytearray(blocks[q_idx])
                    for pos, di in enumerate(data_indices):
                        if di != failed_data_idx:
                            coeff = gf_pow(2, pos)
                            bdata = blocks[di]
                            for b_i in range(self.block_size):
                                q_syn[b_i] ^= gf_mul(coeff, bdata[b_i])
                    dx = bytearray(self.block_size)
                    for b_i in range(self.block_size):
                        dx[b_i] = gf_mul(inv_gx, q_syn[b_i])
                    blocks[failed_data_idx] = bytes(dx)
                    # Now recompute P
                    data_blks = [blocks[di] for di in data_indices]
                    blocks[p_idx] = compute_p_parity(data_blks)

            # Subcase C: 2 data disks failed (Dx and Dy), P and Q both healthy!
            else:
                pos_x = data_indices.index(f1)
                pos_y = data_indices.index(f2)
                if pos_x > pos_y:
                    pos_x, pos_y = pos_y, pos_x
                    f1, f2 = f2, f1

                # Compute P_syn and Q_syn from surviving data disks
                p_syn = bytearray(blocks[p_idx])
                q_syn = bytearray(blocks[q_idx])

                for pos, di in enumerate(data_indices):
                    if di != f1 and di != f2:
                        bdata = blocks[di]
                        coeff = gf_pow(2, pos)
                        for b_i in range(self.block_size):
                            p_syn[b_i] ^= bdata[b_i]
                            q_syn[b_i] ^= gf_mul(coeff, bdata[b_i])

                dx, dy = solve_two_disk_failures(bytes(p_syn), bytes(q_syn), pos_x, pos_y)
                blocks[f1] = dx
                blocks[f2] = dy

        return blocks

    def read_block(self, lbn: int) -> bytes:
        mapping = self.map_lbn(lbn)
        target_disk = self.disks[mapping["disk"]]
        pbn = mapping["pbn"]

        if not target_disk.failed and not target_disk.needs_rebuild:
            return target_disk.read(pbn)

        # Degraded read
        self.oplog.add(f"Degraded read on LBN {lbn} (Disk {target_disk.disk_id} unavailable)", level="warning")
        stripe_blocks = self._reconstruct_stripe(mapping["stripe"], mapping["chunk_offset"])
        return stripe_blocks[mapping["disk"]]

    def write_block(self, lbn: int, data: bytes) -> None:
        mapping = self.map_lbn(lbn)
        stripe = mapping["stripe"]
        pbn = mapping["pbn"]
        target_idx = mapping["disk"]
        p_idx = mapping["p_disk"]
        q_idx = mapping["q_disk"]
        chunk_offset = mapping["chunk_offset"]

        lock_ctx = self.lock_manager.lock(stripe) if self.lock_manager else None

        def _execute():
            # Get full stripe blocks (reconstructing if any disk is degraded)
            stripe_blocks = self._reconstruct_stripe(stripe, chunk_offset)
            # Update target data
            if len(data) < self.block_size:
                padded = data.ljust(self.block_size, b"\x00")
            else:
                padded = data[: self.block_size]
            stripe_blocks[target_idx] = padded

            # Recompute P and Q
            data_blks = [stripe_blocks[di] for di in mapping["data_disks"]]
            new_p = compute_p_parity(data_blks)
            new_q = compute_q_parity(data_blks)

            # Write updated blocks to healthy disks
            if not self.disks[target_idx].failed and not self.disks[target_idx].needs_rebuild:
                self.disks[target_idx].write(pbn, padded)
            if not self.disks[p_idx].failed and not self.disks[p_idx].needs_rebuild:
                self.disks[p_idx].write(pbn, new_p)
            if not self.disks[q_idx].failed and not self.disks[q_idx].needs_rebuild:
                self.disks[q_idx].write(pbn, new_q)

            self.oplog.add(f"Wrote LBN {lbn} -> Disk {target_idx} PBN {pbn}, P-Disk {p_idx}, Q-Disk {q_idx}", level="info")

        if lock_ctx:
            with lock_ctx:
                _execute()
        else:
            _execute()

    def rebuild(self, disk_id: int, progress_cb=None, rate_limit_delay_ms: float = 0.0) -> bool:
        target_disk = None
        target_idx = -1
        for idx, d in enumerate(self.disks):
            if d.disk_id == disk_id:
                target_disk = d
                target_idx = idx
                break

        if target_disk is None:
            self.oplog.add(f"Cannot rebuild Disk {disk_id}: not found in array", level="warning")
            return False

        # Check if too many other disks are failed (> 1 other failure)
        other_failed = [i for i, d in enumerate(self.disks) if i != target_idx and (d.failed or d.needs_rebuild)]
        if len(other_failed) > 1:
            self.oplog.add(f"Cannot rebuild Disk {disk_id}: multiple other disks failed ({len(other_failed)})", level="error")
            return False

        self.oplog.add(f"Starting RAID 6 rebuild on Disk {disk_id}...", level="info")
        num_stripes = self.disk_blocks // self.chunk_size

        for stripe in range(num_stripes):
            for c_offset in range(self.chunk_size):
                pbn = stripe * self.chunk_size + c_offset
                stripe_blks = self._reconstruct_stripe(stripe, c_offset)
                reconstructed_data = stripe_blks[target_idx]
                target_disk.blocks[pbn] = bytearray(reconstructed_data)

            if progress_cb:
                progress_cb(stripe + 1, num_stripes)
            if rate_limit_delay_ms > 0:
                time.sleep(rate_limit_delay_ms / 1000.0)

        target_disk.needs_rebuild = False
        target_disk.failed = False
        self.oplog.add(f"RAID 6 rebuild complete for Disk {disk_id}.", level="info")
        return True

    def io_count_for_write(self) -> dict:
        return {"reads": 3, "writes": 3}

    def layout_snapshot(self) -> list[list[dict]]:
        grid = []
        num_stripes = self.disk_blocks // self.chunk_size
        num_data = self.num_disks - 2

        for disk_idx in range(self.num_disks):
            col = []
            disk = self.disks[disk_idx]
            for pbn in range(self.disk_blocks):
                stripe = pbn // self.chunk_size
                chunk_offset = pbn % self.chunk_size
                p_idx, q_idx, data_indices = self._stripe_layout(stripe)

                if disk_idx == p_idx:
                    kind = "parity"
                    label = f"P{stripe}"
                    lbn = None
                elif disk_idx == q_idx:
                    kind = "parity_q"
                    label = f"Q{stripe}"
                    lbn = None
                else:
                    kind = "data"
                    data_pos = data_indices.index(disk_idx)
                    lbn = stripe * (num_data * self.chunk_size) + data_pos * self.chunk_size + chunk_offset
                    label = f"L{lbn}"

                state = "failed" if disk.failed else ("rebuilt" if disk.needs_rebuild else "ok")
                col.append({
                    "disk_id": disk.disk_id,
                    "pbn": pbn,
                    "lbn": lbn,
                    "stripe": stripe,
                    "kind": kind,
                    "state": state,
                    "label": label,
                })
            grid.append(col)
        return grid
