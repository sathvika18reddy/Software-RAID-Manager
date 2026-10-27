"""Galois Field GF(2^8) arithmetic for RAID 6 dual parity (P + Q).

Implements finite field arithmetic over GF(2^8) using polynomial 0x11d
(x^8 + x^4 + x^3 + x^2 + 1) with generator g = 2, matching Linux MD RAID 6.
"""

# Precomputed exponential and logarithm tables for GF(2^8)
GF_EXP = [0] * 512
GF_LOG = [0] * 256

def _init_tables():
    poly = 0x11d  # x^8 + x^4 + x^3 + x^2 + 1
    val = 1
    for i in range(255):
        GF_EXP[i] = val
        GF_EXP[i + 255] = val
        GF_LOG[val] = i
        val <<= 1
        if val & 0x100:
            val ^= poly
    GF_LOG[0] = 0  # undefined mathematically, clamped to 0

_init_tables()


def gf_add(a: int, b: int) -> int:
    """Addition in GF(2^8) is bitwise XOR."""
    return a ^ b


def gf_sub(a: int, b: int) -> int:
    """Subtraction in GF(2^8) is identical to addition (bitwise XOR)."""
    return a ^ b


def gf_mul(a: int, b: int) -> int:
    """Multiplication in GF(2^8) via log/exp lookup tables."""
    if a == 0 or b == 0:
        return 0
    return GF_EXP[GF_LOG[a] + GF_LOG[b]]


def gf_div(a: int, b: int) -> int:
    """Division in GF(2^8). Raises ZeroDivisionError if b == 0."""
    if b == 0:
        raise ZeroDivisionError("GF(2^8) division by zero")
    if a == 0:
        return 0
    return GF_EXP[(GF_LOG[a] - GF_LOG[b] + 255) % 255]


def gf_inv(a: int) -> int:
    """Multiplicative inverse in GF(2^8)."""
    if a == 0:
        raise ZeroDivisionError("GF(2^8) inversion of zero")
    return GF_EXP[255 - GF_LOG[a]]


def gf_pow(a: int, power: int) -> int:
    """Compute a^power in GF(2^8)."""
    if a == 0:
        return 0 if power > 0 else 1
    return GF_EXP[(GF_LOG[a] * power) % 255]


def gf_mul_block(coeff: int, block: bytes) -> bytes:
    """Multiply every byte of block by coeff in GF(2^8)."""
    if coeff == 0:
        return bytes(len(block))
    if coeff == 1:
        return bytes(block)
    exp_c = GF_LOG[coeff]
    exp_table = GF_EXP
    log_table = GF_LOG
    return bytes(
        exp_table[exp_c + log_table[b]] if b != 0 else 0
        for b in block
    )


def compute_p_parity(blocks: list[bytes]) -> bytes:
    """Compute P parity (standard XOR over all data blocks)."""
    if not blocks:
        return b""
    res = bytearray(blocks[0])
    for b in blocks[1:]:
        for i in range(len(res)):
            res[i] ^= b[i]
    return bytes(res)


def compute_q_parity(blocks: list[bytes]) -> bytes:
    """Compute Q parity: Q = sum_i (g^i * D_i) in GF(2^8) where g = 2."""
    if not blocks:
        return b""
    block_len = len(blocks[0])
    q = bytearray(block_len)
    exp_table = GF_EXP
    log_table = GF_LOG

    for i, b in enumerate(blocks):
        coeff = gf_pow(2, i)  # g^i
        if coeff == 0:
            continue
        exp_c = log_table[coeff]
        for idx in range(block_len):
            val = b[idx]
            if val != 0:
                q[idx] ^= exp_table[exp_c + log_table[val]]
    return bytes(q)


def solve_two_disk_failures(
    p_syn: bytes,
    q_syn: bytes,
    data_idx_x: int,
    data_idx_y: int,
) -> tuple[bytes, bytes]:
    """Solve the 2x2 syndrome system for two failed data disks Dx and Dy (x < y).
    
    Given:
      Dx ^ Dy = P_syn
      (g^x * Dx) ^ (g^y * Dy) = Q_syn
      
    Multiplying the first equation by g^x:
      (g^x * Dx) ^ (g^x * Dy) = g^x * P_syn
      
    Adding to the second equation:
      (g^x ^ g^y) * Dy = Q_syn ^ (g^x * P_syn)
      
    Therefore:
      Dy = [ Q_syn ^ (g^x * P_syn) ] / (g^x ^ g^y)
      Dx = P_syn ^ Dy
    """
    gx = gf_pow(2, data_idx_x)
    gy = gf_pow(2, data_idx_y)
    denom = gx ^ gy
    inv_denom = gf_inv(denom)

    block_len = len(p_syn)
    dx = bytearray(block_len)
    dy = bytearray(block_len)

    exp_table = GF_EXP
    log_table = GF_LOG
    exp_gx = log_table[gx]
    exp_inv = log_table[inv_denom]

    for i in range(block_len):
        p_val = p_syn[i]
        q_val = q_syn[i]

        # term1 = g^x * P_syn[i]
        term1 = exp_table[exp_gx + log_table[p_val]] if p_val != 0 else 0
        numerator = q_val ^ term1

        # dy_val = numerator * inv_denom
        dy_val = exp_table[exp_inv + log_table[numerator]] if numerator != 0 else 0
        dx_val = p_val ^ dy_val

        dy[i] = dy_val
        dx[i] = dx_val

    return bytes(dx), bytes(dy)
