"""County admin: stats, audit log search, retention job, demo seeding."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, current_user, require, scope_to_county
from ..config import settings
from ..db import get_db
from ..models import (
    AuditLog, CheckIn, CheckInStatus, Meeting, Participant, Review, Role, User,
)

router = APIRouter(prefix="/api", tags=["admin"])
STAFF = (Role.officer, Role.admin)


@router.get("/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    checkins = db.scalars(scope_to_county(select(CheckIn), CheckIn, user)).all()
    meetings = db.scalars(scope_to_county(select(Meeting), Meeting, user)).all()
    participants = db.scalars(scope_to_county(select(Participant), Participant, user)).all()
    by_status = Counter(c.status.value for c in checkins)
    decided = {r.checkin_id for r in db.scalars(select(Review)).all()}
    scored = [c.confidence for c in checkins if c.status != CheckInStatus.rejected]
    now = datetime.now(timezone.utc)
    week = now - timedelta(days=7)
    return {
        "participants": len(participants),
        "meetings": len(meetings),
        "open_now": sum(1 for m in meetings
                        if abs((m.starts_at - now).total_seconds()) <= m.time_window_min * 60),
        "checkins": len(checkins),
        "checkins_this_week": sum(1 for c in checkins if c.checked_in_at >= week),
        "confirmed": by_status.get("confirmed", 0),
        "flagged": by_status.get("flagged", 0),
        "rejected": by_status.get("rejected", 0),
        "pending_reviews": sum(1 for c in checkins if c.needs_review and c.id not in decided),
        "avg_confidence": round(sum(scored) / len(scored)) if scored else None,
        "not_checked_out": sum(1 for c in checkins
                               if c.status != CheckInStatus.rejected and not c.checked_out_at),
        "demo_mode": settings.DEMO, "dev_auth": settings.DEV_AUTH,
    }


@router.get("/audit")
def audit_log(limit: int = 200, action: str | None = None, db: Session = Depends(get_db),
              user: User = Depends(require(Role.admin))):
    q = scope_to_county(select(AuditLog), AuditLog, user).order_by(AuditLog.at.desc()).limit(limit)
    if action:
        q = q.where(AuditLog.action == action)
    return [{"id": a.id, "at": a.at.isoformat(), "actor": a.actor_name, "action": a.action,
             "target_type": a.target_type, "target_id": a.target_id, "detail": a.detail,
             "ip": a.ip} for a in db.scalars(q).all()]


@router.post("/retention/run")
def run_retention(request: Request, db: Session = Depends(get_db),
                  user: User = Depends(require(Role.admin))):
    """
    Drop raw coordinates older than the retention window. Distance, verdict and
    reasons survive, so the compliance record is intact but the location history
    is gone. In production a scheduled job calls this, not a person.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.PIN_RETENTION_DAYS)
    rows = db.scalars(select(CheckIn).where(
        CheckIn.checked_in_at < cutoff, CheckIn.pin_purged_at.is_(None),
        CheckIn.lat.is_not(None))).all()
    for c in rows:
        c.lat = c.lng = c.checkout_lat = c.checkout_lng = None
        c.ip = None
        c.pin_purged_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, user, "run_retention", detail=f"purged {len(rows)} pins older than "
          f"{settings.PIN_RETENTION_DAYS}d", request=request)
    return {"purged": len(rows), "cutoff": cutoff.isoformat()}


@router.get("/ops/checkins")
def ops_checkins(db: Session = Depends(get_db), user: User = Depends(require(Role.admin))):
    """
    Every check-in with its coordinates, for the ops map and the simulator.

    This is the one endpoint that hands out bulk location data, so it is
    admin-only and it writes an audit row every time. An officer doing their
    job never needs this; they see coordinates one reviewed check-in at a time.
    """
    from ..schemas import checkin_out
    rows = db.execute(
        select(CheckIn, Meeting, User)
        .join(Meeting, Meeting.id == CheckIn.meeting_id)
        .join(User, User.id == CheckIn.user_id)
        .order_by(CheckIn.checked_in_at.desc()).limit(500)
    ).all()
    audit(db, user, "view_ops_map", detail=f"{len(rows)} check-ins with coordinates")
    out = []
    for c, m, u in rows:
        d = checkin_out(c, m, include_pin=True)
        d["participant"] = {"user_id": u.id, "name": u.name}
        out.append(d)
    return out


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = db.scalar(select(Participant).where(Participant.user_id == user.id))
    return {"id": user.id, "name": user.name, "role": user.role.value,
            "county_id": user.county_id, "participant_id": p.id if p else None}


@router.get("/users")
def users(db: Session = Depends(get_db)):
    """
    Dev-auth only: the role switcher needs a list of people to become.
    Refuses to answer when dev auth is off.
    """
    if not settings.DEV_AUTH:
        raise HTTPException(404, "not available")
    rows = db.scalars(select(User).order_by(User.role, User.name)).all()
    return [{"id": u.id, "name": u.name, "role": u.role.value, "county_id": u.county_id}
            for u in rows]


@router.post("/demo/seed")
def demo_seed():
    if not settings.DEMO:
        raise HTTPException(403, "demo mode is off")
    from ..seed import seed
    return seed(reset=True)
