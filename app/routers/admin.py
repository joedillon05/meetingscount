"""County admin: stats, audit log search, retention job, demo seeding."""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import reviewing
from ..auth import audit, end_all_sessions, require, scope_to_county
from ..config import settings
from ..db import get_db
from ..models import (
    AuditLog, CheckIn, CheckInStatus, Meeting, MeetingSource, Participant, Role, User,
)
from ..schemas import StaffIn, StaffPatch
from ..security import hash_password, temp_password

router = APIRouter(prefix="/api", tags=["admin"])
STAFF = (Role.officer, Role.admin)


@router.get("/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    staff_visible = Meeting.source != MeetingSource.personal
    checkins = db.scalars(scope_to_county(
        select(CheckIn).join(Meeting, Meeting.id == CheckIn.meeting_id),
        CheckIn, user)).all()
    meetings = db.scalars(scope_to_county(select(Meeting).where(staff_visible),
                                          Meeting, user)).all()
    participants = db.scalars(scope_to_county(select(Participant), Participant, user)).all()
    by_status = Counter(c.status.value for c in checkins)
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
        "pending_reviews": sum(reviewing.open_counts(db, user).values()),
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
    rows = db.execute(scope_to_county(
        select(CheckIn, Meeting, User)
        .join(Meeting, Meeting.id == CheckIn.meeting_id)
        .join(User, User.id == CheckIn.user_id)
        .order_by(CheckIn.checked_in_at.desc()).limit(500), CheckIn, user)
    ).all()
    audit(db, user, "view_ops_map", detail=f"{len(rows)} check-ins with coordinates")
    out = []
    for c, m, u in rows:
        d = checkin_out(c, m, include_pin=True)
        d["participant"] = {"user_id": u.id, "name": u.name}
        out.append(d)
    return out


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


# --------------------------------------------------------------------------- #
# Staff accounts
# --------------------------------------------------------------------------- #

@router.get("/staff")
def list_staff(db: Session = Depends(get_db), user: User = Depends(require(Role.admin))):
    """Who can sign in to this county, and how well protected each account is."""
    rows = db.scalars(scope_to_county(
        select(User).where(User.role.in_([Role.officer, Role.admin])), User, user)
        .order_by(User.active.desc(), User.name)).all()
    load = {}
    for p in db.scalars(scope_to_county(select(Participant), Participant, user)):
        load[p.officer_id] = load.get(p.officer_id, 0) + 1
    return [{"id": u.id, "name": u.name, "email": u.email, "role": u.role.value,
             "active": u.active, "mfa": bool(u.totp_enabled_at),
             "must_change_password": u.must_change_password,
             "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
             "participants": load.get(u.id, 0), "is_you": u.id == user.id} for u in rows]


@router.post("/staff", status_code=201)
def add_staff(body: StaffIn, request: Request, db: Session = Depends(get_db),
              user: User = Depends(require(Role.admin))):
    """
    Create an account and hand back a first password to read out. They must
    choose their own before the dashboard will do anything.
    """
    email = body.email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "There is already an account with that email address.")
    if body.role not in (Role.officer, Role.admin):
        raise HTTPException(422, "Staff are officers or admins.")
    first = temp_password()
    person = User(county_id=user.county_id, role=body.role, name=body.name.strip(), email=email,
                  password_hash=hash_password(first), password_set_at=datetime.now(timezone.utc),
                  must_change_password=True)
    db.add(person)
    db.commit()
    audit(db, user, "add_staff", target_type="user", target_id=person.id,
          detail=f"{person.name} as {person.role.value}", request=request)
    return {"id": person.id, "name": person.name, "email": person.email,
            "role": person.role.value, "first_password": first}


@router.post("/staff/{staff_id}/reset-password")
def reset_staff_password(staff_id: int, request: Request, db: Session = Depends(get_db),
                         user: User = Depends(require(Role.admin))):
    person = _own_staff(db, staff_id, user)
    first = temp_password()
    person.password_hash = hash_password(first)
    person.password_set_at = datetime.now(timezone.utc)
    person.must_change_password = True
    person.failed_logins = 0
    person.locked_until = None
    db.commit()
    end_all_sessions(db, person)                       # whoever is signed in, is not any more
    audit(db, user, "reset_staff_password", target_type="user", target_id=person.id,
          detail=person.name, request=request)
    return {"id": person.id, "name": person.name, "first_password": first}


@router.patch("/staff/{staff_id}")
def update_staff(staff_id: int, body: StaffPatch, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require(Role.admin))):
    person = _own_staff(db, staff_id, user)
    data = body.model_dump(exclude_unset=True)
    if person.id == user.id and ("active" in data or "role" in data):
        raise HTTPException(409, "You cannot change your own role or turn off your own account.")
    changed = []
    if "name" in data and data["name"]:
        person.name = data["name"].strip()
        changed.append("name")
    if "role" in data and data["role"] in (Role.officer, Role.admin):
        person.role = data["role"]
        changed.append(f"role to {data['role'].value}")
    if "active" in data and data["active"] != person.active:
        person.active = data["active"]
        changed.append("turned on" if data["active"] else "turned off")
        if not data["active"]:
            end_all_sessions(db, person)
    db.commit()
    audit(db, user, "update_staff", target_type="user", target_id=person.id,
          detail=f"{person.name}: {', '.join(changed) or 'no change'}", request=request)
    return {"id": person.id, "changed": changed}


def _own_staff(db: Session, staff_id: int, user: User) -> User:
    person = db.get(User, staff_id)
    if (not person or person.role not in (Role.officer, Role.admin)
            or (user.role != Role.superuser and person.county_id != user.county_id)):
        raise HTTPException(404, "staff member not found")
    return person
