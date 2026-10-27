#!/usr/bin/env python3
"""
What the old Stripe account holds for the people on a list, before they move.

    export OLD_STRIPE_KEY=sk_live_...          # the old account's secret key, this terminal only
    python tools/stripe_old_account.py Participants.xlsx
    python tools/stripe_old_account.py Participants.xlsx --csv old-customers.csv

Reads only. For each person on the list: their customer in the old account
(found by email, then phone, then name), the subscription they pay for, its
price, when the paid period ends, and the card on file. Then whoever is
paying in the old account but is not on the list, so the list is complete
before the copy. --csv writes the one-column file of customer ids that the
Stripe Dashboard's "Copy customers" upload takes: the people with a live
subscription only. The key is never printed.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import billing, directory, profiles  # noqa: E402
from stripe_common import LIVE, Prices, card_words, default_card, old_client, page, sub_price, when  # noqa: E402


@dataclass
class Found:
    person: profiles.Listed
    customer: dict
    subscription: dict | None
    how: str                      # email, phone, or name
    card: str

    @property
    def live(self) -> bool:
        return bool(self.subscription) and self.subscription.get("status") in LIVE


@dataclass
class Report:
    account: str
    found: list[Found] = field(default_factory=list)
    missing: list[profiles.Listed] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)          # paying there, not on the list

    @property
    def to_copy(self) -> list[str]:
        return [f.customer["id"] for f in self.found if f.live]


def by_name(client: billing.Client, name: str) -> dict | None:
    """Last resort: one customer whose name holds the last name and starts like the first."""
    parts = name.split()
    if len(parts) < 2:
        return None
    first, last = parts[0].lower(), parts[-1]
    hits = client.get("/v1/customers/search",
                      {"query": f"name~'{billing._search_quote(last)}'", "limit": "100"}).get("data") or []
    hits = [c for c in hits if (c.get("name") or "").lower().startswith(first[:3])]
    return hits[0] if len(hits) == 1 else None


def look(rows: list[dict], client: billing.Client) -> Report:
    acct = client.get("/v1/account")
    report = Report(account=" ".join(x for x in (
        acct.get("id"), (acct.get("business_profile") or {}).get("name"), acct.get("email")) if x))
    matched: set[str] = set()
    for person in profiles.listed_people(rows):
        match = billing.match_existing(person.email, person.phone, client)
        if match is not None:
            same_email = person.email and (match.customer.get("email") or "").lower() == person.email.lower()
            found = Found(person, match.customer, match.subscription, "email" if same_email else "phone",
                          card_words(default_card(client, match.customer)))
        else:
            cust = by_name(client, person.name)
            if cust is None:
                report.missing.append(person)
                continue
            found = Found(person, cust, billing.best_subscription(cust["id"], client), "name",
                          card_words(default_card(client, cust)))
        matched.add(found.customer["id"])
        report.found.append(found)

    for status in LIVE:
        for sub in page(client, "/v1/subscriptions", {"status": status, "expand[]": "data.customer"}):
            cid = billing._id(sub.get("customer"))
            if cid in matched:
                continue
            matched.add(cid)
            cust = sub["customer"] if isinstance(sub.get("customer"), dict) else client.get(f"/v1/customers/{cid}")
            report.extra.append(f"{cust.get('name') or '(no name)'}, {_mask(cust.get('email'))}, "
                                f"{status}, paid through {when(billing.period_end(sub))}")
    return report


def _mask(email: str | None) -> str:
    if not email or "@" not in email:
        return "(no email)"
    return email[:2] + "***" + email[email.find("@"):]


def summary(report: Report, prices: Prices) -> str:
    lines = [f"old account: {report.account}", ""]
    lines.append(f"found {len(report.found)} of {len(report.found) + len(report.missing)} on the list:")
    for f in report.found:
        sub = f.subscription
        if sub is None:
            standing = "no subscription"
        else:
            standing = f"{sub.get('status')}, {prices.words(sub_price(sub))}, paid through {when(billing.period_end(sub))}"
            if sub.get("cancel_at_period_end"):
                standing += ", set to end then"
        note = {"email": "", "phone": " (matched by phone)", "name": " (matched by NAME only: check)"}[f.how]
        lines.append(f"  {f.person.name}: {f.customer['id']}  {standing}, {f.card}{note}"
                     + ("" if f.live else "  <- not live, will not move"))
    if report.missing:
        lines.append(f"\nnot in the old account ({len(report.missing)}):")
        lines += [f"  row {p.row}: {p.name}, {p.email or 'no email'}, {p.phone or 'no phone'}"
                  for p in report.missing]
    if report.extra:
        lines.append(f"\npaying in the old account but not on the list ({len(report.extra)}):")
        lines += [f"  {x}" for x in report.extra]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="What the old Stripe account holds for a list of people.")
    ap.add_argument("file", help="a .csv or .xlsx with a header row")
    ap.add_argument("--csv", metavar="PATH", help="write the customer ids to copy, one per line, no header")
    args = ap.parse_args()
    if not os.path.isfile(args.file):
        print(f"no such file: {args.file}", file=sys.stderr)
        return 1
    rows = directory.read_rows(args.file)
    if not rows:
        print("no rows found", file=sys.stderr)
        return 1
    client = old_client()
    try:
        report = look(rows, client)
    except (ValueError, billing.StripeError) as e:
        print(e, file=sys.stderr)
        return 1
    print(summary(report, Prices(client)))
    if args.csv:
        with open(args.csv, "w", newline="") as f:
            csv.writer(f).writerows([cid] for cid in report.to_copy)
        print(f"\nwrote {len(report.to_copy)} customer ids to {args.csv} for the Dashboard copy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
