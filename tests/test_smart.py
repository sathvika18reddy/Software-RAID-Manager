"""Unit tests for SMART failure telemetry and proactive spare migration."""

from raid.virtual_disk import VirtualDisk
from raid.raid5 import Raid5


def test_smart_telemetry_and_warnings():
    disk = VirtualDisk(disk_id=0, num_blocks=16)
    assert disk.is_smart_warning() is False
    assert disk.health_score == 100.0

    # Inject minor error
    disk.record_smart_error(1)
    assert disk.health_score == 85.0
    assert disk.reallocated_sectors == 1
    assert disk.is_smart_warning() is False

    # Inject more errors to cross warning threshold (health < 70 or reallocated >= 3)
    disk.record_smart_error(2)
    assert disk.health_score == 55.0
    assert disk.reallocated_sectors == 3
    assert disk.is_smart_warning() is True

    rep = disk.smart_report()
    assert rep["status"] == "WARNING"
    assert rep["health_score"] == 55.0


def test_proactive_spare_migration_prevents_degradation():
    disks = [VirtualDisk(i, num_blocks=16) for i in range(4)]
    spares = [VirtualDisk(100, num_blocks=16)]
    array = Raid5(disks=disks, spares=spares)

    # Write payload to array
    array.write_block(0, b"CRITICAL_ENTERPRISE_DATABASE_PAYLOAD")

    # Disk 1 shows SMART telemetry failure warnings
    failing_disk = array.disks[1]
    failing_disk.record_smart_error(3)
    assert failing_disk.is_smart_warning() is True

    # Array is NOT degraded yet because failing disk is still alive
    assert array.is_degraded() is False

    # Execute proactive spare migration before disk dies
    success = array.proactive_spare_migration(failing_disk_id=failing_disk.disk_id)
    assert success is True
    assert len(array.spares) == 0

    # Array was NEVER degraded!
    assert array.is_degraded() is False
    # Read payload to confirm 100% data integrity
    assert array.read_block(0)[:36] == b"CRITICAL_ENTERPRISE_DATABASE_PAYLOAD"
