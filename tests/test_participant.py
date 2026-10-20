"""What a participant can do, see, and not see. Section 1 of the build-out."""
from datetime import datetime, timedelta, timezone

from app.compliance import COUNTS, NOT_COUNTED, PENDING, PeriodResult, outcome, streak
from app.models import CheckInStatus, ReviewDecision

from .conftest import as_

NOW = datetime.now(timezone.utc)


def _users(client):
    return client.get("/api/users").json()


def _named(client, name):
    return next(u["id"] for u in _users(client) if u["name"] == name)


def _meeting(client, officer, **kw):
    body = {"name": "Participant test", "category": "public", "lat": 42.2750, "lng": -83.7413,
            "starts_at": NOW.isoformat(), "radius_ft": 500, "time_window_min": 60} | kw
    r = client.post("/api/meetings", json=body, headers=as_(officer))
    assert r.status_code == 201, r.text
    return r.json()


def _check_in(client, who, meeting_id, lat=42.2750, lng=-83.7413, accuracy=8):
    return client.post("/api/checkins", json={
        "meeting_id": meeting_id, "lat": lat, "lng": lng, "accuracy_m": accuracy},
        headers=as_(who))


# --------------------------------------------------------------------------- #
# Consent
# --------------------------------------------------------------------------- #

def test_first_day_participant_must_accept_consent_before_checking_in(client, ids):
    jordan = _named(client, "Jordan Ellis")
    me = client.get("/api/me", headers=as_(jordan)).json()
    assert me["consent"]["required"] is True
    assert me["policy"]["pin_retention_days"] == 30

    m = _meeting(client, ids["officer"], name="Consent test")
    assert _check_in(client, jordan, m["id"]).status_code == 428

    stale = client.post("/api/my/consent", json={"version": "1999-01"}, headers=as_(jordan))
    assert stale.status_code == 409, "agreeing to wording they never saw must not count"

    ok = client.post("/api/my/consent", json={"version": me["consent"]["version"]},
                     headers=as_(jordan))
    assert ok.status_code == 200
    assert client.get("/api/me", headers=as_(jordan)).json()["consent"]["required"] is False
    assert _check_in(client, jordan, m["id"]).status_code == 201


def test_staff_do_not_need_consent(client, ids):
    assert client.get("/api/me", headers=as_(ids["officer"])).json()["consent"]["required"] is False


# --------------------------------------------------------------------------- #
# One meeting counts once
# --------------------------------------------------------------------------- #

def test_a_second_tap_returns_the_same_check_in(client, ids):
    m = _meeting(client, ids["officer"], name="Double tap")
    first = _check_in(client, ids["participant"], m["id"])
    again = _check_in(client, ids["participant"], m["id"])
    assert first.status_code == 201 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert again.json()["duplicate"] is True


def test_best_outcome_per_meeting_is_what_counts():
    class C:  # a check-in, as far as outcome() cares
        def __init__(self, status):
            self.status = status
    assert outcome(C(CheckInStatus.confirmed), None) == COUNTS
    assert outcome(C(CheckInStatus.flagged), None) == PENDING
    assert outcome(C(CheckInStatus.flagged), ReviewDecision.denied) == NOT_COUNTED
    assert outcome(C(CheckInStatus.rejected), None) == NOT_COUNTED
    assert outcome(C(CheckInStatus.rejected), ReviewDecision.approved) == COUNTS
    # A rejection then a confirmed retry: the meeting counts, once.
    assert max(outcome(C(CheckInStatus.rejected), None),
               outcome(C(CheckInStatus.confirmed), None)) == COUNTS


def test_retry_after_rejection_counts_the_meeting_once(client, ids):
    """Rejected from the parking lot, confirmed from inside: one meeting, one count."""
    who = _named(client, "Riley Chen")
    before = client.get("/api/my/progress", headers=as_(who)).json()["summary"]["requirements"][0]

    m = _meeting(client, ids["officer"], name="Retry once")
    assert _check_in(client, who, m["id"], lat=42.2850).json()["status"] == "rejected"
    assert _check_in(client, who, m["id"]).json()["status"] == "confirmed"

    after = client.get("/api/my/progress", headers=as_(who)).json()["summary"]["requirements"][0]
    assert after["counted"] == before["counted"] + 1


# --------------------------------------------------------------------------- #
# Tenancy and personal meetings
# --------------------------------------------------------------------------- #

def test_participant_cannot_check_in_to_someone_elses_added_meeting(client, ids):
    mine = client.post("/api/meetings", json={
        "name": "Therapy", "category": "therapy", "lat": 42.0, "lng": -83.0,
        "address": "Dr. Patel's office", "starts_at": NOW.isoformat()},
        headers=as_(_named(client, "Chris Lindqvist"))).json()
    other = _named(client, "Marcus Webb")
    assert _check_in(client, other, mine["id"], lat=42.0, lng=-83.0).status_code == 404
    assert client.get(f"/api/meetings/{mine['id']}", headers=as_(other)).status_code == 404


def test_staff_see_added_meetings_marked_and_can_decide_against_them(client, ids):
    """An added meeting counts, so the officer has to be able to see it and say no."""
    who = _named(client, "Dana Ruiz")
    mine = client.post("/api/meetings", json={
        "name": "Dr. Patel", "category": "therapy", "lat": 42.0, "lng": -83.0,
        "address": "Dr. Patel's office", "starts_at": NOW.isoformat()}, headers=as_(who)).json()
    # Far from where it was added, so it lands in the review queue.
    c = _check_in(client, who, mine["id"], lat=42.5, lng=-83.0).json()
    assert c["status"] == "rejected"

    officer = ids["officer"]
    listed = client.get("/api/meetings", headers=as_(officer)).json()
    assert not any(m["id"] == mine["id"] for m in listed), "not on the county's meeting list"
    queue = client.get("/api/reviews/queue", headers=as_(officer)).json()["items"]
    assert any(q["id"] == c["id"] and q["meeting"]["added"] for q in queue)
    pid = client.get("/api/me", headers=as_(who)).json()["participant_id"]
    detail = client.get(f"/api/participants/{pid}", headers=as_(officer)).json()
    assert any(ev["id"] == c["id"] for ev in detail["timeline"])
    r = client.post(f"/api/reviews/{c['id']}", json={
        "decision": "denied", "note": "Nowhere near where it was added."}, headers=as_(officer))
    assert r.status_code in (200, 201), r.text

    # The participant still sees their own.
    mine_list = client.get("/api/checkins/mine", headers=as_(who)).json()
    assert any(x["id"] == c["id"] for x in mine_list)


def test_meeting_list_can_be_bounded_by_time(client, ids):
    since, until = NOW - timedelta(hours=12), NOW + timedelta(days=7)
    rows = client.get("/api/meetings", params={"since": since.isoformat(),
                                               "until": until.isoformat()},
                      headers=as_(ids["participant"])).json()
    everything = client.get("/api/meetings", headers=as_(ids["participant"])).json()
    assert rows, "the seed has a meeting open right now"
    assert len(rows) < len(everything), "weeks of past meetings should drop out"
    for m in rows:
        assert since <= datetime.fromisoformat(m["starts_at"]) < until


# --------------------------------------------------------------------------- #
# Check-out
# --------------------------------------------------------------------------- #

def test_checking_out_early_is_written_into_the_record(client, ids):
    m = _meeting(client, ids["officer"], name="Leaves early", duration_min=90)
    c = _check_in(client, ids["participant"], m["id"]).json()
    out = client.post(f"/api/checkins/{c['id']}/checkout", json={"lat": 42.2750, "lng": -83.7413},
                      headers=as_(ids["participant"])).json()
    assert out["checked_out_at"]
    names = {r["name"] for r in out["reasons"]}
    assert {"checkout_distance", "checkout_time"} <= names
    # Informational only: the verdict itself does not move.
    assert out["status"] == c["status"] and out["confidence"] == c["confidence"]


def test_cannot_check_out_of_a_rejected_check_in_or_someone_elses(client, ids):
    m = _meeting(client, ids["officer"], name="Checkout rules")
    far = _check_in(client, ids["participant"], m["id"], lat=42.2850).json()
    assert client.post(f"/api/checkins/{far['id']}/checkout", json={},
                       headers=as_(ids["participant"])).status_code == 409
    good = _check_in(client, ids["participant"], m["id"]).json()
    other = _named(client, "Marcus Webb")
    assert client.post(f"/api/checkins/{good['id']}/checkout", json={},
                       headers=as_(other)).status_code == 404


# --------------------------------------------------------------------------- #
# The participant's own view
# --------------------------------------------------------------------------- #

def test_history_shows_the_officers_decision_and_the_participants_notes(client, ids):
    who = _named(client, "Chris Lindqvist")
    rows = client.get("/api/checkins/mine", headers=as_(who)).json()
    disputed = [r for r in rows if r["disputes"]]
    assert disputed, "the seed gives Chris a disputed check-in"
    assert "basement" in disputed[0]["disputes"][0]["message"]
    assert all("review" in r for r in rows)
    assert all("ends_at" in r["meeting"] for r in rows)


def test_progress_carries_history_and_a_streak(client, ids):
    who = _named(client, "Marcus Webb")
    p = client.get("/api/my/progress", headers=as_(who)).json()
    req = p["summary"]["requirements"][0]
    assert len(req["history"]) >= 2
    assert req["streak"] >= 2, "a steady attender should be on a run"


def test_streak_ignores_an_unfinished_current_period():
    def p(counted):
        return PeriodResult(NOW, NOW, 3, counted, 0, 0)
    assert streak([p(3), p(3), p(1)]) == 2      # this week not done yet: run intact
    assert streak([p(3), p(3), p(3)]) == 3      # this week done: it counts
    assert streak([p(3), p(0), p(3), p(1)]) == 1
    assert streak([]) == 0


def test_participant_can_export_their_own_record(client, ids):
    r = client.get("/api/my/export", headers=as_(ids["participant"]))
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    body = r.json()
    assert body["check_ins"] and body["case"]["requirements"]


def test_history_never_includes_a_period_before_the_order_began(client, ids):
    """Jordan's requirement starts today. Last week must not appear as 'short'."""
    jordan = _named(client, "Jordan Ellis")
    req = client.get("/api/my/progress", headers=as_(jordan)).json()["summary"]["requirements"][0]
    assert len(req["history"]) == 1
    assert req["history"][0]["starts"] == req["starts"]
