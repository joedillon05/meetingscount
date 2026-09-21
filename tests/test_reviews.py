"""The review queue: lifecycle, history, notes, bulk, context. Section 2."""
from datetime import datetime, timedelta, timezone

from .conftest import as_

NOW = datetime.now(timezone.utc)


def _named(client, name):
    return next(u["id"] for u in client.get("/api/users").json() if u["name"] == name)


def _flagged(client, officer, who, name):
    """A fresh check-in the engine flags: right place, fuzzy GPS."""
    m = client.post("/api/meetings", json={
        "name": name, "category": "public", "lat": 42.2750, "lng": -83.7413,
        "starts_at": NOW.isoformat(), "radius_ft": 500, "time_window_min": 60,
    }, headers=as_(officer)).json()
    c = client.post("/api/checkins", json={
        "meeting_id": m["id"], "lat": 42.2750, "lng": -83.7413, "accuracy_m": 140,
    }, headers=as_(who)).json()
    assert c["status"] == "flagged", c
    return c


def _queue(client, who, state="open", **params):
    return client.get("/api/reviews/queue", params={"state": state, **params},
                      headers=as_(who)).json()


def _week(client, who):
    return client.get("/api/my/progress", headers=as_(who)).json()["summary"]["requirements"][0]


# --------------------------------------------------------------------------- #
# "Need more info" is a conversation, not an ending
# --------------------------------------------------------------------------- #

def test_more_info_waits_for_the_participant_then_comes_back(client, ids):
    officer, who = ids["officer"], _named(client, "Marcus Webb")
    c = _flagged(client, officer, who, "More info flow")
    pending_before = _week(client, who)["pending"]

    ask = client.post(f"/api/reviews/{c['id']}", json={"decision": "more_info"},
                      headers=as_(officer))
    assert ask.status_code == 422, "a question needs to be written down"
    ask = client.post(f"/api/reviews/{c['id']}", json={
        "decision": "more_info", "note": "Which room was the meeting in?"}, headers=as_(officer))
    assert ask.json()["state"] == "waiting"

    # Out of the decide pile, into the waiting pile, and still pending, not a miss.
    assert not any(i["id"] == c["id"] for i in _queue(client, officer)["items"])
    assert any(i["id"] == c["id"] for i in _queue(client, officer, "waiting")["items"])
    assert _week(client, who)["pending"] == pending_before

    # The participant sees the question and answers it.
    mine = next(x for x in client.get("/api/checkins/mine", headers=as_(who)).json()
                if x["id"] == c["id"])
    assert mine["awaiting_reply"] is True
    assert mine["review"]["note"] == "Which room was the meeting in?"
    client.post(f"/api/checkins/{c['id']}/dispute",
                json={"message": "Basement, room B. The chair was Pat."}, headers=as_(who))

    back = next(i for i in _queue(client, officer)["items"] if i["id"] == c["id"])
    assert back["replied"] is True

    ok = client.post(f"/api/reviews/{c['id']}", json={"decision": "approved"}, headers=as_(officer))
    assert ok.json()["state"] == "decided"
    assert [h["decision"] for h in ok.json()["history"]] == ["more_info", "approved"]


# --------------------------------------------------------------------------- #
# Decisions are a history, and notes are required where they matter
# --------------------------------------------------------------------------- #

def test_denying_needs_a_note_the_participant_will_see(client, ids):
    c = _flagged(client, ids["officer"], ids["participant"], "Deny note")
    r = client.post(f"/api/reviews/{c['id']}", json={"decision": "denied"},
                    headers=as_(ids["officer"]))
    assert r.status_code == 422
    assert "participant will see" in r.json()["detail"]


def test_a_changed_decision_is_a_new_row_and_the_latest_counts(client, ids):
    officer, who = ids["officer"], _named(client, "Dana Ruiz")
    c = _flagged(client, officer, who, "Change of mind")
    before = _week(client, who)["counted"]

    client.post(f"/api/reviews/{c['id']}", json={"decision": "approved"}, headers=as_(officer))
    assert _week(client, who)["counted"] == before + 1

    change = client.post(f"/api/reviews/{c['id']}", json={
        "decision": "denied", "note": "Facilitator says they left after ten minutes."},
        headers=as_(officer))
    assert change.status_code == 201
    assert [h["decision"] for h in change.json()["history"]] == ["approved", "denied"]
    assert _week(client, who)["counted"] == before

    audit = client.get("/api/audit?limit=20", headers=as_(ids["admin"])).json()
    assert any(a["action"] == "change_review" and a["target_id"] == c["id"] for a in audit)


# --------------------------------------------------------------------------- #
# Bulk
# --------------------------------------------------------------------------- #

def test_bulk_decides_only_what_is_still_open(client, ids):
    officer, who = ids["officer"], _named(client, "Jenna Park")
    a = _flagged(client, officer, who, "Bulk A")
    b = _flagged(client, officer, who, "Bulk B")
    done = _flagged(client, officer, who, "Bulk done")
    client.post(f"/api/reviews/{done['id']}", json={"decision": "approved"}, headers=as_(officer))

    no_note = client.post("/api/reviews/bulk", json={
        "checkin_ids": [a["id"], b["id"]], "decision": "denied"}, headers=as_(officer))
    assert no_note.status_code == 422

    r = client.post("/api/reviews/bulk", json={
        "checkin_ids": [a["id"], b["id"], done["id"], 999999], "decision": "approved",
        "note": "Known weak signal in this building."}, headers=as_(officer)).json()
    assert sorted(r["decided"]) == sorted([a["id"], b["id"]])
    assert {s["id"]: s["reason"] for s in r["skipped"]} == {
        done["id"]: "already decided", 999999: "not found"}


# --------------------------------------------------------------------------- #
# What the officer sees while deciding
# --------------------------------------------------------------------------- #

def test_the_list_has_no_coordinates_but_opening_an_item_does_and_is_logged(client, ids):
    officer = ids["officer"]
    c = _flagged(client, officer, ids["participant"], "Pin rules")
    listed = next(i for i in _queue(client, officer)["items"] if i["id"] == c["id"])
    assert "lat" not in listed

    before = len(client.get("/api/audit?limit=500", headers=as_(ids["admin"])).json())
    item = client.get(f"/api/reviews/{c['id']}", headers=as_(officer)).json()
    assert item["lat"] is not None
    audit = client.get("/api/audit?limit=500", headers=as_(ids["admin"])).json()
    assert len(audit) > before
    assert any(a["action"] == "view_review_item" and a["target_id"] == c["id"] for a in audit)


def test_item_shows_what_each_decision_would_do_to_the_week(client, ids):
    officer, who = ids["officer"], _named(client, "Tyler Nowak")
    c = _flagged(client, officer, who, "Impact")
    item = client.get(f"/api/reviews/{c['id']}", headers=as_(officer)).json()
    public = next(i for i in item["impact"] if i["requirement"]["category"] == "public")
    assert public["if_approved"]["counted"] == public["now"]["counted"] + 1
    assert public["if_denied"]["counted"] == public["now"]["counted"]
    assert public["now"]["pending"] >= 1


def test_venue_context_is_aggregate_only(client, ids):
    """How check-ins go at this place, without naming anyone else who was there."""
    item = client.get(f"/api/reviews/{_flagged(client, ids['officer'], ids['participant'], 'Venue')['id']}",
                      headers=as_(ids["officer"])).json()
    venue = item["context"]["venue"]
    assert set(venue["everyone"]) == {"checkins", "people", "flagged_share", "weak_gps_share",
                                      "median_accuracy_m"}
    assert venue["person"]["checkins"] >= 1
    assert item["context"]["person"]["last_90_days"]["checkins"] >= 1


def test_seeded_downtown_venue_shows_weak_gps_for_everyone(client, ids):
    """Aisha's fuzzy fixes downtown: the context should say it is the place, not her."""
    officer = ids["admin"]
    aisha = next(i for i in _queue(client, officer)["items"]
                 if i["participant"]["name"] == "Aisha Bello")
    venue = client.get(f"/api/reviews/{aisha['id']}", headers=as_(officer)).json()["context"]["venue"]
    assert venue["person"]["checkins"] > 1
    assert venue["everyone"]["weak_gps_share"] > 0


def test_queue_counts_and_my_caseload_filter(client, ids):
    officer = ids["officer"]
    everyone = _queue(client, officer)
    mine = _queue(client, officer, mine_only="true")
    assert set(everyone["counts"]) == {"open", "waiting", "decided"}
    assert everyone["counts"]["open"] == len(everyone["items"])
    assert len(mine["items"]) <= len(everyone["items"])
    me = client.get("/api/me", headers=as_(officer)).json()["id"]
    assert all(i["participant"]["officer_id"] == me for i in mine["items"])


def test_other_county_cannot_open_or_decide(client, ids):
    """A second county's officer gets 404s, not someone else's evidence."""
    from app.db import SessionLocal
    from app.models import County, Role, User
    db = SessionLocal()
    other = County(name="Elsewhere County", state="MI", timezone="America/Detroit")
    db.add(other); db.flush()
    stranger = User(county_id=other.id, role=Role.officer, name="Officer Stranger")
    db.add(stranger); db.commit()
    sid = stranger.id
    db.close()

    c = _flagged(client, ids["officer"], ids["participant"], "Tenancy")
    assert client.get(f"/api/reviews/{c['id']}", headers=as_(sid)).status_code == 404
    assert client.post(f"/api/reviews/{c['id']}", json={"decision": "approved"},
                       headers=as_(sid)).status_code == 404
    assert not any(i["id"] == c["id"] for i in _queue(client, sid)["items"])


def test_decided_tab_lists_recent_decisions_newest_first(client, ids):
    decided = _queue(client, ids["officer"], "decided")["items"]
    assert decided, "earlier tests and the seed decided things"
    times = [d["since"] for d in decided]
    assert times == sorted(times, reverse=True)
    assert all(d["latest"]["decision"] in ("approved", "denied") for d in decided)


def test_bad_state_is_refused(client, ids):
    r = client.get("/api/reviews/queue", params={"state": "everything"}, headers=as_(ids["officer"]))
    assert r.status_code == 422


def test_week_badge_marks_this_weeks_items(client, ids):
    c = _flagged(client, ids["officer"], ids["participant"], "This week")
    item = next(i for i in _queue(client, ids["officer"])["items"] if i["id"] == c["id"])
    assert item["this_week"] is True
    old = [i for i in _queue(client, ids["officer"])["items"]
           if datetime.fromisoformat(i["checked_in_at"]) < NOW - timedelta(days=8)]
    assert all(not i["this_week"] for i in old)


def test_participant_record_carries_no_coordinates(client, ids):
    """The map lives in the review view, logged one check-in at a time. The
    record itself never hands out a location history (principle 3)."""
    case = client.get("/api/caseload", headers=as_(ids["officer"])).json()
    for row in case[:4]:
        detail = client.get(f"/api/participants/{row['participant_id']}",
                            headers=as_(ids["officer"])).json()
        assert all("lat" not in ev for ev in detail["timeline"])
        assert all("review_state" in ev for ev in detail["timeline"])
