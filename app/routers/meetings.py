"""Meetings: list, create, delete."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import directory
from ..auth import audit, current_user, require, scope_to_county
from ..db import get_db
from ..models import CheckIn, Meeting, MeetingSource, Role, User
from ..schemas import MeetingIn
from .checkins import meeting_visible_to

router = APIRouter(prefix="/api/meetings", tags=["meetings"])
ADDED_PER_DAY = 3                # meetings a participant may add in 24 hours
ADDED_START_GRACE_MIN = 60       # it may have started this long ago: still inside the check-in window
ADDED_DAYS_AHEAD = 8             # and may be this far ahead: the week the phone shows


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
        "counts": m.counts_toward_requirement, "added": m.added_by_participant,
        "county_id": m.county_id,
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
    # A participant sees the listed meetings plus the ones they added. A
    # meeting someone added is theirs alone to check in to, so it stays off
    # the county's meeting list; staff see it on that person's record.
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
    fields = body.model_dump(exclude={"source"})
    if user.role == Role.participant:
        # A participant adds a meeting that is not on the list: one they are
        # at right now, or one coming up this week that they will check in to
        # when they get there. It counts, so nothing about it is theirs to
        # bend: the server sets the distance and the window, keeps the start
        # inside a range a check-in can still verify, and marks it as added
        # everywhere it shows. A meeting that already happened is not theirs
        # to add after the fact.
        source = MeetingSource.personal
        fields["address"] = (body.address or "").strip()[:300]
        if not fields["address"]:
            raise HTTPException(422, "Say where the meeting is, so whoever reads your log knows.")
        now = datetime.now(timezone.utc)
        starts = body.starts_at if body.starts_at.tzinfo else body.starts_at.replace(tzinfo=timezone.utc)
        if starts < now - timedelta(minutes=ADDED_START_GRACE_MIN):
            raise HTTPException(422, "A meeting that already happened cannot be added afterwards. "
                                     "If you were there, ask your officer to record it.")
        if starts > now + timedelta(days=ADDED_DAYS_AHEAD):
            raise HTTPException(422, f"You can add a meeting up to {ADDED_DAYS_AHEAD} days ahead.")
        added_today = db.scalar(select(func.count(Meeting.id)).where(
            Meeting.created_by_id == user.id, Meeting.source == MeetingSource.personal,
            Meeting.created_at >= now - timedelta(hours=24))) or 0
        if added_today >= ADDED_PER_DAY:
            raise HTTPException(429, f"You can add {ADDED_PER_DAY} meetings a day. "
                                     "If you went to more, ask your officer to record them.")
        fields |= {"starts_at": starts, "radius_ft": 500, "time_window_min": 60,
                   "duration_min": min(fields["duration_min"], 240)}
    m = Meeting(county_id=user.county_id, created_by_id=user.id, **fields, source=source)
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
