"""Enrolling someone, amending an order, closing a case."""
from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import Session as SessionRow, User

from .conftest import as_

NOW = datetime.now(timezone.utc)
ANY_PHONE = iter(f"+1734555{n:04d}" for n in range(9000, 9999))


def _add(client, officer, **over):
    body = {"name": "Alex Rivera", "phone": next(ANY_PHONE), "case_number": "26-9001-FH"} | over
    return client.post("/api/participants", json=body, headers=as_(officer))


def test_enrolling_someone_gives_them_a_code_that_signs_them_in(client, ids):
    r = _add(client, ids["officer"], name="Nina Alvarez")
    assert r.status_code == 201
    made = r.json()
    assert made["sign_in_code"].isdigit() and len(made["sign_in_code"]) == 6

    client.cookies.clear()
    signed = client.post("/api/auth/code/verify",
                         json={"phone": made["phone"], "code": made["sign_in_code"]})
    assert signed.status_code == 200 and signed.json()["participant_id"] == made["participant_id"]
    client.cookies.clear()


def test_a_participant_without_a_phone_number_cannot_be_enrolled(client, ids):
    r = client.post("/api/participants", json={"name": "No Phone"}, headers=as_(ids["officer"]))
    assert r.status_code == 422
    assert "sign in" in r.json()["detail"]


def test_two_participants_cannot_share_a_phone_number(client, ids):
    """Otherwise a code would sign in the wrong person."""
    first = _add(client, ids["officer"], name="First Person").json()
    again = _add(client, ids["officer"], name="Second Person", phone=first["phone"])
    assert again.status_code == 409
    assert "First Person" in again.json()["detail"]

    # The same guard when a number is edited onto someone else.
    other = _add(client, ids["officer"], name="Third Person").json()
    moved = client.patch(f"/api/participants/{other['participant_id']}",
                         json={"phone": first["phone"]}, headers=as_(ids["officer"]))
    assert moved.status_code == 409


def test_phone_numbers_are_stored_in_one_shape(client, ids):
    made = _add(client, ids["officer"], name="Messy Number", phone="(734) 555-8123").json()
    assert made["phone"] == "+17345558123"


def test_handing_a_case_to_another_officer(client, ids):
    made = _add(client, ids["officer"], name="Moving Case").json()
    officers = client.get("/api/officers", headers=as_(ids["officer"])).json()
    other = next(o for o in officers if o["id"] != ids["officer"])
    r = client.patch(f"/api/participants/{made['participant_id']}",
                     json={"officer_id": other["id"], "case_number": "26-0002-FH"},
                     headers=as_(ids["officer"]))
    assert r.status_code == 200 and set(r.json()["changed"]) == {"officer_id", "case_number"}
    case = client.get("/api/caseload", headers=as_(ids["officer"])).json()
    row = next(c for c in case if c["participant_id"] == made["participant_id"])
    assert row["officer"] == other["name"] and row["case_number"] == "26-0002-FH"


def test_closing_a_case_signs_that_person_out(client, ids):
    made = _add(client, ids["officer"], name="Case Closing").json()
    client.cookies.clear()
    client.post("/api/auth/code/verify", json={"phone": made["phone"], "code": made["sign_in_code"]})
    assert client.get("/api/my/progress").status_code == 200

    db = SessionLocal()
    try:
        open_before = db.query(SessionRow).filter(SessionRow.user_id == made["user_id"],
                                                  SessionRow.revoked_at.is_(None)).count()
    finally:
        db.close()
    assert open_before == 1

    client.cookies.clear()
    closed = client.patch(f"/api/participants/{made['participant_id']}",
                          json={"active": False, "ends_on": NOW.isoformat()},
                          headers=as_(ids["officer"]))
    assert "closed" in closed.json()["changed"]
    db = SessionLocal()
    try:
        assert db.query(SessionRow).filter(SessionRow.user_id == made["user_id"],
                                           SessionRow.revoked_at.is_(None)).count() == 0
        assert db.get(User, made["user_id"]).active is False
    finally:
        db.close()


def test_setting_and_amending_a_requirement(client, ids):
    made = _add(client, ids["officer"], name="Order Amended").json()
    created = client.post("/api/requirements", json={
        "participant_id": made["participant_id"], "category": "public", "count_required": 3,
        "period": "week", "starts_on": NOW.isoformat(),
        "ends_on": (NOW + timedelta(days=90)).isoformat(), "order_reference": "ORD-26-777",
    }, headers=as_(ids["officer"]))
    assert created.status_code == 201
    rid = created.json()["id"]

    detail = client.get(f"/api/participants/{made['participant_id']}",
                        headers=as_(ids["officer"])).json()
    assert detail["requirements"][0]["count_required"] == 3

    amended = client.patch(f"/api/requirements/{rid}", json={"count_required": 2},
                           headers=as_(ids["officer"]))
    assert amended.status_code == 200
    detail = client.get(f"/api/participants/{made['participant_id']}",
                        headers=as_(ids["officer"])).json()
    assert detail["requirements"][0]["count_required"] == 2

    audit = client.get("/api/audit?limit=20", headers=as_(ids["admin"])).json()
    change = next(a for a in audit if a["action"] == "update_requirement")
    assert "was 3 public per week" in change["detail"], "the audit keeps what it used to say"


def test_a_requirement_cannot_end_before_it_starts(client, ids):
    made = _add(client, ids["officer"], name="Bad Dates").json()
    r = client.post("/api/requirements", json={
        "participant_id": made["participant_id"], "count_required": 3, "period": "week",
        "starts_on": NOW.isoformat(), "ends_on": (NOW - timedelta(days=5)).isoformat(),
    }, headers=as_(ids["officer"]))
    assert r.status_code == 422


def test_staff_from_another_county_cannot_enrol_into_this_one(client, ids):
    made = _add(client, ids["officer"], name="Not Yours").json()
    from app.models import County, Role
    db = SessionLocal()
    try:
        other = County(name="Far County", state="MI", timezone="America/Detroit")
        db.add(other); db.flush()
        stranger = User(county_id=other.id, role=Role.officer, name="Officer Elsewhere")
        db.add(stranger); db.commit()
        sid = stranger.id
    finally:
        db.close()
    assert client.patch(f"/api/participants/{made['participant_id']}",
                        json={"case_number": "hijack"}, headers=as_(sid)).status_code == 404


def test_a_participant_cannot_enrol_anyone(client, ids):
    assert client.post("/api/participants", json={"name": "Me Again", "phone": "+17345557777"},
                       headers=as_(ids["participant"])).status_code == 403
