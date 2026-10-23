"""
Self-serve profiles, and the subscription that pays for them, through Stripe.

Most participants are enrolled by their county and never see a price. This
is for everyone else: someone under an order whose county is not on
ActionsCount+ creates a profile at /join, proves the phone number is theirs
with a code, and pays on Stripe's hosted checkout page. They live in one
county of their own (`County.self_serve`), which has no officers.

Stripe holds the card and the subscription. This file holds a copy of the
status, kept current by Stripe's webhooks and by a sync when the person
comes back from checkout, and the handful of calls the app makes: make a
customer, open checkout, open the billing portal, read a subscription.

Nothing goes out unless MC_STRIPE_SECRET_KEY and MC_STRIPE_PRICE_ID are set.
Without them, demo mode hands out a subscription marked "demo" so the flow
can be shown, and anywhere else creating a profile is closed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import County, Program, Subscription, User

log = logging.getLogger("meetingscount.billing")

API = "https://api.stripe.com"
SELF_SERVE_NAME = "Self-pay"          # the county, and its one program
SELF_SERVE_JOIN_CODE = "SELF-PAY"
SIGNATURE_TOLERANCE_S = 300           # how old a webhook may be before it is refused


class StripeError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status, self.code = status, code


class Client(Protocol):
    def post(self, path: str, data: dict, *, idempotency_key: str | None = None) -> dict: ...
    def get(self, path: str, params: dict | None = None) -> dict: ...


class StripeClient:
    """The real thing: form-encoded calls over https, fifteen seconds at most."""

    def __init__(self, secret_key: str):
        self.secret_key = secret_key

    def post(self, path: str, data: dict, *, idempotency_key: str | None = None) -> dict:
        return self._call("POST", path, data=data, idempotency_key=idempotency_key)

    def get(self, path: str, params: dict | None = None) -> dict:
        return self._call("GET", path, params=params)

    def _call(self, method: str, path: str, *, data: dict | None = None,
              params: dict | None = None, idempotency_key: str | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.secret_key}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            r = httpx.request(method, API + path, data=data, params=params, headers=headers,
                              timeout=15)
        except httpx.HTTPError as exc:
            raise StripeError(f"could not reach Stripe: {exc}"[:300]) from exc
        try:
            payload = r.json()
        except ValueError:
            payload = {}
        if r.status_code >= 400:
            err = payload.get("error") or {}
            raise StripeError((err.get("message") or r.text or "Stripe refused the call")[:300],
                              status=r.status_code, code=err.get("code"))
        return payload


# Tests put their own client here.
client: Client | None = None


def get_client() -> Client:
    if not settings.stripe_configured:
        raise StripeError("Stripe is not configured")
    return client if client is not None else StripeClient(settings.STRIPE_SECRET_KEY)


def configured() -> bool:
    return settings.stripe_configured


def signup_open() -> bool:
    """
    Whether /join works here. Needs a way to prove the phone (texting, or
    demo mode showing the code on screen) and a way to pay (Stripe, or demo
    mode handing out a free subscription).
    """
    if not settings.SELF_SIGNUP:
        return False
    if settings.DEMO:
        return True
    return settings.sms_configured and settings.stripe_configured


# --------------------------------------------------------------------------- #
# The price, for the page that shows it
# --------------------------------------------------------------------------- #

_price_cache: dict[str, Any] = {"info": None, "fetched": 0.0, "for": None}


def price_info() -> dict | None:
    """What the subscription costs, in words, from Stripe. Cached for an hour."""
    if not settings.stripe_configured:
        return None
    fresh = (_price_cache["for"] == settings.STRIPE_PRICE_ID
             and time.time() - _price_cache["fetched"] < 3600)
    if fresh:
        return _price_cache["info"]
    try:
        p = get_client().get(f"/v1/prices/{settings.STRIPE_PRICE_ID}")
        info = price_words(p)
    except StripeError as exc:
        log.warning("could not read the price from Stripe: %s", exc)
        info = None
    _price_cache.update(info=info, fetched=time.time())
    _price_cache["for"] = settings.STRIPE_PRICE_ID
    return info


def trial_words(days: int) -> str | None:
    """"First month free", or "First 14 days free", or None without a trial."""
    if days <= 0:
        return None
    if 28 <= days <= 31:
        return "First month free"
    return f"First {days} days free"


def price_words(p: dict) -> dict:
    amount = p.get("unit_amount")
    currency = (p.get("currency") or "usd").lower()
    rec = p.get("recurring") or {}
    interval, every = rec.get("interval", "month"), int(rec.get("interval_count") or 1)
    money = (f"${amount / 100:,.2f}".replace(".00", "") if currency == "usd" and amount is not None
             else f"{(amount or 0) / 100:,.2f} {currency.upper()}")
    per = f"a {interval}" if every == 1 else f"every {every} {interval}s"
    return {"amount": amount, "currency": currency, "interval": interval,
            "interval_count": every, "label": f"{money} {per}"}


# --------------------------------------------------------------------------- #
# Where self-serve people live
# --------------------------------------------------------------------------- #

def self_serve_home(db: Session) -> tuple[County, Program]:
    """The county and program for people who signed up on their own. Made on first use."""
    county = db.scalar(select(County).where(County.self_serve.is_(True)))
    if county is None:
        county = County(name=SELF_SERVE_NAME, state="MI", timezone="America/Detroit",
                        self_serve=True)
        db.add(county)
        db.flush()
    program = db.scalar(select(Program).where(Program.county_id == county.id))
    if program is None:
        program = Program(county_id=county.id, name=SELF_SERVE_NAME, join_code=SELF_SERVE_JOIN_CODE)
        db.add(program)
        db.flush()
    return county, program


# --------------------------------------------------------------------------- #
# A person's standing
# --------------------------------------------------------------------------- #

def row_for(db: Session, user: User) -> Subscription | None:
    return db.scalar(select(Subscription).where(Subscription.user_id == user.id))


def ensure_row(db: Session, user: User) -> Subscription:
    """The subscription row for a new self-serve profile. Free in demo mode without Stripe."""
    row = row_for(db, user)
    if row is None:
        status = "unpaid_yet" if settings.stripe_configured else ("demo" if settings.DEMO else "unpaid_yet")
        row = Subscription(county_id=user.county_id, user_id=user.id, status=status,
                           price_id=settings.STRIPE_PRICE_ID or None)
        db.add(row)
        db.flush()
    return row


def status_out(db: Session, user: User) -> dict | None:
    """What the app shows. None for someone a county enrolled: they never see billing."""
    row = row_for(db, user)
    if row is None:
        return None
    return {
        "status": row.status, "active": row.active or not settings.stripe_configured,
        "words": status_words(row) if settings.stripe_configured else "No payment is needed here.",
        "current_period_end": row.current_period_end.isoformat() if row.current_period_end else None,
        "cancel_at_period_end": row.cancel_at_period_end,
        "payment": settings.stripe_configured,
        "can_pay": settings.stripe_configured and row.status in (
            "unpaid_yet", "canceled", "unpaid", "incomplete", "incomplete_expired"),
        "can_manage": settings.stripe_configured and bool(row.stripe_customer_id)
        and row.status not in ("unpaid_yet", "demo", "covered"),
        "price": price_info(),
        "trial": trial_words(settings.STRIPE_TRIAL_DAYS) if settings.stripe_configured else None,
    }


def status_words(row: Subscription) -> str:
    when = row.current_period_end.strftime("%B %-d").replace(" 0", " ") if row.current_period_end else None
    return {
        "unpaid_yet": "Not set up yet. Pay once and you are in.",
        "demo": "Demo profile. No payment needed here.",
        "covered": "Covered. No payment needed.",
        "trialing": f"Free trial{f' until {when}' if when else ''}.",
        "active": (f"Ends {when}. It will not renew." if row.cancel_at_period_end and when
                   else f"Active{f', renews {when}' if when else ''}."),
        "past_due": "The last payment did not go through. Update your card to keep your record going.",
        "canceled": "Canceled. Pay again to pick up where you left off.",
        "unpaid": "Payments stopped. Pay again to pick up where you left off.",
        "incomplete": "The first payment did not finish.",
        "incomplete_expired": "The first payment did not finish.",
        "paused": "Paused.",
    }.get(row.status, row.status)


def require_paid(db: Session, user: User) -> None:
    """A self-serve participant cannot check in without a live subscription, while payments are on."""
    if not settings.stripe_configured:
        return
    row = row_for(db, user)
    if row is not None and not row.active:
        raise HTTPException(402, "Your subscription is not active. Set up payment under You, "
                                 "then try again.")


# --------------------------------------------------------------------------- #
# Stripe calls the app makes
# --------------------------------------------------------------------------- #

def customer_id(db: Session, row: Subscription, user: User) -> str:
    """The Stripe customer for this person, made the first time it is needed."""
    if row.stripe_customer_id:
        return row.stripe_customer_id
    data = {"name": user.name, "metadata[user_id]": str(user.id)}
    if user.email:
        data["email"] = user.email
    if user.phone:
        data["phone"] = user.phone
    c = get_client().post("/v1/customers", data, idempotency_key=f"customer-{user.id}")
    row.stripe_customer_id = c["id"]
    row.updated_at = datetime.now(timezone.utc)
    db.commit()
    return c["id"]


def promotion(code: str) -> dict | None:
    """
    The Stripe promotion code behind what someone typed, or None. Codes are
    made in the Stripe dashboard (a coupon, then a promotion code on it), so
    a free year for a demo is a 100% coupon that lasts 12 months. Stripe
    matches the code case-insensitively and leaves out inactive ones.
    """
    code = " ".join(code.split())
    if not code or len(code) > 40:
        return None
    found = get_client().get("/v1/promotion_codes", {"code": code, "active": "true", "limit": "1"})
    for p in found.get("data") or []:
        coupon = p.get("coupon") or {}
        if coupon.get("valid") is False:
            continue
        if p.get("expires_at") and int(p["expires_at"]) < time.time():
            continue
        if p.get("max_redemptions") and int(p.get("times_redeemed") or 0) >= int(p["max_redemptions"]):
            continue
        return p
    return None


def checkout_url(db: Session, user: User, base_url: str, code: str | None = None) -> str:
    """
    Stripe's hosted page for the subscription. The card is typed there,
    never here. Without a code: the trial, if one is set, and the card up
    front. With a code: the discount instead of the trial, and no card if
    nothing is due, which is what a free year for a demo needs.
    """
    row = ensure_row(db, user)
    data = {
        "mode": "subscription",
        "customer": customer_id(db, row, user),
        "line_items[0][price]": settings.STRIPE_PRICE_ID,
        "line_items[0][quantity]": "1",
        "client_reference_id": str(user.id),
        "subscription_data[metadata][user_id]": str(user.id),
        "success_url": f"{base_url}/join?paid=1&session_id={{CHECKOUT_SESSION_ID}}",
        "cancel_url": f"{base_url}/join?canceled=1",
    }
    if code:
        promo = promotion(code)
        if promo is None:
            raise HTTPException(422, "That code is not valid, or it has run out. Check it and try again.")
        data["discounts[0][promotion_code]"] = promo["id"]
        data["payment_method_collection"] = "if_required"
    elif settings.STRIPE_TRIAL_DAYS > 0:
        data["subscription_data[trial_period_days]"] = str(settings.STRIPE_TRIAL_DAYS)
    s = get_client().post("/v1/checkout/sessions", data)
    return s["url"]


def portal_url(db: Session, user: User, base_url: str) -> str:
    """Stripe's billing portal: change the card, see invoices, cancel."""
    row = row_for(db, user)
    if row is None or not row.stripe_customer_id:
        raise HTTPException(409, "There is no billing account for this profile yet.")
    s = get_client().post("/v1/billing_portal/sessions",
                          {"customer": row.stripe_customer_id, "return_url": f"{base_url}/#you"})
    return s["url"]


def sync_checkout(db: Session, user: User, session_id: str) -> Subscription:
    """
    Back from checkout, before the webhook has necessarily arrived: read the
    session and its subscription from Stripe and record them. The session
    has to belong to this person's own customer, or it is ignored.
    """
    row = ensure_row(db, user)
    s = get_client().get(f"/v1/checkout/sessions/{session_id}", {"expand[]": "subscription"})
    if _id(s.get("customer")) != row.stripe_customer_id:
        raise HTTPException(403, "That checkout does not belong to this profile.")
    sub = s.get("subscription")
    if isinstance(sub, str):
        sub = get_client().get(f"/v1/subscriptions/{sub}")
    if isinstance(sub, dict):
        apply_subscription(db, row, sub)
    return row


def apply_subscription(db: Session, row: Subscription, sub: dict, *, event_id: str | None = None) -> None:
    """Copy what matters from a Stripe subscription object onto the row, and commit."""
    copy_subscription(row, sub, event_id=event_id)
    db.commit()


def copy_subscription(row: Subscription, sub: dict, *, event_id: str | None = None) -> None:
    """The copy itself, with no commit, for a caller that decides later whether to keep it."""
    row.stripe_subscription_id = sub.get("id") or row.stripe_subscription_id
    row.status = (sub.get("status") or row.status)[:24]
    row.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    items = ((sub.get("items") or {}).get("data") or [{}])
    price = (items[0].get("price") or {}) if items else {}
    row.price_id = _id(price) or row.price_id
    # Older API versions put the period on the subscription, newer ones on
    # each item. Read whichever is there.
    end = sub.get("current_period_end") or (items[0].get("current_period_end") if items else None)
    row.current_period_end = datetime.fromtimestamp(int(end), tz=timezone.utc) if end else None
    if event_id:
        row.last_event_id = event_id
    row.updated_at = datetime.now(timezone.utc)


def _id(obj: Any) -> str | None:
    """Stripe sends related objects as an id or, when expanded, as the object."""
    if isinstance(obj, dict):
        return obj.get("id")
    return obj or None


# --------------------------------------------------------------------------- #
# People who already pay here
# --------------------------------------------------------------------------- #
# The old site took its payments in this same Stripe account, so someone
# moving over usually has a customer and a subscription there already. The
# importer finds them by email, or failing that by phone, and copies that
# subscription onto their row instead of asking for a card a second time.
# Nothing here writes to Stripe.

STANDING = ("active", "trialing", "past_due", "paused", "unpaid", "incomplete",
            "canceled", "incomplete_expired")        # best first, for choosing among several


@dataclass
class Match:
    customer: dict
    subscription: dict | None       # the customer's best one, if they have any
    others: int = 0                 # how many more customers matched; usually none

    @property
    def words(self) -> str:
        who = self.customer.get("email") or self.customer.get("phone") or self.customer["id"]
        standing = self.subscription.get("status") if self.subscription else "no subscription"
        more = (f", {self.others} more customer{'s' if self.others > 1 else ''} matched too"
                if self.others else "")
        return f"Stripe {standing} ({who}{more})"


def _search_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def find_customers(email: str | None, phone: str | None) -> list[dict]:
    """
    Stripe customers with this email, or failing that with this phone number.
    Stripe's email filter is exact and case-sensitive, so the email is tried
    as written and lowercased, then through search. Phones are stored however
    whoever typed them did, so a few shapes are tried and the digits compared.
    """
    c = get_client()
    found: dict[str, dict] = {}
    email = (email or "").strip()
    if email:
        for e in dict.fromkeys((email, email.lower())):
            for cust in c.get("/v1/customers", {"email": e, "limit": "100"}).get("data") or []:
                found[cust["id"]] = cust
        if not found:
            query = f"email:'{_search_quote(email.lower())}'"
            for cust in c.get("/v1/customers/search", {"query": query, "limit": "100"}).get("data") or []:
                found[cust["id"]] = cust
    digits = re.sub(r"\D", "", phone or "")[-10:]
    if not found and len(digits) == 10:
        shapes = (digits, f"{digits[:3]}-{digits[3:6]}-{digits[6:]}",
                  f"({digits[:3]}) {digits[3:6]}-{digits[6:]}")
        for shape in shapes:
            hits = c.get("/v1/customers/search", {"query": f"phone~'{shape}'", "limit": "100"})
            for cust in hits.get("data") or []:
                if re.sub(r"\D", "", cust.get("phone") or "")[-10:] == digits:
                    found[cust["id"]] = cust
            if found:
                break
    return list(found.values())


def _rank(sub: dict | None) -> tuple[int, int]:
    """Live subscriptions first, then the newest."""
    if sub is None:
        return (len(STANDING) + 1, 0)
    standing = sub.get("status") or ""
    return (STANDING.index(standing) if standing in STANDING else len(STANDING),
            -int(sub.get("created") or 0))


def match_existing(email: str | None, phone: str | None) -> Match | None:
    """The Stripe customer this person already is, with their best subscription, or None."""
    customers = find_customers(email, phone)
    if not customers:
        return None
    best: tuple[dict, dict | None] | None = None
    for cust in customers:
        subs = get_client().get("/v1/subscriptions", {"customer": cust["id"], "status": "all",
                                                       "limit": "100"}).get("data") or []
        sub = min(subs, key=_rank) if subs else None
        if best is None or _rank(sub) < _rank(best[1]):
            best = (cust, sub)
    return Match(customer=best[0], subscription=best[1], others=len(customers) - 1)


def taken_by(db: Session, match: Match) -> Subscription | None:
    """The row, if any, that already holds this customer or subscription: both ids are unique."""
    row = _row_by_customer(db, match.customer["id"])
    if row is None and match.subscription:
        row = db.scalar(select(Subscription).where(
            Subscription.stripe_subscription_id == match.subscription["id"]))
    return row


def adopt(db: Session, row: Subscription, match: Match) -> None:
    """Record someone's existing Stripe customer and subscription on their row. No commit."""
    row.stripe_customer_id = match.customer["id"]
    if match.subscription:
        copy_subscription(row, match.subscription)
    row.updated_at = datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #

def sign(payload: bytes, secret: str, at: int | None = None) -> str:
    """
    A Stripe-Signature header for `payload`: HMAC-SHA256 over "<time>.<body>".
    Used by the tests to make calls that look like Stripe's, and by
    signature_ok to check them.
    """
    t = int(at if at is not None else time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def signature_ok(payload: bytes, header: str | None, secret: str | None = None,
                 now: float | None = None) -> bool:
    secret = secret if secret is not None else settings.STRIPE_WEBHOOK_SECRET
    if not secret or not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        t = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - t) > SIGNATURE_TOLERANCE_S:
        return False
    want = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    given = [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    return any(hmac.compare_digest(want, g) for g in given)


SUBSCRIPTION_EVENTS = ("customer.subscription.created", "customer.subscription.updated",
                       "customer.subscription.deleted", "customer.subscription.paused",
                       "customer.subscription.resumed")


def handle_event(db: Session, event: dict) -> str:
    """
    One event from Stripe. Returns a few words saying what was done, for the
    log. The event is a nudge: for a subscription, the current state is read
    back from Stripe rather than trusted from the event, because events can
    arrive out of order.
    """
    kind, event_id = event.get("type", ""), event.get("id")
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        if obj.get("mode") != "subscription":
            return "ignored: not a subscription checkout"
        row = _row_by_customer(db, _id(obj.get("customer")))
        if row is None:
            return "ignored: unknown customer"
        if row.last_event_id == event_id:
            return "already handled"
        sub_id = _id(obj.get("subscription"))
        if not sub_id:
            return "ignored: no subscription on the session"
        apply_subscription(db, row, _latest(sub_id, {"id": sub_id, "status": "active"}),
                           event_id=event_id)
        return f"subscription {row.status} for user {row.user_id}"

    if kind in SUBSCRIPTION_EVENTS:
        row = _row_by_customer(db, _id(obj.get("customer")))
        if row is None:
            return "ignored: unknown customer"
        if row.last_event_id == event_id:
            return "already handled"
        fallback = dict(obj)
        if kind == "customer.subscription.deleted":
            fallback["status"] = "canceled"
        apply_subscription(db, row, _latest(obj.get("id"), fallback), event_id=event_id)
        return f"subscription {row.status} for user {row.user_id}"

    if kind in ("invoice.payment_failed", "invoice.paid", "invoice.payment_succeeded"):
        # Stripe moves the subscription itself and sends that event too.
        return "noted"

    return "ignored"


def _latest(sub_id: str | None, fallback: dict) -> dict:
    """The subscription as Stripe has it now, or what the event carried if Stripe cannot be reached."""
    if not sub_id:
        return fallback
    try:
        return get_client().get(f"/v1/subscriptions/{sub_id}")
    except StripeError as exc:
        log.warning("could not read subscription %s from Stripe, using the event: %s", sub_id, exc)
        return fallback


def _row_by_customer(db: Session, customer: str | None) -> Subscription | None:
    if not customer:
        return None
    return db.scalar(select(Subscription).where(Subscription.stripe_customer_id == customer))


def row_for_event_county(db: Session, event: dict) -> int | None:
    """Which county's audit log an event belongs in: the self-serve one, when it exists."""
    county = db.scalar(select(County).where(County.self_serve.is_(True)))
    return county.id if county else None
