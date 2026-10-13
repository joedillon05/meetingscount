#!/usr/bin/env python3
"""
Delete the demo county and everything in it.

    python tools/retire_demo.py --dry-run                 # say what would go, change nothing
    python tools/retire_demo.py --yes                     # delete "Washtenaw County", the seeded one
    python tools/retire_demo.py --county "Other" --yes

For taking a deployment live after it has run as a demo. The seeded county
has staff accounts whose shared password is printed in the README, so it
cannot stay on a site real people use. Everything that belongs to the
county goes: its programs, people, meetings, check-ins, reviews, texts,
sessions and audit rows. The shared meeting directory and the self-pay
county are untouched, and the tool refuses to delete a self-serve county.
Uses the same MC_DATABASE_URL as the app.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, or_, select, update  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    AuditLog, CheckIn, Confirmation, Confirmer, County, Dispute, LoginCode, Meeting,
    MeetingSeries, Participant, Program, Requirement, Review, Session as SessionRow, Signup,
    SmsLog, Subscription, User,
)

SEEDED = "Washtenaw County"


def retire(db, county_name: str, *, dry_run: bool = False) -> dict[str, int]:
    """Delete the county. Returns how many rows of each kind went (or would go)."""
    county = db.scalar(select(County).where(County.name == county_name))
    if county is None:
        raise SystemExit(f"no county called {county_name!r}")
    if county.self_serve:
        raise SystemExit(f"{county_name!r} is the self-serve county, which holds paying people. Refusing.")
    cid = county.id
    uids = list(db.scalars(select(User.id).where(User.county_id == cid)))
    in_users = lambda col: col.in_(uids) if uids else False      # noqa: E731
    gone_checkins = select(CheckIn.id).where(or_(CheckIn.county_id == cid, in_users(CheckIn.user_id)))

    # Children before parents, so the foreign keys never object. Anything a
    # person from this county touched goes too, even if it landed in another
    # county's column (a check-in at a directory meeting, say).
    steps = [
        ("audit rows", AuditLog, or_(AuditLog.county_id == cid, in_users(AuditLog.actor_id))),
        ("texts", SmsLog, or_(SmsLog.county_id == cid, in_users(SmsLog.user_id))),
        ("confirmations", Confirmation, or_(Confirmation.county_id == cid,
                                            in_users(Confirmation.user_id),
                                            Confirmation.checkin_id.in_(gone_checkins))),
        ("confirmers", Confirmer, or_(Confirmer.county_id == cid, in_users(Confirmer.user_id))),
        ("reviews", Review, or_(Review.county_id == cid, in_users(Review.reviewer_id),
                                Review.checkin_id.in_(gone_checkins))),
        ("disputes", Dispute, or_(Dispute.county_id == cid, in_users(Dispute.user_id),
                                  Dispute.checkin_id.in_(gone_checkins))),
        ("check-ins", CheckIn, or_(CheckIn.county_id == cid, in_users(CheckIn.user_id))),
        ("requirements", Requirement, Requirement.county_id == cid),
        ("subscriptions", Subscription, or_(Subscription.county_id == cid,
                                            in_users(Subscription.user_id))),
        ("sign-ups", Signup, in_users(Signup.user_id)),
        ("sign-in codes", LoginCode, or_(in_users(LoginCode.user_id), in_users(LoginCode.issued_by_id))),
        ("sessions", SessionRow, in_users(SessionRow.user_id)),
        ("participants", Participant, or_(Participant.county_id == cid, in_users(Participant.user_id))),
        ("meetings", Meeting, Meeting.county_id == cid),
        ("meeting series", MeetingSeries, MeetingSeries.county_id == cid),
        ("people", User, User.county_id == cid),
        ("programs", Program, Program.county_id == cid),
        ("counties", County, County.id == cid),
    ]
    counts: dict[str, int] = {}
    for label, model, where in steps:
        counts[label] = db.scalar(select(func.count()).select_from(model).where(where)) or 0
    if dry_run:
        return counts

    # References from rows that stay (directory meetings, other counties'
    # participants) to people who are leaving.
    if uids:
        db.execute(update(Participant).where(in_users(Participant.officer_id),
                                             Participant.county_id != cid).values(officer_id=None))
        db.execute(update(Meeting).where(in_users(Meeting.created_by_id),
                                         Meeting.county_id != cid).values(created_by_id=None))
        db.execute(update(MeetingSeries).where(in_users(MeetingSeries.created_by_id),
                                               MeetingSeries.county_id != cid).values(created_by_id=None))
    for _label, model, where in steps:
        db.execute(delete(model).where(where))
    db.commit()
    return counts


def main() -> int:
    ap = argparse.ArgumentParser(description="Delete the demo county and everything in it.")
    ap.add_argument("--county", default=SEEDED, help=f"the county's name (default: {SEEDED!r})")
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--yes", action="store_true", help="actually delete")
    args = ap.parse_args()
    if not args.dry_run and not args.yes:
        print("pass --dry-run to see what would go, or --yes to delete it", file=sys.stderr)
        return 2

    db = SessionLocal()
    try:
        counts = retire(db, args.county, dry_run=args.dry_run)
    finally:
        db.close()
    verb = "would delete" if args.dry_run else "deleted"
    for label, n in counts.items():
        if n:
            print(f"{verb} {n} {label}")
    if not args.dry_run:
        print(f"{args.county} is gone. The meeting directory and any other county are untouched.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
