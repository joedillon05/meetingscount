"""
Confirmation by text: asking someone who was there, what they see, what they
can answer, the texts that go out, and the Twilio webhooks.

No real text is ever sent. A fake transport collects what would have gone
out, so a test can read the link out of the sponsor's text and open it.
"""
import re
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from app import sms
from app.config import settings
from app.db import SessionLocal
from app.models import Confirmation, SmsLog, User

from .conftest import as_

NOW = datetime.now(timezone.utc)
TOKEN = "test-auth-token"


class FakePhoneCompany:
    """Stands in for Twilio. Remembers every text, can be told to refuse."""

    def __init__(self):
        self.sent = []
        self.refuse = None            # an error string to return instead of sending

    def send(self, to, body):
        if self.refuse:
            return sms.Outcome("failed", error=self.refuse)
        self.sent.append((to, body))
        return sms.Outcome("sent", sid="SM" + secrets.token_hex(16))

    def to(self, number):
        return [b for t, b in self.sent if t == number]


@pytest.fixture
def phones(monkeypatch):
    """A real deployment: demo off, texting switched on, going to the fake."""
    fake = FakePhoneCompany()
    monkeypatch.setattr(settings, "DEMO", False)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "ACtest")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "TWILIO_FROM", "+17345550000")
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://app.example")
    monkeypatch.setattr(sms, "transport", fake)
    yield fake
    monkeypatch.setattr(sms, "transport", None)


ALLOWED = "+17345550170"


@pytest.fixture
def demo_phones(phones, monkeypatch):
    """The preview: demo on, Twilio on, one number on the allow list."""
    monkeypatch.setattr(settings, "DEMO", True)
    monkeypatch.setattr(settings, "SMS_ALLOW", "(734) 555-0170")
    yield phones


@pytest.fixture
def no_phones(monkeypatch):
    """Nothing configured, demo on: links show on screen, nothing is sent."""
    fake = FakePhoneCompany()
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "")
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "")
    monkeypatch.setattr(settings, "TWILIO_FROM", "")
    monkeypatch.setattr(sms, "transport", fake)
    yield fake
    monkeypatch.setattr(sms, "transport", None)


@pytest.fixture(autouse=True)
def fresh_limits():
    from app import security
    security._HITS.clear()
    yield
    security._HITS.clear()


def _named(client, name):
    return next(u["id"] for u in client.get("/api/users").json() if u["name"] == name)


def _meeting(client, officer, **kw):
    body = {"name": "Text test", "category": "public", "lat": 42.2750, "lng": -83.7413,
            "starts_at": NOW.isoformat(), "radius_ft": 500, "time_window_min": 60} | kw
    r = client.post("/api/meetings", json=body, headers=as_(officer))
    assert r.status_code == 201, r.text
    return r.json()


def _checked_in(client, who, meeting_id, out=True):
    r = client.post("/api/checkins", json={"meeting_id": meeting_id, "lat": 42.2750,
                                           "lng": -83.7413, "accuracy_m": 8}, headers=as_(who))
    assert r.status_code == 201, r.text
    c = r.json()
    if out:
        r = client.post(f"/api/checkins/{c['id']}/checkout", json={"lat": 42.2750, "lng": -83.7413},
                        headers=as_(who))
        assert r.status_code == 200, r.text
    return c


def _ask(client, who, checkin_id, **body):
    return client.post(f"/api/checkins/{checkin_id}/confirmation",
                       json={"name": "Kim Alvarez", "phone": "(734) 555-0199",
                             "relationship": "sponsor"} | body, headers=as_(who))


def _link(text):
    return re.search(r"https?://\S+/confirm/([A-Za-z0-9_\-]+)", text)


# --------------------------------------------------------------------------- #
# The whole loop
# --------------------------------------------------------------------------- #

def test_sponsor_gets_one_text_and_confirms_from_the_link(client, ids, phones):
    dana = _named(client, "Dana Ruiz")
    m = _meeting(client, ids["officer"], name="Loop test")
    c = _checked_in(client, dana, m["id"])

    r = _ask(client, dana, c["id"])
    assert r.status_code == 201, r.text
    asked = r.json()
    assert asked["state"] == "asked" and asked["sent"] is True
    assert "demo_link" not in asked, "a real text went out, so no link on screen"

    texts = phones.to("+17345550199")
    assert len(texts) == 1
    body = texts[0]
    assert body.startswith("MeetingsCount:") and "Dana R." in body and "Loop test" in body
    assert "STOP" in body, "a text to someone who never signed up says how to stop them"
    assert "probation" not in body.lower() and "court" not in body.lower()
    token = _link(body).group(1)

    # What the sponsor sees: a first name, the meeting, the time. No more.
    page = client.get(f"/api/confirm/{token}")
    assert page.status_code == 200, page.text
    shown = page.json()
    assert shown["participant"] == "Dana R." and shown["state"] == "asked"
    assert shown["meeting"]["name"] == "Loop test" and shown["asked"] == "Kim"
    assert "lat" not in str(shown) and "case" not in str(shown)

    # They say yes.
    r = client.post(f"/api/confirm/{token}", json={"answer": "yes", "note": "Stayed the whole hour."})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "confirmed"

    # The participant sees it, with the note, and got a text.
    mine = next(x for x in client.get("/api/checkins/mine", headers=as_(dana)).json() if x["id"] == c["id"])
    assert mine["confirmed_by"]["state"] == "confirmed"
    assert mine["confirmed_by"]["answer_note"] == "Stayed the whole hour."
    db = SessionLocal()
    try:
        number = db.get(User, dana).phone
    finally:
        db.close()
    own = phones.to(number)
    assert any("checked out of Loop test" in b and "We texted Kim" in b for b in own), own
    assert any("Kim confirmed you were at Loop test" in b for b in own), own

    # The link is single use.
    assert client.post(f"/api/confirm/{token}", json={"answer": "no"}).status_code == 409
    assert client.get(f"/api/confirm/{token}").json()["state"] == "confirmed"

    # The officer's report says who confirmed, in words.
    pid = client.get("/api/me", headers=as_(dana)).json()["participant_id"]
    report = client.get(f"/api/reports/participant/{pid}?days=7", headers=as_(ids["officer"])).json()
    line = next(l for l in report["check_ins"] if l["meeting"] == "Loop test")
    assert line["confirmation_words"].startswith("Kim Alvarez (sponsor) confirmed by text on ")
    assert "today" not in line["confirmation_words"], "a report is read later"

    # And the token never sits in the text log in usable form.
    db = SessionLocal()
    try:
        logged = db.query(SmsLog).filter(SmsLog.purpose == "ask").order_by(SmsLog.id.desc()).first()
        assert logged.status == "sent" and token not in logged.body and "/confirm/..." in logged.body
    finally:
        db.close()


def test_a_no_is_recorded_and_shows_in_the_queue(client, ids, phones):
    devon = _named(client, "Devon Hargrove")
    m = _meeting(client, ids["officer"], name="Said no")
    # Far enough to be flagged, so it lands in the queue.
    r = client.post("/api/checkins", json={"meeting_id": m["id"], "lat": 42.2762, "lng": -83.7413,
                                           "accuracy_m": 80}, headers=as_(devon))
    c = r.json()
    assert c["status"] == "flagged", c
    assert _ask(client, devon, c["id"], name="Pete Walsh", phone="7345550177",
                relationship="chair").status_code == 201
    token = _link(phones.to("+17345550177")[0]).group(1)
    assert client.post(f"/api/confirm/{token}", json={"answer": "no"}).status_code == 200

    item = client.get(f"/api/reviews/{c['id']}", headers=as_(ids["officer"])).json()
    assert item["confirmed_by"]["state"] == "declined"
    kinds = [e["kind"] for e in item["timeline"]]
    assert "asked" in kinds and "answered" in kinds
    answered = next(e for e in item["timeline"] if e["kind"] == "answered")
    assert answered["yes"] is False and answered["name"] == "Pete Walsh"
    queue = client.get("/api/reviews/queue?state=open", headers=as_(ids["officer"])).json()
    row = next(i for i in queue["items"] if i["id"] == c["id"])
    assert row["confirmed_by"]["state"] == "declined"


# --------------------------------------------------------------------------- #
# Who may ask whom
# --------------------------------------------------------------------------- #

def test_rules_on_asking(client, ids, phones):
    jenna = _named(client, "Jenna Park")
    m = _meeting(client, ids["officer"], name="Rules")
    c = _checked_in(client, jenna, m["id"])
    db = SessionLocal()
    try:
        own = db.get(User, jenna).phone
    finally:
        db.close()

    assert _ask(client, jenna, c["id"], phone=own).status_code == 422, "not your own number"
    assert _ask(client, jenna, c["id"], phone="12").status_code == 422
    assert _ask(client, jenna, c["id"], name="K").status_code == 422

    first = _ask(client, jenna, c["id"])
    assert first.status_code == 201
    again = _ask(client, jenna, c["id"])
    assert again.status_code == 409 and "already asked" in again.json()["detail"]

    # Not somebody else's check-in.
    other = _named(client, "Marcus Webb")
    assert _ask(client, other, c["id"]).status_code == 404

    # Not a personal meeting: those are the participant's alone.
    personal = client.post("/api/meetings", json={"name": "Therapy", "category": "therapy",
                           "lat": 42.2750, "lng": -83.7413, "starts_at": NOW.isoformat()},
                           headers=as_(jenna)).json()
    pc = _checked_in(client, jenna, personal["id"], out=False)
    assert _ask(client, jenna, pc["id"]).status_code == 409

    # Three people at most per check-in.
    assert _ask(client, jenna, c["id"], name="Second Person", phone="7345550181").status_code == 201
    assert _ask(client, jenna, c["id"], name="Third Person", phone="7345550182").status_code == 201
    assert _ask(client, jenna, c["id"], name="Fourth Person", phone="7345550183").status_code == 409


def test_saved_people_are_the_participants_own(client, ids, phones):
    pat = _named(client, "Pat Moreau")
    riley = _named(client, "Riley Chen")
    r = client.post("/api/my/confirmers", json={"name": "Rosa Nguyen", "phone": "734-555-0190",
                                                "relationship": "sponsor"}, headers=as_(pat))
    assert r.status_code == 201, r.text
    saved = r.json()
    assert saved["phone"] == "(734) 555 ... 0190", "the number comes back masked"
    assert [k["id"] for k in client.get("/api/my/confirmers", headers=as_(pat)).json()] == [saved["id"]]
    assert client.get("/api/my/confirmers", headers=as_(riley)).json() == []

    m = _meeting(client, ids["officer"], name="Saved person")
    c = _checked_in(client, pat, m["id"])
    assert _ask(client, pat, c["id"], confirmer_id=saved["id"]).status_code == 201
    assert phones.to("+17345550190"), "the saved number was texted"
    assert _ask(client, riley, c["id"], confirmer_id=saved["id"]).status_code == 404

    assert client.delete(f"/api/my/confirmers/{saved['id']}", headers=as_(riley)).status_code == 404
    assert client.delete(f"/api/my/confirmers/{saved['id']}", headers=as_(pat)).status_code == 204
    assert client.get("/api/my/confirmers", headers=as_(pat)).json() == []


def test_without_texting_the_demo_shows_the_link_and_nothing_goes_out(client, ids, no_phones):
    chris = _named(client, "Chris Lindqvist")
    m = _meeting(client, ids["officer"], name="Demo link")
    c = _checked_in(client, chris, m["id"])
    r = _ask(client, chris, c["id"])
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is False and "/confirm/" in r.json()["demo_link"]
    assert no_phones.sent == []
    token = r.json()["demo_link"].rsplit("/", 1)[1]
    assert client.get(f"/api/confirm/{token}").json()["state"] == "asked"


def test_expired_links_and_bad_tokens(client, ids, phones):
    marcus = _named(client, "Marcus Webb")
    m = _meeting(client, ids["officer"], name="Expiry")
    c = _checked_in(client, marcus, m["id"])
    assert _ask(client, marcus, c["id"]).status_code == 201
    token = _link(phones.to("+17345550199")[-1]).group(1)
    db = SessionLocal()
    try:
        x = db.query(Confirmation).filter(Confirmation.checkin_id == c["id"]).one()
        x.expires_at = NOW - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/confirm/{token}").json()["state"] == "expired"
    assert client.post(f"/api/confirm/{token}", json={"answer": "yes"}).status_code == 410
    assert client.get("/api/confirm/not-a-token").status_code == 404
    assert client.post("/api/confirm/not-a-token", json={"answer": "yes"}).status_code == 404


# --------------------------------------------------------------------------- #
# Sign-in codes by text
# --------------------------------------------------------------------------- #

def test_sign_in_code_goes_by_text_when_texting_is_on(client, phones, monkeypatch):
    monkeypatch.setattr(settings, "DEV_AUTH", False)
    r = client.post("/api/auth/code/request", json={"phone": "+17345550102"})
    assert r.status_code == 200
    assert r.json()["delivered"] is True and "demo_code" not in r.json()
    texts = phones.to("+17345550102")
    assert len(texts) == 1
    code = re.search(r"\b(\d{6})\b", texts[0]).group(1)
    ok = client.post("/api/auth/code/verify", json={"phone": "+17345550102", "code": code})
    assert ok.status_code == 200, ok.text
    client.cookies.clear()

    # An unknown number gets the same answer and no text.
    r = client.post("/api/auth/code/request", json={"phone": "+17345559999"})
    assert r.json()["delivered"] is True and len(phones.sent) == 1


def test_sign_in_code_falls_back_to_the_officer_when_texting_is_off(client, no_phones):
    r = client.post("/api/auth/code/request", json={"phone": "+17345550102"})
    assert r.json()["delivered"] is False and "demo_code" in r.json()
    assert no_phones.sent == []


# --------------------------------------------------------------------------- #
# Replies and delivery reports from Twilio
# --------------------------------------------------------------------------- #

def _twilio_post(client, path, params):
    url = f"https://app.example{path}"
    return client.post(path, data=params, headers={
        "X-Twilio-Signature": sms.sign(url, params, TOKEN),
        "Content-Type": "application/x-www-form-urlencoded"})


def test_a_text_reply_of_yes_answers_the_open_ask(client, ids, phones):
    tyler = _named(client, "Tyler Nowak")
    m = _meeting(client, ids["officer"], name="Reply yes")
    c = _checked_in(client, tyler, m["id"])
    assert _ask(client, tyler, c["id"], name="Lee Park", phone="7345550166").status_code == 201

    r = _twilio_post(client, "/api/sms/inbound", {"From": "+17345550166", "Body": " yes "})
    assert r.status_code == 200 and "thank you" in r.text.lower()
    mine = next(x for x in client.get("/api/checkins/mine", headers=as_(tyler)).json() if x["id"] == c["id"])
    assert mine["confirmed_by"]["state"] == "confirmed"
    assert mine["confirmed_by"]["answered_via"] == "text"

    # Nothing open any more: a second YES is told so, politely.
    r = _twilio_post(client, "/api/sms/inbound", {"From": "+17345550166", "Body": "YES"})
    assert "no open question" in r.text


def test_stop_is_honoured_before_anything_else(client, ids, phones):
    r = _twilio_post(client, "/api/sms/inbound", {"From": "+17345550155", "Body": "STOP"})
    assert r.status_code == 200
    sam = _named(client, "Sam Okafor")
    m = _meeting(client, ids["officer"], name="Stopped")
    c = _checked_in(client, sam, m["id"])
    r = _ask(client, sam, c["id"], name="Quiet Person", phone="7345550155")
    assert r.status_code == 409 and "not to get texts" in r.json()["detail"]
    assert not phones.to("+17345550155")

    _twilio_post(client, "/api/sms/inbound", {"From": "+17345550155", "Body": "START"})
    assert _ask(client, sam, c["id"], name="Quiet Person", phone="7345550155").status_code == 201
    assert phones.to("+17345550155")


def test_webhooks_need_twilios_signature(client, phones):
    r = client.post("/api/sms/inbound", data={"From": "+17345550155", "Body": "STOP"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
    r = client.post("/api/sms/inbound", data={"From": "+17345550155", "Body": "STOP"},
                    headers={"Content-Type": "application/x-www-form-urlencoded",
                             "X-Twilio-Signature": "nope"})
    assert r.status_code == 403


def test_a_webhook_still_pointed_at_the_old_address_keeps_working(client, phones):
    # MC_PUBLIC_URL is app.example; the Twilio console still calls the server
    # by the address it had before the move, which the test client calls testserver.
    params = {"MessageSid": "SMnobody", "MessageStatus": "delivered"}
    def post(url):
        return client.post("/api/sms/status", data=params, headers={
            "X-Twilio-Signature": sms.sign(url, params, TOKEN),
            "Content-Type": "application/x-www-form-urlencoded"})
    assert post("http://testserver/api/sms/status").status_code == 200
    assert post("https://app.example/api/sms/status").status_code == 200
    assert post("https://somewhere.else/api/sms/status").status_code == 403


def test_delivery_report_updates_the_log(client, ids, phones):
    aisha = _named(client, "Aisha Bello")
    m = _meeting(client, ids["officer"], name="Delivery")
    c = _checked_in(client, aisha, m["id"])
    assert _ask(client, aisha, c["id"], name="Ana Ortiz", phone="7345550144").status_code == 201
    db = SessionLocal()
    try:
        row = db.query(SmsLog).filter(SmsLog.to_phone == "+17345550144").one()
        sid = row.provider_sid
    finally:
        db.close()
    r = _twilio_post(client, "/api/sms/status", {"MessageSid": sid, "MessageStatus": "delivered"})
    assert r.status_code == 200
    db = SessionLocal()
    try:
        assert db.query(SmsLog).filter(SmsLog.provider_sid == sid).one().status == "delivered"
    finally:
        db.close()


def test_twilio_refusing_an_unsubscribed_number_is_remembered(client, ids, phones):
    jordan = _named(client, "Jordan Ellis")
    me = client.get("/api/me", headers=as_(jordan)).json()
    client.post("/api/my/consent", json={"version": me["consent"]["version"]}, headers=as_(jordan))
    m = _meeting(client, ids["officer"], name="Refused")
    c = _checked_in(client, jordan, m["id"])
    phones.refuse = f"{sms.UNSUBSCRIBED}: The message From/To pair violates a blacklist rule."
    assert _ask(client, jordan, c["id"], name="Gone Person", phone="7345550133").status_code == 201
    phones.refuse = None
    db = SessionLocal()
    try:
        assert sms.opted_out(db, "+17345550133")
    finally:
        db.close()


def test_the_consent_screen_promises_texts_and_the_first_text_says_how_to_stop(client, phones):
    riley = _named(client, "Riley Chen")
    me = client.get("/api/me", headers=as_(riley)).json()
    r = client.post("/api/my/consent", json={"version": me["consent"]["version"]}, headers=as_(riley))
    assert r.status_code == 200
    db = SessionLocal()
    try:
        number = db.get(User, riley).phone
    finally:
        db.close()
    welcome = phones.to(number)
    assert len(welcome) == 1 and "STOP" in welcome[0]
    # Accepting again does not text again.
    client.post("/api/my/consent", json={"version": me["consent"]["version"]}, headers=as_(riley))
    assert len(phones.to(number)) == 1


# --------------------------------------------------------------------------- #
# The admin's Texts page
# --------------------------------------------------------------------------- #

def test_admin_sees_the_setup_sends_a_test_and_reads_the_log(client, ids, phones):
    assert client.get("/api/sms/setup", headers=as_(ids["officer"])).status_code == 403
    setup = client.get("/api/sms/setup", headers=as_(ids["admin"])).json()
    assert setup["configured"] is True
    assert setup["inbound_url"] == "https://app.example/api/sms/inbound"
    assert setup["status_url"] == "https://app.example/api/sms/status"
    assert setup["sender"] == "(734) 555 ... 0000"

    r = client.post("/api/sms/test", json={"to": "734-555-0170"}, headers=as_(ids["admin"]))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "sent" and r.json()["to"] == "(734) 555 ... 0170"
    body = phones.to("+17345550170")[0]
    assert body.startswith("MeetingsCount:") and "test" in body.lower() and "STOP" in body

    log = client.get("/api/sms/log", headers=as_(ids["admin"])).json()
    assert log[0]["purpose"] == "test" and log[0]["to"] == "(734) 555 ... 0170"
    assert log[0]["who"] and log[0]["status"] == "sent"
    assert client.get("/api/sms/log", headers=as_(ids["officer"])).status_code == 403
    assert client.post("/api/sms/test", json={"to": "12"}, headers=as_(ids["admin"])).status_code == 422


def test_a_test_text_is_refused_while_texting_is_off(client, ids, no_phones):
    setup = client.get("/api/sms/setup", headers=as_(ids["admin"])).json()
    assert setup["configured"] is False
    r = client.post("/api/sms/test", json={"to": "734-555-0170"}, headers=as_(ids["admin"]))
    assert r.status_code == 503 and no_phones.sent == []


# --------------------------------------------------------------------------- #
# The preview: demo on, Twilio on, an allow list
# --------------------------------------------------------------------------- #

def test_the_demo_only_texts_numbers_on_the_allow_list(client, ids, demo_phones):
    dana = _named(client, "Dana Ruiz")
    m = _meeting(client, ids["officer"], name="Allow list")
    c = _checked_in(client, dana, m["id"])

    # Anyone can sign in to a demo, so a stranger's number gets nothing. The
    # link shows on screen, as it does with texting off.
    r = _ask(client, dana, c["id"])
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is False and "/confirm/" in r.json()["demo_link"]

    # A number on the list gets the real text, and no link on screen.
    r = _ask(client, dana, c["id"], name="Joe Tester", phone="734.555.0170")
    assert r.status_code == 201, r.text
    assert r.json()["sent"] is True and "demo_link" not in r.json()
    assert len(demo_phones.to(ALLOWED)) == 1 and _link(demo_phones.to(ALLOWED)[0])
    assert {to for to, _ in demo_phones.sent} == {ALLOWED}, "Dana's receipt is not on the list either"

    log = client.get("/api/sms/log", headers=as_(ids["admin"])).json()
    stranger = next(x for x in log if x["to"] == "(734) 555 ... 0199" and x["purpose"] == "ask")
    assert stranger["status"] == "skipped" and "MC_SMS_ALLOW" in stranger["error"]


def test_a_demo_sign_in_code_shows_on_screen_unless_the_number_is_listed(client, demo_phones, monkeypatch):
    monkeypatch.setattr(settings, "DEV_AUTH", False)
    r = client.post("/api/auth/code/request", json={"phone": "+17345550102"})
    assert r.json()["delivered"] is False and "demo_code" in r.json()
    assert demo_phones.sent == []
    # A listed number that is nobody's gets the same answer as any real one.
    r = client.post("/api/auth/code/request", json={"phone": ALLOWED})
    assert r.json()["delivered"] is True and "demo_code" not in r.json()
    assert demo_phones.sent == []


def test_the_texts_page_names_the_allow_list(client, ids, demo_phones):
    setup = client.get("/api/sms/setup", headers=as_(ids["admin"])).json()
    assert setup["demo"] is True and setup["allow"] == ["(734) 555 ... 0170"]
    r = client.post("/api/sms/test", json={"to": "734-555-0171"}, headers=as_(ids["admin"]))
    assert r.json()["status"] == "skipped" and demo_phones.sent == []
    r = client.post("/api/sms/test", json={"to": "734-555-0170"}, headers=as_(ids["admin"]))
    assert r.json()["status"] == "sent" and len(demo_phones.to(ALLOWED)) == 1


def test_the_allow_list_is_ignored_outside_demo_mode(client, ids, phones, monkeypatch):
    monkeypatch.setattr(settings, "SMS_ALLOW", "(734) 555-0170")
    assert client.get("/api/sms/setup", headers=as_(ids["admin"])).json()["allow"] is None
    r = client.post("/api/sms/test", json={"to": "734-555-0171"}, headers=as_(ids["admin"]))
    assert r.json()["status"] == "sent"


def test_the_preview_password_does_not_stand_in_twilios_way(client, phones, monkeypatch):
    monkeypatch.setattr(settings, "ACCESS_PASSWORD", "door")
    assert client.get("/api/sms/setup").status_code == 401
    params = {"MessageSid": "SMnobody", "MessageStatus": "delivered"}
    assert _twilio_post(client, "/api/sms/status", params).status_code == 200
    assert client.post("/api/sms/inbound", data={"From": "+17345550199", "Body": "YES"}).status_code == 403
