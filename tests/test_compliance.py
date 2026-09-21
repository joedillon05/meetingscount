"""Compliance maths: weeks, what counts, and what a review changes."""
from datetime import datetime, timedelta, timezone

from app.compliance import period_bounds
from app.models import RequirementPeriod


def test_week_starts_monday_local_not_utc():
    # Thursday 17 Sep 2026, 14:00 UTC -> week starts Monday 14 Sep, 00:00 Detroit
    s, e = period_bounds(datetime(2026, 9, 17, 14, tzinfo=timezone.utc),
                         RequirementPeriod.week, "America/Detroit")
    assert s.isoformat() == "2026-09-14T04:00:00+00:00"
    assert (e - s) == timedelta(days=7)


def test_week_boundary_just_after_midnight_local():
    """A check-in at 00:30 Monday Detroit belongs to the new week, not the old."""
    at = datetime(2026, 9, 14, 4, 30, tzinfo=timezone.utc)      # 00:30 Detroit
    s, _ = period_bounds(at, RequirementPeriod.week, "America/Detroit")
    assert s.isoformat() == "2026-09-14T04:00:00+00:00"


def test_month_period():
    s, e = period_bounds(datetime(2026, 9, 17, tzinfo=timezone.utc),
                         RequirementPeriod.month, "America/Detroit")
    assert s.isoformat().startswith("2026-09-01")
    assert e.isoformat().startswith("2026-10-01")
