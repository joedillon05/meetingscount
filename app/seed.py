"""
Demo data: one county, two programs, two officers, eleven participants, nine
weeks of meetings and check-ins.

The point is that every state the dashboard can show actually occurs:
compliant, short, pending review, disputed, reviewed-and-approved,
reviewed-and-denied, never-checked-out, and a participant who stopped showing
up entirely.

    python -m app.seed         wipe and reseed
    POST /api/demo/seed        same, from the dashboard button
"""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from .config import settings
from .db import SessionLocal, create_all, engine
from .geo import feet_to_meters
from .confirming import expires as link_expires, new_token
from .models import (
    AuditLog, Base, CheckIn, CheckInStatus, Confirmation, ConfirmationStatus, Confirmer, County,
    Dispute, Meeting, MeetingCategory, MeetingSource, Participant, Program, Requirement,
    RequirementPeriod, Review, ReviewDecision, Role, SmsLog, SmsOptOut, User,
)
from .routers.auth import DEMO_PASSWORD
from .security import hash_password
from .verification import CheckInContext, MeetingSpec, verify

M_PER_DEG = 111_320
rng = random.Random(20260917)


def _offset(lat: float, lng: float, north_ft: float, east_ft: float) -> tuple[float, float]:
    return (lat + feet_to_meters(north_ft) / M_PER_DEG,
            lng + feet_to_meters(east_ft) / (M_PER_DEG * math.cos(math.radians(lat))))


# Real venues around Ann Arbor / Ypsilanti so the map looks like somewhere.
VENUES = [
    ("AA Open Meeting - St. Andrew's", MeetingCategory.public, "306 N Division St, Ann Arbor", 42.2830, -83.7455, 500),
    ("NA Step Study - Ypsilanti", MeetingCategory.public, "220 N Huron St, Ypsilanti", 42.2450, -83.6130, 500),
    ("Sobriety Court Group - Courthouse", MeetingCategory.public, "101 E Huron St, Ann Arbor", 42.2809, -83.7472, 500),
    ("SMART Recovery - Downtown Detroit", MeetingCategory.public, "Campus Martius, Detroit", 42.3317, -83.0466, 1000),
    ("Community Service - Food Gatherers", MeetingCategory.community_service, "1 Carrot Way, Ann Arbor", 42.3000, -83.7060, 800),
]

PARTICIPANTS = [
    # name, officer index, pattern
    ("Marcus Webb",      0, "compliant"),
    ("Dana Ruiz",        0, "compliant"),
    ("Tyler Nowak",      0, "short"),
    ("Aisha Bello",      0, "flagged_pending"),
    ("Chris Lindqvist",  0, "disputed"),
    ("Jenna Park",       1, "compliant"),
    ("Devon Hargrove",   1, "borderline"),
    ("Sam Okafor",       1, "stopped"),
    ("Riley Chen",       1, "new"),
    ("Pat Moreau",       1, "sloppy"),
    ("Jordan Ellis",     0, "first_day"),
]


def _wipe(db) -> None:
    if settings.DATABASE_URL.startswith("sqlite"):
        # Local dev has no migrations, so rebuild the tables outright. That
        # also picks up any column added since the file was first created.
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        return
    for model in (AuditLog, SmsLog, SmsOptOut, Confirmation, Confirmer, Review, Dispute,
                  CheckIn, Requirement, Meeting, Participant, User, Program, County):
        db.execute(delete(model))
    db.commit()


def _spec(m: Meeting) -> MeetingSpec:
    return MeetingSpec(id=m.id, name=m.name, lat=m.lat, lng=m.lng, starts_at=m.starts_at,
                       duration_min=m.duration_min, radius_ft=m.radius_ft,
                       time_window_min=m.time_window_min, category=m.category.value)


def _checkin(db, m: Meeting, user: User, *, north=0.0, east=0.0, minutes=0.0,
             accuracy=14.0, checkout=True, ip="76.120.44.9") -> CheckIn:
    lat, lng = _offset(m.lat, m.lng, north, east)
    at = m.starts_at + timedelta(minutes=minutes)
    v = verify(CheckInContext(meeting=_spec(m), lat=lat, lng=lng, at=at,
                              accuracy_m=accuracy, ip=ip, user_agent="seed"))
    c = CheckIn(county_id=m.county_id, meeting_id=m.id, user_id=user.id, lat=lat, lng=lng,
                accuracy_m=accuracy, checked_in_at=at, ip=ip, user_agent="seed",
                status=CheckInStatus(v.status), confidence=v.confidence,
                distance_ft=v.distance_ft, reasons=[r.as_dict() for r in v.results])
    out_at = at + timedelta(minutes=m.duration_min - rng.randint(2, 8))
    # A meeting still running has nobody checked out of it yet, which is what
    # lets the phone demo show the "you're checked in" card.
    if checkout and v.status != "rejected" and out_at < datetime.now(timezone.utc):
        c.checked_out_at = out_at
        c.checkout_lat, c.checkout_lng = _offset(m.lat, m.lng, north * 0.8, east * 0.8)
    db.add(c)
    db.flush()
    return c


def seed(reset: bool = True) -> dict:
    create_all()
    db = SessionLocal()
    try:
        if reset:
            _wipe(db)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)

        county = County(name="Washtenaw County", state="MI", timezone="America/Detroit")
        db.add(county)
        db.flush()

        programs = [
            Program(county_id=county.id, name="OWI Probation", join_code="WASH-OWI"),
            Program(county_id=county.id, name="Sobriety Court", join_code="WASH-SC"),
        ]
        db.add_all(programs)
        db.flush()

        admin = User(county_id=county.id, role=Role.admin, name="Admin (county)",
                     email="admin@example.gov")
        officers = [
            User(county_id=county.id, role=Role.officer, name="Officer Ramirez",
                 email="ramirez@example.gov"),
            User(county_id=county.id, role=Role.officer, name="Officer Chen",
                 email="chen@example.gov"),
        ]
        # Demo staff can sign in. One password for all three, printed on the
        # sign-in screen while MC_DEMO is on, and meaningless anywhere real.
        for person in (admin, *officers):
            person.password_hash = hash_password(DEMO_PASSWORD)
            person.password_set_at = now
        db.add_all([admin, *officers])
        db.flush()

        # --- meetings: three weeks back, weekly, plus one open right now ---
        meetings: list[Meeting] = []
        for week in range(8, -1, -1):
            for vi, (name, cat, addr, lat, lng, radius) in enumerate(VENUES):
                start = (now - timedelta(days=week * 7, hours=vi * 2 + 1)).replace(minute=0)
                meetings.append(Meeting(
                    county_id=county.id, name=name, category=cat, source=MeetingSource.staff,
                    address=addr, lat=lat, lng=lng, starts_at=start,
                    duration_min=240 if cat == MeetingCategory.community_service else 60,
                    radius_ft=radius, time_window_min=60, created_by_id=officers[vi % 2].id))
        # One open for check-in right now, for the phone demo.
        live = Meeting(county_id=county.id, name="AA Open Meeting - St. Andrew's",
                       category=MeetingCategory.public, source=MeetingSource.staff,
                       address="306 N Division St, Ann Arbor", lat=42.2830, lng=-83.7455,
                       starts_at=now - timedelta(minutes=20), duration_min=60,
                       radius_ft=500, time_window_min=60, created_by_id=officers[0].id)
        # And one coming up later today.
        soon = Meeting(county_id=county.id, name="Sobriety Court Group - Courthouse",
                       category=MeetingCategory.public, source=MeetingSource.staff,
                       address="101 E Huron St, Ann Arbor", lat=42.2809, lng=-83.7472,
                       starts_at=now + timedelta(hours=3), duration_min=90,
                       radius_ft=500, time_window_min=60, created_by_id=officers[0].id)
        meetings += [live, soon]
        db.add_all(meetings)
        db.flush()

        public_meetings = [m for m in meetings
                           if m.category == MeetingCategory.public and m.starts_at < now]
        service_meetings = [m for m in meetings
                            if m.category == MeetingCategory.community_service and m.starts_at < now]

        participants: list[Participant] = []
        for idx, (name, oi, pattern) in enumerate(PARTICIPANTS, start=1):
            # Readable, stable numbers: a demo works better when the phone
            # number on screen is one someone can type without squinting.
            u = User(county_id=county.id, role=Role.participant, name=name,
                     email=None, phone=f"+1734555{100 + idx:04d}")
            if pattern != "first_day":
                # Everyone with history has been through the consent screen.
                u.consented_at = now - timedelta(days=60)
                u.consent_version = settings.CONSENT_VERSION
            db.add(u)
            db.flush()
            p = Participant(county_id=county.id, program_id=programs[oi % 2].id, user_id=u.id,
                            officer_id=officers[oi].id,
                            case_number=f"26-{rng.randint(1000, 9999)}-FH",
                            enrolled_on=now - timedelta(
                                days={"new": 4, "first_day": 0}.get(pattern, 60)))
            db.add(p)
            db.flush()
            participants.append(p)

            db.add(Requirement(
                county_id=county.id, participant_id=p.id, category=MeetingCategory.public,
                count_required=3, period=RequirementPeriod.week,
                starts_on=now - timedelta(days=0 if pattern == "first_day" else 60),
                ends_on=now + timedelta(days=120),
                order_reference=f"ORD-26-{rng.randint(100, 999)}"))
            if pattern in ("compliant", "short"):
                db.add(Requirement(
                    county_id=county.id, participant_id=p.id,
                    category=MeetingCategory.community_service, count_required=1,
                    period=RequirementPeriod.week, starts_on=now - timedelta(days=60),
                    ends_on=now + timedelta(days=120),
                    order_reference=f"ORD-26-{rng.randint(100, 999)}"))
            db.flush()

            _behave(db, pattern, p, u, public_meetings, service_meetings, now)

        db.commit()

        # A couple of reviews already decided, so the participant detail view
        # shows what a resolved item looks like.
        flagged = db.query(CheckIn).filter(CheckIn.status == CheckInStatus.flagged).all()
        for i, c in enumerate(flagged[:3]):
            db.add(Review(county_id=county.id, checkin_id=c.id, reviewer_id=officers[i % 2].id,
                          decision=ReviewDecision.approved if i % 2 == 0 else ReviewDecision.denied,
                          note=("GPS was poor downtown, participant was seen by the facilitator."
                                if i % 2 == 0 else
                                "Could not corroborate; participant did not respond.")))
        db.commit()

        # A few people have someone who confirms for them by text, so the
        # queue, the record and the report show what that looks like.
        _vouchers(db, county, participants, now)

        counts = {
            "counties": 1, "programs": len(programs),
            "staff": 1 + len(officers), "participants": len(participants),
            "meetings": len(meetings), "checkins": db.query(CheckIn).count(),
            "reviews": db.query(Review).count(), "disputes": db.query(Dispute).count(),
            "confirmations": db.query(Confirmation).count(),
        }
        return counts
    finally:
        db.close()


VOUCHERS = {
    # participant: (who they ask, how they know them)
    "Marcus Webb": ("Kim Alvarez", "sponsor"),
    "Jenna Park": ("Pete Walsh", "chair"),
    "Devon Hargrove": ("Pete Walsh", "chair"),
    "Aisha Bello": ("Rosa Nguyen", "sponsor"),
}


def _vouchers(db, county: County, participants: list[Participant], now: datetime) -> None:
    """
    Saved confirmers and a few asks on recent check-ins: mostly answered yes,
    one still waiting (Devon's latest), one no (Aisha's second most recent).
    """
    for p in participants:
        who = db.get(User, p.user_id)
        if who.name not in VOUCHERS:
            continue
        name, relationship = VOUCHERS[who.name]
        k = Confirmer(county_id=county.id, user_id=who.id, name=name,
                      phone=f"+1734555{200 + p.id:04d}", relationship=relationship)
        db.add(k)
        db.flush()
        # Checked-out ones only: the meeting open right now is left alone, so
        # the phone demo can be the first to ask.
        recent = db.scalars(select(CheckIn).where(CheckIn.user_id == who.id,
                                                  CheckIn.status != CheckInStatus.rejected,
                                                  CheckIn.checked_out_at.is_not(None))
                            .order_by(CheckIn.checked_in_at.desc()).limit(3)).all()
        for i, c in enumerate(recent):
            asked_at = min(c.checked_out_at, now)
            _, digest = new_token()
            x = Confirmation(county_id=county.id, checkin_id=c.id, user_id=who.id,
                             confirmer_id=k.id, name=name, phone=k.phone,
                             relationship=relationship, token_hash=digest, asked_at=asked_at,
                             expires_at=asked_at + (link_expires() - now))
            if who.name == "Devon Hargrove" and i == 0:
                pass                                   # still waiting for Pete
            elif who.name == "Aisha Bello" and i == 1:
                x.status = ConfirmationStatus.declined
                x.answered_at = asked_at + timedelta(minutes=40)
                x.answered_via = "link"
                x.answer_note = "I was not at that one. Rosa."
            else:
                x.status = ConfirmationStatus.confirmed
                x.answered_at = min(asked_at + timedelta(minutes=rng.randint(4, 50)), now)
                x.answered_via = "text" if i % 2 else "link"
            db.add(x)
    db.commit()


def _behave(db, pattern: str, p: Participant, u: User,
            public: list[Meeting], service: list[Meeting], now: datetime) -> None:
    """Give each demo participant a recognizable attendance story."""
    recent = [m for m in public if m.starts_at > now - timedelta(days=63)]
    recent.sort(key=lambda m: m.starts_at)

    if pattern == "compliant":
        for m in recent:
            _checkin(db, m, u, north=rng.uniform(-120, 120), east=rng.uniform(-120, 120),
                     minutes=rng.uniform(-12, 5), accuracy=rng.uniform(6, 18))
        for m in service:
            _checkin(db, m, u, north=60, east=30, minutes=-5, accuracy=12)

    elif pattern == "short":
        for m in recent[::2]:                       # every other meeting: falls behind
            _checkin(db, m, u, north=rng.uniform(-100, 100), east=rng.uniform(-100, 100),
                     minutes=rng.uniform(-10, 8), accuracy=rng.uniform(8, 20))

    elif pattern == "flagged_pending":
        # Attends reliably but works downtown where GPS is unreliable, so a
        # third of their check-ins land in the review queue. The exact case the
        # queue exists for: probably fine, needs a human.
        for i, m in enumerate(recent):
            if i % 3 == 0:
                _checkin(db, m, u, north=80, east=40, minutes=2, accuracy=rng.uniform(140, 220))
            else:
                _checkin(db, m, u, north=40, east=20, minutes=-6, accuracy=12)

    elif pattern == "disputed":
        for i, m in enumerate(recent):
            if i == len(recent) - 1:                # last one rejected, then disputed
                c = _checkin(db, m, u, north=2100, east=800, minutes=4,
                             accuracy=15, checkout=False)
                db.add(Dispute(county_id=p.county_id, checkin_id=c.id, user_id=u.id,
                               message=("I was in the basement meeting room and my phone "
                                        "put me across the street. The chair can confirm "
                                        "I was there the whole hour.")))
            else:
                _checkin(db, m, u, north=50, east=-30, minutes=-8, accuracy=11)

    elif pattern == "borderline":
        for m in recent:
            _checkin(db, m, u, north=rng.uniform(380, 470), east=rng.uniform(-80, 80),
                     minutes=rng.uniform(20, 45), accuracy=rng.uniform(25, 60))

    elif pattern == "stopped":
        # Went to everything for five weeks, then nothing for the last two.
        cutoff = now - timedelta(days=14)
        for m in recent:
            if m.starts_at < cutoff:
                _checkin(db, m, u, north=60, east=20, minutes=-5, accuracy=12)

    elif pattern == "first_day":
        pass                                        # signed up today: consent screen, no history

    elif pattern == "new":
        for m in recent[-4:]:
            _checkin(db, m, u, north=30, east=15, minutes=-3, accuracy=9)

    elif pattern == "sloppy":
        for i, m in enumerate(recent):
            if i == len(recent) - 1:                   # the latest from a VPN-looking address
                _checkin(db, m, u, north=100, east=60, minutes=10,
                         accuracy=20, ip="185.220.101.7")
                continue
            _checkin(db, m, u, north=rng.uniform(-150, 150), east=rng.uniform(-150, 150),
                     minutes=rng.uniform(-5, 50),          # often late
                     accuracy=rng.uniform(10, 40),
                     checkout=(i % 3 != 0))                # forgets to check out


if __name__ == "__main__":
    import sys

    if "--if-empty" in sys.argv:
        create_all()
        db = SessionLocal()
        try:
            if db.query(County).count():
                print("database already has data; not seeding")
                raise SystemExit(0)
        finally:
            db.close()
    print(seed())
