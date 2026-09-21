"""Check in, check out, dispute. The participant-facing core."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, client_ip, current_user, require
from ..config import settings
from ..db import get_db
from ..geo import haversine_m, meters_to_feet
from ..models import CheckIn, CheckInStatus, Dispute, Meeting, Role, User
from ..schemas import CheckInIn, CheckOutIn, DisputeIn, checkin_out
from ..verification import CheckInContext, MeetingSpec, verify

router = APIRouter(prefix="/api/checkins", tags=["checkins"])


def _spec(m: Meeting) -> MeetingSpec:
    return MeetingSpec(id=m.id, name=m.name, lat=m.lat, lng=m.lng, starts_at=m.starts_at,
                       duration_min=m.duration_min, radius_ft=m.radius_ft,
                       time_window_min=m.time_window_min, category=m.category.value)


@router.post("", status_code=201)
def check_in(body: CheckInIn, request: Request,
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = db.get(Meeting, body.meeting_id)
    if not m:
        raise HTTPException(404, "meeting not found")

    at, simulated = datetime.now(timezone.utc), False
    if body.at is not None and settings.DEMO:
        at = body.at if body.at.tzinfo else body.at.replace(tzinfo=timezone.utc)
        simulated = True

    ip = (body.sim_ip if (body.sim_ip and settings.DEMO) else client_ip(request))
    ctx = CheckInContext(meeting=_spec(m), lat=body.lat, lng=body.lng, at=at,
                         accuracy_m=body.accuracy_m, ip=ip,
                         user_agent=request.headers.get("user-agent"))
    v = verify(ctx)

    # Every attempt is stored, rejections included. That is the audit trail.
    c = CheckIn(county_id=m.county_id or user.county_id, meeting_id=m.id, user_id=user.id,
                lat=body.lat, lng=body.lng, accuracy_m=body.accuracy_m, checked_in_at=at,
                ip=ip, user_agent=("simulator " if simulated else "") + (ctx.user_agent or ""),
                status=CheckInStatus(v.status), confidence=v.confidence,
                distance_ft=v.distance_ft, reasons=[r.as_dict() for r in v.results])
    db.add(c)
    db.commit()
    return checkin_out(c, m, include_pin=True)


@router.post("/{checkin_id}/checkout")
def check_out(checkin_id: int, body: CheckOutIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    c = db.get(CheckIn, checkin_id)
    if not c:
        raise HTTPException(404, "check-in not found")
    if c.user_id != user.id and user.role == Role.participant:
        raise HTTPException(403, "not your check-in")
    if c.checked_out_at is None:
        c.checked_out_at = datetime.now(timezone.utc)
        # A second location sample makes drive-by check-ins harder.
        if body.lat is not None and body.lng is not None:
            c.checkout_lat, c.checkout_lng = body.lat, body.lng
            m = db.get(Meeting, c.meeting_id)
            drift = meters_to_feet(haversine_m(body.lat, body.lng, m.lat, m.lng))
            c.reasons = list(c.reasons) + [{
                "name": "checkout_distance", "passed": drift <= m.radius_ft,
                "score": 1.0 if drift <= m.radius_ft else 0.0, "weight": 0.0, "hard": False,
                "reason": f"{drift:.0f} ft from meeting at check-out",
                "data": {"distance_ft": round(drift, 1)},
            }]
        db.commit()
    return checkin_out(c, db.get(Meeting, c.meeting_id), include_pin=True)


@router.get("/mine")
def my_checkins(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.execute(
        select(CheckIn, Meeting).join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(CheckIn.user_id == user.id).order_by(CheckIn.checked_in_at.desc())
    ).all()
    return [checkin_out(c, m, include_pin=True) for c, m in rows]


@router.post("/{checkin_id}/dispute", status_code=201)
def dispute(checkin_id: int, body: DisputeIn, request: Request,
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(CheckIn, checkin_id)
    if not c:
        raise HTTPException(404, "check-in not found")
    if c.user_id != user.id:
        raise HTTPException(403, "not your check-in")
    d = Dispute(county_id=c.county_id, checkin_id=c.id, user_id=user.id, message=body.message)
    db.add(d)
    db.commit()
    audit(db, user, "dispute", target_type="checkin", target_id=c.id, request=request)
    return {"id": d.id, "checkin_id": c.id, "message": d.message,
            "created_at": d.created_at.isoformat()}
