"""
Moving people from another Stripe account: the read-only report of what the
old account holds, and the mover that starts each subscription here the day
the old paid period ends. Two fakes stand in for the two accounts.
"""
import os
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))          # the tools import each other by name

import stripe_move_subscriptions as mover    # noqa: E402
import stripe_old_account as report_tool     # noqa: E402
from stripe_common import Prices             # noqa: E402

from .test_signup import FakeStripe

ROWS = [
    {"First Name": "Amber", "Last Name": "Griffin", "Cell Phone": "248-555-0101",
     "Email": "Amber.G@Example.com"},                        # the old account has it lowercased
    {"First Name": "Eli", "Last Name": "ByPhone", "Cell Phone": "248-555-0102", "Email": ""},
    {"First Name": "Fay", "Last Name": "Lapsed", "Cell Phone": "248-555-0103", "Email": "fay@example.com"},
    {"First Name": "Gus", "Last Name": "Unknown", "Cell Phone": "248-555-0104", "Email": "gus@example.com"},
    {"First Name": "Hal", "Last Name": "ByName", "Cell Phone": "248-555-0105",
     "Email": "hal.new@example.com"},                        # a different email in the old account
]


@pytest.fixture
def accounts():
    old, new = FakeStripe("Meetings Count LLC"), FakeStripe("Actions Count Plus LLC")
    old.prices["price_old"], new.prices["price_new"] = 1500, 1000
    ids = {
        "amber": old.old_site_customer(email="amber.g@example.com", name="Amber Griffin",
                                       card=("visa", "4242", 12, 2028), days=12),
        "eli": old.old_site_customer(phone="(248) 555-0102", name="Eli ByPhone", status="trialing",
                                     card=("mastercard", "4444", 1, 2029), days=5),
        "fay": old.old_site_customer(email="fay@example.com", name="Fay Lapsed", status="canceled",
                                     card=("visa", "1111", 3, 2027)),
        "hal": old.old_site_customer(email="hal.old@example.com", name="Hal ByName",
                                     card=("amex", "0005", 6, 2030), days=20),
        "ivy": old.old_site_customer(email="ivy@example.com", name="Ivy NotListed", days=8),
    }
    return old, new, ids


def posts(fake):
    return [c for c in fake.calls if c[0] == "POST"]


def test_the_report_finds_people_three_ways_and_says_who_is_missing(accounts):
    old, new, ids = accounts
    rep = report_tool.look(ROWS, old)
    assert rep.account.startswith("acct_") and "Meetings Count LLC" in rep.account
    assert {f.person.name: f.how for f in rep.found} == {
        "Amber Griffin": "email", "Eli ByPhone": "phone", "Fay Lapsed": "email", "Hal ByName": "name"}
    assert [p.name for p in rep.missing] == ["Gus Unknown"]
    assert rep.to_copy == [ids["amber"], ids["eli"], ids["hal"]], "canceled Fay is not worth copying"
    assert len(rep.extra) == 1 and rep.extra[0].startswith("Ivy NotListed, iv***@example.com, active")

    text = report_tool.summary(rep, Prices(old))
    assert f"Amber Griffin: {ids['amber']}  active, $15 a month, paid through" in text
    assert "visa ····4242 exp 12/28" in text
    assert "Hal ByName" in text and "matched by NAME only" in text
    assert "Fay Lapsed" in text and "not live, will not move" in text
    assert not posts(old), "the report reads only"


def test_the_plan_changes_nothing_and_says_why_each_person_waits(accounts):
    old, new, ids = accounts
    old.copy_customers_to(new, [ids["amber"], ids["eli"], ids["ivy"]])
    new.post("/v1/subscriptions", {"customer": ids["eli"], "items[0][price]": "price_new"})   # Eli started on his own
    before = len(posts(old)), len(posts(new))
    rows = ROWS + [{"First Name": "Ivy", "Last Name": "NotListed", "Cell Phone": "248-555-0106",
                    "Email": "ivy@example.com"}]
    moves = mover.plan(rows, old, new)
    by = {m.person.name: m for m in moves}
    amber = by["Amber Griffin"]
    assert amber.skip is None and amber.card["kind"] == "payment_method"
    assert amber.card["id"] in new.cards and amber.card["id"] not in old.cards, "the copied card, by its new id"
    assert amber.card["words"] == "visa ····4242 exp 12/28"
    assert by["Eli ByPhone"].skip == "already has a active subscription on the new account"
    assert by["Fay Lapsed"].skip == "old subscription is canceled, not live"
    assert by["Gus Unknown"].skip == "not in the old account"
    assert by["Hal ByName"].skip == "not in the old account", "the mover never guesses by name"
    assert by["Ivy NotListed"].skip == "no card on the new account: has the copy finished?"
    assert (len(posts(old)), len(posts(new))) == before, "a plan writes nothing"

    text = mover.summary(moves, Prices(old), "$10 a month", yes=False)
    assert "Amber Griffin: free until" in text and "then $10 a month (was $15 a month)" in text
    assert "Nothing done" in text and "left alone (5)" in text


def test_moving_starts_here_the_day_the_old_period_ends_and_only_once(accounts):
    old, new, ids = accounts
    old.copy_customers_to(new, [ids["amber"]])
    moves = mover.plan(ROWS, old, new)
    amber = next(m for m in moves if m.person.name == "Amber Griffin")
    mover.carry_out(amber, old, new, "price_new")

    old_sub = next(s for s in old.subscriptions.values() if s["customer"] == ids["amber"])
    new_sub = next(s for s in new.subscriptions.values() if s["customer"] == ids["amber"])
    assert new_sub["status"] == "trialing"
    assert new_sub["trial_end"] == old_sub["items"]["data"][0]["current_period_end"]
    assert new_sub["items"]["data"][0]["price"]["id"] == "price_new"
    assert new_sub["metadata"] == {"moved_from": old_sub["id"]}
    assert old_sub["cancel_at_period_end"] is True, "the old one stops when its paid period ends"
    assert new.customers[ids["amber"]]["invoice_settings"]["default_payment_method"] == amber.card["id"]
    assert amber.done.startswith(f"{new_sub['id']} trialing, first charge ")
    assert "Amber Griffin: " + amber.done in mover.summary(moves, Prices(old), "$10 a month", yes=True)

    again = next(m for m in mover.plan(ROWS, old, new) if m.person.name == "Amber Griffin")
    assert again.skip == "already has a trialing subscription on the new account"
    assert sum(s["customer"] == ids["amber"] for s in new.subscriptions.values()) == 1


def test_an_old_period_ending_too_soon_is_left_for_a_person(accounts):
    old, new, ids = accounts
    old.copy_customers_to(new, [ids["amber"]])
    sub = next(s for s in old.subscriptions.values() if s["customer"] == ids["amber"])
    sub["items"]["data"][0]["current_period_end"] = int(time.time()) + 60
    amber = next(m for m in mover.plan(ROWS, old, new) if m.person.name == "Amber Griffin")
    assert amber.skip and "too soon or already past" in amber.skip
