"""
Confirmation by text, and the Twilio webhooks.

A participant asks someone who was there (POST /api/checkins/{id}/confirmation).
That person gets a text with a link. The link opens a page that needs no
sign-in (GET /confirm/{token}) and talks to /api/confirm/{token}. They can
also just reply YES or NO to the text; Twilio posts that to /api/sms/inbound.

Who may ask whom:
  - only the participant the check-in belongs to
  - never their own number
  - never a number that has said stop
  - at most three asks per check-in, ten a day, and not twice to the same
    person while an ask is still open
  - never on a personal meeting: those are the participant's alone
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import confirming, sms
from ..auth import audit, client_ip, current_user
from ..config import settings
from ..db import SessionLocal, get_db
from ..models import CheckIn, Confirmation, Confirmer, County, Meeting, MeetingSource, User
from ..schemas import AnswerIn, AskIn, ConfirmerIn
from ..security import normalize_phone, too_many

router = APIRouter(prefix="/api", tags=["confirmations"])

ASKS_PER_DAY = 10
RECEIPT_WINDOW_MIN = 10        # a check-out this recent gets a "we texted Kim" receipt
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}
START_WORDS = {"START", "UNSTOP", "YES PLEASE"}
YES_WORDS = {"YES", "Y", "CONFIRM", "CONFIRMED", "YEP", "YEAH"}
NO_WORDS = {"NO", "N", "CANNOT", "CAN'T", "NOPE"}


def _tz(db: Session, county_id: int | None) -> str:
    c = db.get(County, county_id) if county_id else None
    return c.timezone if c else "America/Detroit"


def _base_url(request: Request) -> str:
    return settings.PUBLIC_URL or str(request.base_url).rstrip("/")


def _confirmer_out(k: Confirmer) -> dict:
    return {"id": k.id, "name": k.name, "phone": sms.mask(k.phone),
            "relationship": k.relationship,
            "relationship_words": confirming.relationship_words(k.relationship)}


def _own_confirmers(db: Session, user: User) -> list[Confirmer]:
    return db.scalars(select(Confirmer).where(Confirmer.user_id == user.id,
                                              Confirmer.removed_at.is_(None))
                      .order_by(Confirmer.created_at)).all()


# --------------------------------------------------------------------------- #
# The participant's saved people
# --------------------------------------------------------------------------- #

@router.get("/my/confirmers")
def my_confirmers(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [_confirmer_out(k) for k in _own_confirmers(db, user)]


def _clean_contact(db: Session, user: User, name: str, phone: str | None,
                   relationship: str) -> tuple[str, str, str]:
    name = " ".join((name or "").split())[:120]
    if len(name) < 2:
        raise HTTPException(422, "Give their name, so you know who you asked.")
    number = normalize_phone(phone)
    if not number or len(number) < 11:
        raise HTTPException(422, "Enter their mobile number, with the area code.")
    if normalize_phone(user.phone) == number:
        raise HTTPException(422, "That is your own number. Ask someone who was there with you.")
    if relationship not in confirming.RELATIONSHIPS:
        relationship = "other"
    return name, number, relationship


@router.post("/my/confirmers", status_code=201)
def add_confirmer(body: ConfirmerIn, request: Request, db: Session = Depends(get_db),
                  user: User = Depends(current_user)):
    name, number, relationship = _clean_contact(db, user, body.name, body.phone, body.relationship)
    if len(_own_confirmers(db, user)) >= 10:
        raise HTTPException(409, "You can keep up to ten people. Remove one first.")
    k = Confirmer(county_id=user.county_id, user_id=user.id, name=name, phone=number,
                  relationship=relationship)
    db.add(k)
    db.commit()
    return _confirmer_out(k)


@router.delete("/my/confirmers/{confirmer_id}", status_code=204)
def remove_confirmer(confirmer_id: int, db: Session = Depends(get_db),
                     user: User = Depends(current_user)):
    k = db.get(Confirmer, confirmer_id)
    if not k or k.user_id != user.id or k.removed_at:
        raise HTTPException(404, "not found")
    k.removed_at = datetime.now(timezone.utc)
    db.commit()


# --------------------------------------------------------------------------- #
# Asking
# --------------------------------------------------------------------------- #

@router.post("/checkins/{checkin_id}/confirmation", status_code=201)
def ask(checkin_id: int, body: AskIn, request: Request, background: BackgroundTasks,
        db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not settings.sms_configured and not settings.DEMO:
        raise HTTPException(503, "Texting is not set up on this system yet.")
    c = db.get(CheckIn, checkin_id)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "check-in not found")
    m = db.get(Meeting, c.meeting_id)
    if m.source == MeetingSource.personal:
        raise HTTPException(409, "Personal meetings are yours alone. Nobody needs to confirm them.")
    if c.checked_in_at < datetime.now(timezone.utc) - timedelta(days=settings.CONFIRM_LINK_DAYS):
        raise HTTPException(409, "That check-in is too long ago to ask about now.")

    if body.confirmer_id is not None:
        k = db.get(Confirmer, body.confirmer_id)
        if not k or k.user_id != user.id or k.removed_at:
            raise HTTPException(404, "that person is not on your list")
        name, number, relationship = k.name, k.phone, k.relationship
    else:
        name, number, relationship = _clean_contact(db, user, body.name or "", body.phone,
                                                    body.relationship or "other")
        k = None
        if body.save:
            # The same number again is the same person: keep one row, with
            # whatever they are called this time.
            k = next((x for x in _own_confirmers(db, user) if x.phone == number), None)
            if k is None:
                k = Confirmer(county_id=user.county_id, user_id=user.id, name=name, phone=number,
                              relationship=relationship)
                db.add(k)
            else:
                k.name, k.relationship = name, relationship
            db.flush()

    if sms.opted_out(db, number):
        raise HTTPException(409, f"{confirming.first_name(name)} has asked not to get texts from "
                                 "MeetingsCount. Ask someone else, or ask them to text START "
                                 "to the number first.")
    earlier = db.scalars(select(Confirmation).where(Confirmation.checkin_id == c.id)).all()
    if any(x.phone == number and x.open for x in earlier):
        raise HTTPException(409, f"You already asked {confirming.first_name(name)} about this "
                                 "check-in. The link in their text still works.")
    if len(earlier) >= confirming.ASKS_PER_CHECKIN:
        raise HTTPException(409, "You have asked as many people as you can about this check-in.")
    if too_many(f"ask:{user.id}", ASKS_PER_DAY, 86400):
        raise HTTPException(429, "That is as many asks as you can send today. Try again tomorrow.")

    token, digest = confirming.new_token()
    x = Confirmation(county_id=c.county_id, checkin_id=c.id, user_id=user.id,
                     confirmer_id=k.id if k else None, name=name, phone=number,
                     relationship=relationship, token_hash=digest, expires_at=confirming.expires())
    db.add(x)
    db.commit()
    audit(db, user, "confirmation_asked", target_type="checkin", target_id=c.id,
          detail=f"asked {name} ({confirming.relationship_words(relationship)})", request=request)

    link = f"{_base_url(request)}/confirm/{token}"
    background.add_task(_send_ask, x.id, link)
    out = confirming.confirmation_out(x)
    out["sent"] = settings.sms_configured
    if settings.DEMO and not settings.sms_configured:
        out["demo_link"] = link            # demo only: the text that would have gone out
    return out


def _send_ask(confirmation_id: int, link: str) -> None:
    """Runs after the response is sent. Its own session, because the request's is closed."""
    db = SessionLocal()
    try:
        x = db.get(Confirmation, confirmation_id)
        if x is None:
            return
        who, c, m = confirming.participant_and_meeting(db, x)
        tz = _tz(db, c.county_id)
        sms.send(db, x.phone, confirming.ask_text(who.name, m.name, confirming.when_words(
            c.checked_in_at, tz), link), purpose="ask", user_id=who.id, county_id=c.county_id)
        recent = c.checked_out_at and c.checked_out_at >= datetime.now(timezone.utc) - timedelta(
            minutes=RECEIPT_WINDOW_MIN)
        if recent and who.phone:
            sms.send(db, who.phone, confirming.receipt_text(m.name, confirming.clock(
                c.checked_out_at, tz), x.name), purpose="receipt", user_id=who.id,
                county_id=c.county_id)
    finally:
        db.close()


def _send_answer(confirmation_id: int) -> None:
    db = SessionLocal()
    try:
        x = db.get(Confirmation, confirmation_id)
        if x is None:
            return
        who, c, m = confirming.participant_and_meeting(db, x)
        if who.phone:
            sms.send(db, who.phone, confirming.answer_text(x.name, m.name, x.state == "confirmed"),
                     purpose="answer", user_id=who.id, county_id=c.county_id)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The person asked: no sign-in, one token
# --------------------------------------------------------------------------- #

def _public(db: Session, x: Confirmation) -> dict:
    who, c, m = confirming.participant_and_meeting(db, x)
    tz = _tz(db, c.county_id)
    return {
        "state": x.state,
        "asked": confirming.first_name(x.name),
        "participant": confirming.short_name(who.name),
        "meeting": {"name": m.name, "address": m.address,
                    "starts_at": m.starts_at.isoformat(), "ends_at": m.ends_at.isoformat()},
        "checked_in_at": c.checked_in_at.isoformat(),
        "checked_out_at": c.checked_out_at.isoformat() if c.checked_out_at else None,
        "timezone": tz,
        "answered_at": x.answered_at.isoformat() if x.answered_at else None,
    }


@router.get("/confirm/{token}")
def confirm_page_data(token: str, request: Request, db: Session = Depends(get_db)):
    ip = client_ip(request) or "unknown"
    if too_many(f"confirm-get:{ip}", 60, 900):
        raise HTTPException(429, "Too many requests. Wait a few minutes.")
    x = confirming.by_token(db, token)
    if x is None:
        raise HTTPException(404, "This link is not one we sent, or it was mistyped.")
    return _public(db, x)


@router.post("/confirm/{token}")
def confirm_answer(token: str, body: AnswerIn, request: Request, background: BackgroundTasks,
                   db: Session = Depends(get_db)):
    ip = client_ip(request) or "unknown"
    if too_many(f"confirm-post:{ip}", 20, 900):
        raise HTTPException(429, "Too many requests. Wait a few minutes.")
    x = confirming.by_token(db, token)
    if x is None:
        raise HTTPException(404, "This link is not one we sent, or it was mistyped.")
    if x.state == "expired":
        raise HTTPException(410, "This link has expired. The participant can send a new one.")
    if x.state != "asked":
        raise HTTPException(409, "This link has already been used. Thank you.")
    confirming.answer(db, x, body.answer == "yes", via="link", note=body.note, ip=ip)
    _audit_answer(db, x, request)
    background.add_task(_send_answer, x.id)
    return _public(db, x)


def _audit_answer(db: Session, x: Confirmation, request: Request | None) -> None:
    who = db.get(User, x.user_id)
    said = "confirmed" if x.state == "confirmed" else "could not confirm"
    audit(db, None, "confirmation_answered", target_type="checkin", target_id=x.checkin_id,
          detail=f"{x.name} ({confirming.relationship_words(x.relationship)}) {said} "
                 f"{who.name}'s check-in, by {x.answered_via}",
          request=request, county_id=x.county_id)


# --------------------------------------------------------------------------- #
# Twilio webhooks: replies, and delivery reports
# --------------------------------------------------------------------------- #

async def _twilio_form(request: Request) -> dict:
    """
    Twilio posts form fields and signs the call with the account's auth token.
    Anything that does not carry a good signature is turned away. No token
    configured means no webhook is trusted at all.
    """
    raw = (await request.body()).decode("utf-8", "replace")
    params = dict(parse_qsl(raw, keep_blank_values=True))
    url = _base_url(request) + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    if not sms.signature_ok(url, params, request.headers.get("x-twilio-signature")):
        raise HTTPException(403, "bad signature")
    return params


def _twiml(message: str | None = None) -> Response:
    body = f"<Response><Message>{message}</Message></Response>" if message else "<Response/>"
    return Response(content=f'<?xml version="1.0" encoding="UTF-8"?>{body}',
                    media_type="application/xml")


@router.post("/sms/inbound", include_in_schema=False)
async def inbound(request: Request, background: BackgroundTasks, db: Session = Depends(get_db)):
    params = await _twilio_form(request)
    frm = normalize_phone(params.get("From"))
    words = " ".join((params.get("Body") or "").upper().split())
    if not frm:
        return _twiml()
    if words in STOP_WORDS:
        sms.opt_out(db, frm, "reply")
        audit(db, None, "sms_opt_out", detail=f"{sms.mask(frm)} replied STOP")
        return _twiml()                  # Twilio sends its own confirmation of a STOP
    if words in START_WORDS:
        sms.opt_in(db, frm)
        return _twiml()
    yes = words in YES_WORDS
    if yes or words in NO_WORDS:
        # The most recent open ask to this number, if there is exactly one
        # person it could be about, is answered by the reply.
        pending = [x for x in db.scalars(select(Confirmation).where(Confirmation.phone == frm)
                                         .order_by(Confirmation.asked_at.desc())).all() if x.open]
        if not pending:
            return _twiml("MeetingsCount: there is no open question for this number. "
                          "If you were asked to confirm a meeting, use the link in that text.")
        if len(pending) > 1:
            return _twiml("MeetingsCount: you have more than one question open. Please use the "
                          "link in each text.")
        x = pending[0]
        confirming.answer(db, x, yes, via="text", ip=client_ip(request))
        _audit_answer(db, x, request)
        background.add_task(_send_answer, x.id)
        who = db.get(User, x.user_id)
        return _twiml(f"MeetingsCount: thank you. {confirming.first_name(who.name)} will get a "
                      "text with your answer.")
    return _twiml()


@router.post("/sms/status", include_in_schema=False)
async def delivery_status(request: Request, db: Session = Depends(get_db)):
    params = await _twilio_form(request)
    sid, status = params.get("MessageSid") or params.get("SmsSid"), params.get("MessageStatus")
    if sid and status:
        sms.record_status(db, sid, status, params.get("ErrorCode"))
    return _twiml()
