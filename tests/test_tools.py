"""
The command-line tools a deployment is run with: making the first real
staff account, a court's county and its staff, and deleting the demo county
before real people arrive.

Each test seeds its own throwaway database and runs the tool the way it is
run on the server, as a separate process, so nothing here can touch the
database the other tests share.
"""
import os
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog, CheckIn, County, Meeting, MeetingSeries, Participant, Program, Role, Session as SessionRow,
    TeamHours, TeamTimesheet, User,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(url, *args, ok=True):
    r = subprocess.run([sys.executable, *args], cwd=ROOT, capture_output=True, text=True,
                       env=os.environ | {"MC_DATABASE_URL": url, "MC_DEMO": "0"})
    if ok:
        assert r.returncode == 0, r.stderr[-2000:]
    return r


@pytest.fixture
def seeded_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'tool.db'}"
    run(url, "-m", "app.seed")
    return url


def test_make_staff_creates_a_superuser_with_a_one_time_password(seeded_url):
    r = run(seeded_url, "tools/make_staff.py", "--name", " Joe  Dillon ", "--email", "Joe@Example.com",
            "--role", "superuser")
    assert "created superuser Joe Dillon <joe@example.com>" in r.stdout
    first = next(line.split(": ", 1)[1] for line in r.stdout.splitlines() if line.startswith("first password"))
    assert len(first) == 19                              # MKD4-P7RT-9WQX-HF2N

    with Session(create_engine(seeded_url)) as db:
        u = db.scalar(select(User).where(User.email == "joe@example.com"))
        assert u.role == Role.superuser and u.county_id is None and u.must_change_password
        from app.security import verify_password
        assert verify_password(first, u.password_hash)

    r = run(seeded_url, "tools/make_staff.py", "--name", "Joe", "--email", "joe@example.com",
            "--role", "superuser", ok=False)
    assert r.returncode != 0 and "already an account" in r.stderr

    r = run(seeded_url, "tools/make_staff.py", "--name", "Pat", "--email", "pat@example.gov",
            "--role", "officer", ok=False)
    assert r.returncode != 0 and "--county" in r.stderr
    run(seeded_url, "tools/make_staff.py", "--name", "Pat", "--email", "pat@example.gov",
        "--role", "officer", "--county", "Washtenaw County")
    with Session(create_engine(seeded_url)) as db:
        pat = db.scalar(select(User).where(User.email == "pat@example.gov"))
        assert pat.role == Role.officer and pat.county_id == db.scalar(
            select(County.id).where(County.name == "Washtenaw County"))


def test_make_county_makes_a_court_its_programs_and_then_its_staff(seeded_url):
    taylor = "23rd District Court, Taylor"
    r = run(seeded_url, "tools/make_county.py", "--name", " 23rd  District Court, Taylor ",
            "--program", "Probation=taylor-prob", "--program", "Regional Treatment Court=TAYLOR-RTC")
    assert f"created {taylor} (MI, America/Detroit)" in r.stdout
    assert "added program Probation with join code TAYLOR-PROB" in r.stdout
    assert "added program Regional Treatment Court with join code TAYLOR-RTC" in r.stdout
    with Session(create_engine(seeded_url)) as db:
        county = db.scalar(select(County).where(County.name == taylor))
        assert county.state == "MI" and not county.self_serve
        codes = db.scalars(select(Program.join_code).where(Program.county_id == county.id)).all()
        assert sorted(codes) == ["TAYLOR-PROB", "TAYLOR-RTC"]

    # Running it again does nothing silently: no programs is a refusal, the
    # same programs is a refusal, a new one is added to the county found.
    r = run(seeded_url, "tools/make_county.py", "--name", taylor, ok=False)
    assert r.returncode != 0 and "already a county" in r.stderr
    r = run(seeded_url, "tools/make_county.py", "--name", taylor, "--program", "Probation=TAYLOR-PROB", ok=False)
    assert r.returncode != 0 and "already has every program" in r.stderr
    r = run(seeded_url, "tools/make_county.py", "--name", taylor, "--program", "Diversion=WASH-OWI", ok=False)
    assert r.returncode != 0 and "already belongs to 'OWI Probation'" in r.stderr
    r = run(seeded_url, "tools/make_county.py", "--name", taylor, "--program", "Diversion=TAYLOR-DIV")
    assert f"found {taylor}" in r.stdout and "added program Diversion" in r.stdout
    r = run(seeded_url, "tools/make_county.py", "--name", taylor, "--program", "no code", ok=False)
    assert r.returncode != 0 and "Name=JOIN-CODE" in r.stderr

    r = run(seeded_url, "tools/make_county.py", "--list")
    assert "Washtenaw County: MI, America/Detroit, 3 staff, 11 participants" in r.stdout
    assert f"{taylor}: MI, America/Detroit, 0 staff, 0 participants" in r.stdout
    assert "    Diversion  [TAYLOR-DIV]" in r.stdout
    r = run(seeded_url, "tools/make_county.py", ok=False)
    assert r.returncode != 0 and "--list" in r.stderr

    # Staff go in by the county's name, with a work number if there is one.
    r = run(seeded_url, "tools/make_staff.py", "--name", "Kelsey Havener", "--email", "KHavener@example.gov",
            "--role", "admin", "--county", taylor, "--phone", "734-555-0101")
    assert "created admin Kelsey Havener <khavener@example.gov> +17345550101" in r.stdout
    with Session(create_engine(seeded_url)) as db:
        kelsey = db.scalar(select(User).where(User.email == "khavener@example.gov"))
        assert kelsey.role == Role.admin and kelsey.county_id == county.id and kelsey.phone == "+17345550101"
    r = run(seeded_url, "tools/make_staff.py", "--name", "Tracie", "--email", "tracie@example.gov",
            "--role", "officer", "--county", taylor, "--phone", "734-374-132", ok=False)
    assert r.returncode != 0 and "ten-digit" in r.stderr
    r = run(seeded_url, "tools/make_county.py", "--list")
    assert f"{taylor}: MI, America/Detroit, 1 staff, 0 participants" in r.stdout


def test_retire_demo_deletes_the_county_and_keeps_everyone_else(seeded_url):
    engine = create_engine(seeded_url)
    with Session(engine) as db:
        # The directory (no county) and a self-serve person must survive.
        db.add(MeetingSeries(county_id=None, name="Open AA", source_name="test", external_key="k1",
                             weekday=0, start_minute=1140, lat=42.27, lng=-83.74))
        selfpay = County(name="Self-pay", self_serve=True)
        db.add(selfpay)
        db.flush()
        db.add(User(county_id=selfpay.id, role=Role.participant, name="Lee Tran", phone="+17345550199"))
        db.commit()
        before = {m.__tablename__: db.scalar(select(func.count()).select_from(m))
                  for m in (User, Participant, CheckIn, Meeting, AuditLog, SessionRow)}
        assert before["users"] > 10 and before["checkins"] > 50

    r = run(seeded_url, "tools/retire_demo.py", ok=False)
    assert r.returncode == 2                               # neither --dry-run nor --yes

    r = run(seeded_url, "tools/retire_demo.py", "--dry-run")
    assert "would delete" in r.stdout and "people" in r.stdout
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == before["users"]

    r = run(seeded_url, "tools/retire_demo.py", "--county", "Self-pay", "--yes", ok=False)
    assert r.returncode != 0 and "Refusing" in r.stderr

    r = run(seeded_url, "tools/retire_demo.py", "--yes")
    assert "Washtenaw County is gone" in r.stdout

    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(County)) == 1
        assert db.scalar(select(County.name)) == "Self-pay"
        assert [u.name for u in db.scalars(select(User))] == ["Lee Tran"]
        for model in (Participant, CheckIn, Program, AuditLog, SessionRow):
            assert db.scalar(select(func.count()).select_from(model)) == 0, model.__tablename__
        assert db.scalar(select(func.count()).select_from(Meeting)) == 0
        assert db.scalar(select(func.count()).select_from(MeetingSeries)) == 1

    r = run(seeded_url, "tools/retire_demo.py", "--yes", ok=False)
    assert r.returncode != 0 and "no county called" in r.stderr


def test_import_hours_fills_an_empty_log_and_the_balance_comes_out_as_the_sheet_had_it(seeded_url, tmp_path):
    run(seeded_url, "tools/make_staff.py", "--name", "Joe Dillon", "--email", "joe@example.com", "--role", "superuser")
    run(seeded_url, "tools/make_staff.py", "--name", "Spencer", "--email", "spencer@example.com", "--role", "superuser")
    sheet = tmp_path / "hours.csv"
    sheet.write_text(
        "Date,Work,Hours,Cash ($/hour),Cash earned ($),Billed on\n"
        "6/16/2026,Meeting,1,$17.50,$17.50,Invoice 1\n"
        ",Code,3.5,$17.50,$61.25,Invoice 1\n"          # no date: goes with the row above
        "10/12/2026,Twilio,5.5,$17.50,$96.25,Invoice 2\n"
        "10/22/2026,,4,$17.50,$70.00,\n"
        ",,,,,\n")
    args = ["tools/import_hours.py", "--email", "Joe@Example.com", "--csv", str(sheet),
            "--rate", "2026-06-16:17.50+12.50@500000", "--rate", "2026-10-01:20+15@500000",
            "--paid", "Invoice 1=100", "--paid", "Invoice 2=50.25", "--by", "spencer@example.com"]

    dry = run(seeded_url, *args, "--dry-run")
    assert "would write 4 entries (14 hours)" in dry.stdout
    with Session(create_engine(seeded_url)) as db:
        assert db.scalar(select(func.count()).select_from(TeamHours)) == 0

    done = run(seeded_url, *args)
    assert "wrote 4 entries" in done.stdout and "Invoice 2: 1 entries, 5.5 hours, 110.00 at the rate, paid 50.25" in done.stdout
    with Session(create_engine(seeded_url)) as db:
        joe = db.scalar(select(User).where(User.email == "joe@example.com"))
        hours = db.scalars(select(TeamHours).where(TeamHours.user_id == joe.id).order_by(TeamHours.id)).all()
        assert [h.minutes for h in hours] == [60, 210, 330, 240]
        assert hours[1].worked_on == hours[0].worked_on and "date not recorded" in hours[1].note
        assert hours[3].note == "(no description)" and hours[3].timesheet_id is None
        sheets = db.scalars(select(TeamTimesheet).order_by(TeamTimesheet.id)).all()
        assert [(s.status.value, s.paid_cents, s.amount_cents) for s in sheets] == [
            ("paid", 10000, 4 * 1750 + round(0.5 * 1750)),      # 4.5 h at $17.50
            ("paid", 5025, round(5.5 * 2000))]                  # 5.5 h at $20
        from app.routers.team import balance
        b = balance(db, joe)
        assert b["logged_minutes"] == 840 and b["paid_cents"] == 15025
        assert b["cash_earned_cents"] == 7875 + 11000 + 8000          # the two sheets plus 4 h unsubmitted at $20
        assert b["owed_cents"] == b["cash_earned_cents"] - 15025
        assert b["equity_cents"] == round(4.5 * 1250) + round(5.5 * 1500) + 4 * 1500
        assert abs(b["equity_share"] - b["equity_cents"] / 500_000_00) < 1e-12

    again = run(seeded_url, *args, ok=False)
    assert again.returncode != 0 and "already has hours" in again.stderr
