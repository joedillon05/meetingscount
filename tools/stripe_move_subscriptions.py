#!/usr/bin/env python3
"""
Start each person's subscription on the new Stripe account, timed so the old
one is never billed alongside it.

    export OLD_STRIPE_KEY=sk_live_...           # the old account, this terminal only
    export MC_STRIPE_SECRET_KEY=sk_live_...     # the new account, as the app has it
    export MC_STRIPE_PRICE_ID=price_...         # the price on the new account
    python tools/stripe_move_subscriptions.py Participants.xlsx          # the plan, nothing done
    python tools/stripe_move_subscriptions.py Participants.xlsx --yes
    python tools/stripe_move_subscriptions.py Participants.xlsx --yes --price price_...   # another price

Run after the Dashboard copy has finished (DEPLOY.md, "Moving people from
another Stripe account"). For each person on the list with a live
subscription in the old account and a copied customer in the new one: the
copied card becomes that customer's default, a subscription starts on the new
account with a free period that ends when the old paid period ends, and the
old subscription is set to stop at that same moment. The first charge here
lands on the day the old account would have charged. Nothing is charged today.

Someone with no live old subscription, no copied customer, or no card is left
alone with a note, so the list can be run again once that is fixed. Someone
who already has a subscription on the new account is skipped, so running it
twice does nothing more.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import billing, directory, profiles  # noqa: E402
from stripe_common import (  # noqa: E402
    LIVE, Prices, card_key, card_words, default_card, new_client, old_client, sub_price, when,
)

TOO_SOON = timedelta(minutes=10)       # an old period ending sooner than this is done by hand


@dataclass
class Move:
    person: profiles.Listed
    old_sub: dict | None = None
    new_customer: dict | None = None
    card: dict | None = None           # {"kind": "payment_method" | "source", "id": ..., "words": ...}
    skip: str | None = None            # why nothing will be done
    done: str | None = None            # what was done, after --yes

    @property
    def period_end(self) -> datetime | None:
        return billing.period_end(self.old_sub) if self.old_sub else None


def copied_card(new: billing.Client, customer_id: str, want: tuple | None) -> dict | None:
    """The card on the new account that matches the old default, else the one card there."""
    cards = new.get("/v1/payment_methods", {"customer": customer_id, "type": "card", "limit": "100"})
    found = cards.get("data") or []
    pick = next((pm for pm in found if want and card_key(pm) == want), None) or (found[0] if found else None)
    if pick:
        return {"kind": "payment_method", "id": pick["id"], "words": card_words(pick)}
    try:
        legacy = new.get(f"/v1/customers/{customer_id}/sources", {"object": "card", "limit": "100"})
        found = legacy.get("data") or []
    except billing.StripeError:
        found = []
    pick = next((c for c in found if want and card_key(c) == want), None) or (found[0] if found else None)
    if pick:
        return {"kind": "source", "id": pick["id"], "words": card_words(pick)}
    return None


def plan(rows: list[dict], old: billing.Client, new: billing.Client,
         now: datetime | None = None) -> list[Move]:
    """What would be done for each person, with no call that changes anything."""
    now = now or datetime.now(timezone.utc)
    moves = []
    for person in profiles.listed_people(rows):
        m = Move(person)
        moves.append(m)
        match = billing.match_existing(person.email, person.phone, old)
        if match is None:
            m.skip = "not in the old account"
            continue
        if match.subscription is None or match.subscription.get("status") not in LIVE:
            m.skip = (f"old subscription is {match.subscription.get('status')}, not live"
                      if match.subscription else "no subscription in the old account")
            continue
        m.old_sub = match.subscription
        if m.period_end is None or m.period_end < now + TOO_SOON:
            m.skip = f"old paid period ends {when(m.period_end)}: too soon or already past, do this one by hand"
            continue
        try:
            m.new_customer = new.get(f"/v1/customers/{match.customer['id']}")
        except billing.StripeError as exc:
            if exc.status == 404:
                m.skip = "not copied to the new account yet"
                continue
            raise
        if m.new_customer.get("deleted"):
            m.skip = "deleted on the new account"
            continue
        have = billing.best_subscription(match.customer["id"], new)
        if have is not None and have.get("status") not in ("canceled", "incomplete_expired"):
            m.skip = f"already has a {have.get('status')} subscription on the new account"
            continue
        m.card = copied_card(new, match.customer["id"], card_key(default_card(old, match.customer)))
        if m.card is None:
            m.skip = "no card on the new account: has the copy finished?"
    return moves


def carry_out(m: Move, old: billing.Client, new: billing.Client, price_id: str) -> None:
    """The three calls for one person. Safe to repeat: the subscription is made once."""
    cid, card = m.new_customer["id"], m.card
    current = (billing._id((m.new_customer.get("invoice_settings") or {}).get("default_payment_method"))
               if card["kind"] == "payment_method" else billing._id(m.new_customer.get("default_source")))
    if current != card["id"]:
        key = "invoice_settings[default_payment_method]" if card["kind"] == "payment_method" else "default_source"
        new.post(f"/v1/customers/{cid}", {key: card["id"]})
    sub = new.post("/v1/subscriptions", {
        "customer": cid,
        "items[0][price]": price_id,
        "items[0][quantity]": "1",
        "trial_end": str(int(m.period_end.timestamp())),
        "trial_settings[end_behavior][missing_payment_method]": "cancel",
        "metadata[moved_from]": m.old_sub["id"],
    }, idempotency_key=f"move-{m.old_sub['id']}")
    if not m.old_sub.get("cancel_at_period_end"):
        old.post(f"/v1/subscriptions/{m.old_sub['id']}", {"cancel_at_period_end": "true"})
    m.done = (f"{sub['id']} {sub.get('status')}, first charge {when(m.period_end)}; "
              f"old {m.old_sub['id']} ends then")


def summary(moves: list[Move], old_prices: Prices, new_price: str, yes: bool) -> str:
    ready = [m for m in moves if not m.skip]
    lines = [f"new price: {new_price}", ""]
    if ready:
        lines.append(f"{'done' if yes else 'would move'} ({len(ready)}):")
        for m in ready:
            old_price = old_prices.words(sub_price(m.old_sub))
            differs = "" if old_price == new_price else f" (was {old_price})"
            lines.append(f"  {m.person.name}: " + (m.done if yes else
                         f"free until {when(m.period_end)}, then {new_price}{differs}, {m.card['words']}; "
                         f"old subscription ends {when(m.period_end)}"))
    skipped = [m for m in moves if m.skip]
    if skipped:
        lines.append(f"\nleft alone ({len(skipped)}):")
        lines += [f"  {m.person.name}: {m.skip}" for m in skipped]
    if not yes and ready:
        lines.append("\nNothing done. Run again with --yes to make these subscriptions.")
    return "\n".join(lines)


def main() -> int:
    from app.config import settings
    ap = argparse.ArgumentParser(description="Start subscriptions on the new Stripe account when the old ones end.")
    ap.add_argument("file", help="a .csv or .xlsx with a header row")
    ap.add_argument("--yes", action="store_true", help="make the subscriptions (without it, only the plan is shown)")
    ap.add_argument("--price", help="a price id on the new account; MC_STRIPE_PRICE_ID otherwise")
    args = ap.parse_args()
    price_id = args.price or settings.STRIPE_PRICE_ID
    if not price_id:
        print("Set MC_STRIPE_PRICE_ID, or pass --price, with the price on the new account.", file=sys.stderr)
        return 1
    if not os.path.isfile(args.file):
        print(f"no such file: {args.file}", file=sys.stderr)
        return 1
    rows = directory.read_rows(args.file)
    if not rows:
        print("no rows found", file=sys.stderr)
        return 1
    old, new = old_client(), new_client()
    try:
        new_price = Prices(new).words(price_id)
        if new_price == price_id:
            print(f"The new account has no price {price_id}.", file=sys.stderr)
            return 1
        moves = plan(rows, old, new)
        if args.yes:
            for m in moves:
                if not m.skip:
                    carry_out(m, old, new, price_id)
    except (ValueError, billing.StripeError) as e:
        print(e, file=sys.stderr)
        return 1
    print(summary(moves, Prices(old), new_price, args.yes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
