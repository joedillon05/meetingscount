"""
Bringing people over from the old site from a list, so they only sign in.
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, select, text

from app import billing, profiles
from app.config import settings
from app.db import SessionLocal
from app.models import Participant, Requirement, User

from .test_signup import FakeStripe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROWS = [
    {"First Name": "Avery", "Last Name": "Stone", "Cell Phone": "(734) 555-0191",
     "Email": "avery@example.com", "Meetings Per Week": "4", "Start Date": "9/1/2026",
     "Probation End Date": "3/1/2027"},
    {"First Name": "blake", "Last Name": "rivera", "Cell Phone": "734.555.0192",
     "Email": "", "Meetings Per Week": "", "Start Date": "", "Probation End Date": ""},
    {"First Name": "Casey", "Last Name": "", "Cell Phone": "12", "Email": "",
     "Meetings Per Week": "", "Start Date": "", "Probation End Date": ""},
    {"First Name": "Dup", "Last Name": "Row", "Cell Phone": "7345550192", "Email": "",
     "Meetings Per Week": "", "Start Date": "", "Probation End Date": ""},
]


def test_a_list_becomes_profiles_once(client):
    db = SessionLocal()
    try:
        report = profiles.import_people(db, ROWS, covered=True)
        db.commit()
        assert [x.split(",")[0] for x in report.created] == ["Avery Stone", "Blake Rivera"], \
            "a name typed in lowercase gets its capitals"
        assert any("no usable mobile number" in x for x in report.skipped)
        assert any("same number as an earlier row" in x for x in report.skipped)

        avery = db.query(User).filter(User.phone == "+17345550191").one()
        p = db.query(Participant).filter(Participant.user_id == avery.id).one()
        req = db.query(Requirement).filter(Requirement.participant_id == p.id).one()
        assert req.count_required == 4 and req.starts_on.month == 9
        assert req.ends_on.year == 2027 and p.ends_on.month == 3
        assert req.order_reference == "moved from the old site"
        assert billing.row_for(db, avery).status == "covered" and billing.row_for(db, avery).active

        blake = db.query(User).filter(User.phone == "+17345550192").one()
        assert db.query(Requirement).join(Participant).filter(
            Participant.user_id == blake.id).one().count_required == profiles.DEFAULT_PER_WEEK

        again = profiles.import_people(db, ROWS)
        assert again.created == [] and len(again.already) == 2, "running it twice changes nothing"
    finally:
        db.close()

    # They sign in like anyone else: ask for a code with the number.
    r = client.post("/api/auth/code/request", json={"phone": "734-555-0191"})
    assert r.status_code == 200 and "demo_code" in r.json()


def test_a_list_without_a_phone_column_is_refused():
    db = SessionLocal()
    try:
        try:
            profiles.import_people(db, [{"Name": "Avery Stone", "Notes": "x"}])
            raise AssertionError("should have refused")
        except ValueError as e:
            assert "phone column" in str(e)
    finally:
        db.rollback()
        db.close()


def test_the_command_saves_nothing_without_yes(tmp_path):
    url = f"sqlite:///{tmp_path / 'people.db'}"
    csv = tmp_path / "people.csv"
    csv.write_text("Name,Mobile\nAvery Stone,734-555-0191\nBlake Rivera,734-555-0192\n")
    env = os.environ | {"MC_DATABASE_URL": url, "MC_DEMO": "1"}
    run = lambda *a: subprocess.run([sys.executable, "tools/import_people.py", str(csv), *a],  # noqa: E731
                                    cwd=ROOT, env=env, capture_output=True, text=True)
    count = lambda: create_engine(url).connect().execute(  # noqa: E731
        text("select count(*) from users where role = 'participant'")).scalar()

    r = run()
    assert r.returncode == 0, r.stderr
    assert "2 new" in r.stdout and "Nothing saved" in r.stdout and count() == 0
    r = run("--yes")
    assert r.returncode == 0, r.stderr
    assert "2 new" in r.stdout and count() == 2
    assert "0 new, 2 already here" in run("--yes").stdout

    r = run("--stripe")
    assert r.returncode == 1 and "Stripe is not configured" in r.stderr


# --------------------------------------------------------------------------- #
# People who already pay through Stripe, from the old site
# --------------------------------------------------------------------------- #

STRIPE_ROWS = [
    {"First Name": "Dana", "Last Name": "Pays", "Cell Phone": "734-555-0193",
     "Email": "Dana.Pays@Example.com"},                     # Stripe has it lowercased
    {"First Name": "Eli", "Last Name": "ByPhone", "Cell Phone": "734-555-0194", "Email": ""},
    {"First Name": "Fay", "Last Name": "Lapsed", "Cell Phone": "734-555-0195",
     "Email": "fay@example.com"},
    {"First Name": "Gus", "Last Name": "Unknown", "Cell Phone": "734-555-0196",
     "Email": "gus@example.com"},
    {"First Name": "Dana", "Last Name": "Twin", "Cell Phone": "734-555-0198",
     "Email": "dana.pays@example.com"},                     # a second person on Dana's email
]


@pytest.fixture
def stripe(monkeypatch):
    fake = FakeStripe()
    monkeypatch.setattr(settings, "STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(settings, "STRIPE_PRICE_ID", "price_test")
    monkeypatch.setattr(settings, "PAYMENTS", True)
    monkeypatch.setattr(billing, "client", fake)
    yield fake
    monkeypatch.setattr(billing, "client", None)


def by_phone(db, number):
    return db.scalar(select(User).where(User.phone == number))


def test_people_who_already_pay_on_stripe_keep_their_subscription(stripe):
    dana = stripe.old_site_customer(email="dana.pays@example.com", status="active", days=12)
    stripe.old_site_customer(phone="(734) 555-0194", status="trialing")
    stripe.old_site_customer(email="fay@example.com", status="canceled")
    db = SessionLocal()
    try:
        report = profiles.import_people(db, STRIPE_ROWS, stripe=True)
        db.commit()
        assert [x.split(",")[0] for x in report.created] == ["Dana Pays", "Eli ByPhone", "Fay Lapsed"]
        assert any("Gus Unknown" in x and "no subscription on Stripe" in x for x in report.skipped)
        assert any("Dana Twin" in x and "already belongs to Dana Pays" in x for x in report.skipped)
        assert by_phone(db, "+17345550196") is None, "not made until Stripe knows them"
        assert by_phone(db, "+17345550198") is None

        row = billing.row_for(db, by_phone(db, "+17345550193"))
        assert row.stripe_customer_id == dana and row.status == "active" and row.active
        assert row.stripe_subscription_id and row.price_id == "price_old" and row.current_period_end
        assert billing.status_out(db, by_phone(db, "+17345550193"))["can_manage"]

        eli = billing.row_for(db, by_phone(db, "+17345550194"))
        assert eli.status == "trialing" and eli.stripe_customer_id
        fay = billing.row_for(db, by_phone(db, "+17345550195"))
        assert fay.status == "canceled" and not fay.active, "Stripe's word, not the sheet's"
        assert not [c for c in stripe.calls if c[0] == "POST"], "nothing is written to Stripe"

        # The same list again, with --covered: Gus comes in with no payment asked,
        # nobody is linked twice, and Dana's twin is still refused.
        again = profiles.import_people(db, STRIPE_ROWS, stripe=True, covered=True)
        db.commit()
        assert [x.split(",")[0] for x in again.created] == ["Gus Unknown"]
        assert len(again.already) == 3 and not any("now linked" in x for x in again.already)
        gus = billing.row_for(db, by_phone(db, "+17345550196"))
        assert gus.status == "covered" and gus.stripe_customer_id is None
    finally:
        db.close()


def test_an_account_made_earlier_is_linked_on_the_next_run(stripe):
    # Hal came over before Stripe linking existed: covered, with no customer on the row.
    rows = [{"Name": "Hal Early", "Mobile": "734-555-0197", "Email": "hal@example.com"}]
    db = SessionLocal()
    try:
        profiles.import_people(db, rows, covered=True)
        db.commit()
        hal = by_phone(db, "+17345550197")
        assert billing.row_for(db, hal).stripe_customer_id is None

        cid = stripe.old_site_customer(email="hal@example.com", status="past_due")
        report = profiles.import_people(db, rows, stripe=True)
        db.commit()
        assert report.created == [] and "now linked: Stripe past_due" in report.already[0]
        row = billing.row_for(db, hal)
        assert row.stripe_customer_id == cid and row.status == "past_due" and row.active

        again = profiles.import_people(db, rows, stripe=True)
        assert "now linked" not in again.already[0], "once linked, left alone"
    finally:
        db.close()
