"""
Creating a profile on your own, at /join.

The order of things: name, phone and what the order says, then a code to
the phone, then the account exists and the person is signed in, then
payment (app/routers/billing.py). The account is made only after the code,
so nobody can open a profile on a number that is not theirs.

Like sign-in, the reply to "send me a code" never says whether the number
is already on file. If it is, the code signs that person in to the profile
they already have instead of making a second one.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import billing, confirming, profiles, sms
from ..auth import audit, client_ip, set_session_cookie, start_session
from ..config import settings
from ..db import get_db
from ..models import Role, Signup, User
from ..schemas import SignupStartIn, SignupVerifyIn
from ..security import clear_limit, keyed_hash, new_login_code, normalize_phone, too_many

router = APIRouter(prefix="/api/signup", tags=["signup"])

CLOSED = "Creating a profile is not open here yet. Your officer can enrol you instead."


def _secure(request: Request) -> bool:
    return request.url.scheme == "https"


@router.get("/context")
def context():
    """Public. What the join page needs to draw itself."""
    return {"open": billing.signup_open(), "demo": settings.DEMO,
            "texting": settings.sms_configured, "payment": billing.configured(),
            "price": billing.price_info() if billing.configured() else None,
            "trial_days": settings.STRIPE_TRIAL_DAYS if billing.configured() else 0,
            "trial": billing.trial_words(settings.STRIPE_TRIAL_DAYS) if billing.configured() else None,
            "code_minutes": settings.LOGIN_CODE_MINUTES}


@router.post("/start")
def start(body: SignupStartIn, request: Request, db: Session = Depends(get_db)):
    if not billing.signup_open():
        raise HTTPException(404, CLOSED)
    name = " ".join(body.name.split())
    if len(name) < 2:
        raise HTTPException(422, "Enter your name as your officer knows it.")
    phone = normalize_phone(body.phone)
    if not phone or len(phone) < 12:
        raise HTTPException(422, "Enter the full mobile number, including the area code.")
    email = (body.email or "").strip().lower() or None
    if email and ("@" not in email or "." not in email.split("@")[-1]):
        raise HTTPException(422, "That email address does not look right.")
    ip = client_ip(request) or "unknown"
    if too_many(f"signup:{ip}", 5, 900) or too_many(f"signup:{phone}", 5, 3600):
        raise HTTPException(429, "Too many tries. Wait a few minutes and try again.")

    starts = body.starts_on or datetime.now(timezone.utc)
    if starts.tzinfo is None:
        starts = starts.replace(tzinfo=timezone.utc)
    code = new_login_code()
    row = Signup(phone=phone, name=name[:120], email=email, per_week=body.per_week,
                 starts_on=starts, code_hash=keyed_hash(f"signup:{phone}", code), ip=ip,
                 expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.LOGIN_CODE_MINUTES))
    db.add(row)
    db.commit()

    sent = False
    if sms.can_text(phone):
        text = sms.send(db, phone, confirming.code_text(code, settings.LOGIN_CODE_MINUTES),
                        purpose="signup_code")
        sent = text.status == "sent"
    out = {"delivered": sent, "minutes": settings.LOGIN_CODE_MINUTES,
           "message": (f"We sent a six-digit code to {sms.mask(phone)}. It works for "
                       f"{settings.LOGIN_CODE_MINUTES} minutes.")}
    if not sent:
        if settings.DEMO:
            out["demo_code"] = code                    # demo deployments only
            out["message"] = "Demo: the code is shown below instead of being texted."
        else:
            raise HTTPException(503, "We could not text that number just now. Check it and try again.")
    return out


@router.post("/verify")
def verify(body: SignupVerifyIn, request: Request, response: Response,
           db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    ip = client_ip(request) or "unknown"
    if too_many(f"signup-verify:{ip}", 10, 900) or too_many(f"signup-verify:{phone}", 10, 900):
        raise HTTPException(429, "Too many tries. Wait fifteen minutes.")
    bad = HTTPException(401, "That code is not right, or it has expired. Ask for a new one.")
    row = phone and db.scalar(select(Signup).where(Signup.phone == phone, Signup.used_at.is_(None))
                              .order_by(Signup.created_at.desc()))
    if not row or not row.live:
        raise bad
    if row.code_hash != keyed_hash(f"signup:{phone}", body.code.strip()):
        row.attempts += 1
        db.commit()
        raise bad

    now = datetime.now(timezone.utc)
    row.used_at = now
    user = db.scalar(select(User).where(User.phone == phone, User.role == Role.participant,
                                        User.active.is_(True)))
    created = user is None
    if created:
        user = profiles.make_profile(db, name=row.name, phone=phone, email=row.email,
                                     per_week=row.per_week, starts_on=row.starts_on)
    row.user_id = user.id
    db.commit()

    token, expires = start_session(db, user, request)
    set_session_cookie(response, token, expires, _secure(request))
    clear_limit(f"signup-verify:{ip}")
    audit(db, user, "create_profile" if created else "sign_in", target_type="user",
          target_id=user.id, detail="self-serve" if created else "access code, from /join",
          request=request)
    return {"created": created, "id": user.id, "name": user.name,
            "billing": billing.status_out(db, user)}
