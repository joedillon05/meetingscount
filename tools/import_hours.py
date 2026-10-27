#!/usr/bin/env python3
"""
Bring someone's hours into the Team page from a spreadsheet, once.

    python tools/import_hours.py --email joe@example.com --csv hours.csv \
        --rate 2026-06-16:17.50+12.50@500000 \
        --rate 2027-06-01:20+15@500000 \
        --rate 2028-01-01:22.50+17.50@500000 \
        --paid "Invoice 1=1662.50" --paid "Invoice 2=337.50" \
        --by spencer@example.com

    python tools/import_hours.py ... --csv "https://docs.google.com/spreadsheets/d/<id>/export?format=csv&gid=<gid>"

The CSV has a header row with at least Date, Work and Hours; a "Billed on"
column says which rows were already invoiced. Other columns are ignored.
A row with no date takes the date of the row above it and says so in its
note. Each --rate is cash, equity and the company valuation in dollars,
from a date; equity and the valuation can be left off. Each --paid turns
the rows billed under that label into one timesheet, approved and marked
paid for the amount given, so the running balance on the Money tab comes
out as the spreadsheet had it. --by names the founder who approved and
paid, if that is on record. Refuses to run twice for the same person:
their hours log has to be empty. Uses the same MC_DATABASE_URL as the app.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
import urllib.request
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select  # noqa: E402

from app.auth import audit  # noqa: E402
from app.db import SessionLocal, create_all  # noqa: E402
from app.models import (  # noqa: E402
    Role, TeamHours, TeamRate, TeamTimesheet, TeamTimesheetStatus, User,
)
from app.routers.team import rate_on, value_of, whole  # noqa: E402


def cents(text: str) -> int:
    return round(float(text.replace("$", "").replace(",", "").strip()) * 100)


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise SystemExit(f"cannot read the date {text!r}: use m/d/yyyy or yyyy-mm-dd")


def parse_rate(text: str) -> dict:
    """2026-06-16:17.50+12.50@500000, with the equity and valuation parts optional."""
    try:
        day, rest = text.split(":", 1)
        valuation = None
        if "@" in rest:
            rest, val = rest.split("@", 1)
            valuation = cents(val)
        cash, _, equity = rest.partition("+")
        return {"effective_on": parse_date(day), "cents_per_hour": cents(cash),
                "equity_cents_per_hour": cents(equity) if equity else 0, "valuation_cents": valuation}
    except (ValueError, AttributeError) as exc:
        raise SystemExit(f"cannot read the rate {text!r}: use DATE:CASH+EQUITY@VALUATION") from exc


def read_rows(source: str) -> list[dict]:
    if source.startswith(("http://", "https://")):
        with urllib.request.urlopen(source, timeout=30) as r:          # noqa: S310  the user's own sheet
            text = r.read().decode("utf-8-sig")
    else:
        text = open(source, encoding="utf-8-sig").read()
    reader = csv.DictReader(io.StringIO(text))
    need = {"Date", "Work", "Hours"}
    if not reader.fieldnames or not need <= set(reader.fieldnames):
        raise SystemExit(f"the CSV needs the columns {sorted(need)}; it has {reader.fieldnames}")
    rows, last = [], None
    for raw in reader:
        hours = (raw.get("Hours") or "").strip()
        if not hours:
            continue                                     # blank or formula-only rows at the bottom
        day = parse_date(raw.get("Date"))
        note = (raw.get("Work") or "").strip() or "(no description)"
        if day is None:
            if last is None:
                raise SystemExit("the first row has no date")
            day, note = last, f"{note} (date not recorded; placed with {last.isoformat()})"
        last = day
        rows.append({"worked_on": day, "minutes": round(float(hours) * 60), "note": note[:300],
                     "billed_on": (raw.get("Billed on") or "").strip()})
    return rows


def import_hours(db, *, email: str, rows: list[dict], rates: list[dict], paid: dict[str, int],
                 by_email: str | None, dry_run: bool) -> dict:
    person = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not person or person.role != Role.superuser:
        raise SystemExit(f"no superuser account for {email}")
    other = None
    if by_email:
        other = db.scalar(select(User).where(User.email == by_email.strip().lower()))
        if not other or other.role != Role.superuser:
            raise SystemExit(f"no superuser account for {by_email}")
    if db.scalar(select(TeamHours).where(TeamHours.user_id == person.id)):
        raise SystemExit(f"{person.name} already has hours logged. This tool only fills an empty log.")
    for label in paid:
        if not any(r["billed_on"] == label for r in rows):
            raise SystemExit(f"no rows are billed on {label!r}")
    now = datetime.now(timezone.utc)
    rate_rows = [TeamRate(user_id=person.id, set_by_id=(other or person).id, **r) for r in rates]
    rate_rows.sort(key=lambda r: r.effective_on)
    unpriced = [r for r in rows if rate_on(rate_rows, r["worked_on"]) is None]
    if unpriced:
        raise SystemExit(f"{len(unpriced)} rows fall before the first rate, the earliest on "
                         f"{min(r['worked_on'] for r in unpriced).isoformat()}")

    summary = {"person": person.name, "hours": len(rows), "minutes": sum(r["minutes"] for r in rows),
               "rates": len(rate_rows), "timesheets": []}
    for label, paid_cents in paid.items():
        group = [r for r in rows if r["billed_on"] == label]
        amount = whole(sum(value_of(r["minutes"], rate_on(rate_rows, r["worked_on"]).cents_per_hour)
                           for r in group))
        summary["timesheets"].append({"label": label, "rows": len(group),
                                      "minutes": sum(r["minutes"] for r in group),
                                      "amount_cents": amount, "paid_cents": paid_cents})
    if dry_run:
        return summary

    db.add_all(rate_rows)
    db.flush()
    hours = [TeamHours(user_id=person.id, worked_on=r["worked_on"], minutes=r["minutes"], note=r["note"])
             for r in rows]
    db.add_all(hours)
    db.flush()
    for label, paid_cents in paid.items():
        group = [(r, h) for r, h in zip(rows, hours) if r["billed_on"] == label]
        sheet = TeamTimesheet(
            user_id=person.id, status=TeamTimesheetStatus.paid,
            starts_on=min(r["worked_on"] for r, _ in group), ends_on=max(r["worked_on"] for r, _ in group),
            minutes=sum(h.minutes for _, h in group),
            amount_cents=whole(sum(value_of(h.minutes, rate_on(rate_rows, h.worked_on).cents_per_hour)
                                   for _, h in group)),
            note=f"{label}, brought in from the time sheet", submitted_at=now,
            decided_by_id=other.id if other else None, decided_at=now,
            paid_by_id=other.id if other else None, paid_cents=paid_cents,
            paid_note=f"Cash received for {label}")
        db.add(sheet)
        db.flush()
        for _, h in group:
            h.timesheet_id = sheet.id
    db.commit()
    audit(db, other or person, "team_import_hours", target_type="user", target_id=person.id,
          detail=f"{len(rows)} entries, {len(rate_rows)} rates, {len(paid)} paid timesheets")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Bring someone's hours into the Team page from a spreadsheet.")
    ap.add_argument("--email", required=True, help="the founder whose hours these are")
    ap.add_argument("--csv", required=True, help="a CSV file, or a URL that returns one")
    ap.add_argument("--rate", action="append", default=[], metavar="DATE:CASH+EQUITY@VALUATION",
                    help="a rate from a date, in dollars; repeatable")
    ap.add_argument("--paid", action="append", default=[], metavar="LABEL=DOLLARS",
                    help="rows billed under LABEL become one paid timesheet; repeatable")
    ap.add_argument("--by", help="the founder who approved and paid, if on record")
    ap.add_argument("--dry-run", action="store_true", help="say what would be written, write nothing")
    args = ap.parse_args()
    if not args.rate:
        raise SystemExit("at least one --rate is needed to price the hours")
    paid = {}
    for item in args.paid:
        label, _, amount = item.partition("=")
        if not label or not amount:
            raise SystemExit(f"cannot read {item!r}: use LABEL=DOLLARS")
        paid[label.strip()] = cents(amount)

    create_all()
    db = SessionLocal()
    try:
        out = import_hours(db, email=args.email, rows=read_rows(args.csv),
                           rates=[parse_rate(r) for r in args.rate], paid=paid,
                           by_email=args.by, dry_run=args.dry_run)
    finally:
        db.close()
    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {out['hours']} entries ({out['minutes'] / 60:g} hours) and {out['rates']} rates for {out['person']}")
    for t in out["timesheets"]:
        print(f"  {t['label']}: {t['rows']} entries, {t['minutes'] / 60:g} hours, "
              f"{t['amount_cents'] / 100:,.2f} at the rate, paid {t['paid_cents'] / 100:,.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
