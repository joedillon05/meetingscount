"""
The review queue. Anything the engine flagged or rejected waits here for a
human. The system never decides a violation on its own.

The list carries no coordinates. Opening one item returns its pin, the
context an officer needs to decide it, and what each decision would do to
the participant's week, and that view is written to the audit log.
Principle 3: coordinates only for the check-in under review, and logged.
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import compliance
from ..auth import audit, require
from ..db import get_db
from ..geo import haversine_m, meters_to_feet
from ..models import (
    CheckIn, CheckInStatus, County, Meeting, MeetingSource, Participant, RequirementPeriod,
    Review, ReviewDecision, Role, User,
)
from ..reviewing import (
    latest_decisions, note_out, note_required, review_out, reviewable, states_for,
)
from ..schemas import BulkReviewIn, ReviewIn, checkin_out

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
STAFF = (Role.officer, Role.admin)

VENUE_FT = 150          # meetings this close together are the same room, week to week


def _tz(db: Session, user: User) -> str:
    c = db.get(County, user.county_id) if user.county_id else None
    return c.timezone if c else "America/Detroit"


def _participant_out(u: User, p: Participant | None) -> dict:
    return {"user_id": u.id, "name": u.name,
            "participant_id": p.id if p else None,
            "case_number": p.case_number if p else None,
            "officer_id": p.officer_id if p else None,
            "officer": p.officer.name if p and p.officer else None}


def _since(c: CheckIn, st) -> datetime:
    """When this item started waiting on whoever has it now."""
    if st.state == "open" and st.replied:
        return st.notes[-1].created_at
    if st.latest is not None:
        return st.latest.created_at
    return c.checked_in_at


# --------------------------------------------------------------------------- #
# The queue
# --------------------------------------------------------------------------- #

@router.get("/queue")
def queue(state: str = "open", mine_only: bool = False, days: int = 14,
          db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    """
    One tab of the queue: `open` (needs a decision), `waiting` (asked the
    participant for more), or `decided` (in the last `days` days). Counts for
    all three come back every time so the tabs stay honest.
    """
    if state not in ("open", "waiting", "decided"):
        raise HTTPException(422, "state must be open, waiting or decided")
    rows = reviewable(db, user, mine_only=mine_only)
    states = states_for(db, [c for c, *_ in rows])
    now = datetime.now(timezone.utc)
    week_start, _ = compliance.period_bounds(now, RequirementPeriod.week, _tz(db, user))
    decided_after = now - timedelta(days=days)

    def in_tab(c, tab):
        st = states[c.id]
        if tab == "decided":
            return st.state == "decided" and st.latest.created_at >= decided_after
        return st.state == tab

    counts = {t: sum(1 for c, *_ in rows if in_tab(c, t)) for t in ("open", "waiting", "decided")}
    items = []
    for c, m, u, p in rows:
        if not in_tab(c, state):
            continue
        st = states[c.id]
        d = checkin_out(c, m)                            # no pin in the list
        d |= {"participant": _participant_out(u, p), "state": st.state, "replied": st.replied,
              "latest": review_out(st.latest) if st.latest else None,
              "notes_count": len(st.notes), "this_week": c.checked_in_at >= week_start,
              "since": _since(c, st).isoformat()}
        items.append(d)
    items.sort(key=lambda d: d["since"], reverse=(state == "decided"))
    audit(db, user, "view_review_queue", detail=f"{state}: {len(items)} items")
    return {"items": items, "counts": counts}


# --------------------------------------------------------------------------- #
# Deciding (bulk first, so "/bulk" is not read as a check-in id)
# --------------------------------------------------------------------------- #

@router.post("/bulk")
def decide_bulk(body: BulkReviewIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require(*STAFF))):
    """
    The same decision and note on several items, for a run of the same
    situation (one venue with bad GPS, one week of late starts). Only items
    still waiting for a decision are touched; changing a past decision is
    always done one at a time.
    """
    note = (body.note or "").strip() or None
    if body.decision != ReviewDecision.approved and not note:
        raise HTTPException(422, "A note is needed for this decision. The participants will see it.")
    rows = {c.id: (c, u) for c, m, u, p in reviewable(db, user, ids=body.checkin_ids)}
    states = states_for(db, [c for c, _ in rows.values()])
    decided, skipped = [], []
    for cid in dict.fromkeys(body.checkin_ids):
        if cid not in rows:
            skipped.append({"id": cid, "reason": "not found"})
            continue
        c, u = rows[cid]
        st = states[cid]
        if st.state not in ("open", "waiting"):
            skipped.append({"id": cid, "reason": "already decided"})
            continue
        db.add(Review(county_id=c.county_id, checkin_id=c.id, reviewer_id=user.id,
                      decision=body.decision, note=note))
        decided.append(cid)
    db.commit()
    for cid in decided:
        audit(db, user, "decide_review", target_type="checkin", target_id=cid,
              detail=f"{body.decision.value} (bulk of {len(decided)}): {(note or '')[:100]}",
              request=request)
    return {"decided": decided, "skipped": skipped}


@router.post("/{checkin_id}", status_code=201)
def decide(checkin_id: int, body: ReviewIn, request: Request,
           db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    found = reviewable(db, user, ids=[checkin_id])
    c = found[0][0] if found else db.get(CheckIn, checkin_id)
    # A confirmed check-in nobody has reviewed is not in `reviewable`, but an
    # officer may still review it from the participant's record.
    if c is None or (user.role != Role.superuser and c.county_id != user.county_id):
        raise HTTPException(404, "check-in not found")
    if db.get(Meeting, c.meeting_id).source == MeetingSource.personal:
        raise HTTPException(404, "check-in not found")      # never staff business

    st = states_for(db, [c])[c.id]
    note = (body.note or "").strip() or None
    why = note_required(body.decision, st, c.status)
    if why and not note:
        raise HTTPException(422, why)
    if st.state == "decided" and st.decision == body.decision:
        raise HTTPException(409, "That is already the decision on this check-in.")

    r = Review(county_id=c.county_id, checkin_id=c.id, reviewer_id=user.id,
               decision=body.decision, note=note)
    db.add(r)
    db.commit()
    action = "change_review" if st.state == "decided" else "decide_review"
    audit(db, user, action, target_type="checkin", target_id=c.id,
          detail=f"{body.decision.value}: {(note or '')[:120]}", request=request)
    after = states_for(db, [c])[c.id]
    return {"id": r.id, "checkin_id": c.id, "decision": r.decision.value, "note": r.note,
            "by": user.name, "at": r.created_at.isoformat(), "state": after.state,
            "history": [review_out(x) for x in after.history]}


# --------------------------------------------------------------------------- #
# One item, with everything needed to decide it
# --------------------------------------------------------------------------- #

@router.get("/{checkin_id}")
def item(checkin_id: int, request: Request, db: Session = Depends(get_db),
         user: User = Depends(require(*STAFF))):
    found = reviewable(db, user, ids=[checkin_id])
    if not found:
        raise HTTPException(404, "check-in not found")
    c, m, u, p = found[0]
    st = states_for(db, [c])[c.id]
    tz, now = _tz(db, user), datetime.now(timezone.utc)

    d = checkin_out(c, m, include_pin=True)             # reviewing IS the reason for the pin
    d |= {"participant": _participant_out(u, p), "state": st.state, "replied": st.replied,
          "latest": review_out(st.latest) if st.latest else None,
          "since": _since(c, st).isoformat(),
          "checkout": ({"lat": c.checkout_lat, "lng": c.checkout_lng}
                       if c.checkout_lat is not None else None),
          "timeline": _timeline(c, st),
          "impact": _impact(db, c, m, p, tz) if p else [],
          "context": {"venue": _venue(db, m, c.user_id),
                      "person": _person(db, p, c.user_id, tz, now) if p else None}}
    audit(db, user, "view_review_item", target_type="checkin", target_id=c.id,
          detail=u.name, request=request)
    return d


def _timeline(c: CheckIn, st) -> list[dict]:
    """The check-in's life so far, in order: engine verdict, notes, decisions."""
    ev = [{"kind": "checkin", "at": c.checked_in_at.isoformat(), "status": c.status.value,
           "confidence": c.confidence}]
    if c.checked_out_at:
        ev.append({"kind": "checkout", "at": c.checked_out_at.isoformat()})
    ev += [{"kind": "note", "at": n.created_at.isoformat(), **note_out(n)} for n in st.notes]
    ev += [{"kind": "review", **review_out(r)} for r in st.history]
    return sorted(ev, key=lambda e: e["at"])


def _impact(db: Session, c: CheckIn, m: Meeting, p: Participant, tz: str) -> list[dict]:
    """What counting it, or not, does to each requirement it could satisfy."""
    out = []
    for r in p.requirements:
        if r.starts_on > c.checked_in_at or (r.ends_on and r.ends_on < c.checked_in_at):
            continue
        if r.category is not None and r.category != m.category:
            continue
        run = lambda o=None: compliance.evaluate(db, r, c.checked_in_at, tz, o).as_dict()  # noqa: E731
        out.append({
            "requirement": {"id": r.id, "category": r.category.value if r.category else "any",
                            "count_required": r.count_required, "period": r.period.value},
            "now": run(),
            "if_approved": run({c.id: ReviewDecision.approved}),
            "if_denied": run({c.id: ReviewDecision.denied}),
        })
    return out


def _venue(db: Session, m: Meeting, user_id: int) -> dict:
    """
    How check-ins usually go at this place. If a third of everyone's fixes
    here are fuzzy, a fuzzy fix says more about the building than the
    person. Aggregates only: no other participant is identified.
    """
    box = 0.002
    near = db.scalars(select(Meeting).where(
        Meeting.county_id == m.county_id, Meeting.source != MeetingSource.personal,
        Meeting.lat.between(m.lat - box, m.lat + box),
        Meeting.lng.between(m.lng - box / math.cos(math.radians(m.lat)),
                            m.lng + box / math.cos(math.radians(m.lat))))).all()
    ids = [x.id for x in near if meters_to_feet(haversine_m(x.lat, x.lng, m.lat, m.lng)) <= VENUE_FT]
    rows = db.scalars(select(CheckIn).where(CheckIn.meeting_id.in_(ids or [-1]))).all()

    def weak_gps(c):
        return any(r.get("name") == "gps_accuracy" and not r.get("passed") for r in c.reasons)

    def share(xs, pred):
        return round(sum(1 for x in xs if pred(x)) / len(xs), 2) if xs else None

    acc = [c.accuracy_m for c in rows if c.accuracy_m is not None]
    mine = [c for c in rows if c.user_id == user_id]
    decisions = latest_decisions(db, [c.id for c in mine])
    dist = [c.distance_ft for c in mine]
    return {
        "name": m.name, "address": m.address, "radius_ft": m.radius_ft,
        "everyone": {
            "checkins": len(rows), "people": len({c.user_id for c in rows}),
            "flagged_share": share(rows, lambda c: c.status != CheckInStatus.confirmed),
            "weak_gps_share": share(rows, weak_gps),
            "median_accuracy_m": round(statistics.median(acc)) if acc else None,
        },
        "person": {
            "checkins": len(mine),
            "confirmed": sum(1 for c in mine if c.status == CheckInStatus.confirmed),
            "flagged": sum(1 for c in mine if c.status == CheckInStatus.flagged),
            "rejected": sum(1 for c in mine if c.status == CheckInStatus.rejected),
            "approved": sum(1 for v in decisions.values() if v == ReviewDecision.approved),
            "denied": sum(1 for v in decisions.values() if v == ReviewDecision.denied),
            "median_distance_ft": round(statistics.median(dist)) if dist else None,
        },
    }


def _person(db: Session, p: Participant, user_id: int, tz: str, now: datetime) -> dict:
    """The participant's recent record, for "is this out of character?"."""
    since = now - timedelta(days=90)
    rows = db.execute(select(CheckIn).join(Meeting, Meeting.id == CheckIn.meeting_id)
                      .where(CheckIn.user_id == user_id, CheckIn.checked_in_at >= since,
                             Meeting.source != MeetingSource.personal)).scalars().all()
    decisions = latest_decisions(db, [c.id for c in rows])
    weeks = []
    for r in p.requirements:
        if r.starts_on <= now and (r.ends_on is None or r.ends_on >= now):
            h = compliance.history(db, r, tz, periods=8, now=now)
            weeks.append({"category": r.category.value if r.category else "any",
                          "period": r.period.value, "required": r.count_required,
                          "history": [{"starts": x.starts.isoformat(), "status": x.status,
                                       "counted": x.counted} for x in h]})
    return {
        "enrolled_on": p.enrolled_on.isoformat(),
        "last_90_days": {
            "checkins": len(rows),
            "confirmed": sum(1 for c in rows if c.status == CheckInStatus.confirmed),
            "flagged": sum(1 for c in rows if c.status == CheckInStatus.flagged),
            "rejected": sum(1 for c in rows if c.status == CheckInStatus.rejected),
            "approved": sum(1 for v in decisions.values() if v == ReviewDecision.approved),
            "denied": sum(1 for v in decisions.values() if v == ReviewDecision.denied),
            "not_checked_out": sum(1 for c in rows
                                   if c.status != CheckInStatus.rejected and not c.checked_out_at),
        },
        "requirements": weeks,
    }
