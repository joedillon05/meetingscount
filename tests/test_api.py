"""End-to-end behaviour over the real API, including the access rules."""
from datetime import datetime, timedelta, timezone

from .conftest import as_


def test_healthz_needs_no_auth(client):
    assert client.get("/healthz").json()["ok"] is True


def test_anonymous_is_refused(client):
    assert client.get("/api/caseload").status_code == 401


def test_participant_cannot_see_the_caseload(client, ids):
    """The whole point of roles: a participant must never read other people's records."""
    r = client.get("/api/caseload", headers=as_(ids["participant"]))
    assert r.status_code == 403


def test_participant_cannot_read_the_audit_log(client, ids):
    assert client.get("/api/audit", headers=as_(ids["participant"])).status_code == 403


def test_officer_cannot_read_the_audit_log(client, ids):
    """Audit is admin-only, so an officer cannot check who has been watching them."""
    assert client.get("/api/audit", headers=as_(ids["officer"])).status_code == 403


def test_officer_cannot_bulk_export_coordinates(client, ids):
    """The ops map is the one bulk-location endpoint and it is admin-only."""
    assert client.get("/api/ops/checkins", headers=as_(ids["officer"])).status_code == 403
    assert client.get("/api/ops/checkins", headers=as_(ids["admin"])).status_code == 200


def test_caseload_hides_coordinates_from_confirmed_checkins(client, ids):
    """Staff see outcomes, not location history. Only reviewable rows carry a pin."""
    case = client.get("/api/caseload", headers=as_(ids["officer"])).json()
    detail = client.get(f"/api/participants/{case[0]['participant_id']}",
                        headers=as_(ids["officer"])).json()
    for ev in detail["timeline"]:
        if ev["status"] == "confirmed":
            assert "lat" not in ev, "a confirmed check-in leaked coordinates"


def test_a_meeting_a_participant_adds_counts_and_is_marked_as_theirs(client, ids):
    """It counts, but the participant cannot bend it: the server sets the
    source, the distance and the window, keeps the start where a check-in can
    still verify it, and it says where it is."""
    from app.models import Meeting
    from app.db import SessionLocal
    db = SessionLocal()
    db.query(Meeting).filter(Meeting.created_by_id == ids["participant"]).delete()
    db.commit(); db.close()

    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).isoformat()
    body = {"name": "Tuesday night group", "category": "public", "source": "staff",
            "lat": 42.0, "lng": -83.0, "starts_at": now.isoformat(), "radius_ft": 5280,
            "time_window_min": 480}
    r = client.post("/api/meetings", json=body, headers=as_(ids["participant"]))
    assert r.status_code == 422, "where it is, is required"
    body |= {"address": "St. Mary's, 2nd floor"}
    r = client.post("/api/meetings", json=body | {"starts_at": yesterday}, headers=as_(ids["participant"]))
    assert r.status_code == 422 and "already happened" in r.json()["detail"], "no backdating"
    r = client.post("/api/meetings", json=body | {"starts_at": (now + timedelta(days=9)).isoformat()},
                    headers=as_(ids["participant"]))
    assert r.status_code == 422 and "days ahead" in r.json()["detail"]
    m = client.post("/api/meetings", json=body, headers=as_(ids["participant"])).json()
    assert m["source"] == "personal" and m["added"] is True and m["counts"] is True
    assert m["radius_ft"] == 500 and m["time_window_min"] == 60
    assert m["starts_at"] > yesterday

    for _ in range(2):
        assert client.post("/api/meetings", json=body | {"address": "here"},
                           headers=as_(ids["participant"])).status_code == 201
    assert client.post("/api/meetings", json=body | {"address": "here"},
                       headers=as_(ids["participant"])).status_code == 429, "three a day"


def test_checkin_verdict_and_review_flow(client, ids):
    now = datetime.now(timezone.utc)
    m = client.post("/api/meetings", json={
        "name": "Test meeting", "category": "public", "lat": 42.2750, "lng": -83.7413,
        "starts_at": now.isoformat(), "radius_ft": 500, "time_window_min": 60,
    }, headers=as_(ids["officer"])).json()

    # Half a mile away -> rejected, and it is still stored.
    far = client.post("/api/checkins", json={
        "meeting_id": m["id"], "lat": 42.2850, "lng": -83.7413, "accuracy_m": 8,
    }, headers=as_(ids["participant"])).json()
    assert far["status"] == "rejected"
    assert any(r["name"] == "distance" and not r["passed"] for r in far["reasons"])

    # Walks over and tries again: standing right there, good signal -> confirmed.
    good = client.post("/api/checkins", json={
        "meeting_id": m["id"], "lat": 42.2750, "lng": -83.7413, "accuracy_m": 8,
    }, headers=as_(ids["participant"])).json()
    assert good["status"] == "confirmed"
    assert good["id"] != far["id"]

    # It appears in the queue, an officer decides, and it leaves the queue.
    queue = client.get("/api/reviews/queue", headers=as_(ids["officer"])).json()["items"]
    assert any(c["id"] == far["id"] for c in queue)

    d = client.post(f"/api/reviews/{far['id']}", json={
        "decision": "approved", "note": "Facilitator confirmed attendance."},
        headers=as_(ids["officer"]))
    assert d.status_code == 201

    queue2 = client.get("/api/reviews/queue", headers=as_(ids["officer"])).json()["items"]
    assert not any(c["id"] == far["id"] for c in queue2)

    # Reversing a decision without saying why is refused. Nothing is silently
    # overwritten: a reasoned change is a new row, and the first one stays.
    again = client.post(f"/api/reviews/{far['id']}", json={"decision": "denied"},
                        headers=as_(ids["officer"]))
    assert again.status_code == 422
    same = client.post(f"/api/reviews/{far['id']}", json={"decision": "approved", "note": "x"},
                       headers=as_(ids["officer"]))
    assert same.status_code == 409


def test_demo_timestamp_override_is_honoured_in_demo_mode(client, ids):
    """The simulator needs this; production must not have it (MC_DEMO=0)."""
    now = datetime.now(timezone.utc)
    m = client.post("/api/meetings", json={
        "name": "Window test", "category": "public", "lat": 42.0, "lng": -83.0,
        "starts_at": now.isoformat(), "time_window_min": 60,
    }, headers=as_(ids["officer"])).json()
    late = client.post("/api/checkins", json={
        "meeting_id": m["id"], "lat": 42.0, "lng": -83.0, "accuracy_m": 10,
        "at": (now + timedelta(minutes=120)).isoformat(),
    }, headers=as_(ids["participant"])).json()
    assert late["status"] == "rejected"
    assert any(r["name"] == "time_window" and not r["passed"] for r in late["reasons"])


def test_every_staff_view_writes_an_audit_row(client, ids):
    before = len(client.get("/api/audit?limit=500", headers=as_(ids["admin"])).json())
    case = client.get("/api/caseload", headers=as_(ids["officer"])).json()
    client.get(f"/api/participants/{case[0]['participant_id']}", headers=as_(ids["officer"]))
    after = client.get("/api/audit?limit=500", headers=as_(ids["admin"])).json()
    assert len(after) > before
    assert any(a["action"] == "view_participant" for a in after)


def test_report_carries_a_content_hash_and_a_careful_note(client, ids):
    case = client.get("/api/caseload", headers=as_(ids["officer"])).json()
    r = client.get(f"/api/reports/participant/{case[0]['participant_id']}",
                   headers=as_(ids["officer"])).json()
    assert len(r["content_hash"]) == 64
    # The report must never assert absence, only that it could not verify.
    assert "did not attend" not in r["note"].lower()
    assert "could not verify" in r["note"].lower()


def test_retention_job_drops_old_pins_but_keeps_the_verdict(client, ids):
    r = client.post("/api/retention/run", headers=as_(ids["admin"]))
    assert r.status_code == 200
    ops = client.get("/api/ops/checkins", headers=as_(ids["admin"])).json()
    purged = [c for c in ops if c["pin_purged"]]
    for c in purged:
        assert c["lat"] is None
        assert c["distance_ft"] is not None and c["reasons"], "verdict must survive"


def test_www_goes_to_the_one_public_address(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://app.example")
    r = client.get("/officer?x=1", headers={"host": "www.app.example"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://app.example/officer?x=1"
    assert client.get("/healthz", headers={"host": "app.example"}).status_code == 200
    assert client.get("/healthz", headers={"host": "meetingscount-preview.fly.dev"}).status_code == 200


def test_the_old_address_forwards_pages_but_never_api_calls(client, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(settings, "REDIRECT_FROM", "example, www.example")
    r = client.get("/log/abc?x=1", headers={"host": "example"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://app.example/log/abc?x=1"
    assert client.get("/", headers={"host": "www.example"}, follow_redirects=False).status_code == 301
    assert client.get("/api/auth/context", headers={"host": "example"}).status_code == 200
    assert client.post("/api/sms/status", data={}, headers={"host": "example"}).status_code == 403, \
        "a webhook reaches the app, and is turned away only for its signature"
    assert client.get("/healthz", headers={"host": "example"}).status_code == 200
    assert client.get("/officer", headers={"host": "app.example"}, follow_redirects=False).status_code == 200
