"""Unit tests for Monte Carlo reliability simulation and analytical cross-checks."""

import math
import numpy as np
import pytest
from raid.reliability import (
    ReliabilityParams,
    tolerance,
    run_simulation,
    analytic_mttdl_raid5,
    analytic_loss_probability,
    analytic_loss_probability_raid0,
    sweep_rebuild_time,
    sweep_num_disks,
    compare_levels,
)


def test_tolerance_values():
    assert tolerance("RAID0", 4) == 0
    assert tolerance("RAID 0", 2) == 0
    assert tolerance("RAID1", 2) == 1
    assert tolerance("RAID 1", 3) == 2
    assert tolerance("RAID5", 4) == 1
    assert tolerance("RAID 5", 8) == 1
    with pytest.raises(ValueError):
        tolerance("RAID6", 4)


def test_determinism():
    p1 = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=50000,
        rebuild_hours=24,
        mission_hours=20000,
        n_trials=2000,
        seed=12345,
    )
    p2 = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=50000,
        rebuild_hours=24,
        mission_hours=20000,
        n_trials=2000,
        seed=12345,
    )
    r1 = run_simulation(p1)
    r2 = run_simulation(p2)
    assert r1.losses == r2.losses
    assert r1.p_loss == r2.p_loss
    assert r1.ci_low == r2.ci_low
    assert r1.ci_high == r2.ci_high


def test_raid0_exact_analytic():
    """Simulated RAID 0 loss probability should match 1 - exp(-N*T/MTTF) within statistical margin."""
    mttf = 100000.0
    n = 4
    t = 20000.0
    p_analytic = analytic_loss_probability_raid0(mttf=mttf, n=n, mission_hours=t)

    params = ReliabilityParams(
        level="RAID0",
        num_disks=n,
        mttf_hours=mttf,
        rebuild_hours=24.0,
        mission_hours=t,
        n_trials=10000,
        seed=42,
    )
    res = run_simulation(params)

    # Check that analytic probability falls within simulated 95% CI
    assert res.ci_low <= p_analytic <= res.ci_high


def test_raid5_analytic_crosscheck():
    """With accelerated parameters, simulated RAID 5 loss matches analytic MTTDL approximation within 3 SE."""
    mttf = 20000.0
    rebuild = 24.0
    n = 4
    mission = 43800.0
    n_trials = 40000

    mttdl = analytic_mttdl_raid5(mttf=mttf, rebuild=rebuild, n=n)
    p_analytic = analytic_loss_probability(mttdl=mttdl, mission_hours=mission)

    params = ReliabilityParams(
        level="RAID5",
        num_disks=n,
        mttf_hours=mttf,
        rebuild_hours=rebuild,
        mission_hours=mission,
        n_trials=n_trials,
        seed=42,
    )
    res = run_simulation(params)

    se = math.sqrt(res.p_loss * (1.0 - res.p_loss) / n_trials)
    assert abs(res.p_loss - p_analytic) <= 3.0 * se


def test_monotonicity():
    """RAID 5 p_loss increases with longer rebuild times and more disks."""
    base = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=25000,
        rebuild_hours=24,
        mission_hours=43800,
        n_trials=8000,
        seed=42,
    )

    # Rebuild time sweep
    df_reb = sweep_rebuild_time(base, [6.0, 24.0, 96.0])
    p_reb = df_reb["p_loss"].tolist()
    assert p_reb[0] <= p_reb[1] <= p_reb[2]

    # Num disks sweep
    df_disks = sweep_num_disks(base, [3, 5, 8])
    p_disks = df_disks["p_loss"].tolist()
    assert p_disks[0] <= p_disks[1] <= p_disks[2]


def test_ordering():
    """For identical mission and MTTF, p_loss(RAID 1) <= p_loss(RAID 5) <= p_loss(RAID 0)."""
    base = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=30000,
        rebuild_hours=24,
        mission_hours=43800,
        n_trials=10000,
        seed=42,
    )
    df_comp = compare_levels(base, ["RAID 1", "RAID 5", "RAID 0"])
    p_r1 = df_comp[df_comp["level"] == "RAID 1"]["p_loss"].iloc[0]
    p_r5 = df_comp[df_comp["level"] == "RAID 5"]["p_loss"].iloc[0]
    p_r0 = df_comp[df_comp["level"] == "RAID 0"]["p_loss"].iloc[0]

    assert p_r1 <= p_r5 <= p_r0
    assert p_r5 < p_r0


def test_edge_cases():
    """Rebuild time = 0 yields 0 losses for RAID 5; tiny mission yields 0 losses."""
    p_zero_reb = ReliabilityParams(
        level="RAID5",
        num_disks=4,
        mttf_hours=20000,
        rebuild_hours=0.0,
        mission_hours=43800,
        n_trials=2000,
        seed=42,
    )
    r_zero = run_simulation(p_zero_reb)
    assert r_zero.losses == 0
    assert r_zero.p_loss == 0.0

    p_tiny_mission = ReliabilityParams(
        level="RAID0",
        num_disks=4,
        mttf_hours=100000,
        rebuild_hours=24.0,
        mission_hours=0.1,  # 6 minutes
        n_trials=1000,
        seed=42,
    )
    r_tiny = run_simulation(p_tiny_mission)
    assert r_tiny.losses == 0
