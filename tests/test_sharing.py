"""
A participant's own log, and sending it by text to someone without an
account: an officer outside the system, a lawyer, a spouse keeping track.
"""
import re
from datetime import datetime, timezone

from .conftest import as_
from .test_texts import _named, fresh_limits, no_phones, phones  # noqa: F401  fixtures

NOW = datetime.now(timezone.utc)


def _token(text):
    return re.search(r"/log/([A-Za-z0-9_\-]+)", text).group(1)


def test_a_participant_reads_their_own_log_with_added_meetings_marked(client, ids):
    who = _named(client, "Marcus Webb")
    m = client.post("/api/meetings", json={
        "name": "Thursday group", "category": "public", "address": "Church basement",
        "lat": 42.2750, "lng": -83.7413, "starts_at": NOW.isoformat()}, headers=as_(who)).json()
    r = client.post("/api/checkins", json={"meeting_id": m["id"], "lat": 42.2750, "lng": -83.7413,
                                           "accuracy_m": 8}, headers=as_(who))
    assert r.status_code == 201, r.text

    log = client.get("/api/my/log", headers=as_(who)).json()
    assert log["participant"] == "Marcus Webb" and log["content_hash"]
    line = next(x for x in log["check_ins"] if x["meeting"] == "Thursday group")
    assert line["added_by_participant"] is True and line["recorded_at"] == "42.2750, -83.7413"
    assert line["outcome"] == "counted"
    assert client.get("/api/my/log", headers=as_(ids["officer"])).status_code == 404


def test_shared_by_text_opened_without_signing_in_and_stopped(client, ids, phones):
    who = _named(client, "Jordan Ellis")
    r = client.post("/api/my/shares", json={"name": "Sam Ellis", "phone": "(734) 555-0161"},
                    headers=as_(who))
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is True and "demo_link" not in r.json()
    assert r.json()["phone"] == "(734) 555 ... 0161"

    text = phones.to("+17345550161")[0]
    assert text.startswith("ActionsCount+:") and "Jordan E." in text and "STOP" in text
    token = _token(text)

    client.cookies.clear()
    log = client.get(f"/api/shared/{token}")
    assert log.status_code == 200 and log.json()["participant"] == "Jordan Ellis"
    assert log.json()["shared_with"] == "Sam Ellis"
    assert client.get(f"/log/{token}").status_code == 200, "the page itself opens"

    shares = client.get("/api/my/shares", headers=as_(who)).json()
    assert shares[0]["opens"] == 1 and shares[0]["last_opened_at"]

    assert client.delete(f"/api/my/shares/{shares[0]['id']}", headers=as_(_named(client, "Dana Ruiz"))
                         ).status_code == 404, "only the participant stops their own share"
    assert client.delete(f"/api/my/shares/{shares[0]['id']}", headers=as_(who)).status_code == 204
    assert client.get(f"/api/shared/{token}").status_code == 410
    assert client.get("/api/shared/not-a-token").status_code == 404
    assert client.get("/api/my/shares", headers=as_(who)).json() == []


def test_rules_on_sharing(client, ids, phones):
    who = _named(client, "Chris Lindqvist")
    me = client.get("/api/me", headers=as_(who)).json()
    own = client.post("/api/my/shares", json={"name": "Me", "phone": me.get("phone") or "+17345550103"},
                      headers=as_(who))
    assert own.status_code in (201, 422)
    assert client.post("/api/my/shares", json={"name": "X", "phone": "7345550162"},
                       headers=as_(who)).status_code == 422, "a name"
    assert client.post("/api/my/shares", json={"name": "Lawyer", "phone": "12"},
                       headers=as_(who)).status_code == 422, "a number"
    assert client.post("/api/my/shares", json={"name": "Lawyer", "phone": "7345550162"},
                       headers=as_(who)).status_code == 201
    again = client.post("/api/my/shares", json={"name": "Lawyer", "phone": "734.555.0162"},
                        headers=as_(who))
    assert again.status_code == 409, "the same person twice"


def test_without_texting_the_demo_shows_the_link(client, ids, no_phones):
    who = _named(client, "Dana Ruiz")
    r = client.post("/api/my/shares", json={"name": "Pat Ruiz", "phone": "7345550163"},
                    headers=as_(who))
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is False and "/log/" in r.json()["demo_link"]
    assert no_phones.sent == []
    token = r.json()["demo_link"].rsplit("/", 1)[1]
    assert client.get(f"/api/shared/{token}").status_code == 200
