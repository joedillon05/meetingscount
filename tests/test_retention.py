"""
The promise on the consent screen: exact locations are deleted after the
retention window, without anyone pressing a button.
"""
from datetime import datetime, timedelta, timezone

from app import retention
from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, CheckIn, CheckInStatus, Meeting

from .conftest import as_


def _old_checkin(db, user_id, days_ago):
    m = db.query(Meeting).first()
    c = CheckIn(county_id=m.county_id, meeting_id=m.id, user_id=user_id, lat=42.1, lng=-83.1,
                accuracy_m=10, checked_in_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
                checkout_lat=42.1, checkout_lng=-83.1, ip="10.0.0.1",
                status=CheckInStatus.confirmed, confidence=90, distance_ft=40, reasons=[])
    db.add(c)
    db.commit()
    return c.id


def test_the_scheduled_purge_drops_old_pins_and_keeps_the_verdict(client, ids):
    db = SessionLocal()
    try:
        old = _old_checkin(db, ids["participant"], settings.PIN_RETENTION_DAYS + 2)
        recent = _old_checkin(db, ids["participant"], 1)
        before = db.query(AuditLog).filter(AuditLog.action == "run_retention").count()
    finally:
        db.close()

    out = retention.run_once()
    assert out is not None and out["purged"] >= 1

    db = SessionLocal()
    try:
        c = db.get(CheckIn, old)
        assert c.lat is None and c.lng is None and c.checkout_lat is None and c.ip is None
        assert c.pin_purged_at is not None
        assert c.distance_ft == 40 and c.status == CheckInStatus.confirmed, "the result survives"
        r = db.get(CheckIn, recent)
        assert r.lat == 42.1 and r.pin_purged_at is None, "inside the window, untouched"
        rows = db.query(AuditLog).filter(AuditLog.action == "run_retention").count()
        assert rows == before + 1
        note = db.query(AuditLog).filter(AuditLog.action == "run_retention").order_by(AuditLog.id.desc()).first()
        assert note.actor_id is None and "on schedule" in note.detail
    finally:
        db.close()

    # A second pass with nothing to do writes nothing.
    assert retention.run_once()["purged"] == 0
    db = SessionLocal()
    try:
        assert db.query(AuditLog).filter(AuditLog.action == "run_retention").count() == before + 1
    finally:
        db.close()

    # The dashboard button still works and says who pressed it.
    mine = client.get("/api/checkins/mine", headers=as_(ids["participant"])).json()
    assert any(x["id"] == old and x["pin_purged"] for x in mine)
    r = client.post("/api/retention/run", headers=as_(ids["admin"]))
    assert r.status_code == 200 and r.json()["purged"] == 0


def test_a_support_group_is_a_kind_of_meeting(client, ids):
    m = client.post("/api/meetings", json={
        "name": "Tuesday peer group", "category": "support_group", "lat": 42.0, "lng": -83.0,
        "address": "Community center", "starts_at": datetime.now(timezone.utc).isoformat()},
        headers=as_(ids["participant"]))
    assert m.status_code == 201, m.text
    assert m.json()["category"] == "support_group"
