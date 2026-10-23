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
