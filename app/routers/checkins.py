"""Check in, check out, dispute. The participant-facing core."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing, confirming
from ..auth import audit, client_ip, current_user
from ..config import settings
from ..db import get_db
from ..geo import haversine_m, meters_to_feet
from ..models import CheckIn, CheckInStatus, Dispute, Meeting, MeetingSource, Role, User
from ..reviewing import note_out, review_out, states_for
from ..schemas import CheckInIn, CheckOutIn, DisputeIn, checkin_out
from ..verification import CheckInContext, MeetingSpec, verify

router = APIRouter(prefix="/api/checkins", tags=["checkins"])

# Leaving more than this long before the end is written into the record.
EARLY_CHECKOUT_MIN = 10


def _spec(m: Meeting) -> MeetingSpec:
    return MeetingSpec(id=m.id, name=m.name, lat=m.lat, lng=m.lng, starts_at=m.starts_at,
                       duration_min=m.duration_min, radius_ft=m.radius_ft,
                       time_window_min=m.time_window_min, category=m.category.value)


def meeting_visible_to(m: Meeting | None, user: User) -> bool:
    """
    Whether `user` may see or check in to `m`. Everyone is kept to their own
    county, and a personal meeting belongs to the person who created it.
    """
    if m is None:
        return False
    if user.role == Role.superuser:
        return True
    if m.county_id is not None and m.county_id != user.county_id:
        return False
    if m.source == MeetingSource.personal:
        return m.created_by_id == user.id
    return True


def require_consent(user: User) -> None:
    """A participant cannot check in before they have seen what is collected."""
    if user.role == Role.participant and user.consent_version != settings.CONSENT_VERSION:
        raise HTTPException(428, "Please review and accept the consent screen first.")


@router.post("", status_code=201)
def check_in(body: CheckInIn, request: Request, response: Response,
             db: Session = Depends(get_db), user: User = Depends(current_user)):
    m = db.get(Meeting, body.meeting_id)
    if not meeting_visible_to(m, user):
        raise HTTPException(404, "meeting not found")
    require_consent(user)
    billing.require_paid(db, user)

    # A second tap on a meeting that already has a standing check-in returns
    # that check-in instead of making a new one. Retrying after a rejection
    # is allowed: that is someone walking closer or waiting for a better fix.
    standing = db.scalar(
        select(CheckIn).where(CheckIn.user_id == user.id, CheckIn.meeting_id == m.id,
                              CheckIn.status != CheckInStatus.rejected)
        .order_by(CheckIn.checked_in_at.desc()))
    if standing is not None:
        response.status_code = 200
        return checkin_out(standing, m, include_pin=True) | {"duplicate": True}

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
                ip=ip, user_agent=(("simulator " if simulated else "") + (ctx.user_agent or ""))[:300],
                status=CheckInStatus(v.status), confidence=v.confidence,
                distance_ft=v.distance_ft, reasons=[r.as_dict() for r in v.results])
    db.add(c)
    db.commit()
    return checkin_out(c, m, include_pin=True)


@router.post("/{checkin_id}/checkout")
def check_out(checkin_id: int, body: CheckOutIn, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    c = db.get(CheckIn, checkin_id)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "check-in not found")
    m = db.get(Meeting, c.meeting_id)
    if c.status == CheckInStatus.rejected:
        raise HTTPException(409, "a check-in that was not confirmed has nothing to check out of")
    if c.checked_out_at is None:
        now = datetime.now(timezone.utc)
        c.checked_out_at = now
        extra = []
        # A second location sample makes drive-by check-ins harder.
        if body.lat is not None and body.lng is not None:
            c.checkout_lat, c.checkout_lng = body.lat, body.lng
            drift = meters_to_feet(haversine_m(body.lat, body.lng, m.lat, m.lng))
            extra.append({
                "name": "checkout_distance", "passed": drift <= m.radius_ft,
                "score": 1.0 if drift <= m.radius_ft else 0.0, "weight": 0.0, "hard": False,
                "reason": f"{drift:.0f} ft from meeting at check-out",
                "data": {"distance_ft": round(drift, 1)},
            })
        left_early = (m.ends_at - now).total_seconds() / 60
        if left_early > EARLY_CHECKOUT_MIN:
            extra.append({
                "name": "checkout_time", "passed": False, "score": 0.0, "weight": 0.0,
                "hard": False,
                "reason": f"checked out {left_early:.0f} min before the meeting ended",
                "data": {"minutes_before_end": round(left_early, 1)},
            })
        if extra:
            c.reasons = list(c.reasons) + extra
        db.commit()
    return checkin_out(c, m, include_pin=True)


@router.get("/mine")
def my_checkins(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """
    Everything recorded about this person's check-ins, including what their
    officer decided and why. Principle 4: nothing is hidden from them.
    """
    rows = db.execute(
        select(CheckIn, Meeting).join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(CheckIn.user_id == user.id).order_by(CheckIn.checked_in_at.desc())
    ).all()
    states = states_for(db, [c for c, _ in rows])
    out = []
    for c, m in rows:
        st = states[c.id]
        d = checkin_out(c, m, include_pin=True)
        d["review"] = review_out(st.latest) if st.latest else None
        d["reviews"] = [review_out(r) for r in st.history]
        d["review_state"] = st.state
        d["awaiting_reply"] = st.state == "waiting"
        d["disputes"] = [note_out(n) for n in st.notes]
        out.append(d)
    confirming.attach(db, out, [c for c, _ in rows])
    return out


@router.post("/{checkin_id}/dispute", status_code=201)
def dispute(checkin_id: int, body: DisputeIn, request: Request,
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = db.get(CheckIn, checkin_id)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "check-in not found")
    d = Dispute(county_id=c.county_id, checkin_id=c.id, user_id=user.id,
                message=body.message.strip())
    db.add(d)
    db.commit()
    audit(db, user, "dispute", target_type="checkin", target_id=c.id, request=request)
    return {"id": d.id, "checkin_id": c.id, "message": d.message,
            "created_at": d.created_at.isoformat()}
