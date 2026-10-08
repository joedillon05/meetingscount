"""
Text messages, through Twilio.

The one file that knows how a text is sent. Everything else says "text this
number this" and reads what came back from the log.

Nothing goes out unless MC_TWILIO_ACCOUNT_SID, MC_TWILIO_AUTH_TOKEN and
MC_TWILIO_FROM are all set. Without them every message is written to the
log as skipped, which is how development, the tests and the demo run: sign-in
codes are read out by an officer and confirmation links show on screen.

Every attempt lands in sms_log with the status Twilio gave it, so a county
can answer "did that text go out" without anyone's inbox. The body is kept
with any link token blanked out, so a copy of the log is not a stack of
usable links.

Twilio handles STOP, START and HELP itself on a number with Advanced Opt-Out
switched on. This file keeps its own list as well, from the inbound webhook
and from Twilio refusing a send (error 21610), so a number that said stop is
never even tried.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import SmsLog, SmsOptOut
from .security import normalize_phone

log = logging.getLogger("meetingscount.sms")

UNSUBSCRIBED = "21610"          # Twilio: the recipient has opted out of this number


@dataclass
class Outcome:
    status: str                 # sent | failed | skipped
    sid: str | None = None
    error: str | None = None


class Transport(Protocol):
    def send(self, to: str, body: str) -> Outcome: ...


class TwilioTransport:
    """The real thing. One HTTPS call per message, ten seconds at most."""

    def __init__(self, account_sid: str, auth_token: str, sender: str,
                 status_callback: str | None = None):
        self.account_sid, self.auth_token, self.sender = account_sid, auth_token, sender
        self.status_callback = status_callback
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    def send(self, to: str, body: str) -> Outcome:
        data = {"To": to, "Body": body}
        # A Messaging Service (MG...) picks the number and handles opt-outs
        # and compliance; a bare number sends from that number.
        data["MessagingServiceSid" if self.sender.startswith("MG") else "From"] = self.sender
        if self.status_callback:
            data["StatusCallback"] = self.status_callback
        try:
            r = httpx.post(self.url, data=data, auth=(self.account_sid, self.auth_token), timeout=10)
        except httpx.HTTPError as exc:
            return Outcome("failed", error=f"could not reach Twilio: {exc}"[:300])
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code >= 400:
            code = str(payload.get("code") or r.status_code)
            return Outcome("failed", error=f"{code}: {payload.get('message') or r.text}"[:300])
        return Outcome("sent", sid=payload.get("sid"))


class SilentTransport:
    """Nothing configured. Says so in the log and sends nothing."""

    def send(self, to: str, body: str) -> Outcome:
        log.info("text not sent (texting is not configured) to %s: %s", to, redact(body))
        return Outcome("skipped", error="texting is not configured")


# Tests and the simulator put their own transport here.
transport: Transport | None = None


def get_transport() -> Transport:
    """Nothing configured means nothing sent, whatever transport is plugged in."""
    if not settings.sms_configured:
        return SilentTransport()
    if transport is not None:
        return transport
    callback = f"{settings.PUBLIC_URL}/api/sms/status" if settings.PUBLIC_URL else None
    return TwilioTransport(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN,
                           settings.TWILIO_FROM, callback)


def redact(body: str) -> str:
    """The same text with any confirmation link token blanked out."""
    return re.sub(r"(/confirm/)[A-Za-z0-9_\-]+", r"\1...", body)


def mask(phone: str | None) -> str:
    """+17345550142 -> (734) 555 ... 0142, enough to recognise, not to dial."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]} ... {digits[-4:]}"
    return f"... {digits[-4:]}" if len(digits) >= 4 else "that number"


# --------------------------------------------------------------------------- #
# Opt-outs
# --------------------------------------------------------------------------- #

def opted_out(db: Session, phone: str | None) -> bool:
    phone = normalize_phone(phone)
    return bool(phone) and db.scalar(select(SmsOptOut).where(SmsOptOut.phone == phone)) is not None


def opt_out(db: Session, phone: str | None, source: str) -> None:
    phone = normalize_phone(phone)
    if not phone or opted_out(db, phone):
        return
    db.add(SmsOptOut(phone=phone, source=source))
    db.commit()


def opt_in(db: Session, phone: str | None) -> None:
    phone = normalize_phone(phone)
    row = db.scalar(select(SmsOptOut).where(SmsOptOut.phone == phone)) if phone else None
    if row is not None:
        db.delete(row)
        db.commit()


# --------------------------------------------------------------------------- #
# Sending
# --------------------------------------------------------------------------- #

def send(db: Session, to: str | None, body: str, *, purpose: str, user_id: int | None = None,
         county_id: int | None = None) -> SmsLog:
    """
    Send one text and log it. Returns the log row, whose status says what
    happened: sent, failed, or skipped (not configured, or the number asked
    not to be texted). Never raises for a delivery problem.
    """
    phone = normalize_phone(to)
    row = SmsLog(county_id=county_id, user_id=user_id, to_phone=phone or (to or "")[:32],
                 purpose=purpose, body=redact(body)[:600], status="queued")
    if not phone:
        row.status, row.error = "failed", "no usable phone number"
    elif opted_out(db, phone):
        row.status, row.error = "skipped", "this number asked not to be texted"
    else:
        outcome = get_transport().send(phone, body)
        row.status, row.provider_sid, row.error = outcome.status, outcome.sid, outcome.error
        if outcome.error and outcome.error.startswith(UNSUBSCRIBED):
            opt_out(db, phone, "provider")
    row.updated_at = datetime.now(timezone.utc)
    db.add(row)
    db.commit()
    return row


def has_sent(db: Session, phone: str | None, purpose: str) -> bool:
    """Whether a text with this purpose has ever gone to this number."""
    phone = normalize_phone(phone)
    if not phone:
        return False
    return db.scalar(select(SmsLog.id).where(SmsLog.to_phone == phone, SmsLog.purpose == purpose,
                                             SmsLog.status.in_(("sent", "delivered")))) is not None


def record_status(db: Session, sid: str, status: str, error_code: str | None = None) -> bool:
    """Twilio's delivery report for a message we sent. True if we knew the message."""
    row = db.scalar(select(SmsLog).where(SmsLog.provider_sid == sid))
    if row is None:
        return False
    row.status = status[:16]
    if error_code:
        row.error = f"{error_code}"[:300]
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return True


# --------------------------------------------------------------------------- #
# Twilio webhook signatures
# --------------------------------------------------------------------------- #

def sign(url: str, params: dict, token: str) -> str:
    """
    Twilio's signature for a webhook call: HMAC-SHA1 over the full URL with
    every form field appended, sorted by name, base64. Used by the tests to
    make calls that look like Twilio's, and by signature_ok to check them.
    """
    s = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), s.encode(), hashlib.sha1).digest()).decode()


def signature_ok(url: str, params: dict, signature: str | None, token: str | None = None) -> bool:
    token = token if token is not None else settings.TWILIO_AUTH_TOKEN
    if not token or not signature:
        return False
    return hmac.compare_digest(sign(url, params, token), signature)
