"""
Turning check-ins into "is this person compliant?".

A Requirement says N meetings of a category per week (or month). Compliance
for a period counts the check-ins in that period that COUNT:

  confirmed                          -> counts
  flagged or rejected, approved      -> counts
  flagged, not yet reviewed          -> pending (shown separately, not counted)
  officer asked for more information -> pending, whatever the engine said
  denied, or rejected and undecided  -> does not count

The latest review is the one that counts; see app/reviewing.py.

Personal meetings a participant created themselves never count toward a court
requirement, only toward their own tracking.

Weeks run Monday to Sunday in the county's timezone, because that is how a
probation officer thinks about a week, not UTC.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import (
    CheckIn, CheckInStatus, Meeting, MeetingSource, Participant, Requirement,
    RequirementPeriod, ReviewDecision,
)
from .reviewing import latest_decisions


def period_bounds(when: datetime, period: RequirementPeriod, tz: str) -> tuple[datetime, datetime]:
    """Start (inclusive) and end (exclusive) of the period containing `when`."""
    zone = ZoneInfo(tz)
    local = when.astimezone(zone)
    if period == RequirementPeriod.week:
        start = (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    else:
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end = (start + timedelta(days=32)).replace(day=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


@dataclass
class PeriodResult:
    starts: datetime
    ends: datetime
    required: int
    counted: int
    pending: int
    not_counted: int

    @property
    def met(self) -> bool:
        return self.counted >= self.required

    @property
    def could_still_be_met(self) -> bool:
        return not self.met and self.counted + self.pending >= self.required

    @property
    def status(self) -> str:
        if self.met:
            return "met"
        if self.could_still_be_met:
            return "pending"
        return "short"

    def as_dict(self) -> dict:
        return {
            "starts": self.starts.isoformat(), "ends": self.ends.isoformat(),
            "required": self.required, "counted": self.counted, "pending": self.pending,
            "not_counted": self.not_counted, "status": self.status,
        }


def evaluate(db: Session, req: Requirement, when: datetime, tz: str,
             overrides: dict[int, ReviewDecision] | None = None) -> PeriodResult:
    """
    Compliance for the single period containing `when`. `overrides` answers
    "what if": the review screen uses it to show what a decision would change
    before the officer makes it.
    """
    start, end = period_bounds(when, req.period, tz)

    q = (select(CheckIn, Meeting)
         .join(Meeting, Meeting.id == CheckIn.meeting_id)
         .where(CheckIn.user_id == _participant_user_id(db, req.participant_id))
         .where(CheckIn.checked_in_at >= start, CheckIn.checked_in_at < end))
    if req.category is not None:
        q = q.where(Meeting.category == req.category)

    pairs = db.execute(q).all()
    eligible = [(c, m) for c, m in pairs if m.source != MeetingSource.personal]
    decisions = latest_decisions(db, [c.id for c, _ in eligible]) | (overrides or {})

    # One meeting counts once, however many times someone tapped. A retry
    # after a rejection is normal (walk closer, wait for a better fix), so
    # each meeting takes the best outcome among its check-ins.
    best: dict[int, int] = {}
    for c, m in eligible:
        o = outcome(c, decisions.get(c.id))
        best[m.id] = max(best.get(m.id, o), o)

    counted = sum(1 for o in best.values() if o == COUNTS)
    pending = sum(1 for o in best.values() if o == PENDING)
    not_counted = sum(1 for o in best.values() if o == NOT_COUNTED)
    return PeriodResult(start, end, req.count_required, counted, pending, not_counted)


NOT_COUNTED, PENDING, COUNTS = 0, 1, 2


def outcome(c: CheckIn, decision: ReviewDecision | None) -> int:
    """Where one check-in stands. Ordered, so the best of several is max()."""
    if decision == ReviewDecision.approved:
        return COUNTS                                    # includes an overridden rejection
    if c.status == CheckInStatus.confirmed:
        return COUNTS
    if decision == ReviewDecision.more_info:
        return PENDING                                   # waiting on the participant, not a miss
    if c.status == CheckInStatus.flagged and decision is None:
        return PENDING
    return NOT_COUNTED


def _participant_user_id(db: Session, participant_id: int) -> int:
    p = db.get(Participant, participant_id)
    return p.user_id if p else -1


def history(db: Session, req: Requirement, tz: str, periods: int = 8,
            now: datetime | None = None) -> list[PeriodResult]:
    """The last `periods` periods, oldest first. Drives the trend chart."""
    now = now or datetime.now(timezone.utc)
    step = timedelta(days=7) if req.period == RequirementPeriod.week else timedelta(days=31)
    out, seen = [], set()
    for i in range(periods - 1, -1, -1):
        at = now - step * i
        start, end = period_bounds(at, req.period, tz)
        # Only periods the order actually covers. A week that ended before the
        # requirement began must never show up as "short" in a report.
        if end <= req.starts_on or (req.ends_on and start >= req.ends_on) or start in seen:
            continue
        seen.add(start)
        out.append(evaluate(db, req, at, tz))
    return out


def streak(periods: list[PeriodResult]) -> int:
    """
    Periods met in a row, newest first. The current period only adds to the
    run once it is met; an unfinished week does not break a streak.
    """
    run = 0
    for i, p in enumerate(reversed(periods)):
        if p.met:
            run += 1
        elif i == 0:
            continue                                     # this week, still in progress
        else:
            break
    return run


def summarize(db: Session, participant: Participant, tz: str,
              now: datetime | None = None) -> dict:
    """
    One participant's current standing across all their requirements.
    This is a caseload row.
    """
    now = now or datetime.now(timezone.utc)
    reqs = [r for r in participant.requirements
            if r.starts_on <= now and (r.ends_on is None or r.ends_on >= now)]
    results = [(r, evaluate(db, r, now, tz)) for r in reqs]

    if not results:
        overall = "none"
    elif all(p.met for _r, p in results):
        overall = "met"
    elif any(p.status == "short" for _r, p in results):
        overall = "short"
    else:
        overall = "pending"

    return {
        "overall": overall,
        "requirements": [
            {"id": r.id, "category": r.category.value if r.category else "any",
             "period": r.period.value, "order_reference": r.order_reference,
             **p.as_dict()}
            for r, p in results
        ],
    }
