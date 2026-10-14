"""Meetings: list, create, delete."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import directory
from ..auth import audit, current_user, require, scope_to_county
from ..db import get_db
from ..models import CheckIn, Meeting, MeetingSource, Role, User
from ..schemas import MeetingIn
from .checkins import meeting_visible_to

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
        "series_id": m.series_id,
    }


@router.get("")
def list_meetings(since: datetime | None = None, until: datetime | None = None,
                  lat: float | None = None, lng: float | None = None,
                  within_mi: float = directory.DEFAULT_WITHIN_MI,
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    """
    Meetings in the caller's county, and for a participant the directory
    meetings near the town they chose (or near `lat`,`lng` if given), in the
    window `since` to `until`. The phone asks for "today and the next week"
    instead of every meeting the county has ever held. Directory meetings
    within that window are created as rows here, so each one has an id a
    check-in can point at.
    """
    # The caller's county, plus meetings that belong to no county: those a
    # superuser made, which everyone may check in to. Directory meetings
    # also have no county, but a participant gets the ones near their town
    # below rather than the whole state's list.
    q = select(Meeting)
    if user.role != Role.superuser:
        q = q.where((Meeting.county_id == user.county_id)
                    | (Meeting.county_id.is_(None) & (Meeting.source != MeetingSource.directory)))
    if since is not None:
        q = q.where(Meeting.starts_at >= _aware(since))
    if until is not None:
        q = q.where(Meeting.starts_at < _aware(until))
    # A participant sees countable meetings plus their own personal ones.
    # Staff never see personal meetings: those are the participant's own
    # tracking (therapy, often) and the court has no business in them.
    if user.role == Role.participant:
        q = q.where((Meeting.source != MeetingSource.personal) | (Meeting.created_by_id == user.id))
    else:
        # Staff see the county's own list. Directory meetings have a section
        # of their own on the dashboard, so they are left out here even for
        # a superuser, who is not scoped to one county.
        q = q.where(Meeting.source.not_in((MeetingSource.personal, MeetingSource.directory)))
    rows = list(db.scalars(q).all())

    if user.role == Role.participant:
        centre = (lat, lng) if lat is not None and lng is not None else (
            (user.area_lat, user.area_lng) if user.area_lat is not None else None)
        if centre and since is not None and until is not None:
            near = [s for s, _ in directory.nearby(db, centre[0], centre[1], within_mi,
                                                   county_id=user.county_id)]
            rows += directory.occurrences(db, near, _aware(since), _aware(until))
    rows.sort(key=lambda m: m.starts_at, reverse=True)
    return [with_window(m) for m in rows]


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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
    if not meeting_visible_to(m, user):
        raise HTTPException(404, "meeting not found")
    return with_window(m)


@router.delete("/{meeting_id}", status_code=204)
def delete_meeting(meeting_id: int, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(require(Role.officer, Role.admin))):
    m = db.get(Meeting, meeting_id)
    if not meeting_visible_to(m, user):
        raise HTTPException(404, "meeting not found")
    n = db.scalar(select(CheckIn).where(CheckIn.meeting_id == meeting_id))
    if n is not None:
        raise HTTPException(409, "meeting has check-ins; it cannot be deleted")
    db.delete(m)
    db.commit()
    audit(db, user, "delete_meeting", target_type="meeting", target_id=meeting_id,
          detail=m.name, request=request)
