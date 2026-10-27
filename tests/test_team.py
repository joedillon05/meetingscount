"""
The founders' back office: tasks, hours, timesheets, expenses.

Two superusers, Joe and Spencer, made straight in the database the way
tools/make_staff.py would. Nobody else can see any of this.
"""
from datetime import date, timedelta

import pytest

from app.db import SessionLocal
from app.models import Role, User

from .conftest import as_

TODAY = date.today()
D = lambda n: (TODAY - timedelta(days=n)).isoformat()      # noqa: E731


@pytest.fixture(scope="module")
def team(client):
    db = SessionLocal()
    try:
        joe = User(role=Role.superuser, name="Joe Dillon", email="joe@team.example")
        spencer = User(role=Role.superuser, name="Spencer Reed", email="spencer@team.example")
        db.add_all([joe, spencer])
        db.commit()
        return {"joe": joe.id, "spencer": spencer.id}
    finally:
        db.close()


def _task(client, who, **over):
    return client.post("/api/team/tasks", json={"title": "Call Washtenaw back"} | over, headers=as_(who))


# --------------------------------------------------------------------------- #
# Who gets in
# --------------------------------------------------------------------------- #

def test_only_superusers_can_see_the_team_pages(client, ids, team):
    for role in ("admin", "officer", "participant"):
        assert client.get("/api/team/overview", headers=as_(ids[role])).status_code == 403
        assert client.get("/api/team/tasks", headers=as_(ids[role])).status_code == 403
        assert client.post("/api/team/hours", json={"worked_on": D(0), "minutes": 60, "note": "x"},
                           headers=as_(ids[role])).status_code == 403
    assert client.get("/api/team/overview").status_code == 401
    r = client.get("/api/team/overview", headers=as_(team["joe"]))
    assert r.status_code == 200
    # Other test modules make superusers of their own; these two are among them.
    assert {"Joe Dillon", "Spencer Reed"} <= {p["name"] for p in r.json()["people"]}
    assert next(p for p in r.json()["people"] if p["name"] == "Joe Dillon")["is_you"]


def test_a_task_can_only_be_given_to_someone_on_the_team(client, ids, team):
    r = _task(client, team["joe"], assignee_id=ids["admin"])
    assert r.status_code == 422 and "not on the team" in r.json()["detail"]
    r = _task(client, team["joe"], county_id=999_999)
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Tasks
# --------------------------------------------------------------------------- #

def test_a_task_goes_from_open_to_done_with_notes_along_the_way(client, ids, team):
    made = _task(client, team["joe"], assignee_id=team["spencer"], due_on=D(-3),
                 details="They asked for the pilot pricing.").json()
    assert made["status"] == "open" and made["assignee"] == "Spencer Reed"
    assert made["created_by"] == "Joe Dillon" and made["due_on"] == D(-3)

    noted = client.post(f"/api/team/tasks/{made['id']}/notes", json={"body": "Left a voicemail."},
                        headers=as_(team["spencer"])).json()
    assert [n["author"] for n in noted["notes"]] == ["Spencer Reed"]

    waiting = client.patch(f"/api/team/tasks/{made['id']}", json={"status": "waiting"},
                           headers=as_(team["spencer"])).json()
    assert waiting["status"] == "waiting" and waiting["done_at"] is None

    done = client.patch(f"/api/team/tasks/{made['id']}", json={"status": "done"},
                        headers=as_(team["spencer"])).json()
    assert done["status"] == "done" and done["done_at"]

    rows = client.get("/api/team/tasks", headers=as_(team["joe"])).json()
    mine = next(t for t in rows if t["id"] == made["id"])
    assert mine["status"] == "done" and len(mine["notes"]) == 1

    audit = client.get("/api/audit?limit=20", headers=as_(team["joe"])).json()
    assert any(a["action"] == "team_update_task" and "status to done" in a["detail"] for a in audit)


def test_open_tasks_come_first_soonest_due_first(client, ids, team):
    late = _task(client, team["joe"], title="Overdue thing", due_on=D(5)).json()
    soon = _task(client, team["joe"], title="Due tomorrow", due_on=D(-1)).json()
    never = _task(client, team["joe"], title="No date").json()
    rows = client.get("/api/team/tasks", headers=as_(team["joe"])).json()
    order = [t["id"] for t in rows if t["id"] in (late["id"], soon["id"], never["id"])]
    assert order == [late["id"], soon["id"], never["id"]]
    assert rows.index(next(t for t in rows if t["status"] == "done")) > rows.index(
        next(t for t in rows if t["id"] == never["id"]))


# --------------------------------------------------------------------------- #
# Hours, rates and timesheets
# --------------------------------------------------------------------------- #

def test_hours_are_yours_alone_until_they_are_on_a_timesheet(client, ids, team):
    h = client.post("/api/team/hours", json={"worked_on": D(2), "minutes": 90, "note": "Cook mode"},
                    headers=as_(team["joe"]))
    assert h.status_code == 201
    h = h.json()
    assert h["editable"] and h["cents"] is None, "no rate yet, so no value yet"

    # Spencer can read Joe's log but not touch it.
    seen = client.get(f"/api/team/hours?user_id={team['joe']}", headers=as_(team["spencer"])).json()
    assert any(x["id"] == h["id"] for x in seen)
    assert client.patch(f"/api/team/hours/{h['id']}", json={"minutes": 120},
                        headers=as_(team["spencer"])).status_code == 404
    assert client.delete(f"/api/team/hours/{h['id']}", headers=as_(team["spencer"])).status_code == 404

    edited = client.patch(f"/api/team/hours/{h['id']}", json={"minutes": 120, "note": "Cook mode, timers"},
                          headers=as_(team["joe"])).json()
    assert edited["minutes"] == 120 and edited["note"] == "Cook mode, timers"
    assert client.delete(f"/api/team/hours/{h['id']}", headers=as_(team["joe"])).status_code == 204
    assert client.post("/api/team/hours", json={"worked_on": D(-10), "minutes": 60, "note": "future"},
                       headers=as_(team["joe"])).status_code == 422


def test_a_timesheet_prices_each_day_at_the_rate_then_in_force(client, ids, team):
    joe, spencer = team["joe"], team["spencer"]
    assert client.post("/api/team/timesheets", json={"starts_on": D(30), "ends_on": D(0)},
                       headers=as_(joe)).status_code == 422, "nothing to submit"

    # $40/h from a month ago, $50/h from a week ago.
    client.post("/api/team/rates", json={"user_id": joe, "cents_per_hour": 4000, "effective_on": D(30)},
                headers=as_(spencer))
    client.post("/api/team/rates", json={"user_id": joe, "cents_per_hour": 5000, "effective_on": D(7)},
                headers=as_(joe))
    old = client.post("/api/team/hours", json={"worked_on": D(10), "minutes": 90, "note": "Old rate"},
                      headers=as_(joe)).json()
    new = client.post("/api/team/hours", json={"worked_on": D(3), "minutes": 150, "note": "New rate"},
                      headers=as_(joe)).json()
    assert (old["cents"], new["cents"]) == (6000, 12500)

    before = client.get("/api/team/overview", headers=as_(joe)).json()
    me = next(p for p in before["people"] if p["is_you"])
    assert me["rate"]["cents_per_hour"] == 5000
    assert me["balance"]["unsubmitted_minutes"] == 240 and me["balance"]["unsubmitted_cents"] == 18500
    assert me["balance"]["owed_cents"] == 18500, "earned is owed, on a timesheet or not"

    sheet = client.post("/api/team/timesheets", json={"starts_on": D(14), "ends_on": D(0),
                                                     "note": "Two weeks of cook mode"}, headers=as_(joe))
    assert sheet.status_code == 201
    sheet = sheet.json()
    assert sheet["status"] == "submitted" and sheet["minutes"] == 240 and sheet["amount_cents"] == 18500
    assert [h["note"] for h in sheet["hours"]] == ["Old rate", "New rate"]

    # Frozen now: editing is refused and a raise changes nothing.
    assert client.patch(f"/api/team/hours/{old['id']}", json={"minutes": 600},
                        headers=as_(joe)).status_code == 409
    client.post("/api/team/rates", json={"user_id": joe, "cents_per_hour": 9000, "effective_on": D(60)},
                headers=as_(joe))
    assert client.get(f"/api/team/timesheets/{sheet['id']}", headers=as_(joe)).json()["amount_cents"] == 18500

    # Only the other founder decides, and the overview says it is waiting on them.
    assert client.post(f"/api/team/timesheets/{sheet['id']}/approve", json={},
                       headers=as_(joe)).status_code == 403
    assert client.get("/api/team/overview", headers=as_(spencer)).json()["awaiting_me"] == 1
    assert client.post(f"/api/team/timesheets/{sheet['id']}/pay", json={},
                       headers=as_(spencer)).status_code == 409, "approve before paying"
    approved = client.post(f"/api/team/timesheets/{sheet['id']}/approve", json={},
                           headers=as_(spencer)).json()
    assert approved["status"] == "approved" and approved["decided_by"] == "Spencer Reed"

    mid = next(p for p in client.get("/api/team/overview", headers=as_(joe)).json()["people"] if p["is_you"])
    assert mid["balance"]["approved_cents"] == 18500 and mid["balance"]["owed_cents"] == 18500
    assert mid["balance"]["unsubmitted_minutes"] == 0

    paid = client.post(f"/api/team/timesheets/{sheet['id']}/pay",
                       json={"note": "Zelle", "paid_on": D(0)}, headers=as_(spencer)).json()
    assert paid["status"] == "paid" and paid["paid_cents"] == 18500 and paid["paid_note"] == "Zelle"
    after = next(p for p in client.get("/api/team/overview", headers=as_(joe)).json()["people"] if p["is_you"])
    assert after["balance"]["owed_cents"] == 0 and after["balance"]["paid_cents"] == 18500
    assert after["balance"]["cash_earned_cents"] == 18500

    actions = {a["action"] for a in client.get("/api/audit?limit=50", headers=as_(joe)).json()}
    assert {"team_set_rate", "team_submit_timesheet", "team_approve_timesheet",
            "team_pay_timesheet"} <= actions


def test_a_returned_timesheet_frees_its_hours(client, ids, team):
    joe, spencer = team["joe"], team["spencer"]
    h = client.post("/api/team/hours", json={"worked_on": D(1), "minutes": 30, "note": "Typo fix"},
                    headers=as_(joe)).json()
    sheet = client.post("/api/team/timesheets", json={"starts_on": D(1), "ends_on": D(1)},
                        headers=as_(joe)).json()
    assert client.post(f"/api/team/timesheets/{sheet['id']}/return", json={},
                       headers=as_(spencer)).status_code == 422, "a reason is required"
    back = client.post(f"/api/team/timesheets/{sheet['id']}/return",
                       json={"note": "Half an hour for a typo?"}, headers=as_(spencer)).json()
    assert back["status"] == "returned" and back["decision_note"].startswith("Half")

    mine = next(x for x in client.get("/api/team/hours", headers=as_(joe)).json() if x["id"] == h["id"])
    assert mine["editable"] and mine["timesheet_id"] is None
    assert client.patch(f"/api/team/hours/{h['id']}", json={"minutes": 15},
                        headers=as_(joe)).status_code == 200
    # A returned sheet counts for nothing.
    me = next(p for p in client.get("/api/team/overview", headers=as_(joe)).json()["people"] if p["is_you"])
    assert me["balance"]["submitted_cents"] == 0 and me["balance"]["unsubmitted_minutes"] == 15
    client.delete(f"/api/team/hours/{h['id']}", headers=as_(joe))


def test_a_timesheet_needs_a_rate_for_every_day_on_it(client, ids, team):
    spencer = team["spencer"]
    client.post("/api/team/hours", json={"worked_on": D(4), "minutes": 60, "note": "Demo at the court"},
                headers=as_(spencer))
    r = client.post("/api/team/timesheets", json={"starts_on": D(4), "ends_on": D(4)}, headers=as_(spencer))
    assert r.status_code == 422 and "No hourly rate" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Expenses
# --------------------------------------------------------------------------- #

def test_an_expense_someone_paid_is_owed_until_the_other_founder_reimburses_it(client, ids, team):
    joe, spencer = team["joe"], team["spencer"]
    fly = client.post("/api/team/expenses", json={"spent_on": D(5), "vendor": "Fly.io", "amount_cents": 1234,
                                                 "note": "October hosting", "paid_by_id": joe},
                      headers=as_(joe))
    assert fly.status_code == 201
    fly = fly.json()
    assert fly["owed"] and fly["paid_by"] == "Joe Dillon"
    business = client.post("/api/team/expenses", json={"spent_on": D(5), "vendor": "Twilio",
                                                      "amount_cents": 2000, "paid_by_id": None},
                           headers=as_(spencer)).json()
    assert not business["owed"] and business["paid_by"] is None

    me = next(p for p in client.get("/api/team/overview", headers=as_(joe)).json()["people"] if p["is_you"])
    assert me["balance"]["expenses_owed_cents"] == 1234 and me["balance"]["owed_cents"] == 1234

    assert client.post(f"/api/team/expenses/{business['id']}/reimburse", json={},
                       headers=as_(joe)).status_code == 409
    assert client.post(f"/api/team/expenses/{fly['id']}/reimburse", json={},
                       headers=as_(joe)).status_code == 403, "not your own"
    done = client.post(f"/api/team/expenses/{fly['id']}/reimburse", json={"note": "Venmo"},
                       headers=as_(spencer)).json()
    assert not done["owed"] and done["reimbursed_by"] == "Spencer Reed" and done["reimbursed_note"] == "Venmo"
    assert client.patch(f"/api/team/expenses/{fly['id']}", json={"amount_cents": 1},
                        headers=as_(joe)).status_code == 409, "part of the record now"
    assert client.delete(f"/api/team/expenses/{fly['id']}", headers=as_(joe)).status_code == 409

    me = next(p for p in client.get("/api/team/overview", headers=as_(joe)).json()["people"] if p["is_you"])
    assert me["balance"]["owed_cents"] == 0

    edited = client.patch(f"/api/team/expenses/{business['id']}", json={"amount_cents": 2100},
                          headers=as_(joe)).json()
    assert edited["amount_cents"] == 2100
    assert client.delete(f"/api/team/expenses/{business['id']}", headers=as_(spencer)).status_code == 204
    assert all(e["id"] != business["id"] for e in client.get("/api/team/expenses", headers=as_(joe)).json())


def test_the_team_pages_never_show_up_in_a_county_admins_audit_log(client, ids, team):
    rows = client.get("/api/audit?limit=500", headers=as_(ids["admin"])).json()
    assert not any(a["action"].startswith("team_") for a in rows)


# --------------------------------------------------------------------------- #
# The invoice sent for a timesheet
# --------------------------------------------------------------------------- #

def test_the_person_paid_attaches_the_invoice_and_the_payer_gets_its_link(client, ids, team):
    joe, spencer = team["joe"], team["spencer"]
    client.post("/api/team/rates", json={"user_id": joe, "cents_per_hour": 5000, "effective_on": D(400)},
                headers=as_(joe))
    client.post("/api/team/hours", json={"worked_on": D(20), "minutes": 60, "note": "Invoice test"},
                headers=as_(joe))
    sheet = client.post("/api/team/timesheets", json={"starts_on": D(20), "ends_on": D(20)},
                        headers=as_(joe)).json()
    url = f"/api/team/timesheets/{sheet['id']}/invoice"
    body = {"invoice_ref": "1042", "invoice_url": "https://found.com/pay/abc123"}

    assert client.post(url, json=body, headers=as_(spencer)).status_code == 403, "not the payer's to set"
    assert client.post(url, json={"invoice_ref": "1042", "invoice_url": "http://found.com/pay/abc123"},
                       headers=as_(joe)).status_code == 422, "https only"
    done = client.post(url, json=body, headers=as_(joe)).json()
    assert done["invoice_ref"] == "1042" and done["invoice_url"] == body["invoice_url"] and done["invoiced_at"]

    # Still there after approval, and the payment note names it by default.
    client.post(f"/api/team/timesheets/{sheet['id']}/approve", json={}, headers=as_(spencer))
    listed = next(s for s in client.get("/api/team/timesheets", headers=as_(spencer)).json()
                  if s["id"] == sheet["id"])
    assert listed["invoice_url"] == body["invoice_url"]
    paid = client.post(f"/api/team/timesheets/{sheet['id']}/pay", json={}, headers=as_(spencer)).json()
    assert paid["paid_note"] == "Invoice 1042"
    assert client.post(url, json=body, headers=as_(joe)).status_code == 409, "paid sheets are a record"

    actions = {a["action"] for a in client.get("/api/audit?limit=50", headers=as_(joe)).json()}
    assert "team_invoice_timesheet" in actions


def test_an_invoice_can_be_removed_again(client, ids, team):
    joe = team["joe"]
    client.post("/api/team/hours", json={"worked_on": D(21), "minutes": 30, "note": "Remove test"},
                headers=as_(joe))
    sheet = client.post("/api/team/timesheets", json={"starts_on": D(21), "ends_on": D(21)},
                        headers=as_(joe)).json()
    url = f"/api/team/timesheets/{sheet['id']}/invoice"
    client.post(url, json={"invoice_ref": "1043"}, headers=as_(joe))
    cleared = client.post(url, json={}, headers=as_(joe)).json()
    assert cleared["invoice_ref"] is None and cleared["invoice_url"] is None and cleared["invoiced_at"] is None


# --------------------------------------------------------------------------- #
# Equity
# --------------------------------------------------------------------------- #

def test_equity_accrues_per_hour_against_the_valuation_then_in_force(client, ids, team):
    joe, spencer = team["joe"], team["spencer"]
    bad = client.post("/api/team/rates", json={"user_id": spencer, "cents_per_hour": 1750,
                                               "equity_cents_per_hour": 1250, "effective_on": D(100)},
                      headers=as_(joe))
    assert bad.status_code == 422 and "valuation" in bad.json()["detail"]

    # $17.50 cash and $12.50 equity at a $500,000 valuation, then the same
    # equity at a $1,000,000 valuation from a week ago.
    for day, valuation in ((D(100), 500_000_00), (D(7), 1_000_000_00)):
        r = client.post("/api/team/rates", json={"user_id": spencer, "cents_per_hour": 1750,
                                                 "equity_cents_per_hour": 1250, "valuation_cents": valuation,
                                                 "effective_on": day}, headers=as_(spencer))
        assert r.status_code == 201 and r.json()["equity_cents_per_hour"] == 1250
    assert client.post("/api/team/hours", json={"worked_on": D(30), "minutes": 24 * 60, "note": "A full day"},
                       headers=as_(spencer)).status_code == 201
    assert client.post("/api/team/hours", json={"worked_on": D(2), "minutes": 60, "note": "An hour"},
                       headers=as_(spencer)).status_code == 201

    mine = client.get("/api/team/hours", headers=as_(spencer)).json()
    assert {h["note"]: (h["cents"], h["equity_cents"]) for h in mine}.items() >= {
        "A full day": (24 * 1750, 24 * 1250), "An hour": (1750, 1250)}.items()
    me = next(p for p in client.get("/api/team/overview", headers=as_(spencer)).json()["people"]
              if p["is_you"])
    assert me["rate"]["valuation_cents"] == 1_000_000_00
    b = me["balance"]
    # Every entry counts, including one logged in an earlier test: each at
    # the valuation in force on its day, so a dollar of equity earned after
    # the valuation doubled is half the share.
    expected_cents = sum(h["equity_cents"] for h in mine)
    expected_share = sum(h["equity_cents"] / (1_000_000_00 if h["worked_on"] >= D(7) else 500_000_00)
                         for h in mine)
    assert b["equity_cents"] == expected_cents == 24 * 1250 + 1250 + 1250
    assert abs(b["equity_share"] - expected_share) < 1e-12
    assert abs(b["total_earned_cents"] - (b["cash_earned_cents"] + b["equity_cents"])) <= 1
    # Owed is cash earned less cash paid, with nothing paid yet.
    assert b["owed_cents"] == b["cash_earned_cents"]
