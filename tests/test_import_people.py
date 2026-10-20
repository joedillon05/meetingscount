"""
Bringing people over from the old site from a list, so they only sign in.
"""
import os
import subprocess
import sys

from sqlalchemy import create_engine, text

from app import billing, profiles
from app.db import SessionLocal
from app.models import Participant, Requirement, User

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROWS = [
    {"First Name": "Avery", "Last Name": "Stone", "Cell Phone": "(734) 555-0191",
     "Email": "avery@example.com", "Meetings Per Week": "4", "Start Date": "9/1/2026"},
    {"First Name": "Blake", "Last Name": "Rivera", "Cell Phone": "734.555.0192",
     "Email": "", "Meetings Per Week": "", "Start Date": ""},
    {"First Name": "Casey", "Last Name": "", "Cell Phone": "12", "Email": "",
     "Meetings Per Week": "", "Start Date": ""},
    {"First Name": "Dup", "Last Name": "Row", "Cell Phone": "7345550192", "Email": "",
     "Meetings Per Week": "", "Start Date": ""},
]


def test_a_list_becomes_profiles_once(client):
    db = SessionLocal()
    try:
        report = profiles.import_people(db, ROWS, covered=True)
        db.commit()
        assert [x.split(",")[0] for x in report.created] == ["Avery Stone", "Blake Rivera"]
        assert any("no usable mobile number" in x for x in report.skipped)
        assert any("same number as an earlier row" in x for x in report.skipped)

        avery = db.query(User).filter(User.phone == "+17345550191").one()
        p = db.query(Participant).filter(Participant.user_id == avery.id).one()
        req = db.query(Requirement).filter(Requirement.participant_id == p.id).one()
        assert req.count_required == 4 and req.starts_on.month == 9
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
