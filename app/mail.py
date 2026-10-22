"""
Email, through SendGrid.

The one file that knows how an email is sent, the same shape as sms.py: the
rest of the app says "email this address this" and reads the log. Nothing goes
out unless MC_SENDGRID_API_KEY and MC_EMAIL_FROM are both set; without them
every message is written to the log as skipped, which is how development,
the tests and the demo run.

Only the subject is logged, never the body, so the log is not a second copy
of every link that went out.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx
from sqlalchemy.orm import Session

from .config import settings
from .models import EmailLog

log = logging.getLogger("meetingscount.mail")

SENDGRID_URL = "https://api.sendgrid.com/v3/mail/send"


@dataclass
class Outcome:
    status: str                 # sent | failed | skipped
    message_id: str | None = None
    error: str | None = None


class Transport(Protocol):
    def send(self, to: str, subject: str, text: str, html: str | None) -> Outcome: ...


class SendGridTransport:
    """The real thing. One HTTPS call per message, ten seconds at most."""

    def __init__(self, api_key: str, sender: str, sender_name: str):
        self.api_key, self.sender, self.sender_name = api_key, sender, sender_name

    def send(self, to: str, subject: str, text: str, html: str | None) -> Outcome:
        content = [{"type": "text/plain", "value": text}]
        if html:
            content.append({"type": "text/html", "value": html})
        payload = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": self.sender, "name": self.sender_name},
            "subject": subject,
            "content": content,
            # No open or click tracking: a tracking link would rewrite the
            # one link in the message, and nobody needs to know when a
            # lawyer opened an email.
            "tracking_settings": {"click_tracking": {"enable": False},
                                  "open_tracking": {"enable": False}},
        }
        try:
            r = httpx.post(SENDGRID_URL, json=payload, timeout=10,
                           headers={"Authorization": f"Bearer {self.api_key}"})
        except httpx.HTTPError as exc:
            return Outcome("failed", error=f"could not reach SendGrid: {exc}"[:300])
        if r.status_code >= 400:
            detail = r.text
            try:
                errs = r.json().get("errors") or []
                detail = "; ".join(e.get("message", "") for e in errs) or r.text
            except ValueError:
                pass
            return Outcome("failed", error=f"{r.status_code}: {detail}"[:300])
        return Outcome("sent", message_id=r.headers.get("X-Message-Id"))


class SilentTransport:
    """Nothing configured. Says so in the log and sends nothing."""

    def send(self, to: str, subject: str, text: str, html: str | None) -> Outcome:
        log.info("email not sent (email is not configured) to %s: %s", to, subject)
        return Outcome("skipped", error="email is not configured")


# Tests put their own transport here.
transport: Transport | None = None


def get_transport() -> Transport:
    if not settings.email_configured:
        return SilentTransport()
    if transport is not None:
        return transport
    return SendGridTransport(settings.SENDGRID_API_KEY, settings.EMAIL_FROM, settings.EMAIL_FROM_NAME)


def can_email(address: str | None) -> bool:
    """Whether a message to this address would go anywhere."""
    return settings.email_configured and valid(address)


def valid(address: str | None) -> bool:
    a = (address or "").strip()
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", a)) and len(a) <= 200


def normalize(address: str | None) -> str | None:
    a = (address or "").strip().lower()
    return a if valid(a) else None


def mask(address: str | None) -> str:
    """s***@example.com: enough to recognise, not enough to copy."""
    a = normalize(address)
    if not a:
        return ""
    name, _, domain = a.partition("@")
    return f"{name[0]}***@{domain}"


def send(db: Session, to: str, subject: str, text: str, *, html: str | None = None,
         purpose: str, user_id: int | None = None, county_id: int | None = None) -> EmailLog:
    """Send one email and log what happened. Never raises."""
    row = EmailLog(county_id=county_id, user_id=user_id, to_email=to, purpose=purpose,
                   subject=subject[:200], status="queued")
    if not settings.email_configured:
        out = SilentTransport().send(to, subject, text, html)
    else:
        try:
            out = get_transport().send(to, subject, text, html)
        except Exception as exc:                                     # noqa: BLE001
            out = Outcome("failed", error=f"{type(exc).__name__}: {exc}"[:300])
    row.status, row.provider_id, row.error = out.status, out.message_id, out.error
    db.add(row)
    db.commit()
    return row
