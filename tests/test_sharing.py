"""
A participant's own log, and sending it by text to someone without an
account: an officer outside the system, a lawyer, a spouse keeping track.
"""
import re
from datetime import datetime, timezone

import pytest

from app import mail
from app.config import settings
from app.db import SessionLocal
from app.models import EmailLog

from .conftest import as_
from .test_texts import _named, fresh_limits, no_phones, phones  # noqa: F401  fixtures

NOW = datetime.now(timezone.utc)


class FakeMailer:
    """Stands in for SendGrid. Remembers every email, can be told to refuse."""

    def __init__(self):
        self.sent = []
        self.refuse = None

    def send(self, to, subject, text, html):
        if self.refuse:
            return mail.Outcome("failed", error=self.refuse)
        self.sent.append((to, subject, text, html))
        return mail.Outcome("sent", message_id="msg-" + str(len(self.sent)))

    def to(self, address):
        return [(s, t, h) for a, s, t, h in self.sent if a == address]


@pytest.fixture
def mailer(monkeypatch):
    """A real deployment: demo off, email switched on, going to the fake."""
    fake = FakeMailer()
    monkeypatch.setattr(settings, "DEMO", False)
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "SG.test")
    monkeypatch.setattr(settings, "EMAIL_FROM", "attendance@example.com")
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(mail, "transport", fake)
    yield fake
    monkeypatch.setattr(mail, "transport", None)


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


def test_shared_by_email_with_the_same_link_and_the_same_rules(client, ids, mailer, phones):
    who = _named(client, "Riley Chen")
    r = client.post("/api/my/shares", json={"name": "Attorney Diaz", "email": "Diaz@Example.com"},
                    headers=as_(who))
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is True and r.json()["via"] == "email"
    assert r.json()["email"] == "d***@example.com" and r.json()["phone"] is None

    subject, text, html = mailer.to("diaz@example.com")[0]
    assert "Riley C." in subject and "/log/" in text and "/log/" in html
    assert "password" in text, "it says what the email will never ask for"
    token = _token(text)
    client.cookies.clear()
    log = client.get(f"/api/shared/{token}")
    assert log.status_code == 200 and log.json()["shared_with"] == "Attorney Diaz"

    db = SessionLocal()
    try:
        row = db.query(EmailLog).filter(EmailLog.to_email == "diaz@example.com").one()
        assert row.status == "sent" and row.purpose == "share" and row.provider_id == "msg-1"
    finally:
        db.close()

    again = client.post("/api/my/shares", json={"name": "Diaz", "email": "diaz@example.com"},
                        headers=as_(who))
    assert again.status_code == 409, "the same address twice"
    assert client.post("/api/my/shares", json={"name": "Diaz", "email": "not-an-address"},
                       headers=as_(who)).status_code == 422
    assert client.post("/api/my/shares", json={"name": "Diaz"}, headers=as_(who)).status_code == 422, \
        "a number or an address"
    both = client.post("/api/my/shares", json={"name": "Diaz", "email": "x@example.com",
                                               "phone": "7345550199"}, headers=as_(who))
    assert both.status_code == 422, "one or the other"

    shares = client.get("/api/my/shares", headers=as_(who)).json()
    mine = next(s for s in shares if s["name"] == "Attorney Diaz")
    assert mine["via"] == "email" and mine["opens"] == 1
    assert client.delete(f"/api/my/shares/{mine['id']}", headers=as_(who)).status_code == 204
    assert client.get(f"/api/shared/{token}").status_code == 410


def test_email_not_set_up_is_said_plainly_outside_demo(client, ids, phones, monkeypatch):
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "")
    monkeypatch.setattr(settings, "EMAIL_FROM", "")
    who = _named(client, "Pat Moreau")
    r = client.post("/api/my/shares", json={"name": "Someone", "email": "someone@example.com"},
                    headers=as_(who))
    assert r.status_code == 503 and "Email is not set up" in r.json()["detail"]
    assert client.get("/api/auth/context").json()["emailing"] is False


def test_without_texting_the_demo_shows_the_link(client, ids, no_phones):
    who = _named(client, "Dana Ruiz")
    r = client.post("/api/my/shares", json={"name": "Pat Ruiz", "phone": "7345550163"},
                    headers=as_(who))
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is False and "/log/" in r.json()["demo_link"]
    assert no_phones.sent == []
    token = r.json()["demo_link"].rsplit("/", 1)[1]
    assert client.get(f"/api/shared/{token}").status_code == 200
