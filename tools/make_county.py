#!/usr/bin/env python3
"""
Create a court's county on a deployment, with the programs its officers
put participants in.

    python tools/make_county.py --name "23rd District Court, Taylor" \
        --program "Probation=TAYLOR-PROB" --program "Regional Treatment Court=TAYLOR-RTC"
    python tools/make_county.py --list

A county is a tenant: its staff see only its own participants, so it has to
exist before tools/make_staff.py can put anyone in it. Run it again with the
same name to add programs to a county that is already there. --list shows
every county with its programs and how many staff and participants it holds.
Uses the same MC_DATABASE_URL as the app.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app.db import SessionLocal, create_all  # noqa: E402
from app.models import County, Participant, Program, Role, User  # noqa: E402

JOIN_CODE_MAX = 16


def parse_program(spec: str) -> tuple[str, str]:
    """'Probation=TAYLOR-PROB' -> ('Probation', 'TAYLOR-PROB')."""
    name, sep, code = spec.partition("=")
    name, code = " ".join(name.split()), code.strip().upper()
    if not sep or not name or not code:
        raise SystemExit(f"a program is written Name=JOIN-CODE, not {spec!r}")
    if len(code) > JOIN_CODE_MAX or " " in code:
        raise SystemExit(f"the join code {code!r} must be up to {JOIN_CODE_MAX} characters with no spaces")
    return name, code


def make_county(db, *, name: str, state: str, timezone: str,
                programs: list[tuple[str, str]]) -> tuple[County, bool, list[Program]]:
    """Returns the county, whether it was just created, and the programs added."""
    name = " ".join(name.split())
    county = db.scalar(select(County).where(County.name == name))
    created = county is None
    if created:
        county = County(name=name, state=state.strip().upper(), timezone=timezone.strip())
        db.add(county)
        db.flush()
    elif not programs:
        raise SystemExit(f"there is already a county called {name!r}")
    added = []
    for pname, code in programs:
        if db.scalar(select(Program).where(Program.county_id == county.id, Program.name == pname)):
            continue                                            # already there, nothing to do
        taken = db.scalar(select(Program).where(Program.join_code == code))
        if taken is not None:
            raise SystemExit(f"the join code {code!r} already belongs to {taken.name!r}")
        program = Program(county_id=county.id, name=pname, join_code=code)
        db.add(program)
        added.append(program)
    if not created and not added:
        raise SystemExit(f"{name!r} already has every program listed")
    db.commit()
    return county, created, added


def list_counties(db) -> list[str]:
    lines = []
    for county in db.scalars(select(County).order_by(County.id)):
        staff = db.scalar(select(func.count()).select_from(User).where(
            User.county_id == county.id, User.role.in_([Role.officer, Role.admin])))
        people = db.scalar(select(func.count()).select_from(Participant).where(
            Participant.county_id == county.id))
        kind = " (self-serve)" if county.self_serve else ""
        lines.append(f"{county.name}{kind}: {county.state}, {county.timezone}, "
                     f"{staff} staff, {people} participants")
        for program in db.scalars(select(Program).where(Program.county_id == county.id)
                                  .order_by(Program.id)):
            lines.append(f"    {program.name}  [{program.join_code}]")
    return lines or ["no counties yet"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Create a county and its programs, or list them.")
    ap.add_argument("--name", help="the county's name, as staff and participants will see it")
    ap.add_argument("--state", default="MI")
    ap.add_argument("--timezone", default="America/Detroit")
    ap.add_argument("--program", action="append", default=[], metavar="NAME=JOIN-CODE",
                    help="a program in the county; repeat for more")
    ap.add_argument("--list", action="store_true", help="show every county instead")
    args = ap.parse_args()
    if args.list == bool(args.name):
        ap.error("pass --name to create a county, or --list to see them")

    create_all()
    db = SessionLocal()
    try:
        if args.list:
            print("\n".join(list_counties(db)))
            return 0
        programs = [parse_program(p) for p in args.program]
        county, created, added = make_county(db, name=args.name, state=args.state,
                                             timezone=args.timezone, programs=programs)
    finally:
        db.close()
    print(f"{'created' if created else 'found'} {county.name} ({county.state}, {county.timezone})")
    for program in added:
        print(f"added program {program.name} with join code {program.join_code}")
    print("next: python tools/make_staff.py --role admin --county " + repr(county.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
