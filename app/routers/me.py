"""
The signed-in person's own record: who they are, consent, progress, export.

Principle 4 lives here. A participant can see everything recorded about them
and take a copy of it, without asking anyone.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing, compliance, confirming, sms
from ..auth import audit, current_user
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import County, Participant, Program, User
from ..schemas import ConsentIn
from .checkins import my_checkins

router = APIRouter(prefix="/api", tags=["me"])


def _county(db: Session, user: User) -> County | None:
    return db.get(County, user.county_id) if user.county_id else None


def _tz(db: Session, user: User) -> str:
    c = _county(db, user)
    return c.timezone if c else "America/Detroit"


def _participant(db: Session, user: User) -> Participant | None:
    return db.scalar(select(Participant).where(Participant.user_id == user.id))


@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _participant(db, user)
    county = _county(db, user)
    program = db.get(Program, p.program_id) if p and p.program_id else None
    return {
        "id": user.id, "name": user.name, "role": user.role.value,
        "county_id": user.county_id, "participant_id": p.id if p else None,
        "must_change_password": user.must_change_password,
        "mfa": bool(user.totp_enabled_at),
        # The self-pay county is plumbing, not something a person belongs to.
        "county": county.name if county and not county.self_serve else None,
        "timezone": county.timezone if county else "America/Detroit",
        "program": program.name if program else None,
        "officer": p.officer.name if p and p.officer else None,
        "case_number": p.case_number if p else None,
        "area": ({"label": user.area_label, "lat": user.area_lat, "lng": user.area_lng}
                 if user.area_lat is not None else None),
        # None for someone their county enrolled. A self-serve profile sees
        # its subscription here and nowhere else.
        "billing": billing.status_out(db, user),
        "consent": {
            "version": settings.CONSENT_VERSION,
            "accepted_at": user.consented_at.isoformat() if user.consented_at else None,
            "required": (user.role.value == "participant"
                         and user.consent_version != settings.CONSENT_VERSION),
        },
        # What the consent screen promises, read from the live settings so
        # the words and the behaviour cannot drift apart.
        "policy": {
            "pin_retention_days": settings.PIN_RETENTION_DAYS,
            "record_retention_days": county.retention_days if county else 1095,
        },
    }


@router.post("/my/consent")
def accept_consent(body: ConsentIn, request: Request, background: BackgroundTasks,
                   db: Session = Depends(get_db), user: User = Depends(current_user)):
    if body.version != settings.CONSENT_VERSION:
        raise HTTPException(409, "The consent text has changed. Reload and read it again.")
    user.consented_at = datetime.now(timezone.utc)
    user.consent_version = body.version
    db.commit()
    audit(db, user, "accept_consent", target_type="user", target_id=user.id,
          detail=f"version {body.version}", request=request)
    # The consent screen says texts may come. The first one says how to stop
    # them, once per number, the way carriers expect.
    if settings.sms_configured and user.phone and not sms.has_sent(db, user.phone, "welcome"):
        background.add_task(_welcome, user.id)
    return {"accepted_at": user.consented_at.isoformat(), "version": body.version}


def _welcome(user_id: int) -> None:
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        if u and u.phone:
            sms.send(db, u.phone, confirming.welcome_text(), purpose="welcome", user_id=u.id,
                     county_id=u.county_id)
    finally:
        db.close()


@router.get("/my/progress")
def my_progress(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """
    A participant's own standing: this period for each requirement, the last
    eight periods, and how many in a row they have met.
    """
    p = _participant(db, user)
    if not p:
        return {"has_requirements": False, "summary": None}
    tz, now = _tz(db, user), datetime.now(timezone.utc)
    summary = compliance.summarize(db, p, tz, now)
    by_id = {r.id: r for r in p.requirements}
    for r in summary["requirements"]:
        hist = compliance.history(db, by_id[r["id"]], tz, periods=8, now=now)
        r["history"] = [h.as_dict() for h in hist]
        r["streak"] = compliance.streak(hist)
    return {"has_requirements": bool(summary["requirements"]),
            "case_number": p.case_number,
            "officer": p.officer.name if p.officer else None,
            "summary": summary}


@router.get("/my/export")
def my_export(request: Request, db: Session = Depends(get_db),
              user: User = Depends(current_user)):
    """Everything recorded about the caller, as one JSON file they can keep."""
    p = _participant(db, user)
    body = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "person": {"name": user.name, "email": user.email, "phone": user.phone,
                   "consent_version": user.consent_version,
                   "consented_at": user.consented_at.isoformat() if user.consented_at else None},
        "case": ({"case_number": p.case_number,
                  "officer": p.officer.name if p.officer else None,
                  "enrolled_on": p.enrolled_on.isoformat(),
                  "requirements": [
                      {"category": r.category.value if r.category else "any",
                       "count_required": r.count_required, "period": r.period.value,
                       "starts_on": r.starts_on.isoformat(),
                       "ends_on": r.ends_on.isoformat() if r.ends_on else None,
                       "order_reference": r.order_reference}
                      for r in p.requirements]} if p else None),
        "check_ins": my_checkins(db, user),
    }
    audit(db, user, "export_own_record", target_type="user", target_id=user.id,
          detail=f"{len(body['check_ins'])} check-ins", request=request)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return Response(json.dumps(body, indent=2), media_type="application/json",
                    headers={"Content-Disposition":
                             f'attachment; filename="meetingscount-record-{stamp}.json"'})
