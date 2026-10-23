#!/usr/bin/env python3
"""
What Stripe knows about a list of people, before importing them.

    python tools/stripe_check.py people.xlsx

Reads only. Says which Stripe account the server's key belongs to, how many
customers and subscriptions it holds, and for each person on the list how
many customers match their email (exactly, lowercased, by the part before
the @) and their phone. When a conversion list matches nobody, this is how
to tell a different Stripe account from customers filed under other emails
or paying without a subscription. Emails of people not on the list are
masked. Uses the same Stripe settings as the app.
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import billing, directory  # noqa: E402
from app.config import settings  # noqa: E402


def _page(client, path: str, params: dict, cap: int = 1000) -> list[dict]:
    out, params = [], dict(params, limit="100")
    while True:
        page = client.get(path, params)
        data = page.get("data") or []
        out += data
        if not page.get("has_more") or len(out) >= cap or not data:
            return out
        params["starting_after"] = data[-1]["id"]


def _mask(email: str | None) -> str:
    if not email:
        return "(no email)"
    at = email.find("@")
    return email[:2] + "***" + email[at:] if at > 0 else email[:2] + "***"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    if not billing.configured():
        print("Stripe is not configured here: MC_STRIPE_SECRET_KEY and MC_STRIPE_PRICE_ID, "
              "with MC_PAYMENTS on.", file=sys.stderr)
        return 1
    rows = directory.read_rows(sys.argv[1])
    client = billing.get_client()

    acct = client.get("/v1/account")
    profile = acct.get("business_profile") or {}
    dashboard = (acct.get("settings") or {}).get("dashboard") or {}
    print(f"key: {settings.STRIPE_SECRET_KEY[:7]}...   price: {settings.STRIPE_PRICE_ID}")
    print(f"account: {acct.get('id')}  {profile.get('name') or dashboard.get('display_name') or ''}"
          f"  {acct.get('email') or ''}")

    customers = _page(client, "/v1/customers", {})
    print(f"customers: {len(customers)}{' or more' if len(customers) >= 1000 else ''}")
    for c in customers[:10]:
        print(f"   {c['id'][:12]}...  {_mask(c.get('email'))}  "
              f"phone {'yes' if c.get('phone') else 'no'}  {(c.get('name') or '')[:20]}")
    subs = _page(client, "/v1/subscriptions", {"status": "all"})
    by: dict[str, int] = {}
    for s in subs:
        by[s.get("status") or "?"] = by.get(s.get("status") or "?", 0) + 1
    print(f"subscriptions: {len(subs)}  " + ", ".join(f"{k} {v}" for k, v in sorted(by.items())))

    cols = {re.sub(r"[^a-z]", "", k.lower()): k for k in (rows[0].keys() if rows else [])}
    email_col = next((cols[k] for k in ("email", "emailaddress") if k in cols), None)
    phone_col = next((cols[k] for k in ("cellphone", "phone", "mobile", "cell") if k in cols), None)
    name_cols = [cols[k] for k in ("name", "firstname", "lastname") if k in cols]
    print("\nper person: exact email / lowercased / part before the @ / phone")
    for r in rows:
        name = " ".join(str(r.get(k) or "") for k in name_cols).strip()
        email = str(r.get(email_col) or "").strip() if email_col else ""
        digits = re.sub(r"\D", "", str(r.get(phone_col) or ""))[-10:] if phone_col else ""
        n = lambda path, params: len(client.get(path, params).get("data") or [])   # noqa: E731
        exact = n("/v1/customers", {"email": email, "limit": "100"}) if email else 0
        lower = n("/v1/customers", {"email": email.lower(), "limit": "100"}) if email else 0
        local = billing._search_quote(email.split("@")[0].lower()) if email else ""
        part = n("/v1/customers/search", {"query": f"email~'{local}'", "limit": "100"}) if local else 0
        phone = 0
        if len(digits) == 10:
            shapes = (digits, f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
                      f"({digits[:3]}) {digits[3:6]}-{digits[6:]}", digits[-7:])
            phone = sum(n("/v1/customers/search", {"query": f"phone~'{q}'", "limit": "100"})
                        for q in shapes)
        print(f"  {name}: {exact} / {lower} / {part} / {phone}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
