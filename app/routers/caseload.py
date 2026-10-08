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

from .. import compliance, confirming, reviewing
from ..auth import audit, end_all_sessions, require, scope_to_county
from ..config import settings
from ..db import get_db
from ..models import (
    CheckIn, County, Meeting, MeetingSource, Participant, Program, Requirement, Role, User,
)
from ..schemas import (
    ParticipantIn, ParticipantPatch, RequirementIn, RequirementPatch, checkin_out,
)
from ..security import normalize_phone
from .auth import issue_first_code

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

    # Items waiting on a decision per participant. Personal meetings are the
    # participant's own business and stay out of every staff view.
    pending = reviewing.open_counts(db, user)

    last_rows = db.execute(
        select(CheckIn.user_id, func.max(CheckIn.checked_in_at))
        .join(Meeting, Meeting.id == CheckIn.meeting_id)
        .where(Meeting.source != MeetingSource.personal)
        .group_by(CheckIn.user_id)
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
        .where(CheckIn.user_id == p.user_id, Meeting.source != MeetingSource.personal)
        .order_by(CheckIn.checked_in_at.desc())
    ).all()
    states = reviewing.states_for(db, [c for c, _ in rows])

    timeline = []
    for c, m in rows:
        # No coordinates here. The map for a check-in is in its review view,
        # which is logged one check-in at a time (principle 3).
        d = checkin_out(c, m)
        st = states[c.id]
        d["review"] = reviewing.review_out(st.latest) if st.latest else None
        d["reviews"] = [reviewing.review_out(r) for r in st.history]
        d["review_state"] = st.state
        d["disputes"] = [reviewing.note_out(n) for n in st.notes]
        timeline.append(d)
    confirming.attach(db, timeline, [c for c, _ in rows])

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
        "officer_id": p.officer_id, "program_id": p.program_id, "active": p.user.active,
        "ends_on": p.ends_on.isoformat() if p.ends_on else None,
        "enrolled_on": p.enrolled_on.isoformat(),
        "notes": p.notes,
        "summary": compliance.summarize(db, p, tz),
        "requirements": reqs, "timeline": timeline,
    }


@router.post("/participants", status_code=201)
def create_participant(body: ParticipantIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    """
    Enrol someone. The phone number is how they sign in, so it has to be
    there and it has to be theirs alone. The reply carries a first sign-in
    code, so an officer sitting with the person can hand it over now.
    """
    phone = normalize_phone(body.phone)
    if not phone:
        raise HTTPException(422, "A mobile number is needed: it is how they sign in.")
    clash = db.scalar(select(User).where(User.phone == phone, User.role == Role.participant))
    if clash:
        raise HTTPException(409, f"That number is already on file for {clash.name}.")

    u = User(county_id=user.county_id, role=Role.participant, name=body.name.strip(),
             email=(body.email or "").strip().lower() or None, phone=phone)
    db.add(u)
    db.flush()
    p = Participant(county_id=user.county_id, user_id=u.id,
                    program_id=body.program_id, officer_id=body.officer_id or user.id,
                    case_number=(body.case_number or "").strip() or None)
    db.add(p)
    db.commit()
    audit(db, user, "create_participant", target_type="participant", target_id=p.id,
          detail=u.name, request=request)

    code = issue_first_code(db, u, user)
    return {"participant_id": p.id, "user_id": u.id, "name": u.name, "phone": u.phone,
            "sign_in_code": code, "code_minutes": settings.LOGIN_CODE_MINUTES}


@router.patch("/participants/{participant_id}")
def update_participant(participant_id: int, body: ParticipantPatch, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    """Correct a case number, hand the case to another officer, close it."""
    p = _own_participant(db, participant_id, user)
    person = db.get(User, p.user_id)
    changed = []

    data = body.model_dump(exclude_unset=True)
    if "phone" in data:
        phone = normalize_phone(data["phone"])
        if not phone:
            raise HTTPException(422, "A mobile number is needed: it is how they sign in.")
        clash = db.scalar(select(User).where(User.phone == phone, User.role == Role.participant,
                                             User.id != person.id))
        if clash:
            raise HTTPException(409, f"That number is already on file for {clash.name}.")
        if phone != person.phone:
            person.phone = phone
            changed.append("phone")
    for field in ("name", "email"):
        if field in data and data[field] is not None:
            value = data[field].strip() or None
            if field == "email" and value:
                value = value.lower()
            if value != getattr(person, field):
                setattr(person, field, value)
                changed.append(field)
    for field in ("case_number", "program_id", "officer_id", "notes"):
        if field in data and data[field] != getattr(p, field):
            setattr(p, field, data[field])
            changed.append(field)
    if "ends_on" in data:
        p.ends_on = _aware(data["ends_on"])
        changed.append("ends_on")
    if "active" in data and data["active"] != person.active:
        person.active = data["active"]
        changed.append("closed" if not data["active"] else "reopened")
        if not data["active"]:
            end_all_sessions(db, person)              # a closed case signs out
    db.commit()
    audit(db, user, "update_participant", target_type="participant", target_id=p.id,
          detail=f"{person.name}: {', '.join(changed) or 'no change'}", request=request)
    return {"participant_id": p.id, "changed": changed}


@router.post("/requirements", status_code=201)
def create_requirement(body: RequirementIn, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(require(*STAFF))):
    p = _own_participant(db, body.participant_id, user)
    data = body.model_dump()
    for k in ("starts_on", "ends_on"):
        data[k] = _aware(data.get(k))
    if data["ends_on"] and data["ends_on"] <= data["starts_on"]:
        raise HTTPException(422, "The end date has to be after the start date.")
    r = Requirement(county_id=p.county_id, **data)
    db.add(r)
    db.commit()
    audit(db, user, "create_requirement", target_type="participant", target_id=p.id,
          detail=f"{r.count_required} {r.category.value if r.category else 'any'} per "
                 f"{r.period.value}", request=request)
    return {"id": r.id}


@router.patch("/requirements/{requirement_id}")
def update_requirement(requirement_id: int, body: RequirementPatch, request: Request,
                       db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    """
    An order gets amended. Changing the count or the dates changes what past
    periods needed, so the audit row keeps what it was before.
    """
    r = db.get(Requirement, requirement_id)
    if not r or (user.role != Role.superuser and r.county_id != user.county_id):
        raise HTTPException(404, "requirement not found")
    before = (f"{r.count_required} {r.category.value if r.category else 'any'} per "
              f"{r.period.value} from {r.starts_on.date()}")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(r, field, _aware(value) if field in ("starts_on", "ends_on") else value)
    if r.ends_on and r.ends_on <= r.starts_on:
        raise HTTPException(422, "The end date has to be after the start date.")
    db.commit()
    audit(db, user, "update_requirement", target_type="participant", target_id=r.participant_id,
          detail=f"was {before}", request=request)
    return {"id": r.id}


def _own_participant(db: Session, participant_id: int, user: User) -> Participant:
    p = db.get(Participant, participant_id)
    if not p or (user.role != Role.superuser and p.county_id != user.county_id):
        raise HTTPException(404, "participant not found")
    return p


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


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


@router.get("/officers")
def officers(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    q = scope_to_county(select(User).where(User.role.in_([Role.officer, Role.admin])), User, user)
    return [{"id": u.id, "name": u.name, "role": u.role.value} for u in db.scalars(q).all()]


@router.get("/programs")
def programs(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    q = scope_to_county(select(Program), Program, user)
    return [{"id": p.id, "name": p.name, "join_code": p.join_code} for p in db.scalars(q).all()]
