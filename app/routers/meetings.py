"""Meetings: list, create, delete."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, current_user, require, scope_to_county
from ..db import get_db
from ..models import CheckIn, Meeting, MeetingSource, Role, User
from ..schemas import MeetingIn

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


def with_window(m: Meeting, now: datetime | None = None) -> dict:
    """Meeting plus the state the UI needs: is check-in open right now?"""
    now = now or datetime.now(timezone.utc)
    w = timedelta(minutes=m.time_window_min)
    opens, closes = m.starts_at - w, m.starts_at + w
    if now < opens:
        state = "upcoming"
    elif now <= closes:
        state = "open"
    elif now <= m.ends_at:
        state = "in_progress"        # meeting running, too late to check in
    else:
        state = "ended"
    return {
        "id": m.id, "name": m.name, "category": m.category.value, "source": m.source.value,
        "address": m.address, "lat": m.lat, "lng": m.lng,
        "starts_at": m.starts_at.isoformat(), "duration_min": m.duration_min,
        "radius_ft": m.radius_ft, "time_window_min": m.time_window_min,
        "opens_at": opens.isoformat(), "closes_at": closes.isoformat(),
        "ends_at": m.ends_at.isoformat(), "window_state": state,
        "counts": m.counts_toward_requirement, "county_id": m.county_id,
    }


@router.get("")
def list_meetings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = scope_to_county(select(Meeting), Meeting, user).order_by(Meeting.starts_at.desc())
    rows = db.scalars(q).all()
    # A participant sees countable meetings plus their own personal ones.
    if user.role == Role.participant:
        rows = [m for m in rows
                if m.source != MeetingSource.personal or m.created_by_id == user.id]
    return [with_window(m) for m in rows]


@router.post("", status_code=201)
def create_meeting(body: MeetingIn, request: Request,
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    source = body.source
    # A participant can only ever create a personal meeting, whatever they send.
    if user.role == Role.participant:
        source = MeetingSource.personal
    m = Meeting(county_id=user.county_id, created_by_id=user.id,
                **body.model_dump(exclude={"source"}), source=source)
    if m.starts_at.tzinfo is None:
        m.starts_at = m.starts_at.replace(tzinfo=timezone.utc)
    db.add(m)
    db.commit()
    audit(db, user, "create_meeting", target_type="meeting", target_id=m.id,
          detail=m.name, request=request)
    return with_window(m)


@router.get("/{meeting_id}")
def get_meeting(meeting_id: int, db: Session = Depends(get_db),
                user: User = Depends(current_user)):
    m = db.get(Meeting, meeting_id)
    if not m:
        raise HTTPException(404, "meeting not found")
    return with_window(m)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require(Role.officer, Role.admin))):
    m = db.get(Meeting, meeting_id)
    if not m:
        raise HTTPException(404, "meeting not found")
    n = db.scalar(select(CheckIn).where(CheckIn.meeting_id == meeting_id))
    if n is not None:
        raise HTTPException(409, "meeting has check-ins; it cannot be deleted")
    db.delete(m)
    db.commit()
    audit(db, user, "delete_meeting", target_type="meeting", target_id=meeting_id,
          detail=m.name, request=request)
