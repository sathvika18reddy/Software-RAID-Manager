"""Workload generator for RAID simulation benchmarks."""

import random


def generate_workload(
    kind: str,
    n_requests: int,
    max_lbn: int,
    read_ratio: float = 0.7,
    seed: int = 42,
) -> list[dict]:
    """Generate a sequence of read/write requests.
    
    Args:
        kind: 'sequential', 'random', or 'mixed'
        n_requests: number of requests to generate
        max_lbn: upper bound for LBN (exclusive)
        read_ratio: proportion of read operations (0.0 to 1.0)
        seed: random seed for reproducibility
    
    Returns:
        list of {'op': 'r'|'w', 'lbn': int}
    """
    if max_lbn <= 0:
        raise ValueError("max_lbn must be greater than 0")

    rng = random.Random(seed)
    requests = []

    if kind == "sequential":
        curr_lbn = 0
        for _ in range(n_requests):
            op = "r" if rng.random() < read_ratio else "w"
            requests.append({"op": op, "lbn": curr_lbn})
            curr_lbn = (curr_lbn + 1) % max_lbn

    elif kind == "random":
        for _ in range(n_requests):
            op = "r" if rng.random() < read_ratio else "w"
            lbn = rng.randint(0, max_lbn - 1)
            requests.append({"op": op, "lbn": lbn})

    elif kind == "mixed":
        # Alternating chunks of 5 sequential requests followed by 5 random requests
        curr_lbn = 0
        is_seq = True
        for i in range(n_requests):
            if i > 0 and i % 5 == 0:
                is_seq = not is_seq
                if is_seq:
                    curr_lbn = rng.randint(0, max_lbn - 1)

            op = "r" if rng.random() < read_ratio else "w"
            if is_seq:
                lbn = curr_lbn
                curr_lbn = (curr_lbn + 1) % max_lbn
            else:
                lbn = rng.randint(0, max_lbn - 1)

            requests.append({"op": op, "lbn": lbn})

    else:
        raise ValueError(f"Unknown workload kind: {kind}. Choose 'sequential', 'random', or 'mixed'.")

    return requests


def parse_trace_csv(csv_content: str, max_lbn: int) -> list[dict]:
    """Parse a CSV trace string (header: op,lbn or op,pbn) into requests."""
    requests = []
    lines = csv_content.strip().splitlines()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        op_str, lbn_str = parts[0].lower(), parts[1]
        if op_str in ("op", "type", "operation"):
            continue  # header
        op = "r" if op_str in ("r", "read") else "w"
        try:
            raw_lbn = int(lbn_str)
            lbn = raw_lbn % max_lbn if max_lbn > 0 else 0
            requests.append({"op": op, "lbn": lbn})
        except ValueError:
            continue
    return requests


def get_sample_trace(name: str, max_lbn: int, n_requests: int = 40) -> list[dict]:
    """Return a pre-configured realistic storage workload trace."""
    norm = name.strip().upper()
    if norm == "OLTP":
        # OLTP: 80% random reads, 20% random writes across entire volume
        return generate_workload("random", n_requests=n_requests, max_lbn=max_lbn, read_ratio=0.8, seed=101)
    elif norm in ("WEB", "WEB-SERVER", "WEBSERVER"):
        # Web-Server: 90% sequential reads with periodic write logging
        return generate_workload("sequential", n_requests=n_requests, max_lbn=max_lbn, read_ratio=0.9, seed=202)
    elif norm in ("BATCH", "ANALYTICS"):
        # Mixed: 50% reads, 50% writes in sequential/random chunks
        return generate_workload("mixed", n_requests=n_requests, max_lbn=max_lbn, read_ratio=0.5, seed=303)
    else:
        return generate_workload("mixed", n_requests=n_requests, max_lbn=max_lbn, read_ratio=0.7, seed=42)

