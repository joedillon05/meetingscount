#!/usr/bin/env python3
"""
Bring a meeting list into the directory.

    python tools/import_meetings.py "data/semi-aa-meetings-2026-01-28.csv"
    python tools/import_meetings.py list.xlsx --prune        # retire meetings no longer listed
    python tools/import_meetings.py list.xlsx --dry-run      # say what would change, change nothing
    python tools/import_meetings.py list.xlsx --if-empty     # for a first boot: only when the directory is empty

Takes a .csv or .xlsx with a header row. Columns are matched by name, so
"Meeting Name", "Day", "Start_time_formatted", "Street", "City", "Zip Code",
"latitude" and "longitude" all work, as do plainer names. Times may be
spreadsheet times (a fraction of a day), "7:00 PM" or "19:00". Rows without a
street address or coordinates are skipped and counted: nobody can check in
to an online meeting.

A meeting is recognised next time by its page URL, so running the same file
twice changes nothing, and a corrected row updates the meeting in place.
Uses the same MC_DATABASE_URL as the app.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select  # noqa: E402

from app import directory  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.models import MeetingSeries  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a meeting list into the directory.")
    ap.add_argument("file", help="a .csv or .xlsx with a header row")
    ap.add_argument("--source", help="where the list comes from, such as aa-semi.org "
                                     "(default: the host of the first page URL)")
    ap.add_argument("--timezone", default="America/Detroit", help="the meetings' local timezone")
    ap.add_argument("--prune", action="store_true",
                    help="mark meetings from this source that are not in the file as inactive")
    ap.add_argument("--dry-run", action="store_true", help="report, then roll back")
    ap.add_argument("--if-empty", action="store_true",
                    help="do nothing if the directory already has meetings")
    args = ap.parse_args()

    rows = directory.read_rows(args.file)
    if not rows:
        print("no rows found", file=sys.stderr)
        return 1

    create_all()
    db = SessionLocal()
    try:
        if args.if_empty and db.scalar(select(func.count(MeetingSeries.id))):
            print("directory already has meetings; not importing")
            return 0
        report = directory.import_rows(db, rows, source_name=args.source, prune=args.prune,
                                       timezone_name=args.timezone)
        if args.dry_run:
            db.rollback()
            print("dry run, nothing saved")
        print(report.summary())
        if args.dry_run:
            return 0
        total = db.scalar(select(func.count(MeetingSeries.id)).where(MeetingSeries.active.is_(True)))
        print(f"directory now has {total} active meetings")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
