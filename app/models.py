"""
The data model.

Shape: County (the tenant) -> Program -> People. A participant has
Requirements (what the court ordered), attends Meetings, and produces
CheckIns. Anything a check-in's automatic verdict doesn't settle becomes a
Review that a human decides. Everything staff look at is written to AuditLog.

Tenancy: every table that holds anyone's data carries county_id. Application
code filters on it; Postgres row-level security enforces it (see
alembic/versions/002_rls.py). Belt and braces, because a missed WHERE clause
in a multi-county system is a data breach.
"""
from __future__ import annotations

import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, TypeDecorator


class UTCDateTime(TypeDecorator):
    """
    Always store and return timezone-aware UTC.

    SQLite has no real timestamp type and hands back naive datetimes, which
    then blow up when compared with aware ones. Postgres keeps the zone. This
    makes both behave identically so the compliance math never has to care
    which database it is running on.
    """
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else \
            value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, enum.Enum):
    participant = "participant"
    officer = "officer"          # probation officer / coordinator
    admin = "admin"              # county admin: staff, settings, audit
    superuser = "superuser"      # us, for support. Break-glass only.


class MeetingCategory(str, enum.Enum):
    public = "public"                        # open AA/NA and similar
    private = "private"
    community_service = "community_service"
    therapy = "therapy"


class MeetingSource(str, enum.Enum):
    directory = "directory"      # imported from a meeting feed; countable
    staff = "staff"              # created by an officer/admin; countable
    personal = "personal"        # created by a participant; self-tracking only


class CheckInStatus(str, enum.Enum):
    confirmed = "confirmed"
    flagged = "flagged"
    rejected = "rejected"


class ReviewDecision(str, enum.Enum):
    approved = "approved"        # counts toward the requirement
    denied = "denied"            # does not count
    more_info = "more_info"      # waiting on the participant


class RequirementPeriod(str, enum.Enum):
    week = "week"
    month = "month"


class ConfirmationStatus(str, enum.Enum):
    asked = "asked"              # text sent, no answer yet
    confirmed = "confirmed"      # they said they saw the participant there
    declined = "declined"        # they said they could not confirm it


# --------------------------------------------------------------------------- #
# Tenancy
# --------------------------------------------------------------------------- #

class County(Base):
    __tablename__ = "counties"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(2), default="MI")
    timezone: Mapped[str] = mapped_column(String(64), default="America/Detroit")
    # Per-county policy. Defaults come from the big-picture doc.
    default_radius_ft: Mapped[float] = mapped_column(Float, default=500)
    default_window_min: Mapped[int] = mapped_column(Integer, default=60)
    retention_days: Mapped[int] = mapped_column(Integer, default=1095)   # 3 years
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    programs: Mapped[list[Program]] = relationship(back_populates="county")


class Program(Base):
    """A court program inside a county: sobriety court, OWI probation, diversion."""
    __tablename__ = "programs"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int] = mapped_column(ForeignKey("counties.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    join_code: Mapped[str] = mapped_column(String(16), unique=True)   # what an officer gives a participant
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    county: Mapped[County] = relationship(back_populates="programs")


# --------------------------------------------------------------------------- #
# People
# --------------------------------------------------------------------------- #

class User(Base):
    """
    Staff and participants both live here. `cognito_sub` is the link to the
    identity provider; it is null while dev auth is on.
    """
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    cognito_sub: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.participant)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(200), index=True)
    # Encrypt at the column level before this holds a real number (KMS; see
    # the security notes in README). Stored plain in dev only.
    phone: Mapped[str | None] = mapped_column(String(32))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # When the participant accepted the consent screen, and which wording they
    # saw. A new CONSENT_VERSION asks everyone again.
    consented_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    consent_version: Mapped[str | None] = mapped_column(String(16))

    # Where to look for meetings: a town the participant picked, as the
    # directory's centre for that town. Chosen once, not a location log.
    area_label: Mapped[str | None] = mapped_column(String(80))
    area_lat: Mapped[float | None] = mapped_column(Float)
    area_lng: Mapped[float | None] = mapped_column(Float)

    # Sign-in. Staff use a password and, if they turn it on, an authenticator
    # app. Participants use their phone number and a one-time code, so there
    # is no password to lose. Hashes only; see app/security.py.
    password_hash: Mapped[str | None] = mapped_column(String(200))
    password_set_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    totp_secret: Mapped[str | None] = mapped_column(String(64))
    totp_enabled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    participant: Mapped[Participant | None] = relationship(
        back_populates="user", uselist=False, foreign_keys="Participant.user_id")


class Participant(Base):
    """Court-side facts about a participant. Separate from User so staff rows stay clean."""
    __tablename__ = "participants"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int] = mapped_column(ForeignKey("counties.id"), index=True)
    program_id: Mapped[int | None] = mapped_column(ForeignKey("programs.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    officer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    case_number: Mapped[str | None] = mapped_column(String(64))
    enrolled_on: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ends_on: Mapped[datetime | None] = mapped_column(UTCDateTime)
    notes: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="participant", foreign_keys=[user_id])
    officer: Mapped[User | None] = relationship(foreign_keys=[officer_id])
    requirements: Mapped[list[Requirement]] = relationship(back_populates="participant")


class Requirement(Base):
    """
    What the order says: "3 public meetings a week from Sep 1 to Dec 31".
    A participant can have more than one (meetings AND community service).
    """
    __tablename__ = "requirements"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int] = mapped_column(ForeignKey("counties.id"), index=True)
    participant_id: Mapped[int] = mapped_column(ForeignKey("participants.id"), index=True)
    category: Mapped[MeetingCategory | None] = mapped_column(Enum(MeetingCategory))  # null = any
    count_required: Mapped[int] = mapped_column(Integer, default=3)
    period: Mapped[RequirementPeriod] = mapped_column(Enum(RequirementPeriod), default=RequirementPeriod.week)
    starts_on: Mapped[datetime] = mapped_column(UTCDateTime)
    ends_on: Mapped[datetime | None] = mapped_column(UTCDateTime)
    order_reference: Mapped[str | None] = mapped_column(String(120))   # court order number
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    participant: Mapped[Participant] = relationship(back_populates="requirements")


# --------------------------------------------------------------------------- #
# Meetings and check-ins
# --------------------------------------------------------------------------- #

class MeetingSeries(Base):
    """
    A meeting that happens every week: the directory. Imported from an area's
    published list, one row per name, weekday and time. Each week's instance
    becomes a Meeting row as it is needed, so a check-in always points at one
    date and the compliance maths never has to know about recurrence.

    county_id is null for the shared directory, which every county sees.
    """
    __tablename__ = "meeting_series"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[MeetingCategory] = mapped_column(Enum(MeetingCategory), default=MeetingCategory.public)
    source_name: Mapped[str] = mapped_column(String(60), index=True)      # aa-semi.org
    source_url: Mapped[str | None] = mapped_column(String(300))
    external_key: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    weekday: Mapped[int] = mapped_column(Integer)                         # 0 = Monday
    start_minute: Mapped[int] = mapped_column(Integer)                    # after local midnight
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    timezone: Mapped[str] = mapped_column(String(64), default="America/Detroit")
    street: Mapped[str | None] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(80), index=True)
    state: Mapped[str] = mapped_column(String(2), default="MI")
    zip: Mapped[str | None] = mapped_column(String(10))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    radius_ft: Mapped[float] = mapped_column(Float, default=500)
    time_window_min: Mapped[int] = mapped_column(Integer, default=60)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    imported_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    __table_args__ = (Index("ix_meeting_series_latlng", "lat", "lng"),)

    @property
    def address(self) -> str:
        parts = [p for p in (self.street, self.city) if p]
        tail = " ".join(p for p in (self.state, self.zip) if p)
        return ", ".join(parts + ([tail] if tail else []))


class Meeting(Base):
    __tablename__ = "meetings"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    series_id: Mapped[int | None] = mapped_column(ForeignKey("meeting_series.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[MeetingCategory] = mapped_column(Enum(MeetingCategory), default=MeetingCategory.public)
    source: Mapped[MeetingSource] = mapped_column(Enum(MeetingSource), default=MeetingSource.staff)
    address: Mapped[str | None] = mapped_column(String(300))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    starts_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    radius_ft: Mapped[float] = mapped_column(Float, default=500)
    time_window_min: Mapped[int] = mapped_column(Integer, default=60)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    # One row per series and date, however many phones ask for the same week.
    __table_args__ = (Index("ux_meetings_series_start", "series_id", "starts_at", unique=True),)

    @property
    def counts_toward_requirement(self) -> bool:
        """Personal meetings are self-tracking only. A participant cannot
        invent a meeting at home and have it satisfy a court order."""
        return self.source in (MeetingSource.directory, MeetingSource.staff)

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_min)


class CheckIn(Base):
    __tablename__ = "checkins"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    meeting_id: Mapped[int] = mapped_column(ForeignKey("meetings.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    # The pin, exactly as the device reported it. Nulled out by the retention
    # job after PIN_RETENTION_DAYS; distance_ft and the reasons survive.
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float)
    pin_purged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    checked_in_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    checkout_lat: Mapped[float | None] = mapped_column(Float)
    checkout_lng: Mapped[float | None] = mapped_column(Float)

    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    status: Mapped[CheckInStatus] = mapped_column(Enum(CheckInStatus), index=True)
    confidence: Mapped[int] = mapped_column(Integer)
    distance_ft: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list] = mapped_column(JSON, default=list)

    __table_args__ = (Index("ix_checkins_user_time", "user_id", "checked_in_at"),
                      Index("ix_checkins_user_meeting", "user_id", "meeting_id"))

    @property
    def needs_review(self) -> bool:
        return self.status in (CheckInStatus.flagged, CheckInStatus.rejected)


class Review(Base):
    """
    A human decision on a check-in the engine could not settle. This is the
    'a human decides anything that hurts' principle, in a table.

    Append-only. A changed decision, or a decision after asking for more
    information, is a new row; the latest row is the one that counts. See
    app/reviewing.py.
    """
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    checkin_id: Mapped[int] = mapped_column(ForeignKey("checkins.id"), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[ReviewDecision] = mapped_column(Enum(ReviewDecision))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    checkin: Mapped[CheckIn] = relationship()
    reviewer: Mapped[User] = relationship()


class Dispute(Base):
    """A participant's side of the story on a flagged or rejected check-in."""
    __tablename__ = "disputes"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    checkin_id: Mapped[int] = mapped_column(ForeignKey("checkins.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Confirmer(Base):
    """
    Someone a participant can ask to confirm they were at a meeting: a
    sponsor, the chair, a counsellor. Chosen by the participant and saved so
    the next ask is one tap. Seen only by the participant, and by an officer
    reviewing a check-in this person answered for.
    """
    __tablename__ = "confirmers"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32))
    relationship: Mapped[str] = mapped_column(String(32), default="other")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    removed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Confirmation(Base):
    """
    One ask: a text to one person about one check-in, and what they answered.
    The link in the text carries a random token and only its hash is stored,
    the same as a session. Who was asked is copied onto the row so the record
    stays whole if the saved contact is removed later.
    """
    __tablename__ = "confirmations"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    checkin_id: Mapped[int] = mapped_column(ForeignKey("checkins.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    confirmer_id: Mapped[int | None] = mapped_column(ForeignKey("confirmers.id"))
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32))
    relationship: Mapped[str] = mapped_column(String(32), default="other")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    asked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    status: Mapped[ConfirmationStatus] = mapped_column(Enum(ConfirmationStatus),
                                                        default=ConfirmationStatus.asked)
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    answered_via: Mapped[str | None] = mapped_column(String(16))     # link | text
    answer_note: Mapped[str | None] = mapped_column(String(400))
    answer_ip: Mapped[str | None] = mapped_column(String(64))

    @property
    def open(self) -> bool:
        """Still waiting for an answer, and the link still works."""
        return self.status == ConfirmationStatus.asked and self.expires_at > utcnow()

    @property
    def state(self) -> str:
        """asked | confirmed | declined | expired, for screens and reports."""
        if self.status == ConfirmationStatus.asked and self.expires_at <= utcnow():
            return "expired"
        return self.status.value


class SmsLog(Base):
    """
    Every text the system tried to send and what came of it. The body is kept
    with any link token blanked out. See app/sms.py.
    """
    __tablename__ = "sms_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    to_phone: Mapped[str] = mapped_column(String(32), index=True)
    purpose: Mapped[str] = mapped_column(String(32))      # login_code, ask, receipt, answer, welcome
    body: Mapped[str] = mapped_column(String(600))
    status: Mapped[str] = mapped_column(String(16))       # queued, sent, delivered, failed, skipped
    provider_sid: Mapped[str | None] = mapped_column(String(64), index=True)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SmsOptOut(Base):
    """A number that replied STOP, or that Twilio refused as unsubscribed. Never texted."""
    __tablename__ = "sms_opt_outs"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    source: Mapped[str] = mapped_column(String(16), default="reply")   # reply | provider
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Session(Base):
    """
    One signed-in browser. The cookie holds a random token; only its hash is
    stored, so a database dump does not let anyone sign in as a person.
    Revocable, and every row says which device and address it came from.
    """
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))

    user: Mapped[User] = relationship()


class LoginCode(Base):
    """
    A one-time code for a participant to sign in with. Hashed with a server
    key, single use, short lived, and thrown away after a few wrong guesses.
    Until SMS is wired up, an officer reads the code out and `issued_by_id`
    records who did.
    """
    __tablename__ = "login_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    issued_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    delivered_via: Mapped[str] = mapped_column(String(16), default="officer")

    @property
    def live(self) -> bool:
        return self.used_at is None and self.attempts < 5 and self.expires_at > utcnow()


class AuditLog(Base):
    """
    Append-only. Every staff read of someone's record lands here, because
    insider misuse is likelier than an outside breach. Never updated, never
    deleted by application code.
    """
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    county_id: Mapped[int | None] = mapped_column(ForeignKey("counties.id"), index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    actor_name: Mapped[str | None] = mapped_column(String(120))   # denormalized: survives user deletion
    action: Mapped[str] = mapped_column(String(64), index=True)   # view_participant, decide_review, export...
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(400))
    ip: Mapped[str | None] = mapped_column(String(64))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
