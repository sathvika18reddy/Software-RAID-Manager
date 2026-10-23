"""Configuration constants for Software RAID Manager."""

BLOCK_SIZE = 512          # bytes per block
DEFAULT_DISK_BLOCKS = 64  # blocks per disk (small so the UI stays readable)
SEEK_COST_PER_BLOCK = 0.5 # simulated ms per block of head movement
ROTATIONAL_DELAY = 2.0    # simulated ms
TRANSFER_TIME = 0.1       # simulated ms per block
