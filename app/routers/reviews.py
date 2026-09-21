"""
The review queue. Anything the engine flagged or rejected waits here for a
human. The system never decides a violation on its own.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import audit, require, scope_to_county
from ..db import get_db
from ..models import CheckIn, CheckInStatus, Dispute, Meeting, Participant, Review, Role, User
from ..schemas import ReviewIn, checkin_out

router = APIRouter(prefix="/api/reviews", tags=["reviews"])
STAFF = (Role.officer, Role.admin)


@router.get("/queue")
def queue(mine_only: bool = False, db: Session = Depends(get_db),
          user: User = Depends(require(*STAFF))):
    """Undecided flagged/rejected check-ins, oldest first (they age badly)."""
    decided = select(Review.checkin_id)
    q = (select(CheckIn, Meeting, User)
         .join(Meeting, Meeting.id == CheckIn.meeting_id)
         .join(User, User.id == CheckIn.user_id)
         .where(CheckIn.status.in_([CheckInStatus.flagged, CheckInStatus.rejected]))
         .where(CheckIn.id.not_in(decided))
         .order_by(CheckIn.checked_in_at.asc()))
    q = scope_to_county(q, CheckIn, user)

    rows = db.execute(q).all()
    if mine_only:
        mine = {p.user_id for p in db.scalars(
            select(Participant).where(Participant.officer_id == user.id)).all()}
        rows = [r for r in rows if r[2].id in mine]

    disputes = {}
    for d in db.scalars(select(Dispute).where(
            Dispute.checkin_id.in_([c.id for c, _, _ in rows] or [-1]))).all():
        disputes.setdefault(d.checkin_id, []).append(
            {"message": d.message, "created_at": d.created_at.isoformat()})

    pmap = {p.user_id: p.id for p in db.scalars(select(Participant)).all()}
    out = []
    for c, m, u in rows:
        # Reviewing IS the reason coordinates are visible here.
        d = checkin_out(c, m, include_pin=True)
        d["participant"] = {"user_id": u.id, "name": u.name, "participant_id": pmap.get(u.id)}
        d["disputes"] = disputes.get(c.id, [])
        out.append(d)
    audit(db, user, "view_review_queue", detail=f"{len(out)} pending")
    return out


@router.post("/{checkin_id}", status_code=201)
def decide(checkin_id: int, body: ReviewIn, request: Request,
           db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    c = db.get(CheckIn, checkin_id)
    if not c or (user.role != Role.superuser and c.county_id != user.county_id):
        raise HTTPException(404, "check-in not found")
    if db.scalar(select(Review).where(Review.checkin_id == checkin_id)):
        raise HTTPException(409, "already reviewed")

    r = Review(county_id=c.county_id, checkin_id=c.id, reviewer_id=user.id,
               decision=body.decision, note=body.note)
    db.add(r)
    db.commit()
    audit(db, user, "decide_review", target_type="checkin", target_id=c.id,
          detail=f"{body.decision.value}: {(body.note or '')[:120]}", request=request)
    return {"id": r.id, "checkin_id": c.id, "decision": r.decision.value,
            "note": r.note, "by": user.name, "at": r.created_at.isoformat()}
