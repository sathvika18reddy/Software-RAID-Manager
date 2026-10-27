"""Reliability Simulation: Monte Carlo Data-Loss Analysis for RAID Arrays."""

from dataclasses import dataclass
import numpy as np
import pandas as pd


@dataclass
class ReliabilityParams:
    """Parameters for Monte Carlo RAID reliability simulation."""
    level: str                  # "RAID0" | "RAID1" | "RAID5"
    num_disks: int
    mttf_hours: float           # mean time to failure of one disk (hours)
    rebuild_hours: float        # duration required to reconstruct a failed disk (hours)
    mission_hours: float        # operational mission time (hours, e.g. 43,800 for 5 years)
    n_trials: int = 10000       # number of Monte Carlo trials
    seed: int | None = 42       # random seed for reproducibility
    p_ure_per_rebuild: float = 0.0  # optional unrecoverable read error probability during rebuild


@dataclass
class ReliabilityResult:
    """Aggregated output from a reliability simulation."""
    params: ReliabilityParams
    losses: int
    p_loss: float
    ci_low: float
    ci_high: float
    mean_time_to_loss: float | None


def tolerance(level: str, num_disks: int) -> int:
    """Return maximum number of concurrently failed disks tolerated without data loss."""
    lvl = level.upper().replace(" ", "").replace("-", "")
    if lvl == "RAID0":
        return 0
    elif lvl == "RAID1":
        return max(0, num_disks - 1)
    elif lvl == "RAID5":
        return 1
    elif lvl in ("RAID10", "RAID1+0", "RAID01"):
        return 1  # 1 disk guaranteed; up to N/2 if in different pairs
    elif lvl == "RAID6":
        return 2
    raise ValueError(f"Unknown RAID level: '{level}'. Supported levels: 'RAID0', 'RAID1', 'RAID5', 'RAID6', 'RAID10'.")


def simulate_trial(params: ReliabilityParams, rng: np.random.Generator) -> tuple[bool, float | None]:
    """Simulate a single mission lifetime across all disks in the array.
    
    Returns:
        (lost, time_of_loss): True and timestamp of data loss if tolerance is exceeded,
                              else (False, None).
    """
    tol = tolerance(params.level, params.num_disks)
    events: list[tuple[float, int]] = []
    earliest_ure_loss: float | None = None

    for _ in range(params.num_disks):
        t = 0.0
        while True:
            # Time until next failure
            dt = rng.exponential(scale=params.mttf_hours)
            t += dt
            if t >= params.mission_hours:
                break

            # Disk failure start event
            events.append((t, +1))

            # Optional Unrecoverable Read Error (URE) during rebuild for RAID 5
            if params.p_ure_per_rebuild > 0.0 and params.level.upper().replace(" ", "") in ("RAID5", "RAID-5"):
                if rng.random() < params.p_ure_per_rebuild:
                    if earliest_ure_loss is None or t < earliest_ure_loss:
                        earliest_ure_loss = t

            down_end = t + params.rebuild_hours
            events.append((down_end, -1))
            t = down_end

    # Sort events deterministically: by time, then recovery (-1) before failure (+1)
    events.sort(key=lambda e: (e[0], e[1]))

    disks_down = 0
    for time_ev, delta in events:
        if time_ev >= params.mission_hours:
            break

        # Check if URE triggered before or at this time
        if earliest_ure_loss is not None and earliest_ure_loss <= time_ev:
            return True, earliest_ure_loss

        disks_down += delta
        if disks_down > tol:
            return True, time_ev

    if earliest_ure_loss is not None and earliest_ure_loss < params.mission_hours:
        return True, earliest_ure_loss

    return False, None


def run_simulation(params: ReliabilityParams) -> ReliabilityResult:
    """Execute n_trials Monte Carlo simulations and return statistical estimates."""
    rng = np.random.default_rng(params.seed)
    losses = 0
    loss_times = []

    for _ in range(params.n_trials):
        lost, t_loss = simulate_trial(params, rng)
        if lost:
            losses += 1
            if t_loss is not None:
                loss_times.append(t_loss)

    p_loss = losses / params.n_trials if params.n_trials > 0 else 0.0
    # 95% Confidence Interval using normal approximation: p ± 1.96 * sqrt(p(1-p)/n)
    margin = 1.96 * np.sqrt(p_loss * (1.0 - p_loss) / params.n_trials) if params.n_trials > 0 else 0.0
    ci_low = max(0.0, p_loss - margin)
    ci_high = min(1.0, p_loss + margin)
    mean_time = float(np.mean(loss_times)) if loss_times else None

    return ReliabilityResult(
        params=params,
        losses=losses,
        p_loss=p_loss,
        ci_low=ci_low,
        ci_high=ci_high,
        mean_time_to_loss=mean_time,
    )


# --- Analytical Cross-Check Formulations ---

def analytic_mttdl_raid5(mttf: float, rebuild: float, n: int) -> float:
    """Compute approximate Mean Time to Data Loss (MTTDL) for RAID 5.
    
    Formula (rebuild << MTTF):
        MTTDL ≈ MTTF^2 / (N * (N - 1) * rebuild)
    """
    if rebuild <= 0.0:
        return float("inf")
    if n < 2 or mttf <= 0.0:
        return 0.0
    return (mttf ** 2) / (n * (n - 1) * rebuild)


def analytic_loss_probability(mttdl: float, mission_hours: float) -> float:
    """Compute analytical cumulative loss probability: P(loss in T) ≈ 1 - exp(-T / MTTDL)."""
    if mttdl <= 0.0:
        return 1.0
    if np.isinf(mttdl):
        return 0.0
    return float(1.0 - np.exp(-mission_hours / mttdl))


def analytic_loss_probability_raid0(mttf: float, n: int, mission_hours: float) -> float:
    """Exact analytic loss probability for RAID 0: P(loss) = 1 - exp(-N * T / MTTF)."""
    if mttf <= 0.0:
        return 1.0
    return float(1.0 - np.exp(-n * mission_hours / mttf))


# --- Parameter Sweeps ---

def sweep_rebuild_time(base: ReliabilityParams, rebuild_values: list[float]) -> pd.DataFrame:
    """Sweep rebuild_hours and evaluate data loss probability."""
    records = []
    for val in rebuild_values:
        p = ReliabilityParams(
            level=base.level,
            num_disks=base.num_disks,
            mttf_hours=base.mttf_hours,
            rebuild_hours=val,
            mission_hours=base.mission_hours,
            n_trials=base.n_trials,
            seed=base.seed,
            p_ure_per_rebuild=base.p_ure_per_rebuild,
        )
        res = run_simulation(p)
        records.append({
            "rebuild_hours": val,
            "level": base.level,
            "p_loss": res.p_loss,
            "ci_low": res.ci_low,
            "ci_high": res.ci_high,
            "losses": res.losses,
        })
    return pd.DataFrame(records)


def sweep_num_disks(base: ReliabilityParams, disk_counts: list[int]) -> pd.DataFrame:
    """Sweep num_disks and evaluate data loss probability."""
    records = []
    for n in disk_counts:
        p = ReliabilityParams(
            level=base.level,
            num_disks=n,
            mttf_hours=base.mttf_hours,
            rebuild_hours=base.rebuild_hours,
            mission_hours=base.mission_hours,
            n_trials=base.n_trials,
            seed=base.seed,
            p_ure_per_rebuild=base.p_ure_per_rebuild,
        )
        res = run_simulation(p)
        records.append({
            "num_disks": n,
            "level": base.level,
            "p_loss": res.p_loss,
            "ci_low": res.ci_low,
            "ci_high": res.ci_high,
            "losses": res.losses,
        })
    return pd.DataFrame(records)


def compare_levels(base: ReliabilityParams, levels: list[str]) -> pd.DataFrame:
    """Compare data loss probability across RAID architectures."""
    records = []
    for lvl in levels:
        norm_lvl = lvl.upper().replace(" ", "")
        n_disks = 2 if norm_lvl == "RAID1" else base.num_disks
        p = ReliabilityParams(
            level=lvl,
            num_disks=n_disks,
            mttf_hours=base.mttf_hours,
            rebuild_hours=base.rebuild_hours,
            mission_hours=base.mission_hours,
            n_trials=base.n_trials,
            seed=base.seed,
            p_ure_per_rebuild=base.p_ure_per_rebuild,
        )
        res = run_simulation(p)
        records.append({
            "level": lvl,
            "num_disks": n_disks,
            "p_loss": res.p_loss,
            "ci_low": res.ci_low,
            "ci_high": res.ci_high,
            "losses": res.losses,
        })
    return pd.DataFrame(records)
