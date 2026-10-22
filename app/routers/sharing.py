"""
A participant's own log, and sharing it by text or email.

The log is the same attendance report an officer prints, built from the same
code, so the two can never disagree. A participant can open it, print it,
and send a link to anyone they choose: an officer who is not on the system,
a lawyer, a spouse keeping track. The link shows the log as it stands when it
is opened, so the person they sent it to always sees the latest, until the
participant stops sharing.

Rules on sharing:
  - only the participant shares their own log
  - never to their own number, never to a number that has said stop
  - by text or by email, one or the other for each person
  - at most ten people at a time, and five new shares a day
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import confirming, mail, sms
from ..auth import audit, current_user
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import County, LogShare, Participant, User
from ..schemas import ShareIn
from ..security import normalize_phone, token_hash, too_many
from .reports import _build

router = APIRouter(prefix="/api", tags=["sharing"])

SHARES_AT_ONCE = 10
SHARES_PER_DAY = 5


def _participant(db: Session, user: User) -> Participant:
    p = db.scalar(select(Participant).where(Participant.user_id == user.id))
    if p is None:
        raise HTTPException(404, "There is no log on this account.")
    return p


def _tz(db: Session, county_id: int | None) -> str:
    c = db.get(County, county_id) if county_id else None
    return c.timezone if c else "America/Detroit"


def _log(db: Session, p: Participant) -> dict:
    """Everything since they started, up to a year."""
    started = p.enrolled_on if p.enrolled_on.tzinfo else p.enrolled_on.replace(tzinfo=timezone.utc)
    days = max(7, min(365, (datetime.now(timezone.utc) - started).days + 1))
    return _build(db, p, _tz(db, p.county_id), days)


def _share_out(s: LogShare) -> dict:
    return {"id": s.id, "name": s.name, "via": s.via,
            "phone": sms.mask(s.phone) if s.phone else None,
            "email": mail.mask(s.email) if s.email else None,
            "active": s.active, "created_at": s.created_at.isoformat(),
            "last_opened_at": s.last_opened_at.isoformat() if s.last_opened_at else None,
            "opens": s.opens}


def _base_url(request: Request) -> str:
    return settings.PUBLIC_URL or str(request.base_url).rstrip("/")


@router.get("/my/log")
def my_log(request: Request, db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _participant(db, user)
    body = _log(db, p)
    audit(db, user, "view_own_log", target_type="user", target_id=user.id, request=request)
    return body


@router.get("/my/shares")
def my_shares(db: Session = Depends(get_db), user: User = Depends(current_user)):
    rows = db.scalars(select(LogShare).where(LogShare.user_id == user.id, LogShare.revoked_at.is_(None))
                      .order_by(LogShare.created_at.desc())).all()
    return [_share_out(s) for s in rows]


@router.post("/my/shares", status_code=201)
def share(body: ShareIn, request: Request, background: BackgroundTasks,
          db: Session = Depends(get_db), user: User = Depends(current_user)):
    p = _participant(db, user)
    name = " ".join((body.name or "").split())[:80]
    if len(name) < 2:
        raise HTTPException(422, "Give their name, so you know who has your log.")
    number = normalize_phone(body.phone) if (body.phone or "").strip() else None
    address = mail.normalize(body.email) if (body.email or "").strip() else None
    if (body.phone or "").strip() and (not number or len(number) < 11):
        raise HTTPException(422, "Enter their mobile number, with the area code.")
    if (body.email or "").strip() and not address:
        raise HTTPException(422, "That email address does not look right.")
    if not number and not address:
        raise HTTPException(422, "Enter a mobile number or an email address to send it to.")
    if number and address:
        raise HTTPException(422, "Pick one: text them or email them. You can add the other afterwards.")
    if number:
        if not settings.sms_configured and not settings.DEMO:
            raise HTTPException(503, "Texting is not set up on this system yet.")
        if normalize_phone(user.phone) == number:
            raise HTTPException(422, "That is your own number. Open your log from here instead.")
        if sms.opted_out(db, number):
            raise HTTPException(409, f"{confirming.first_name(name)} has asked not to get texts from "
                                     f"{confirming.BRAND}. Ask them to text START to the number first.")
    else:
        if not settings.email_configured and not settings.DEMO:
            raise HTTPException(503, "Email is not set up on this system yet. Send a text instead.")
        if (user.email or "").strip().lower() == address:
            raise HTTPException(422, "That is your own email. Open your log from here instead.")
    live = db.scalars(select(LogShare).where(LogShare.user_id == user.id,
                                             LogShare.revoked_at.is_(None))).all()
    if any((number and s.phone == number) or (address and s.email == address) for s in live):
        raise HTTPException(409, f"{confirming.first_name(name)} already has your log. "
                                 "The link they were sent still works.")
    if len(live) >= SHARES_AT_ONCE:
        raise HTTPException(409, f"You can share with {SHARES_AT_ONCE} people at a time. "
                                 "Stop sharing with someone first.")
    if too_many(f"share:{user.id}", SHARES_PER_DAY, 86400):
        raise HTTPException(429, "That is as many as you can send today. Try again tomorrow.")

    token, digest = confirming.new_token()
    s = LogShare(county_id=p.county_id, user_id=user.id, name=name, phone=number, email=address,
                 token_hash=digest)
    db.add(s)
    db.commit()
    where = sms.mask(number) if number else mail.mask(address)
    audit(db, user, "share_log", target_type="user", target_id=user.id,
          detail=f"with {name} ({where}) by {s.via}", request=request)

    link = f"{_base_url(request)}/log/{token}"
    background.add_task(_send_share, s.id, link)
    out = _share_out(s) | {"sent": sms.can_text(number) if number else mail.can_email(address)}
    if settings.DEMO and not out["sent"]:
        out["demo_link"] = link            # demo only: the message that would have gone out
    return out


def _send_share(share_id: int, link: str) -> None:
    """Runs after the response is sent, with its own session."""
    db = SessionLocal()
    try:
        s = db.get(LogShare, share_id)
        who = db.get(User, s.user_id) if s else None
        if s is None or who is None:
            return
        if s.phone:
            sms.send(db, s.phone, confirming.share_text(who.name, link), purpose="share",
                     user_id=who.id, county_id=s.county_id)
        elif s.email:
            subject, text, html = confirming.share_email(who.name, link)
            mail.send(db, s.email, subject, text, html=html, purpose="share",
                      user_id=who.id, county_id=s.county_id)
    finally:
        db.close()


@router.delete("/my/shares/{share_id}", status_code=204)
def stop_sharing(share_id: int, request: Request, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    s = db.get(LogShare, share_id)
    if not s or s.user_id != user.id or not s.active:
        raise HTTPException(404, "not found")
    s.revoked_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, user, "stop_sharing_log", target_type="user", target_id=user.id,
          detail=f"with {s.name} ({sms.mask(s.phone) if s.phone else mail.mask(s.email)})",
          request=request)


@router.get("/shared/{token}")
def shared_log(token: str, request: Request, db: Session = Depends(get_db)):
    """The person a log was sent to. No sign-in: the token is the key."""
    s = db.scalar(select(LogShare).where(LogShare.token_hash == token_hash(token))) \
        if token and len(token) <= 64 else None
    if s is None:
        raise HTTPException(404, "This link is not valid. Check you have the whole link from the text.")
    if not s.active:
        raise HTTPException(410, "This log is no longer shared with you.")
    p = db.scalar(select(Participant).where(Participant.user_id == s.user_id))
    if p is None:
        raise HTTPException(410, "This log is no longer available.")
    s.last_opened_at = datetime.now(timezone.utc)
    s.opens = (s.opens or 0) + 1
    db.commit()
    body = _log(db, p) | {"shared_with": s.name}
    audit(db, None, "open_shared_log", target_type="user", target_id=s.user_id,
          detail=f"{s.name} opened it", request=request, county_id=s.county_id)
    return body
