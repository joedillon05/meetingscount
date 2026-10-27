"""
What the Stripe migration tools share: the old account's client, dates in
Michigan time, prices in words, paging, and the card on a customer.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import billing  # noqa: E402

LIVE = ("active", "trialing", "past_due")     # a subscription worth moving
LOCAL = ZoneInfo("America/Detroit")


def old_client() -> billing.Client:
    """The old account, from OLD_STRIPE_KEY in the environment. Never printed."""
    key = os.environ.get("OLD_STRIPE_KEY", "")
    if not key.startswith("sk_"):
        sys.exit("Set OLD_STRIPE_KEY to the old account's secret key in this terminal "
                 "(export OLD_STRIPE_KEY=sk_live_...), then run again.")
    return billing.StripeClient(key)


def new_client() -> billing.Client:
    """The account the app bills through, from MC_STRIPE_SECRET_KEY as the app reads it."""
    from app.config import settings
    if not settings.STRIPE_SECRET_KEY.startswith("sk_"):
        sys.exit("Set MC_STRIPE_SECRET_KEY to the new account's secret key in this terminal, "
                 "then run again.")
    return billing.StripeClient(settings.STRIPE_SECRET_KEY)


def when(moment: datetime | int | None) -> str:
    if not moment:
        return "no date"
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(int(moment), tz=LOCAL)
    return moment.astimezone(LOCAL).strftime("%b %-d, %Y")


def page(client: billing.Client, path: str, params: dict, cap: int = 2000) -> list[dict]:
    out, params = [], dict(params, limit="100")
    while True:
        got = client.get(path, params)
        data = got.get("data") or []
        out += data
        if not got.get("has_more") or not data or len(out) >= cap:
            return out
        params["starting_after"] = data[-1]["id"]


class Prices:
    """Price ids to words like "$15 a month", each fetched once."""

    def __init__(self, client: billing.Client):
        self.client, self.seen = client, {}

    def words(self, price_id: str | None) -> str:
        if not price_id:
            return "no price"
        if price_id not in self.seen:
            try:
                self.seen[price_id] = billing.price_words(self.client.get(f"/v1/prices/{price_id}"))["label"]
            except billing.StripeError:
                self.seen[price_id] = price_id
        return self.seen[price_id]


def sub_price(sub: dict) -> str | None:
    items = (sub.get("items") or {}).get("data") or []
    return billing._id(items[0].get("price")) if items else None


def card_words(card: dict | None) -> str:
    if not card:
        return "NO CARD ON FILE"
    c = card.get("card") or card                   # a PaymentMethod nests it; a legacy Card is the card
    return (f"{c.get('brand') or 'card'} ····{c.get('last4')} "
            f"exp {c.get('exp_month')}/{str(c.get('exp_year') or '')[-2:]}")


def card_key(card: dict | None) -> tuple | None:
    """What stays the same about a card when Stripe copies it: number tail and expiry."""
    if not card:
        return None
    c = card.get("card") or card
    return (str(c.get("last4") or ""), int(c.get("exp_month") or 0), int(c.get("exp_year") or 0))


def default_card(client: billing.Client, customer: dict) -> dict | None:
    """The card a customer is charged on: the default payment method, else the
    default source, else their one card. None when there is nothing."""
    cid = customer["id"]
    pm = billing._id((customer.get("invoice_settings") or {}).get("default_payment_method"))
    try:
        if pm:
            return client.get(f"/v1/payment_methods/{pm}")
        src = billing._id(customer.get("default_source"))
        if src:
            return client.get(f"/v1/customers/{cid}/sources/{src}")
        cards = client.get("/v1/payment_methods", {"customer": cid, "type": "card", "limit": "100"})
        found = cards.get("data") or []
        return found[0] if found else None
    except billing.StripeError:
        return None
