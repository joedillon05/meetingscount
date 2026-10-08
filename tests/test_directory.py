"""
The meeting directory: importing an area's list, turning weekly meetings into
dated ones, and what a participant sees near the town they chose.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from app import directory
from app.db import SessionLocal
from app.models import Meeting, MeetingSeries

from .conftest import as_

NOW = datetime.now(timezone.utc)

# Rows the way a spreadsheet hands them over: the file's own headers, times
# as fractions of a day, everything a string.
ROWS = [
    {"Meeting Name": "Thursday Night Step", "Unique url - identification": "https://aa-semi.org/meetings/thursday-night-step/",
     "Day": "Thursday", "Start_time_formatted": "0.79166666666666663", "End_Time": "0.83333333333333326",
     "Street": "306 N Division St", "City": "Ann Arbor", "State": "MI", "Zip Code": "48104",
     "latitude": "42.2830", "longitude": "-83.7455"},
    {"Meeting Name": "Noon Reflections", "Unique url - identification": "https://aa-semi.org/meetings/noon-reflections/",
     "Day": "Monday", "Start_time_formatted": "0.5", "End_Time": "0.54166666666666663",
     "Street": "220 N Huron St", "City": "Ypsilanti", "State": "MI", "Zip Code": "48197",
     "latitude": "42.2450", "longitude": "-83.6130"},
    {"Meeting Name": "Far Away Group", "Unique url - identification": "https://aa-semi.org/meetings/far-away/",
     "Day": "Tuesday", "Start_time_formatted": "0.75", "End_Time": "0.79166666666666663",
     "Street": "431 17th St", "City": "Port Huron", "State": "MI", "Zip Code": "48060",
     "latitude": "42.9787", "longitude": "-82.4453"},
    {"Meeting Name": "Zoom Only Serenity", "Unique url - identification": "https://aa-semi.org/meetings/zoom-only/",
     "Day": "Sunday", "Start_time_formatted": "0.79166666666666663", "End_Time": "0.83333333333333326",
     "Street": "", "City": "Detroit", "State": "MI", "Zip Code": "48226",
     "latitude": "42.3317", "longitude": "-83.0466"},
    {"Meeting Name": "No Day Given", "Unique url - identification": "https://aa-semi.org/meetings/no-day/",
     "Day": "", "Start_time_formatted": "0.75", "End_Time": "0.79166666666666663",
     "Street": "1 Main St", "City": "Detroit", "State": "MI", "Zip Code": "48226",
     "latitude": "42.33", "longitude": "-83.05"},
]


def _db():
    return SessionLocal()


def _named(client, name):
    return next(u["id"] for u in client.get("/api/users").json() if u["name"] == name)


# --------------------------------------------------------------------------- #
# Reading a spreadsheet
# --------------------------------------------------------------------------- #

def test_times_and_days_in_the_forms_spreadsheets_use():
    assert directory.parse_minutes("0.79166666666666663") == 19 * 60
    assert directory.parse_minutes("0.5") == 12 * 60
    assert directory.parse_minutes("0") == 0
    assert directory.parse_minutes("19:30") == 19 * 60 + 30
    assert directory.parse_minutes("7:30 PM") == 19 * 60 + 30
    assert directory.parse_minutes("12:00 AM") == 0
    assert directory.parse_minutes("12 pm") == 12 * 60
    assert directory.parse_minutes("7pm") == 19 * 60
    assert directory.parse_minutes("") is None and directory.parse_minutes("noon") is None
    assert directory.parse_weekday("Thursday") == 3 and directory.parse_weekday("thu") == 3
    assert directory.parse_weekday("SUNDAY") == 6 and directory.parse_weekday("") is None


def test_columns_are_matched_by_name_however_they_are_spelled():
    cols = directory.map_columns(["Meeting Name", "Unique url - identification", "Day",
                                  "Start_time_formatted", "End_Time", "Street", "City", "State",
                                  "Zip Code", "latitude", "longitude"])
    assert cols["name"] == "Meeting Name" and cols["url"] == "Unique url - identification"
    assert cols["start"] == "Start_time_formatted" and cols["zip"] == "Zip Code"
    plain = directory.map_columns(["name", "url", "day", "start", "end", "street", "city",
                                   "state", "zip", "lat", "lng"])
    assert plain["lat"] == "lat" and plain["lng"] == "lng" and plain["end"] == "end"


def test_import_keeps_the_directory_in_step_with_the_file():
    db = _db()
    try:
        report = directory.import_rows(db, ROWS)
        assert report.source == "aa-semi.org"
        assert report.created == 3 and report.updated == 0
        reasons = [why for _, why in report.skipped]
        assert any("online" in r for r in reasons), "a meeting with no street is not one to check in to"
        assert any("weekday" in r for r in reasons)

        # The same file again changes nothing but the timestamps.
        again = directory.import_rows(db, ROWS)
        assert again.created == 0 and again.updated == 3
        assert db.query(MeetingSeries).filter_by(source_name="aa-semi.org").count() == 3

        # A corrected row updates the meeting in place; a dropped row is retired, not deleted.
        fixed = [dict(ROWS[0], **{"Meeting Name": "Thursday Night Steps", "Start_time_formatted": "0.8125"})]
        pruned = directory.import_rows(db, fixed, prune=True)
        assert pruned.updated == 1 and pruned.deactivated == 2
        s = db.query(MeetingSeries).filter_by(source_url=ROWS[0]["Unique url - identification"]).one()
        assert s.name == "Thursday Night Steps" and s.start_minute == 19 * 60 + 30 and s.active
        assert db.query(MeetingSeries).filter_by(active=False).count() == 2
        assert s.address == "306 N Division St, Ann Arbor, MI 48104"

        # And back, so the rest of these tests have three meetings.
        directory.import_rows(db, ROWS, prune=True)
        assert db.query(MeetingSeries).filter_by(active=True).count() == 3
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# From "Thursdays at 7" to dates
# --------------------------------------------------------------------------- #

def test_occurrences_land_on_the_right_local_evening_and_only_once():
    db = _db()
    try:
        directory.import_rows(db, ROWS)
        s = db.query(MeetingSeries).filter_by(name="Thursday Night Step").one()
        since, until = NOW - timedelta(days=1), NOW + timedelta(days=14)
        made = directory.occurrences(db, [s], since, until)
        assert len(made) == 2
        for m in made:
            local = m.starts_at.astimezone(ZoneInfo("America/Detroit"))
            assert local.weekday() == 3 and (local.hour, local.minute) == (19, 0)
            assert m.source.value == "directory" and m.counts_toward_requirement
            assert m.county_id is None and m.series_id == s.id and m.duration_min == 60
            assert m.address == "306 N Division St, Ann Arbor, MI 48104"
        again = directory.occurrences(db, [s], since, until)
        assert len(again) == 2 and {m.id for m in again} == {m.id for m in made}
        assert db.query(Meeting).filter_by(series_id=s.id).count() == 2
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# What a participant sees
# --------------------------------------------------------------------------- #

def test_participant_sees_meetings_near_their_town_and_staff_see_their_county(client, ids):
    db = _db()
    try:
        directory.import_rows(db, ROWS)
    finally:
        db.close()
    dana = _named(client, "Dana Ruiz")
    me = client.get("/api/me", headers=as_(dana)).json()
    assert me["area"]["label"] == "Ann Arbor, MI", "the demo seed starts everyone in Ann Arbor"

    since, until = quote((NOW - timedelta(hours=18)).isoformat()), quote((NOW + timedelta(days=8)).isoformat())
    rows = client.get(f"/api/meetings?since={since}&until={until}", headers=as_(dana)).json()
    names = {m["name"] for m in rows}
    assert "Thursday Night Step" in names and "Noon Reflections" in names
    assert "Far Away Group" not in names, "Port Huron is not near Ann Arbor"
    assert all(m["counts"] for m in rows if m.get("series_id"))

    staff = client.get(f"/api/meetings?since={since}&until={until}", headers=as_(ids["officer"])).json()
    assert not any(m.get("series_id") for m in staff), "the dashboard lists the county's own meetings"

    # Choosing a different town changes what is near.
    r = client.post("/api/my/area", json={"label": "Port Huron, MI", "lat": 42.9787, "lng": -82.4453},
                    headers=as_(dana))
    assert r.status_code == 200
    rows = client.get(f"/api/meetings?since={since}&until={until}", headers=as_(dana)).json()
    names = {m["name"] for m in rows}
    assert "Far Away Group" in names and "Thursday Night Step" not in names

    # A point with no label becomes the nearest town, so no exact spot is kept.
    r = client.post("/api/my/area", json={"lat": 42.2500, "lng": -83.6200}, headers=as_(dana))
    assert r.json()["label"] == "Ypsilanti, MI"
    me = client.get("/api/me", headers=as_(dana)).json()
    assert me["area"]["lat"] == 42.245 and me["area"]["lng"] == -83.613

    assert client.delete("/api/my/area", headers=as_(dana)).status_code == 204
    rows = client.get(f"/api/meetings?since={since}&until={until}", headers=as_(dana)).json()
    assert not any(m.get("series_id") for m in rows), "no town chosen, no directory"
    client.post("/api/my/area", json={"label": "Ann Arbor, MI", "lat": 42.2808, "lng": -83.7430},
                headers=as_(dana))


def test_a_check_in_at_a_directory_meeting_counts(client, ids):
    db = _db()
    try:
        directory.import_rows(db, ROWS)
        s = db.query(MeetingSeries).filter_by(name="Thursday Night Step").one()
        # Make this week's Thursday meeting start right now, so a check-in is on time.
        made = directory.occurrences(db, [s], NOW - timedelta(days=1), NOW + timedelta(days=8))
        m = made[0]
        m.starts_at = NOW
        db.commit()
        mid = m.id
    finally:
        db.close()
    jenna = _named(client, "Jenna Park")
    r = client.post("/api/checkins", json={"meeting_id": mid, "lat": 42.2831, "lng": -83.7454,
                                           "accuracy_m": 8}, headers=as_(jenna))
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["status"] == "confirmed" and c["meeting"]["counts"] is True
    mine = next(x for x in client.get("/api/checkins/mine", headers=as_(jenna)).json() if x["id"] == c["id"])
    assert mine["meeting"]["name"] == "Thursday Night Step"
    # And the officer can see it on the record, with no coordinates.
    pid = client.get("/api/me", headers=as_(jenna)).json()["participant_id"]
    rec = client.get(f"/api/participants/{pid}", headers=as_(ids["officer"])).json()
    ev = next(e for e in rec["timeline"] if e["id"] == c["id"])
    assert "lat" not in ev


def test_search_and_places(client, ids):
    db = _db()
    try:
        directory.import_rows(db, ROWS)
    finally:
        db.close()
    dana = _named(client, "Dana Ruiz")
    hits = client.get("/api/directory?q=step", headers=as_(dana)).json()
    assert [h["name"] for h in hits] == ["Thursday Night Step"]
    assert hits[0]["miles"] is not None, "a participant with a town sees distances"
    assert client.get("/api/directory?q=step&day=1", headers=as_(dana)).json() == []
    assert client.get("/api/directory?q=48197", headers=as_(ids["officer"])).json()[0]["city"] == "Ypsilanti"

    places = client.get("/api/directory/places", headers=as_(dana)).json()
    assert {p["city"] for p in places} >= {"Ann Arbor", "Ypsilanti", "Port Huron"}
    assert client.get("/api/directory/places?q=ypsi", headers=as_(dana)).json()[0]["label"] == "Ypsilanti, MI"

    assert client.get("/api/directory/stats", headers=as_(dana)).status_code == 403
    st = client.get("/api/directory/stats", headers=as_(ids["officer"])).json()
    assert st["meetings"] >= 3 and st["sources"][0]["name"] == "aa-semi.org"


def test_reseeding_keeps_the_directory():
    from app.seed import seed
    db = _db()
    try:
        directory.import_rows(db, ROWS)
        before = db.query(MeetingSeries).count()
    finally:
        db.close()
    seed(reset=True)
    db = _db()
    try:
        assert db.query(MeetingSeries).count() == before
    finally:
        db.close()
