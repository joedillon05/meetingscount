"""
The command-line tools a deployment is run with: making the first real
staff account, and deleting the demo county before real people arrive.

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
    User,
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
