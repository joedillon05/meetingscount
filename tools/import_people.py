#!/usr/bin/env python3
"""
Bring people over from the old site, so they only have to sign in.

    python tools/import_people.py people.csv                  # say what would happen, change nothing
    python tools/import_people.py people.csv --yes            # make the profiles
    python tools/import_people.py people.xlsx --yes --covered --text
    python tools/import_people.py people.xlsx --yes --stripe

Takes a .csv or .xlsx with a header row. Columns are matched by name: a name
(or first and last name) and a mobile number are needed; an email, meetings
a week (3 if missing), a start date and a probation end date are used when
they are there. Anyone
whose number already has an account is left alone, so the same list can be
run twice.

  --covered  these people already paid on the old site: no payment is asked for
  --stripe   these people pay through Stripe already, in this same Stripe
             account: find each one there by email, then by phone, and keep
             the subscription they have. Someone Stripe does not know is left
             out, with a note, so the list can be run again once Stripe is
             put right; with --covered as well they come in with no payment asked
  --text     text each new person that their account is ready, with the link

Without --covered or --stripe they set up payment the first time they open the
app, the same as someone who signed up at /join. Uses the same MC_DATABASE_URL
and Stripe settings as the app.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import billing, confirming, directory, profiles, sms  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.models import User  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Make profiles for people moving over from the old site.")
    ap.add_argument("file", help="a .csv or .xlsx with a header row")
    ap.add_argument("--yes", action="store_true", help="make the profiles (without it, nothing is saved)")
    ap.add_argument("--covered", action="store_true", help="no payment is asked of these people")
    ap.add_argument("--stripe", action="store_true",
                    help="find each person in Stripe and keep the subscription they already pay for")
    ap.add_argument("--text", action="store_true", help="text each new person that their account is ready")
    args = ap.parse_args()

    if args.stripe and not billing.configured():
        print("Stripe is not configured here: --stripe needs MC_STRIPE_SECRET_KEY and "
              "MC_STRIPE_PRICE_ID, with MC_PAYMENTS on.", file=sys.stderr)
        return 1
    rows = directory.read_rows(args.file)
    if not rows:
        print("no rows found", file=sys.stderr)
        return 1

    create_all()
    db = SessionLocal()
    try:
        try:
            report = profiles.import_people(db, rows, covered=args.covered, stripe=args.stripe)
        except (ValueError, billing.StripeError) as e:
            print(e, file=sys.stderr)
            return 1
        if not args.yes:
            db.rollback()
            print(report.summary())
            print("\nNothing saved. Run again with --yes to make these profiles.")
            return 0
        db.commit()
        print(report.summary())
        if args.text:
            url = settings.PUBLIC_URL or "the app"
            sent = 0
            for uid in report.new_users:
                u = db.get(User, uid)
                row = sms.send(db, u.phone, confirming.ready_text(u.name, url), purpose="moved",
                               user_id=u.id, county_id=u.county_id)
                sent += row.status == "sent"
            print(f"texted {sent} of {len(report.new_users)}"
                  + ("" if sent == len(report.new_users) else ": see the Texts page for the rest"))
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
