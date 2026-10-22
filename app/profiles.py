"""
Self-serve profiles: made one at a time at /join, or many at once from a list
of people moving over from the old site.

Both go through make_profile, so a person brought in from a list is exactly
what they would have been had they signed up themselves: a participant in the
self-pay county, a weekly requirement, and a subscription row.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import billing
from .models import Participant, Requirement, RequirementPeriod, Role, User
from .security import normalize_phone

DEFAULT_PER_WEEK = 3


def make_profile(db: Session, *, name: str, phone: str, email: str | None, per_week: int,
                 starts_on: datetime, ends_on: datetime | None = None,
                 order_reference: str = "self-reported", status: str | None = None) -> User:
    """A new self-serve participant. The caller commits. `ends_on` is when
    probation ends, if the person knows; the requirement stops there too."""
    county, program = billing.self_serve_home(db)
    user = User(county_id=county.id, role=Role.participant, name=name, email=email, phone=phone)
    db.add(user)
    db.flush()
    p = Participant(county_id=county.id, user_id=user.id, program_id=program.id,
                    officer_id=None, enrolled_on=datetime.now(timezone.utc), ends_on=ends_on)
    db.add(p)
    db.flush()
    db.add(Requirement(county_id=county.id, participant_id=p.id, category=None,
                       count_required=per_week, period=RequirementPeriod.week,
                       starts_on=starts_on, ends_on=ends_on, order_reference=order_reference))
    row = billing.ensure_row(db, user)
    if status:
        row.status = status
    return user


# --------------------------------------------------------------------------- #
# Bringing in a list
# --------------------------------------------------------------------------- #

COLUMNS = {
    "name": ("name", "fullname", "participant", "participantname", "client", "clientname", "member"),
    "first": ("first", "firstname", "givenname"),
    "last": ("last", "lastname", "surname", "familyname"),
    "phone": ("phone", "mobile", "cell", "cellphone", "phonenumber", "mobilenumber",
              "mobilephone", "cellnumber", "number", "textnumber"),
    "email": ("email", "emailaddress", "mail"),
    "per_week": ("perweek", "meetingsperweek", "meetingsaweek", "required", "requiredperweek",
                 "weekly", "meetingsrequired"),
    "start": ("start", "startdate", "startson", "starts", "enrolled", "enrolledon", "since",
              "joined", "datejoined"),
    "end": ("end", "enddate", "endson", "ends", "probationend", "probationenddate",
            "probationends", "through", "until"),
}


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (header or "").lower())


def _columns(headers: list[str]) -> dict[str, str]:
    """Which header in the file means which field."""
    by_key = {_key(h): h for h in headers}
    return {field_: next((by_key[k] for k in keys if k in by_key), None)
            for field_, keys in COLUMNS.items()}


def _date(value: str) -> datetime | None:
    v = (value or "").strip()
    if not v:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", v):                      # a spreadsheet date: days since 1899-12-30
        return datetime(1899, 12, 30, tzinfo=timezone.utc) + timedelta(days=float(v))
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d %H:%M:%S", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    new_users: list[int] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{len(self.created)} new, {len(self.already)} already here, {len(self.skipped)} skipped"]
        lines += [f"  new: {x}" for x in self.created]
        lines += [f"  already here: {x}" for x in self.already]
        lines += [f"  skipped: {x}" for x in self.skipped]
        return "\n".join(lines)


def import_people(db: Session, rows: list[dict], *, covered: bool = False) -> ImportReport:
    """
    One profile per row with a name and a usable mobile number. A number that
    already has an account is left alone, so running the same list twice is
    harmless. `covered` marks everyone in this list as needing no payment, for
    people who already paid on the old site. The caller commits.
    """
    report = ImportReport()
    if not rows:
        return report
    cols = _columns(list(rows[0].keys()))
    if not cols["phone"] or not (cols["name"] or cols["first"]):
        raise ValueError("The list needs a name column (or first and last name) and a phone column. "
                         f"Found: {', '.join(rows[0].keys())}")
    seen: set[str] = set()
    for i, r in enumerate(rows, start=2):                    # row 1 is the header
        get = lambda f: str(r.get(cols[f]) or "").strip() if cols[f] else ""   # noqa: E731
        name = " ".join((get("name") or f"{get('first')} {get('last')}").split())[:120]
        phone = normalize_phone(get("phone"))
        if len(name) < 2:
            report.skipped.append(f"row {i}: no name")
            continue
        if not phone or len(phone) < 11:
            report.skipped.append(f"row {i}: {name}, no usable mobile number")
            continue
        if phone in seen:
            report.skipped.append(f"row {i}: {name}, same number as an earlier row")
            continue
        seen.add(phone)
        existing = db.scalar(select(User).where(User.phone == phone, User.role == Role.participant))
        if existing is not None:
            report.already.append(f"{name} (has an account as {existing.name})")
            continue
        try:
            per_week = max(1, min(21, int(float(get("per_week") or DEFAULT_PER_WEEK))))
        except ValueError:
            per_week = DEFAULT_PER_WEEK
        email = get("email") or None
        starts_on = _date(get("start")) or datetime.now(timezone.utc)
        ends_on = _date(get("end"))
        if ends_on is not None and ends_on <= starts_on:
            ends_on = None                                   # a typo in the sheet, not a one-day order
        user = make_profile(db, name=name, phone=phone, email=email[:200] if email else None,
                            per_week=per_week, starts_on=starts_on, ends_on=ends_on,
                            order_reference="moved from the old site",
                            status="covered" if covered else None)
        report.created.append(f"{name}, {per_week} a week")
        report.new_users.append(user.id)
    return report
