from datetime import datetime, timedelta, timezone

from app.geo import feet_to_meters, haversine_m
from app.verification import CheckInContext, MeetingSpec, verify

# Michigan Union, Ann Arbor
BASE = (42.2750, -83.7413)


def meeting(**kw) -> MeetingSpec:
    defaults = dict(id=1, name="Test", lat=BASE[0], lng=BASE[1],
                    starts_at=datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc))
    return MeetingSpec(**{**defaults, **kw})


def offset_lat(ft: float) -> float:
    """Latitude degrees for a north/south offset of `ft` feet."""
    return feet_to_meters(ft) / 111_320


def ctx(dist_ft=0, minutes=0, accuracy=10, **kw) -> CheckInContext:
    m = meeting(**kw)
    return CheckInContext(meeting=m, lat=m.lat + offset_lat(dist_ft), lng=m.lng,
                          at=m.starts_at + timedelta(minutes=minutes), accuracy_m=accuracy)


def test_haversine_sanity():
    d = haversine_m(BASE[0], BASE[1], BASE[0] + offset_lat(500), BASE[1])
    assert abs(d - feet_to_meters(500)) < 1


def test_on_time_and_close_is_confirmed():
    v = verify(ctx(dist_ft=50, minutes=-5))
    assert v.status == "confirmed" and v.confidence >= 70


def test_too_far_is_rejected():
    v = verify(ctx(dist_ft=800))
    assert v.status == "rejected"
    assert any(r.name == "distance" and not r.passed for r in v.results)


def test_edge_of_radius_still_passes():
    assert verify(ctx(dist_ft=495)).status != "rejected"


def test_too_early_is_rejected():
    assert verify(ctx(minutes=-90)).status == "rejected"


def test_too_late_is_rejected():
    assert verify(ctx(minutes=61)).status == "rejected"


def test_bad_gps_is_flagged_not_rejected():
    v = verify(ctx(dist_ft=20, accuracy=400))
    assert v.status == "flagged"


def test_custom_radius_for_urban_meeting():
    # Same 700 ft offset: rejected at 500 ft radius, accepted at 1000 ft.
    assert verify(ctx(dist_ft=700)).status == "rejected"
    assert verify(ctx(dist_ft=700, radius_ft=1000)).status != "rejected"


def test_reasons_are_human_readable():
    v = verify(ctx(dist_ft=100))
    assert any("ft from meeting" in r for r in v.reasons)
