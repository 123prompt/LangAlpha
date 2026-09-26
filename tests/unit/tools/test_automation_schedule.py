from datetime import datetime, timezone

from src.tools.automation.tools import _parse_schedule


def test_one_time_without_offset_reads_on_the_users_clock():
    parsed = _parse_schedule("2026-10-28T14:15:00", "America/New_York")
    assert parsed["trigger_type"] == "once"
    assert parsed["next_run_at"] == datetime(2026, 10, 28, 18, 15, tzinfo=timezone.utc)


def test_one_time_with_offset_keeps_it():
    parsed = _parse_schedule("2026-10-28T14:15:00+00:00", "Asia/Tokyo")
    assert parsed["next_run_at"] == datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc)


def test_unknown_zone_falls_back_to_utc():
    parsed = _parse_schedule("2026-10-28T14:15:00", "Not/AZone")
    assert parsed["next_run_at"] == datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc)
