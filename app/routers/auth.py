"""
Signing in.

Staff use an email and a password, and an authenticator app if they turn one
on. Participants use their phone number and a one-time code, because many of
them change phones often and a password is one more thing to lose.

Rules that show up in a county security questionnaire, and where they live:

  passwords hashed with scrypt              app/security.hash_password
  lockout after repeated failures           _record_failure, below
  rate limits per address and per account   app/security.too_many
  no account enumeration                    one message for every failure
  sessions expire, and idle staff are out   app/auth.session_user
  a password change signs out every device  change_password, below
  every sign-in and failure is logged       audit(), below
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import (
    audit, clear_session_cookie, client_ip, current_user, end_all_sessions, end_session,
    require, set_session_cookie, start_session,
)
from .. import billing, confirming, sms
from ..config import settings
from ..db import get_db
from ..models import LoginCode, Participant, Role, User
from ..schemas import CodeRequestIn, CodeVerifyIn, LoginIn, PasswordIn, TotpIn
from ..security import (
    clear_limit, code_hash, grouped, hash_password, new_login_code, new_totp_secret,
    normalize_phone, password_problem, too_many, totp_uri, totp_verify, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])

LOCKOUT_AFTER = 5
LOCKOUT_MINUTES = 15
WRONG = "That email and password do not match an account."
STAFF = (Role.officer, Role.admin)


def _secure(request: Request) -> bool:
    """Cookies are https-only anywhere but local development."""
    return request.url.scheme == "https"


def _locked(user: User) -> bool:
    return bool(user.locked_until and user.locked_until > datetime.now(timezone.utc))


def _record_failure(db: Session, user: User) -> None:
    user.failed_logins = (user.failed_logins or 0) + 1
    if user.failed_logins >= LOCKOUT_AFTER:
        user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
        user.failed_logins = 0
    db.commit()


def _me(user: User, db: Session) -> dict:
    p = db.scalar(select(Participant).where(Participant.user_id == user.id))
    return {"id": user.id, "name": user.name, "role": user.role.value,
            "participant_id": p.id if p else None,
            "must_change_password": user.must_change_password,
            "mfa": bool(user.totp_enabled_at)}


# --------------------------------------------------------------------------- #
# What the sign-in screen needs to draw itself
# --------------------------------------------------------------------------- #

@router.get("/context")
def context(db: Session = Depends(get_db)):
    """
    Public. Says which ways in exist, and in demo mode hands over the demo
    accounts so a pitch does not start with someone mistyping a password.
    """
    from sqlalchemy import func
    from ..models import MeetingSeries
    out = {"demo": settings.DEMO, "dev_auth": settings.DEV_AUTH,
           "staff_password": True, "participant_code": True,
           "texting": settings.sms_configured,
           "self_signup": billing.signup_open(),
           "directory": bool(db.scalar(select(func.count(MeetingSeries.id))
                                       .where(MeetingSeries.active.is_(True))))}
    if settings.DEMO:
        out["demo_password"] = DEMO_PASSWORD
    return out


DEMO_PASSWORD = "washtenaw-demo-2026"        # only ever used by app/seed.py


@router.get("/demo-accounts")
def demo_accounts(db: Session = Depends(get_db)):
    """The seeded accounts, so a demo can be driven from the sign-in screen."""
    if not settings.DEMO:
        raise HTTPException(404, "not available")
    # Only people in a seeded county. Someone who made their own profile at
    # /join is real, whatever mode the server is in.
    from ..models import County
    demo_counties = select(County.id).where(County.self_serve.is_(False))
    staff = db.scalars(select(User).where(User.role.in_([Role.officer, Role.admin]),
                                          User.active.is_(True),
                                          User.county_id.in_(demo_counties)).order_by(User.role)).all()
    parts = db.scalars(select(User).where(User.role == Role.participant, User.active.is_(True),
                                          User.county_id.in_(demo_counties)).order_by(User.name)).all()
    return {
        "password": DEMO_PASSWORD,
        "staff": [{"name": u.name, "email": u.email, "role": u.role.value} for u in staff if u.email],
        "participants": [{"name": u.name, "phone": u.phone} for u in parts if u.phone],
    }


# --------------------------------------------------------------------------- #
# Staff: email and password
# --------------------------------------------------------------------------- #

@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    ip = client_ip(request) or "unknown"
    if too_many(f"login:{ip}", 10, 900) or too_many(f"login:{email}", 10, 900):
        raise HTTPException(429, "Too many attempts. Wait fifteen minutes and try again.")

    user = db.scalar(select(User).where(User.email.is_not(None)).where(User.email == email))
    if user and _locked(user):
        raise HTTPException(423, "This account is locked for a few minutes after repeated "
                                 "failed sign-ins. Try again shortly.")
    if not user or not user.active or not verify_password(body.password, user.password_hash):
        if user:
            _record_failure(db, user)
        audit(db, user, "sign_in_failed", target_type="user", target_id=user.id if user else None,
              detail=email, request=request)
        raise HTTPException(401, WRONG)

    if user.totp_enabled_at:
        if not body.code:
            return {"mfa_required": True}
        if not totp_verify(user.totp_secret, body.code):
            _record_failure(db, user)
            audit(db, user, "sign_in_failed", target_type="user", target_id=user.id,
                  detail="wrong authenticator code", request=request)
            raise HTTPException(401, "That code is not right. Codes change every 30 seconds.")

    token, expires = start_session(db, user, request)
    set_session_cookie(response, token, expires, _secure(request))
    clear_limit(f"login:{ip}")
    clear_limit(f"login:{email}")
    audit(db, user, "sign_in", target_type="user", target_id=user.id,
          detail="password" + (" + authenticator" if user.totp_enabled_at else ""), request=request)
    return _me(user, db)


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    from ..auth import session_user
    user = session_user(request, db)
    end_session(db, request)
    clear_session_cookie(response, _secure(request))
    if user:
        audit(db, user, "sign_out", target_type="user", target_id=user.id, request=request)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Participants: phone number and a one-time code
# --------------------------------------------------------------------------- #

def _issue_code(db: Session, user: User, *, by: User | None, via: str) -> str:
    code = new_login_code()
    db.add(LoginCode(user_id=user.id, code_hash=code_hash(user.id, code),
                     expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.LOGIN_CODE_MINUTES),
                     issued_by_id=by.id if by else None, delivered_via=via))
    db.commit()
    return code


def issue_first_code(db: Session, person: User, by: User) -> str:
    """A code for someone just enrolled, so they can sign in before they leave."""
    return _issue_code(db, person, by=by, via="officer")


@router.post("/code/request")
def request_code(body: CodeRequestIn, request: Request, db: Session = Depends(get_db)):
    """
    The participant asks for a code. With texting set up, the code goes to
    the number by text. Without it, the code waits for an officer to read
    out; the officer sees it on the participant's record. Either way the
    reply never says whether the number is known, so this cannot be used to
    find out who is on probation.
    """
    phone = normalize_phone(body.phone)
    ip = client_ip(request) or "unknown"
    if not phone or too_many(f"code:{ip}", 5, 900) or too_many(f"code:{phone}", 5, 3600):
        raise HTTPException(429, "Too many requests. Wait a few minutes, or ask your officer "
                                 "to read you a code.")
    texting = sms.can_text(phone)
    out = ({"delivered": True, "ask_officer": False,
            "message": f"If that number is on file, a code is on its way to it. It works for "
                       f"{settings.LOGIN_CODE_MINUTES} minutes."}
           if texting else
           {"delivered": False, "ask_officer": True,
            "message": "Your officer can read you a code. It works for "
                       f"{settings.LOGIN_CODE_MINUTES} minutes."})
    user = db.scalar(select(User).where(User.phone == phone, User.role == Role.participant,
                                        User.active.is_(True)))
    if not user:
        return out                                   # same answer either way
    code = _issue_code(db, user, by=None, via="text" if texting else "requested")
    sent = False
    if texting:
        row = sms.send(db, user.phone, confirming.code_text(code, settings.LOGIN_CODE_MINUTES),
                       purpose="login_code", user_id=user.id, county_id=user.county_id)
        sent = row.status == "sent"
    audit(db, user, "login_code_requested", target_type="user", target_id=user.id,
          detail="sent by text" if sent else ("text failed" if texting else None), request=request)
    if settings.DEMO and not sent:
        out["demo_code"] = code                      # demo deployments only
    return out


@router.post("/code/verify")
def verify_code(body: CodeVerifyIn, request: Request, response: Response,
                db: Session = Depends(get_db)):
    phone = normalize_phone(body.phone)
    ip = client_ip(request) or "unknown"
    if too_many(f"verify:{ip}", 10, 900) or too_many(f"verify:{phone}", 10, 900):
        raise HTTPException(429, "Too many tries. Wait fifteen minutes.")
    user = phone and db.scalar(select(User).where(User.phone == phone,
                                                  User.role == Role.participant,
                                                  User.active.is_(True)))
    bad = HTTPException(401, "That code is not right, or it has expired. Ask your officer for a new one.")
    if not user:
        raise bad
    row = db.scalar(select(LoginCode).where(LoginCode.user_id == user.id, LoginCode.used_at.is_(None))
                    .order_by(LoginCode.created_at.desc()))
    if not row or not row.live:
        raise bad
    if row.code_hash != code_hash(user.id, body.code.strip()):
        row.attempts += 1
        db.commit()
        audit(db, user, "sign_in_failed", target_type="user", target_id=user.id,
              detail="wrong access code", request=request)
        raise bad

    row.used_at = datetime.now(timezone.utc)
    token, expires = start_session(db, user, request)
    set_session_cookie(response, token, expires, _secure(request))
    clear_limit(f"verify:{ip}")
    audit(db, user, "sign_in", target_type="user", target_id=user.id, detail="access code",
          request=request)
    return _me(user, db)


@router.post("/participants/{participant_id}/access-code")
def issue_access_code(participant_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require(*STAFF))):
    """An officer generates a code to read out. Logged, like any other access."""
    p = db.get(Participant, participant_id)
    if not p or (user.role != Role.superuser and p.county_id != user.county_id):
        raise HTTPException(404, "participant not found")
    person = db.get(User, p.user_id)
    if not person.phone:
        raise HTTPException(409, "This participant has no phone number on file. Add one first.")
    code = _issue_code(db, person, by=user, via="officer")
    audit(db, user, "issue_access_code", target_type="participant", target_id=p.id,
          detail=person.name, request=request)
    return {"code": code, "phone": person.phone, "name": person.name,
            "minutes": settings.LOGIN_CODE_MINUTES}


# --------------------------------------------------------------------------- #
# Looking after your own account
# --------------------------------------------------------------------------- #

@router.post("/password")
def change_password(body: PasswordIn, request: Request, response: Response,
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    if user.password_hash and not verify_password(body.current_password or "", user.password_hash):
        raise HTTPException(401, "Your current password is not right.")
    problem = password_problem(body.new_password, email=user.email)
    if problem:
        raise HTTPException(422, problem)
    user.password_hash = hash_password(body.new_password)
    user.password_set_at = datetime.now(timezone.utc)
    user.must_change_password = False
    db.commit()
    # Changing a password signs out everywhere, then signs this browser back
    # in, which is what someone expects after "my password may be known".
    end_all_sessions(db, user)
    token, expires = start_session(db, user, request)
    set_session_cookie(response, token, expires, _secure(request))
    audit(db, user, "change_password", target_type="user", target_id=user.id, request=request)
    return {"ok": True}


@router.post("/totp/start")
def totp_start(db: Session = Depends(get_db), user: User = Depends(require(*STAFF))):
    """A secret to type into an authenticator app. Not active until confirmed."""
    if user.totp_enabled_at:
        raise HTTPException(409, "An authenticator app is already set up for this account.")
    user.totp_secret = new_totp_secret()
    db.commit()
    account = user.email or user.name
    return {"secret": user.totp_secret, "formatted": grouped(user.totp_secret),
            "uri": totp_uri(user.totp_secret, account)}


@router.post("/totp/enable")
def totp_enable(body: TotpIn, request: Request, db: Session = Depends(get_db),
                user: User = Depends(require(*STAFF))):
    if not user.totp_secret:
        raise HTTPException(409, "Start the setup first.")
    if not totp_verify(user.totp_secret, body.code):
        raise HTTPException(401, "That code is not right. Codes change every 30 seconds.")
    user.totp_enabled_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, user, "enable_mfa", target_type="user", target_id=user.id, request=request)
    return {"ok": True}


@router.post("/totp/disable")
def totp_disable(body: PasswordIn, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(require(*STAFF))):
    if not verify_password(body.current_password or "", user.password_hash):
        raise HTTPException(401, "Your password is not right.")
    user.totp_secret = None
    user.totp_enabled_at = None
    db.commit()
    audit(db, user, "disable_mfa", target_type="user", target_id=user.id, request=request)
    return {"ok": True}


@router.get("/sessions")
def my_sessions(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Where this account is signed in, so someone can spot a device they do not know."""
    from ..models import Session as SessionRow
    rows = db.scalars(select(SessionRow).where(SessionRow.user_id == user.id,
                                               SessionRow.revoked_at.is_(None))
                      .order_by(SessionRow.last_seen_at.desc())).all()
    now = datetime.now(timezone.utc)
    return [{"id": r.id, "created_at": r.created_at.isoformat(),
             "last_seen_at": r.last_seen_at.isoformat(), "ip": r.ip,
             "user_agent": r.user_agent, "expired": r.expires_at <= now} for r in rows]


@router.post("/sessions/end-others")
def end_other_sessions(request: Request, response: Response, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    end_all_sessions(db, user)
    token, expires = start_session(db, user, request)
    set_session_cookie(response, token, expires, _secure(request))
    audit(db, user, "end_sessions", target_type="user", target_id=user.id, request=request)
    return {"ok": True}
