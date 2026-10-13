#!/usr/bin/env python3
"""
Create a staff account from the command line.

    python tools/make_staff.py --name "Joe Dillon" --email joe@example.com --role superuser
    python tools/make_staff.py --name "Pat Lee" --email pat@example.gov --role admin --county "Washtenaw County"

The first real account on a deployment has to come from somewhere other
than the dashboard, and a superuser (who sees every county, including the
self-pay one) can only be made here. Prints a first password once; the
person must choose their own at first sign-in. Uses the same
MC_DATABASE_URL as the app.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal, create_all  # noqa: E402
from app.models import County, Role, User  # noqa: E402
from app.security import hash_password, temp_password  # noqa: E402


def make_staff(db, *, name: str, email: str, role: Role, county: str | None) -> tuple[User, str]:
    email = email.strip().lower()
    if db.scalar(select(User).where(User.email == email)):
        raise SystemExit(f"there is already an account for {email}")
    county_id = None
    if role != Role.superuser:
        if not county:
            raise SystemExit("an officer or admin belongs to a county: pass --county")
        row = db.scalar(select(County).where(County.name == county))
        if row is None:
            raise SystemExit(f"no county called {county!r}")
        county_id = row.id
    first = temp_password()
    person = User(county_id=county_id, role=role, name=" ".join(name.split()), email=email,
                  password_hash=hash_password(first), password_set_at=datetime.now(timezone.utc),
                  must_change_password=True)
    db.add(person)
    db.commit()
    return person, first


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a staff account.")
    ap.add_argument("--name", required=True)
    ap.add_argument("--email", required=True)
    ap.add_argument("--role", choices=["officer", "admin", "superuser"], default="admin")
    ap.add_argument("--county", help="the county's name, for an officer or admin")
    args = ap.parse_args()

    create_all()
    db = SessionLocal()
    try:
        person, first = make_staff(db, name=args.name, email=args.email, role=Role(args.role),
                                   county=args.county)
    finally:
        db.close()
    print(f"created {person.role.value} {person.name} <{person.email}>")
    print(f"first password: {first}")
    print("sign in at /officer and choose a new password. This one is not shown again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
