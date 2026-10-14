"""
Creating a profile at /join, and paying for it through Stripe.

No real call to Stripe is ever made. A fake stands in for it: it remembers
customers, mints checkout and portal links, and holds subscriptions a test
can move between states the way a person on Stripe's pages would.
"""
import itertools
import json
import re
import secrets
import time
from datetime import datetime, timezone

import pytest

from app import billing, security, sms
from app.config import settings
from app.db import SessionLocal
from app.models import (
    County, Meeting, MeetingCategory, MeetingSource, Participant, Requirement, Role, Subscription,
    User,
)

from .conftest import as_
from .test_texts import FakePhoneCompany

WEBHOOK = "whsec_test"
_numbers = itertools.count(200)


def fresh_phone() -> str:
    return f"(734) 555-0{next(_numbers):03d}"


class FakeStripe:
    """Stands in for Stripe's API. Only the calls the app makes exist here."""

    def __init__(self):
        self.customers, self.subscriptions, self.sessions, self.calls = {}, {}, {}, []
        self.refuse = None            # an error to raise instead of answering
        # Promotion codes made in the dashboard: a free year for demos, and
        # one that has been used up.
        self.promotions = {
            "FREEYEAR": {"id": "promo_free", "code": "FREEYEAR", "active": True,
                         "coupon": {"id": "year", "percent_off": 100, "duration": "repeating",
                                    "duration_in_months": 12, "valid": True}},
            "GONE": {"id": "promo_gone", "code": "GONE", "active": True, "max_redemptions": 5,
                     "times_redeemed": 5, "coupon": {"id": "x", "percent_off": 100, "valid": True}},
        }

    def post(self, path, data, *, idempotency_key=None):
        self.calls.append(("POST", path, data))
        if self.refuse:
            raise billing.StripeError(self.refuse, status=500)
        if path == "/v1/customers":
            cid = "cus_" + secrets.token_hex(4)
            self.customers[cid] = dict(data, id=cid)
            return self.customers[cid]
        if path == "/v1/checkout/sessions":
            sid = "cs_test_" + secrets.token_hex(4)
            self.sessions[sid] = {"id": sid, "object": "checkout.session", "mode": data["mode"],
                                  "customer": data["customer"], "subscription": None,
                                  "success_url": data["success_url"], "cancel_url": data["cancel_url"],
                                  "url": f"https://checkout.stripe.com/c/pay/{sid}"}
            return self.sessions[sid]
        if path == "/v1/billing_portal/sessions":
            return {"customer": data["customer"], "return_url": data["return_url"],
                    "url": f"https://billing.stripe.com/p/session/{secrets.token_hex(4)}"}
        raise billing.StripeError(f"unexpected POST {path}", status=404)

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if self.refuse:
            raise billing.StripeError(self.refuse, status=500)
        if path.startswith("/v1/prices/"):
            return {"id": path.rsplit("/", 1)[1], "unit_amount": 1200, "currency": "usd",
                    "recurring": {"interval": "month", "interval_count": 1}}
        if path == "/v1/promotion_codes":
            p = self.promotions.get((params or {}).get("code", "").upper())
            return {"data": [p] if p else []}
        if path.startswith("/v1/checkout/sessions/"):
            s = self.sessions.get(path.rsplit("/", 1)[1])
            if not s:
                raise billing.StripeError("No such checkout.session", status=404)
            out = dict(s)
            if (params or {}).get("expand[]") == "subscription" and s["subscription"]:
                out["subscription"] = self.subscriptions[s["subscription"]]
            return out
        if path.startswith("/v1/subscriptions/"):
            sub = self.subscriptions.get(path.rsplit("/", 1)[1])
            if not sub:
                raise billing.StripeError("No such subscription", status=404)
            return sub
        raise billing.StripeError(f"unexpected GET {path}", status=404)

    # What happens on Stripe's own pages.
    def pay(self, session_id, status="active", days=30):
        s = self.sessions[session_id]
        sub_id = "sub_" + secrets.token_hex(4)
        self.subscriptions[sub_id] = {
            "id": sub_id, "object": "subscription", "customer": s["customer"], "status": status,
            "cancel_at_period_end": False,
            "items": {"data": [{"price": {"id": settings.STRIPE_PRICE_ID},
                                "current_period_end": int(time.time()) + days * 86400}]}}
        s["subscription"], s["status"] = sub_id, "complete"
        return sub_id

    def move(self, sub_id, status, **fields):
        self.subscriptions[sub_id].update(status=status, **fields)
        return self.subscriptions[sub_id]


def event(kind, obj, eid=None):
    return {"id": eid or "evt_" + secrets.token_hex(6), "object": "event", "type": kind,
            "created": int(time.time()), "data": {"object": obj}}


def post_webhook(client, ev, secret=WEBHOOK, at=None, header=None):
    body = json.dumps(ev).encode()
    headers = {"Content-Type": "application/json"}
    sig = header if header is not None else billing.sign(body, secret, at)
    if sig:
        headers["Stripe-Signature"] = sig
    return client.post("/api/billing/webhook", content=body, headers=headers)


@pytest.fixture(autouse=True)
def fresh_limits():
    security._HITS.clear()
    yield
    security._HITS.clear()


@pytest.fixture
def stripe(monkeypatch):
    """A real deployment: demo off, Stripe and texting on, both faked."""
    fake, phones = FakeStripe(), FakePhoneCompany()
    monkeypatch.setattr(settings, "DEMO", False)
    monkeypatch.setattr(settings, "DEV_AUTH", False)
    monkeypatch.setattr(settings, "SELF_SIGNUP", True)
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_PRICE_ID", "price_test")
    monkeypatch.setattr(settings, "STRIPE_WEBHOOK_SECRET", WEBHOOK)
    monkeypatch.setattr(settings, "STRIPE_TRIAL_DAYS", 0)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setattr(settings, "TWILIO_FROM", "+17345550000")
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(sms, "transport", phones)
    monkeypatch.setattr(billing, "client", fake)
    billing._price_cache.update(info=None, fetched=0.0)
    billing._price_cache["for"] = None
    yield fake, phones
    monkeypatch.setattr(sms, "transport", None)
    monkeypatch.setattr(billing, "client", None)


@pytest.fixture
def demo_no_stripe(monkeypatch):
    """A demo deployment with nothing paid for: codes on screen, profiles free."""
    phones = FakePhoneCompany()
    monkeypatch.setattr(settings, "DEMO", True)
    monkeypatch.setattr(settings, "SELF_SIGNUP", True)
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "")
    monkeypatch.setattr(settings, "STRIPE_PRICE_ID", "")
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "TWILIO_FROM", "")
    monkeypatch.setattr(sms, "transport", phones)
    yield phones
    monkeypatch.setattr(sms, "transport", None)


def _code_texted(phones, phone):
    texts = phones.to(security.normalize_phone(phone))
    assert texts, "no code was texted"
    return re.search(r"\b(\d{6})\b", texts[-1]).group(1)


def _join(client, phones, phone=None, name="Lee Tran", **extra):
    phone = phone or fresh_phone()
    r = client.post("/api/signup/start", json={"name": name, "phone": phone, "per_week": 2,
                                               "email": "lee@example.com"} | extra)
    assert r.status_code == 200, r.text
    r = client.post("/api/signup/verify", json={"phone": phone, "code": _code_texted(phones, phone)})
    assert r.status_code == 200, r.text
    return r.json()


def _consent(client):
    r = client.post("/api/my/consent", json={"version": settings.CONSENT_VERSION})
    assert r.status_code == 200, r.text


def _open_meeting():
    """A directory meeting, which every county sees, happening now."""
    db = SessionLocal()
    try:
        m = Meeting(county_id=None, name="Open AA", category=MeetingCategory.public,
                    source=MeetingSource.directory, lat=42.2750, lng=-83.7413,
                    starts_at=datetime.now(timezone.utc), duration_min=60, radius_ft=500,
                    time_window_min=60)
        db.add(m)
        db.commit()
        return m.id
    finally:
        db.close()


def _check_in(client, meeting_id):
    return client.post("/api/checkins", json={"meeting_id": meeting_id, "lat": 42.2750,
                                              "lng": -83.7413, "accuracy_m": 8})


def _checkout(client, fake):
    r = client.post("/api/billing/checkout")
    assert r.status_code == 200, r.text
    assert r.json()["url"].startswith("https://checkout.stripe.com/")
    return r.json()["url"].rsplit("/", 1)[1]


def _me(client):
    r = client.get("/api/me")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Making a profile
# --------------------------------------------------------------------------- #

def test_the_join_page_knows_whether_it_is_open(client, stripe, monkeypatch):
    ctx = client.get("/api/signup/context").json()
    assert ctx["open"] and ctx["payment"] and ctx["texting"]
    assert ctx["price"]["label"] == "$12 a month"
    assert client.get("/api/auth/context").json()["self_signup"] is True

    monkeypatch.setattr(settings, "SELF_SIGNUP", False)
    assert client.get("/api/signup/context").json()["open"] is False
    r = client.post("/api/signup/start", json={"name": "Lee Tran", "phone": fresh_phone()})
    assert r.status_code == 404


def test_a_profile_exists_only_after_the_code_and_cannot_check_in_until_paid(client, stripe):
    fake, phones = stripe
    phone = fresh_phone()
    r = client.post("/api/signup/start", json={"name": "  Lee   Tran ", "phone": phone,
                                               "email": "Lee@Example.com", "per_week": 2})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is True and "demo_code" not in r.json()
    assert "lee" not in r.json()["message"].lower()      # never says whether the number is known

    db = SessionLocal()
    assert db.query(User).filter(User.phone == security.normalize_phone(phone)).count() == 0
    db.close()

    r = client.post("/api/signup/verify", json={"phone": phone, "code": _code_texted(phones, phone)})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is True
    assert r.json()["billing"]["status"] == "unpaid_yet" and r.json()["billing"]["active"] is False

    me = _me(client)
    assert me["name"] == "Lee Tran" and me["officer"] is None
    assert me["county"] is None                      # the self-pay county is plumbing, not shown
    assert me["billing"]["can_pay"] is True and me["billing"]["price"]["label"] == "$12 a month"

    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == me["id"]).one()
        assert u.email == "lee@example.com" and u.role == Role.participant
        county = db.get(County, u.county_id)
        assert county.self_serve is True
        p = db.query(Participant).filter(Participant.user_id == u.id).one()
        req = db.query(Requirement).filter(Requirement.participant_id == p.id).one()
        assert req.count_required == 2 and req.period.value == "week" and req.category is None
    finally:
        db.close()

    progress = client.get("/api/my/progress").json()
    assert progress["has_requirements"] is True
    assert progress["summary"]["requirements"][0]["category"] == "any"

    _consent(client)
    r = _check_in(client, _open_meeting())
    assert r.status_code == 402, r.text
    assert "subscription" in r.json()["detail"].lower()


def test_wrong_codes_fail_and_a_code_works_once(client, stripe):
    fake, phones = stripe
    phone = fresh_phone()
    assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": phone}).status_code == 200
    code = _code_texted(phones, phone)
    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/api/signup/verify", json={"phone": phone, "code": wrong})
    assert r.status_code == 401
    assert client.post("/api/signup/verify", json={"phone": phone, "code": code}).status_code == 200
    client.cookies.clear()
    r = client.post("/api/signup/verify", json={"phone": phone, "code": code})
    assert r.status_code == 401, "a used code signed someone in again"


def test_a_number_that_already_has_a_profile_is_signed_in_not_duplicated(client, stripe):
    fake, phones = stripe
    db = SessionLocal()
    existing = db.query(User).filter(User.role == Role.participant, User.phone.is_not(None)).first()
    db.close()

    start = client.post("/api/signup/start", json={"name": "Someone Else", "phone": existing.phone})
    assert start.status_code == 200
    fresh = client.post("/api/signup/start", json={"name": "Someone Else", "phone": fresh_phone()})
    assert start.json()["delivered"] == fresh.json()["delivered"]         # same answer either way

    r = client.post("/api/signup/verify", json={"phone": existing.phone,
                                                "code": _code_texted(phones, existing.phone)})
    assert r.status_code == 200, r.text
    assert r.json()["created"] is False and r.json()["id"] == existing.id
    assert r.json()["billing"] is None                 # their county pays, nothing to buy
    db = SessionLocal()
    assert db.query(User).filter(User.phone == existing.phone).count() == 1
    assert db.query(Subscription).filter(Subscription.user_id == existing.id).count() == 0
    db.close()
    assert client.post("/api/billing/checkout").status_code == 404


def test_sign_up_is_rate_limited(client, stripe):
    for _ in range(5):
        assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": fresh_phone()}).status_code == 200
    assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": fresh_phone()}).status_code == 429


def test_bad_input_is_explained(client, stripe):
    assert client.post("/api/signup/start", json={"name": "L", "phone": fresh_phone()}).status_code == 422
    assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": "555-01"}).status_code == 422
    assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": fresh_phone(),
                                                  "email": "not-an-address"}).status_code == 422
    assert client.post("/api/signup/start", json={"name": "Lee Tran", "phone": fresh_phone(),
                                                  "per_week": 0}).status_code == 422


# --------------------------------------------------------------------------- #
# Paying
# --------------------------------------------------------------------------- #

def test_checkout_then_the_webhook_turns_the_profile_on(client, stripe):
    fake, phones = stripe
    me = _join(client, phones)
    _consent(client)
    meeting = _open_meeting()

    session = _checkout(client, fake)
    s = fake.sessions[session]
    assert s["success_url"].startswith("https://app.example/join?paid=1&session_id=")
    assert s["cancel_url"] == "https://app.example/join?canceled=1"
    customer = fake.customers[s["customer"]]
    assert customer["metadata[user_id]"] == str(me["id"])
    assert customer["phone"].startswith("+1734") and customer["email"] == "lee@example.com"

    # Stripe tells us, before the person is even back in the app.
    sub_id = fake.pay(session)
    ev = event("checkout.session.completed", {"id": session, "object": "checkout.session",
                                              "mode": "subscription", "customer": s["customer"],
                                              "subscription": sub_id})
    r = post_webhook(client, ev)
    assert r.status_code == 200, r.text
    assert r.json()["did"].startswith("subscription active")

    b = client.get("/api/billing").json()["subscription"]
    assert b["status"] == "active" and b["active"] is True
    assert b["current_period_end"] and b["can_manage"] is True and b["can_pay"] is False
    assert b["words"].startswith("Active, renews ")
    assert _check_in(client, meeting).status_code == 201

    # The same event again does nothing. Stripe does retry.
    assert post_webhook(client, ev).json()["did"] == "already handled"
    assert client.post("/api/billing/checkout").status_code == 409

    # A later cancellation on Stripe's side switches check-in off.
    fake.move(sub_id, "canceled")
    r = post_webhook(client, event("customer.subscription.deleted", fake.subscriptions[sub_id]))
    assert r.json()["did"].startswith("subscription canceled")
    b = _me(client)["billing"]
    assert b["active"] is False and b["can_pay"] is True
    assert b["words"].startswith("Canceled")
    assert _check_in(client, meeting).status_code == 402

    r = client.post("/api/billing/portal")
    assert r.status_code == 200 and r.json()["url"].startswith("https://billing.stripe.com/")
    assert fake.calls[-1][2]["return_url"] == "https://app.example/#you"


def test_the_first_month_free_rides_on_checkout_with_the_card_up_front(client, stripe, monkeypatch):
    fake, phones = stripe
    monkeypatch.setattr(settings, "STRIPE_TRIAL_DAYS", 30)
    assert client.get("/api/signup/context").json()["trial"] == "First month free"
    me = _join(client, phones)
    assert me["billing"]["trial"] == "First month free"
    _checkout(client, fake)
    sent = next(d for m, p, d in fake.calls if p == "/v1/checkout/sessions")
    assert sent["subscription_data[trial_period_days]"] == "30"
    assert "discounts[0][promotion_code]" not in sent and "payment_method_collection" not in sent


def test_a_promotion_code_replaces_the_trial_and_needs_no_card(client, stripe, monkeypatch):
    fake, phones = stripe
    monkeypatch.setattr(settings, "STRIPE_TRIAL_DAYS", 30)
    _join(client, phones)
    r = client.post("/api/billing/checkout", json={"code": " freeyear "})
    assert r.status_code == 200, r.text
    sent = next(d for m, p, d in fake.calls if p == "/v1/checkout/sessions")
    assert sent["discounts[0][promotion_code]"] == "promo_free"
    assert sent["payment_method_collection"] == "if_required"
    assert "subscription_data[trial_period_days]" not in sent

    r = client.post("/api/billing/checkout", json={"code": "NOPE"})
    assert r.status_code == 422 and "not valid" in r.json()["detail"]
    r = client.post("/api/billing/checkout", json={"code": "GONE"})
    assert r.status_code == 422, "a used-up code was accepted"
    assert client.post("/api/billing/checkout", json={"code": ""}).status_code == 200


def test_code_guessing_is_rate_limited(client, stripe):
    fake, phones = stripe
    _join(client, phones)
    for _ in range(10):
        assert client.post("/api/billing/checkout", json={"code": "NOPE"}).status_code == 422
    assert client.post("/api/billing/checkout", json={"code": "FREEYEAR"}).status_code == 429


def test_trial_words():
    assert billing.trial_words(0) is None
    assert billing.trial_words(30) == "First month free"
    assert billing.trial_words(31) == "First month free"
    assert billing.trial_words(14) == "First 14 days free"


def test_back_from_checkout_the_app_syncs_without_waiting_for_the_webhook(client, stripe):
    fake, phones = stripe
    _join(client, phones)
    session = _checkout(client, fake)
    fake.pay(session, status="trialing")

    r = client.post("/api/billing/sync", json={"session_id": session})
    assert r.status_code == 200, r.text
    assert r.json()["subscription"]["status"] == "trialing"
    assert r.json()["subscription"]["active"] is True
    assert r.json()["subscription"]["words"].startswith("Free trial until ")

    # Somebody else's session id does nothing to this profile.
    other = fake.post("/v1/checkout/sessions", {"mode": "subscription", "customer": "cus_other",
                                                "success_url": "x", "cancel_url": "y"})
    assert client.post("/api/billing/sync", json={"session_id": other["id"]}).status_code == 403
    assert client.post("/api/billing/sync", json={"session_id": "cs_test_nope"}).status_code == 502


def test_past_due_keeps_working_while_stripe_retries_the_card(client, stripe):
    fake, phones = stripe
    _join(client, phones)
    _consent(client)
    session = _checkout(client, fake)
    sub_id = fake.pay(session)
    post_webhook(client, event("customer.subscription.created", fake.subscriptions[sub_id]))

    fake.move(sub_id, "past_due")
    post_webhook(client, event("customer.subscription.updated", fake.subscriptions[sub_id]))
    b = _me(client)["billing"]
    assert b["status"] == "past_due" and b["active"] is True and b["can_manage"] is True
    assert _check_in(client, _open_meeting()).status_code == 201

    fake.move(sub_id, "active", cancel_at_period_end=True)
    post_webhook(client, event("customer.subscription.updated", fake.subscriptions[sub_id]))
    b = _me(client)["billing"]
    assert b["cancel_at_period_end"] is True and b["words"].startswith("Ends ")


def test_webhooks_need_a_good_signature(client, stripe):
    fake, phones = stripe
    ev = event("customer.subscription.updated", {"id": "sub_x", "customer": "cus_nobody"})
    assert post_webhook(client, ev, secret="whsec_wrong").status_code == 400
    assert post_webhook(client, ev, at=int(time.time()) - 1000).status_code == 400
    assert post_webhook(client, ev, header="").status_code == 400
    assert post_webhook(client, ev, header="t=abc,v1=def").status_code == 400
    r = post_webhook(client, ev)
    assert r.status_code == 200 and r.json()["did"] == "ignored: unknown customer"
    assert post_webhook(client, event("invoice.paid", {})).json()["did"] == "noted"
    assert post_webhook(client, event("charge.refunded", {})).json()["did"] == "ignored"


def test_stripe_being_down_is_an_error_not_a_crash(client, stripe):
    fake, phones = stripe
    _join(client, phones)
    fake.refuse = "Stripe is having a bad day"
    r = client.post("/api/billing/checkout")
    assert r.status_code == 502 and "bad day" in r.json()["detail"]


def test_staff_have_nothing_to_pay(client, ids):
    r = client.post("/api/billing/checkout", headers=as_(ids["officer"]))
    assert r.status_code == 403
    assert client.get("/api/billing", headers=as_(ids["officer"])).json()["subscription"] is None


# --------------------------------------------------------------------------- #
# Demo, and the words
# --------------------------------------------------------------------------- #

def test_a_demo_without_stripe_gives_a_free_profile_with_the_code_on_screen(client, demo_no_stripe):
    phones = demo_no_stripe
    ctx = client.get("/api/signup/context").json()
    assert ctx["open"] is True and ctx["payment"] is False and ctx["price"] is None
    phone = fresh_phone()
    r = client.post("/api/signup/start", json={"name": "Lee Tran", "phone": phone})
    assert r.status_code == 200, r.text
    assert r.json()["delivered"] is False and len(r.json()["demo_code"]) == 6
    assert phones.sent == []
    r = client.post("/api/signup/verify", json={"phone": phone, "code": r.json()["demo_code"]})
    assert r.status_code == 200, r.text
    b = r.json()["billing"]
    assert b["status"] == "demo" and b["active"] is True and b["payment"] is False
    _consent(client)
    assert _check_in(client, _open_meeting()).status_code == 201
    assert client.post("/api/billing/checkout").status_code == 409


def test_price_words():
    monthly = {"unit_amount": 1200, "currency": "usd", "recurring": {"interval": "month"}}
    assert billing.price_words(monthly)["label"] == "$12 a month"
    cents = monthly | {"unit_amount": 1999}
    assert billing.price_words(cents)["label"] == "$19.99 a month"
    quarterly = monthly | {"recurring": {"interval": "month", "interval_count": 3}}
    assert billing.price_words(quarterly)["label"] == "$12 every 3 months"
    euro = {"unit_amount": 1000, "currency": "eur", "recurring": {"interval": "year"}}
    assert billing.price_words(euro)["label"] == "10.00 EUR a year"


def test_signatures_match_stripes_scheme():
    body = b'{"id":"evt_1"}'
    header = billing.sign(body, "whsec_a", at=1_700_000_000)
    assert header.startswith("t=1700000000,v1=")
    assert billing.signature_ok(body, header, "whsec_a", now=1_700_000_100)
    assert not billing.signature_ok(body, header, "whsec_b", now=1_700_000_100)
    assert not billing.signature_ok(body + b" ", header, "whsec_a", now=1_700_000_100)
    assert not billing.signature_ok(body, header, "whsec_a", now=1_700_001_000)


# --------------------------------------------------------------------------- #
# Payments switched off
# --------------------------------------------------------------------------- #

def test_payments_off_keeps_the_secrets_but_gates_nobody(client, stripe, monkeypatch):
    fake, phones = stripe
    _join(client, phones)                      # a profile made while payments were on
    _consent(client)
    meeting = _open_meeting()
    assert _check_in(client, meeting).status_code == 402

    monkeypatch.setattr(settings, "PAYMENTS", False)
    assert settings.stripe_configured is False
    b = _me(client)["billing"]
    assert b["active"] is True and b["payment"] is False and b["words"] == "No payment is needed here."
    assert _check_in(client, meeting).status_code == 201
    assert client.post("/api/billing/checkout").status_code == 409
    ctx = client.get("/api/signup/context").json()
    assert ctx["payment"] is False and ctx["open"] is False      # live, no payments: closed

    monkeypatch.setattr(settings, "DEMO", True)
    assert client.get("/api/signup/context").json()["open"] is True


def test_the_demo_account_list_never_names_a_self_serve_person(client, stripe, monkeypatch):
    fake, phones = stripe
    me = _join(client, phones, name="Real Person")
    monkeypatch.setattr(settings, "DEMO", True)
    listed = client.get("/api/auth/demo-accounts").json()
    assert all(p["name"] != "Real Person" for p in listed["participants"])
    assert me["name"] == "Real Person"


# --------------------------------------------------------------------------- #
# Meetings a superuser makes
# --------------------------------------------------------------------------- #

def test_a_meeting_a_superuser_makes_is_on_every_phone(client, demo_no_stripe):
    from datetime import timedelta
    db = SessionLocal()
    su = User(role=Role.superuser, name="Support", email="support@example.com")
    db.add(su)
    db.commit()
    su_id = su.id
    db.close()

    soon = datetime.now(timezone.utc) + timedelta(hours=1)
    r = client.post("/api/meetings", json={"name": "Thursday night group", "category": "public",
                                           "lat": 42.2750, "lng": -83.7413, "starts_at": soon.isoformat()},
                    headers=as_(su_id))
    assert r.status_code == 201, r.text
    assert r.json()["county_id"] is None
    meeting_id = r.json()["id"]

    phone = fresh_phone()
    code = client.post("/api/signup/start", json={"name": "Lee Tran", "phone": phone}).json()["demo_code"]
    assert client.post("/api/signup/verify", json={"phone": phone, "code": code}).status_code == 200
    since = (datetime.now(timezone.utc) - timedelta(hours=18)).isoformat()
    until = (datetime.now(timezone.utc) + timedelta(days=8)).isoformat()
    r = client.get("/api/meetings", params={"since": since, "until": until})
    assert r.status_code == 200, r.text
    listed = r.json()
    assert meeting_id in [m["id"] for m in listed]
    # Nothing from the shared directory leaks in just because it has no county either.
    assert all(m["source"] != "directory" for m in listed)
    _consent(client)
    assert _check_in(client, meeting_id).status_code == 201
