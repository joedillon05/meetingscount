"""Signing in: passwords, one-time codes, sessions, lockout, MFA."""
from datetime import datetime, timedelta, timezone

import pytest

from app import security
from app.config import settings
from app.db import SessionLocal
from app.models import LoginCode, Session as SessionRow, User
from app.routers.auth import DEMO_PASSWORD

from .conftest import as_

GOOD = "correct-horse-battery"          # long enough for the policy


@pytest.fixture
def real_auth(monkeypatch):
    """No dev-auth header shortcut, the way any deployment runs."""
    monkeypatch.setattr(settings, "DEV_AUTH", False)
    yield


@pytest.fixture(autouse=True)
def fresh_limits():
    security._HITS.clear()
    yield
    security._HITS.clear()


def _user(email=None, phone=None, name=None):
    db = SessionLocal()
    try:
        q = db.query(User)
        if email:
            return q.filter(User.email == email).one()
        if phone:
            return q.filter(User.phone == phone).one()
        return q.filter(User.name == name).one()
    finally:
        db.close()


def _edit(user_id, **fields):
    db = SessionLocal()
    try:
        u = db.get(User, user_id)
        for k, v in fields.items():
            setattr(u, k, v)
        db.commit()
    finally:
        db.close()


def _sign_in(client, email="chen@example.gov", password=DEMO_PASSWORD, **extra):
    return client.post("/api/auth/login", json={"email": email, "password": password, **extra})


def _sign_out(client):
    client.post("/api/auth/logout")
    client.cookies.clear()


# --------------------------------------------------------------------------- #
# Staff
# --------------------------------------------------------------------------- #

def test_without_a_session_nothing_is_readable(client, real_auth):
    _sign_out(client)
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/caseload").status_code == 401
    assert client.get("/api/reviews/queue").status_code == 401


def test_the_dev_header_does_not_work_once_dev_auth_is_off(client, ids, real_auth):
    _sign_out(client)
    assert client.get("/api/caseload", headers=as_(ids["officer"])).status_code == 401


def test_staff_sign_in_and_out(client, real_auth):
    _sign_out(client)
    r = _sign_in(client)
    assert r.status_code == 200 and r.json()["role"] == "officer"
    assert client.get("/api/caseload").status_code == 200
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.get("/api/caseload").status_code == 401


def test_wrong_password_and_unknown_email_look_the_same(client, real_auth):
    _sign_out(client)
    bad = _sign_in(client, password="not-the-password")
    missing = _sign_in(client, email="nobody@example.gov", password="not-the-password")
    assert bad.status_code == missing.status_code == 401
    assert bad.json()["detail"] == missing.json()["detail"]


def test_repeated_failures_lock_the_account_briefly(client, real_auth):
    _sign_out(client)
    who = _user(email="ramirez@example.gov")
    for _ in range(5):
        _sign_in(client, email=who.email, password="wrong-wrong-wrong")
    locked = _sign_in(client, email=who.email)           # right password, still locked
    assert locked.status_code == 423
    _edit(who.id, locked_until=None, failed_logins=0)
    assert _sign_in(client, email=who.email).status_code == 200


def test_too_many_attempts_from_one_address_are_refused(client, real_auth):
    _sign_out(client)
    codes = {_sign_in(client, email=f"someone{i}@example.gov", password="x" * 14).status_code
             for i in range(12)}
    assert 429 in codes


def test_every_sign_in_and_failure_is_written_to_the_audit_log(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client, password="wrong-wrong-wrong")
    _sign_in(client, email="admin@example.gov")
    rows = client.get("/api/audit?limit=50").json()
    actions = [r["action"] for r in rows]
    assert "sign_in" in actions and "sign_in_failed" in actions
    _sign_out(client)


# --------------------------------------------------------------------------- #
# Authenticator app
# --------------------------------------------------------------------------- #

def test_authenticator_app_setup_and_then_required_at_sign_in(client, real_auth):
    _sign_out(client)
    _sign_in(client, email="admin@example.gov")
    start = client.post("/api/auth/totp/start").json()
    assert len(start["secret"]) >= 16 and start["uri"].startswith("otpauth://totp/")
    assert client.post("/api/auth/totp/enable", json={"code": "000000"}).status_code == 401
    code = security.totp_code(start["secret"])
    assert client.post("/api/auth/totp/enable", json={"code": code}).status_code == 200
    _sign_out(client)

    asked = _sign_in(client, email="admin@example.gov")
    assert asked.json() == {"mfa_required": True}
    assert client.get("/api/caseload").status_code == 401, "no session until the code is given"

    wrong = _sign_in(client, email="admin@example.gov", code="123456")
    assert wrong.status_code == 401
    ok = _sign_in(client, email="admin@example.gov", code=security.totp_code(start["secret"]))
    assert ok.status_code == 200

    client.post("/api/auth/totp/disable", json={"current_password": DEMO_PASSWORD})
    _sign_out(client)


def test_totp_accepts_the_previous_step_for_a_slow_typist():
    secret = security.new_totp_secret()
    now = 1_800_000_000
    assert security.totp_verify(secret, security.totp_code(secret, now - 30), at=now)
    assert not security.totp_verify(secret, security.totp_code(secret, now - 300), at=now)


# --------------------------------------------------------------------------- #
# Participants: phone and a one-time code
# --------------------------------------------------------------------------- #

def test_officer_issues_a_code_and_the_participant_signs_in_with_it(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client)                                     # Officer Chen
    case = client.get("/api/caseload").json()
    row = next(c for c in case if c["officer"] == "Officer Chen")
    issued = client.post(f"/api/auth/participants/{row['participant_id']}/access-code")
    assert issued.status_code == 200
    code, phone = issued.json()["code"], issued.json()["phone"]
    assert len(code) == 6 and code.isdigit()
    _sign_out(client)

    assert client.post("/api/auth/code/verify", json={"phone": phone, "code": "000000"}).status_code == 401
    ok = client.post("/api/auth/code/verify", json={"phone": phone, "code": code})
    assert ok.status_code == 200 and ok.json()["role"] == "participant"
    assert client.get("/api/my/progress").status_code == 200
    assert client.get("/api/caseload").status_code == 403, "a participant is still a participant"
    _sign_out(client)

    # Single use.
    assert client.post("/api/auth/code/verify", json={"phone": phone, "code": code}).status_code == 401


def test_a_code_works_however_the_phone_number_is_typed(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client)
    case = client.get("/api/caseload").json()
    row = next(c for c in case if c["officer"] == "Officer Chen")
    code = client.post(f"/api/auth/participants/{row['participant_id']}/access-code").json()["code"]
    _sign_out(client)
    messy = "(734) 555-0" + _user(name=row["name"]).phone[-3:]
    r = client.post("/api/auth/code/verify", json={"phone": messy, "code": code})
    assert r.status_code == 200
    _sign_out(client)


def test_an_expired_code_is_refused(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client)
    case = client.get("/api/caseload").json()
    row = next(c for c in case if c["officer"] == "Officer Chen")
    issued = client.post(f"/api/auth/participants/{row['participant_id']}/access-code").json()
    db = SessionLocal()
    try:
        latest = db.query(LoginCode).order_by(LoginCode.id.desc()).first()
        latest.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    _sign_out(client)
    r = client.post("/api/auth/code/verify", json={"phone": issued["phone"], "code": issued["code"]})
    assert r.status_code == 401


def test_asking_for_a_code_never_reveals_whether_the_number_is_known(client, real_auth):
    _sign_out(client)
    known = client.post("/api/auth/code/request", json={"phone": _user(name="Riley Chen").phone})
    unknown = client.post("/api/auth/code/request", json={"phone": "+15550000000"})
    assert known.status_code == unknown.status_code == 200
    strip = lambda d: {k: v for k, v in d.items() if k != "demo_code"}    # noqa: E731
    assert strip(known.json()) == strip(unknown.json())


def test_guessing_codes_is_rate_limited(client, real_auth):
    _sign_out(client)
    phone = _user(name="Riley Chen").phone
    codes = {client.post("/api/auth/code/verify", json={"phone": phone, "code": "000000"}).status_code
             for _ in range(12)}
    assert 429 in codes


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #

def test_a_staff_session_ends_after_being_left_idle(client, real_auth):
    _sign_out(client)
    _sign_in(client)
    assert client.get("/api/me").status_code == 200
    db = SessionLocal()
    try:
        row = db.query(SessionRow).filter(SessionRow.revoked_at.is_(None)) \
                .order_by(SessionRow.id.desc()).first()
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=45)
        db.commit()
    finally:
        db.close()
    assert client.get("/api/me").status_code == 401
    client.cookies.clear()


def test_a_participant_phone_stays_signed_in_far_longer_than_staff(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client)
    case = client.get("/api/caseload").json()
    row = next(c for c in case if c["officer"] == "Officer Chen")
    issued = client.post(f"/api/auth/participants/{row['participant_id']}/access-code").json()
    _sign_out(client)
    client.post("/api/auth/code/verify", json={"phone": issued["phone"], "code": issued["code"]})
    db = SessionLocal()
    try:
        row = db.query(SessionRow).order_by(SessionRow.id.desc()).first()
        life = row.expires_at - row.created_at
    finally:
        db.close()
    assert life > timedelta(days=20)
    _sign_out(client)


def test_changing_a_password_signs_out_other_devices(client, real_auth):
    _sign_out(client)
    _sign_in(client, email="ramirez@example.gov")
    me = _user(email="ramirez@example.gov")
    before = _open_sessions(me.id)

    short = client.post("/api/auth/password", json={"current_password": DEMO_PASSWORD,
                                                    "new_password": "short"})
    assert short.status_code == 422
    wrong = client.post("/api/auth/password", json={"current_password": "nope",
                                                    "new_password": GOOD})
    assert wrong.status_code == 401
    ok = client.post("/api/auth/password", json={"current_password": DEMO_PASSWORD,
                                                 "new_password": GOOD})
    assert ok.status_code == 200
    assert _open_sessions(me.id) == 1, "every other device is signed out"
    assert before >= 1
    assert client.get("/api/me").status_code == 200, "this browser stays signed in"

    _sign_out(client)
    assert _sign_in(client, email="ramirez@example.gov", password=GOOD).status_code == 200
    client.post("/api/auth/password", json={"current_password": GOOD, "new_password": DEMO_PASSWORD})
    _sign_out(client)


def _open_sessions(user_id):
    db = SessionLocal()
    try:
        return db.query(SessionRow).filter(SessionRow.user_id == user_id,
                                           SessionRow.revoked_at.is_(None)).count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# The pieces underneath
# --------------------------------------------------------------------------- #

def test_password_hashes_are_salted_and_verify():
    a, b = security.hash_password(GOOD), security.hash_password(GOOD)
    assert a != b, "same password, different hash"
    assert a.startswith("scrypt$") and GOOD not in a
    assert security.verify_password(GOOD, a)
    assert not security.verify_password("wrong-wrong-wrong", a)
    assert not security.verify_password(GOOD, None)


def test_session_tokens_are_stored_only_as_hashes():
    token, hashed = security.new_session_token()
    assert len(token) > 30 and token != hashed and len(hashed) == 64


def test_phone_numbers_normalize_to_one_shape():
    for raw in ["734-555-0101", "(734) 555-0101", "+1 734 555 0101", "17345550101"]:
        assert security.normalize_phone(raw) == "+17345550101"
    assert security.normalize_phone("") is None


def test_password_policy_rejects_the_obvious():
    assert security.password_problem("short")
    assert security.password_problem("password1234")
    assert security.password_problem("me@example.gov", email="me@example.gov")
    assert security.password_problem(GOOD) is None


# --------------------------------------------------------------------------- #
# Staff accounts
# --------------------------------------------------------------------------- #

def test_an_admin_adds_an_officer_who_must_pick_their_own_password(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client, email="admin@example.gov")
    made = client.post("/api/staff", json={"name": "Officer Vance", "email": "vance@example.gov",
                                           "role": "officer"})
    assert made.status_code == 201
    first = made.json()["first_password"]
    assert len(first) >= 12 and first.count("-") == 3
    _sign_out(client)

    signed = _sign_in(client, email="vance@example.gov", password=first)
    assert signed.status_code == 200 and signed.json()["must_change_password"] is True
    blocked = client.get("/api/caseload")
    assert blocked.status_code == 403 and "own password" in blocked.json()["detail"]

    client.post("/api/auth/password", json={"current_password": first, "new_password": GOOD})
    assert client.get("/api/caseload").status_code == 200
    _sign_out(client)


def test_two_staff_cannot_share_an_email(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client, email="admin@example.gov")
    again = client.post("/api/staff", json={"name": "Someone Else", "email": "chen@example.gov"})
    assert again.status_code == 409
    _sign_out(client)


def test_resetting_a_password_signs_that_person_out(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client, email="admin@example.gov")
    staff = client.get("/api/staff").json()
    chen = next(s for s in staff if s["email"] == "chen@example.gov")
    r = client.post(f"/api/staff/{chen['id']}/reset-password")
    assert r.status_code == 200 and r.json()["first_password"]
    assert _open_sessions(chen["id"]) == 0
    _sign_out(client)

    # And the old password no longer works.
    assert _sign_in(client).status_code == 401
    # Put the demo account back the way the seed left it.
    db = SessionLocal()
    try:
        from app.security import hash_password
        u = db.get(User, chen["id"])
        u.password_hash = hash_password(DEMO_PASSWORD)
        u.must_change_password = False
        u.failed_logins = 0
        u.locked_until = None
        db.commit()
    finally:
        db.close()


def test_an_admin_cannot_switch_off_their_own_account(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client, email="admin@example.gov")
    me = client.get("/api/me").json()["id"]
    r = client.patch(f"/api/staff/{me}", json={"active": False})
    assert r.status_code == 409
    _sign_out(client)


def test_officers_cannot_manage_staff(client, ids, real_auth):
    _sign_out(client)
    _sign_in(client)                                   # an officer
    assert client.get("/api/staff").status_code == 403
    assert client.post("/api/staff", json={"name": "No", "email": "no@example.gov"}).status_code == 403
    _sign_out(client)
