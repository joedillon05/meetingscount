"""
The officer's view: caseload, participant detail, requirements.

Every route that opens a person's record writes an audit row before it
returns. That is deliberate and not optional.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import compliance
from ..auth import audit, current_user, require, scope_to_county
from ..db import get_db
from ..models import (
    CheckIn, CheckInStatus, County, Dispute, Meeting, Participant, Program,
    Requirement, Review, Role, User,
)
from ..schemas import ParticipantIn, RequirementIn, checkin_out

router = APIRouter(prefix="/api", tags=["caseload"])

STAFF = (Role.officer, Role.admin)


def _tz(db: Session, user: User) -> str:
    c = db.get(County, user.county_id) if user.county_id else None
    return c.timezone if c else "America/Detroit"


@router.get("/caseload")
def caseload(mine_only: bool = False, db: Session = Depends(get_db),
             user: User = Depends(require(*STAFF))):
    """One row per participant: who they are, this period's progress, what needs attention."""
    tz, now = _tz(db, user), datetime.now(timezone.utc)
    q = scope_to_county(select(Participant), Participant, user)
    if mine_only:
        q = q.where(Participant.officer_id == user.id)
    participants = db.scalars(q).all()

    # Pending reviews per participant, in one query rather than N.
    reviewed = select(Review.checkin_id)
    pending_rows = db.execute(
        select(CheckIn.user_id, func.count(CheckIn.id))
        .where(CheckIn.status.in_([CheckInStatus.flagged, CheckInStatus.rejected]))
        .where(CheckIn.id.not_in(reviewed))
        .group_by(CheckIn.user_id)
    ).all()
    pending = dict(pending_rows)

    last_rows = db.execute(
        select(CheckIn.user_id, func.max(CheckIn.checked_in_at)).group_by(CheckIn.user_id)
    ).all()
    last_seen = dict(last_rows)

    out = []
    for p in participants:
        summary = compliance.summarize(db, p, tz, now)
        last = last_seen.get(p.user_id)
        out.append({
            "participant_id": p.id, "user_id": p.user_id, "name": p.user.name,
            "case_number": p.case_number,
            "officer": p.officer.name if p.officer else None,
            "officer_id": p.officer_id,
            "program_id": p.program_id,
            "overall": summary["overall"], "requirements": summary["requirements"],
            "pending_reviews": pending.get(p.user_id, 0),
            "last_checkin_at": (last.isoformat() if isinstance(last, datetime)
                                else (last if last else None)),
        })
    order = {"short": 0, "pending": 1, "met": 2, "none": 3}
    out.sort(key=lambda r: (order.get(r["overall"], 9), -r["pending_reviews"], r["name"]))
    audit(db, user, "view_caseload", detail=f"{len(out)} participants")
    return out


@router.get("/participants/{participant_id}")
def participant_detail(participant_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    p = db.get(Participant, participant_id)
    if not p or (user.role != Role.superuser and p.county_id != user.county_id):
        raise HTTPException(404, "participant not found")
    tz = _tz(db, user)

    rows = db.execute(
        select(CheckIn, Meeting).join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(CheckIn.user_id == p.user_id).order_by(CheckIn.checked_in_at.desc())
    ).all()
    decisions = {r.checkin_id: r for r in db.scalars(
        select(Review).where(Review.checkin_id.in_([c.id for c, _ in rows] or [-1]))).all()}
    disputes = {}
    for d in db.scalars(select(Dispute).where(
            Dispute.checkin_id.in_([c.id for c, _ in rows] or [-1]))).all():
        disputes.setdefault(d.checkin_id, []).append(
            {"message": d.message, "created_at": d.created_at.isoformat()})

    timeline = []
    for c, m in rows:
        # Staff see coordinates only for check-ins that need a human look.
        d = checkin_out(c, m, include_pin=c.needs_review)
        rv = decisions.get(c.id)
        d["review"] = ({"decision": rv.decision.value, "note": rv.note,
                        "by": rv.reviewer.name, "at": rv.created_at.isoformat()}
                       if rv else None)
        d["disputes"] = disputes.get(c.id, [])
        timeline.append(d)

    reqs = []
    for r in p.requirements:
        reqs.append({
            "id": r.id, "category": r.category.value if r.category else "any",
            "count_required": r.count_required, "period": r.period.value,
            "starts_on": r.starts_on.isoformat(),
            "ends_on": r.ends_on.isoformat() if r.ends_on else None,
            "order_reference": r.order_reference,
            "history": [h.as_dict() for h in compliance.history(db, r, tz, periods=8)],
        })

    audit(db, user, "view_participant", target_type="participant", target_id=p.id,
          detail=p.user.name, request=request)
    return {
        "participant_id": p.id, "user_id": p.user_id, "name": p.user.name,
        "email": p.user.email, "phone": p.user.phone, "case_number": p.case_number,
        "officer": p.officer.name if p.officer else None,
        "enrolled_on": p.enrolled_on.isoformat(),
        "notes": p.notes,
        "summary": compliance.summarize(db, p, tz),
        "requirements": reqs, "timeline": timeline,
    }


@router.post("/participants", status_code=201)
def create_participant(body: ParticipantIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    u = User(county_id=user.county_id, role=Role.participant, name=body.name,
             email=body.email, phone=body.phone)
    db.add(u)
    db.flush()
    p = Participant(county_id=user.county_id, user_id=u.id,
                    program_id=body.program_id, officer_id=body.officer_id or user.id,
                    case_number=body.case_number)
    db.add(p)
    db.commit()
    audit(db, user, "create_participant", target_type="participant", target_id=p.id,
          detail=body.name, request=request)
    return {"participant_id": p.id, "user_id": u.id, "name": u.name}


@router.post("/requirements", status_code=201)
def create_requirement(body: RequirementIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    p = db.get(Participant, body.participant_id)
    if not p or (user.role != Role.superuser and p.county_id != user.county_id):
        raise HTTPException(404, "participant not found")
    data = body.model_dump()
    for k in ("starts_on", "ends_on"):
        if data.get(k) and data[k].tzinfo is None:
            data[k] = data[k].replace(tzinfo=timezone.utc)
    r = Requirement(county_id=p.county_id, **data)
    db.add(r)
    db.commit()
    audit(db, user, "create_requirement", target_type="participant", target_id=p.id,
          detail=f"{r.count_required}/{r.period.value}", request=request)
    return {"id": r.id}


@router.delete("/requirements/{requirement_id}", status_code=204)
def delete_requirement(requirement_id: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    r = db.get(Requirement, requirement_id)
    if not r or (user.role != Role.superuser and r.county_id != user.county_id):
        raise HTTPException(404, "requirement not found")
    db.delete(r)
    db.commit()
    audit(db, user, "delete_requirement", target_type="requirement",
          target_id=requirement_id, request=request)


@router.get("/my/progress")
def my_progress(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """
    A participant's own standing. They see everything recorded about them,
    including the reasons behind every verdict. Principle 4.
    """
    p = db.scalar(select(Participant).where(Participant.user_id == user.id))
    if not p:
        return {"has_requirements": False, "summary": None}
    return {"has_requirements": bool(p.requirements),
            "case_number": p.case_number,
            "officer": p.officer.name if p.officer else None,
            "summary": compliance.summarize(db, p, _tz(db, user))}


@router.get("/officers")
def officers(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    q = scope_to_county(select(User).where(User.role.in_([Role.officer, Role.admin])), User, user)
    return [{"id": u.id, "name": u.name, "role": u.role.value} for u in db.scalars(q).all()]


@router.get("/programs")
def programs(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    q = scope_to_county(select(Program), Program, user)
    return [{"id": p.id, "name": p.name, "join_code": p.join_code} for p in db.scalars(q).all()]
