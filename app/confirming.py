"""
Asking someone who was there to confirm a check-in.

A participant checks out, picks a person (a sponsor, the chair, a counsellor)
and that person gets one text with a link. The link shows the participant's
first name, the meeting and the time, and two buttons. Their answer is kept
on the check-in, where the participant, the officer and the report all see
it. It is one more layer of evidence; it decides nothing on its own.

What the person asked sees is deliberately small. Nothing says why the
participant is tracking meetings. The participant chose to ask them.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import CheckIn, Confirmation, ConfirmationStatus, Meeting, User
from .security import token_hash

RELATIONSHIPS = {
    "sponsor": "sponsor",
    "chair": "meeting chair",
    "counselor": "counselor",
    "family": "family",
    "friend": "friend",
    "other": "someone who was there",
}

ASKS_PER_CHECKIN = 3

# The name every text starts with. Carriers want the sender named in each one.
BRAND = "ActionsCount+"


# --------------------------------------------------------------------------- #
# Names, times, words
# --------------------------------------------------------------------------- #

def first_name(full: str) -> str:
    return (full or "").strip().split(" ")[0] or "A participant"


def short_name(full: str) -> str:
    """Jordan Ellis -> Jordan E. The person asked already knows who this is."""
    parts = (full or "").strip().split()
    if len(parts) >= 2:
        return f"{parts[0]} {parts[-1][0]}."
    return parts[0] if parts else "A participant"


def local(dt: datetime, tz: str) -> datetime:
    return dt.astimezone(ZoneInfo(tz))


def clock(dt: datetime, tz: str) -> str:
    """6:00 PM, without a leading zero."""
    return local(dt, tz).strftime("%I:%M %p").lstrip("0")


def when_words(dt: datetime, tz: str, now: datetime | None = None) -> str:
    """'today at 6:00 PM', 'yesterday at 7:30 PM', or 'on Tue, Sep 29 at 6:00 PM'."""
    now = now or datetime.now(timezone.utc)
    d, today = local(dt, tz).date(), local(now, tz).date()
    if d == today:
        day = "today"
    elif d == today - timedelta(days=1):
        day = "yesterday"
    else:
        day = "on " + local(dt, tz).strftime("%a, %b %d").replace(" 0", " ")
    return f"{day} at {clock(dt, tz)}"


def relationship_words(key: str | None) -> str:
    return RELATIONSHIPS.get(key or "other", RELATIONSHIPS["other"])


# --------------------------------------------------------------------------- #
# The texts themselves. Every one names the sender, and every one to someone
# who never signed up says how to stop.
# --------------------------------------------------------------------------- #

def code_text(code: str, minutes: int) -> str:
    return (f"{BRAND}: your sign-in code is {code}. It works for {minutes} minutes. "
            "Do not share it with anyone.")


def welcome_text() -> str:
    return (f"{BRAND} will text you sign-in codes and answers from people you ask to "
            "confirm a check-in. Reply STOP to opt out, HELP for help. Message and data rates "
            "may apply.")


def ready_text(name: str, url: str) -> str:
    return (f"{BRAND}: hi {first_name(name)}, your account is ready on the new site. Sign in at "
            f"{url} with this phone number and we will text you a code. Reply STOP to opt out.")


def share_text(participant: str, link: str) -> str:
    return (f"{BRAND}: {short_name(participant)} shared their meeting log with you. It stays "
            f"up to date whenever you open it: {link}\nReply STOP to opt out.")


def ask_text(participant: str, meeting: str, when: str, link: str) -> str:
    return (f"{BRAND}: {short_name(participant)} asked you to confirm they were at "
            f"{meeting} {when}. Tap to answer: {link}\nReply STOP to opt out.")


def receipt_text(meeting: str, out_at: str, confirmer: str) -> str:
    return (f"{BRAND}: you checked out of {meeting} at {out_at}. We texted "
            f"{first_name(confirmer)} to confirm you were there.")


def answer_text(confirmer: str, meeting: str, yes: bool) -> str:
    who = first_name(confirmer)
    if yes:
        return f"{BRAND}: {who} confirmed you were at {meeting}. It is on your record."
    return (f"{BRAND}: {who} could not confirm you were at {meeting}. You can add a note "
            "in the app, and your officer will see both.")


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #

def new_token() -> tuple[str, str]:
    """(token for the link, hash to store)."""
    token = secrets.token_urlsafe(24)
    return token, token_hash(token)


def by_token(db: Session, token: str) -> Confirmation | None:
    if not token or len(token) > 64:
        return None
    return db.scalar(select(Confirmation).where(Confirmation.token_hash == token_hash(token)))


def for_checkins(db: Session, checkins: list[CheckIn]) -> dict[int, list[Confirmation]]:
    """Every ask on each check-in, oldest first, in one query."""
    ids = [c.id for c in checkins] or [-1]
    out: dict[int, list[Confirmation]] = {}
    for x in db.scalars(select(Confirmation).where(Confirmation.checkin_id.in_(ids))
                        .order_by(Confirmation.asked_at, Confirmation.id)):
        out.setdefault(x.checkin_id, []).append(x)
    return out


def best(xs: list[Confirmation]) -> Confirmation | None:
    """
    The ask that says the most: a yes beats a no beats one still open beats
    one that ran out. Screens that only have room for one line use this.
    """
    rank = {"confirmed": 0, "declined": 1, "asked": 2, "expired": 3}
    return min(xs, key=lambda x: (rank[x.state], -x.asked_at.timestamp())) if xs else None


def confirmation_out(x: Confirmation) -> dict:
    return {
        "id": x.id, "checkin_id": x.checkin_id, "confirmer_id": x.confirmer_id, "name": x.name,
        "relationship": x.relationship, "relationship_words": relationship_words(x.relationship),
        "state": x.state,
        "asked_at": x.asked_at.isoformat(), "expires_at": x.expires_at.isoformat(),
        "answered_at": x.answered_at.isoformat() if x.answered_at else None,
        "answered_via": x.answered_via, "answer_note": x.answer_note,
    }


def on_words(dt: datetime, tz: str) -> str:
    """'on Thu, Oct 8 at 1:19 PM'. A report is read later, so never 'today'."""
    return "on " + local(dt, tz).strftime("%a, %b %d").replace(" 0", " ") + f" at {clock(dt, tz)}"


def words(x: Confirmation | None, tz: str) -> str | None:
    """One line for a report or a record: who, what they said, when."""
    if x is None:
        return None
    who = f"{x.name} ({relationship_words(x.relationship)})"
    if x.state == "confirmed":
        return f"{who} confirmed by text {on_words(x.answered_at, tz)}"
    if x.state == "declined":
        return f"{who} could not confirm it {on_words(x.answered_at, tz)}"
    if x.state == "asked":
        return f"{who} was asked to confirm, no answer yet"
    return f"{who} was asked to confirm and did not answer"


def expires() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=settings.CONFIRM_LINK_DAYS)


def answer(db: Session, x: Confirmation, yes: bool, *, via: str, note: str | None = None,
           ip: str | None = None) -> None:
    x.status = ConfirmationStatus.confirmed if yes else ConfirmationStatus.declined
    x.answered_at = datetime.now(timezone.utc)
    x.answered_via = via
    x.answer_note = (note or "").strip()[:400] or None
    x.answer_ip = ip
    db.commit()


def participant_and_meeting(db: Session, x: Confirmation) -> tuple[User, CheckIn, Meeting]:
    c = db.get(CheckIn, x.checkin_id)
    return db.get(User, x.user_id), c, db.get(Meeting, c.meeting_id)


def attach(db: Session, rows: list[dict], checkins: list[CheckIn]) -> None:
    """Add `confirmations` and `confirmed_by` to serialized check-ins, in place."""
    by_id = for_checkins(db, checkins)
    for d in rows:
        xs = by_id.get(d["id"], [])
        d["confirmations"] = [confirmation_out(x) for x in xs]
        top = best(xs)
        d["confirmed_by"] = confirmation_out(top) if top else None


def timeline_events(xs: list[Confirmation]) -> list[dict]:
    """The asks and answers on one check-in, for a timeline."""
    ev = []
    for x in xs:
        ev.append({"kind": "asked", "at": x.asked_at.isoformat(), "name": x.name,
                   "relationship_words": relationship_words(x.relationship)})
        if x.answered_at:
            ev.append({"kind": "answered", "at": x.answered_at.isoformat(), "name": x.name,
                       "relationship_words": relationship_words(x.relationship),
                       "yes": x.state == "confirmed", "note": x.answer_note,
                       "via": x.answered_via})
    return ev
