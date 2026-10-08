"""
The meeting directory: recurring meetings imported from an area's list.

Three jobs:

  import_rows   take rows from a spreadsheet and keep the directory in step
                with them, by each meeting's page URL
  occurrences   turn "Thursdays at 7" into the Meeting rows for a date range,
                creating the ones that do not exist yet
  nearby and search
                what a phone or a dashboard asks for

A directory meeting belongs to no county (county_id null) and counts toward
a requirement like a staff-made one: the source is directory, and only the
directory and staff sources count. A check-in from a phone always points at
one Meeting row, one date, so nothing downstream knows about recurrence.
"""
from __future__ import annotations

import csv
import hashlib
import math
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .geo import haversine_m
from .models import Meeting, MeetingCategory, MeetingSeries, MeetingSource

MILE_M = 1609.344
DEFAULT_WITHIN_MI = 25
MAX_WITHIN_MI = 60

WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
            "saturday": 5, "sunday": 6}

# What a column may be called. Matched after lower-casing and dropping
# everything but letters, so "Start_time_formatted" and "Start time" agree.
COLUMNS = {
    "name": ("meetingname", "name", "meeting"),
    "url": ("uniqueurlidentification", "url", "uniqueurl", "link", "sourceurl", "page"),
    "day": ("day", "weekday", "dayofweek"),
    "start": ("starttimeformatted", "starttime", "start", "time", "timeformatted"),
    "end": ("endtime", "end"),
    "duration": ("duration", "durationmin", "length", "minutes"),
    "street": ("street", "address", "address1", "streetaddress"),
    "city": ("city", "town"),
    "state": ("state",),
    "zip": ("zipcode", "zip", "postalcode", "postcode"),
    "lat": ("latitude", "lat"),
    "lng": ("longitude", "lng", "lon", "long"),
}


# --------------------------------------------------------------------------- #
# Parsing one spreadsheet row
# --------------------------------------------------------------------------- #

def _squash(s: str) -> str:
    return re.sub(r"[^a-z]", "", str(s or "").lower())


def map_columns(headers: list[str]) -> dict[str, str]:
    """Our field name -> the header the file uses, for the headers we recognise."""
    found = {}
    squashed = {_squash(h): h for h in headers}
    for field_name, names in COLUMNS.items():
        for n in names:
            if n in squashed:
                found[field_name] = squashed[n]
                break
    return found


def parse_weekday(value) -> int | None:
    s = _squash(value)
    for name, n in WEEKDAYS.items():
        if s == name or (len(s) >= 3 and name.startswith(s[:3]) and s == name[:len(s)]):
            return n
    if s.isdigit() and 0 <= int(s) <= 6:
        return int(s)
    return None


def parse_minutes(value) -> int | None:
    """
    Minutes after midnight from an Excel time (a fraction of a day, which
    is how a spreadsheet stores 7:00 PM), '19:00', '7:00 PM' or '7pm'.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        f = float(s)
        if 0 <= f < 1.0000001:
            return int(round(f * 1440)) % 1440
        if 1 <= f < 24 and "." not in s:                # a bare hour, 24h
            return int(f) * 60
    except ValueError:
        pass
    m = re.match(r"^\s*(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?\s*$", s, re.I)
    if not m:
        return None
    h, mins, ap = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ap.startswith("p") and h != 12:
        h += 12
    if ap.startswith("a") and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        return None
    return h * 60 + mins


def _float(value) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def external_key(source_name: str, url: str | None, name: str, weekday: int, start: int,
                 lat: float, lng: float) -> str:
    """
    What makes a row the same meeting next import. The page URL when the list
    has one per meeting; otherwise the name, day, time and place.
    """
    if url:
        return f"{source_name}:{url.strip().rstrip('/')}"[:120]
    raw = f"{source_name}|{name.strip().lower()}|{weekday}|{start}|{lat:.4f}|{lng:.4f}"
    return f"{source_name}:{hashlib.sha1(raw.encode()).hexdigest()}"[:120]


@dataclass
class ParsedRow:
    name: str
    url: str | None
    weekday: int
    start_minute: int
    duration_min: int
    street: str
    city: str | None
    state: str
    zip: str | None
    lat: float
    lng: float


def parse_row(row: dict, cols: dict[str, str]) -> ParsedRow | str:
    """A ParsedRow, or the reason this row cannot be a meeting people check in to."""
    get = lambda k: str(row.get(cols[k], "") or "").strip() if k in cols else ""   # noqa: E731
    name = " ".join(get("name").split())[:200]
    if not name:
        return "no name"
    weekday = parse_weekday(get("day"))
    if weekday is None:
        return f"no weekday ({get('day') or 'blank'})"
    start = parse_minutes(get("start"))
    if start is None:
        return f"no start time ({get('start') or 'blank'})"
    lat, lng = _float(get("lat")), _float(get("lng"))
    if lat is None or lng is None or not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return "no coordinates"
    street = get("street")[:120]
    if not street or re.search(r"\b(online|zoom|virtual|phone|teleconference)\b", street + " " + name, re.I):
        return "online or no street address"
    duration = None
    if get("duration"):
        d = _float(get("duration"))
        duration = int(d) if d and 5 <= d <= 1440 else None
    if duration is None and get("end"):
        end = parse_minutes(get("end"))
        if end is not None:
            duration = (end - start) % 1440 or 60
    duration = duration or 60
    return ParsedRow(name=name, url=get("url")[:300] or None, weekday=weekday, start_minute=start,
                     duration_min=duration, street=street, city=get("city")[:80] or None,
                     state=(get("state")[:2].upper() or "MI"), zip=get("zip")[:10] or None,
                     lat=lat, lng=lng)


# --------------------------------------------------------------------------- #
# Importing
# --------------------------------------------------------------------------- #

@dataclass
class ImportReport:
    source: str
    created: int = 0
    updated: int = 0
    deactivated: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)

    @property
    def kept(self) -> int:
        return self.created + self.updated

    def summary(self) -> str:
        lines = [f"{self.source}: {self.created} new, {self.updated} updated, "
                 f"{len(self.skipped)} skipped, {self.deactivated} no longer listed"]
        reasons: dict[str, int] = {}
        for _, why in self.skipped:
            reasons[why] = reasons.get(why, 0) + 1
        for why, n in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  skipped {n}: {why}")
        return "\n".join(lines)


def source_from_url(url: str | None) -> str | None:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1).lower()[:60] if m else None


def import_rows(db: Session, rows: list[dict], *, source_name: str | None = None,
                prune: bool = False, timezone_name: str = "America/Detroit",
                county_id: int | None = None, now: datetime | None = None) -> ImportReport:
    """
    Bring the directory in step with `rows` (dicts keyed by the file's own
    headers). A row already known by its key is updated; a new one is added;
    with `prune`, a meeting from this source missing from the file is marked
    inactive, never deleted, because check-ins may point at it.
    """
    now = now or datetime.now(timezone.utc)
    headers = list(rows[0].keys()) if rows else []
    cols = map_columns(headers)
    for needed in ("name", "day", "start", "lat", "lng"):
        if needed not in cols:
            raise ValueError(f"no column for {needed}; headers are {headers}")
    if not source_name:
        for r in rows:
            source_name = source_from_url(str(r.get(cols.get("url", ""), "") or ""))
            if source_name:
                break
        source_name = source_name or "import"
    report = ImportReport(source=source_name)

    existing = {s.external_key: s for s in db.scalars(
        select(MeetingSeries).where(MeetingSeries.source_name == source_name))}
    seen: set[str] = set()
    for n, row in enumerate(rows, start=2):                  # row 1 is the header
        parsed = parse_row(row, cols)
        if isinstance(parsed, str):
            report.skipped.append((n, parsed))
            continue
        key = external_key(source_name, parsed.url, parsed.name, parsed.weekday,
                           parsed.start_minute, parsed.lat, parsed.lng)
        if key in seen:
            report.skipped.append((n, "same meeting listed twice"))
            continue
        seen.add(key)
        s = existing.get(key)
        if s is None:
            s = MeetingSeries(external_key=key, source_name=source_name, county_id=county_id,
                              category=MeetingCategory.public, timezone=timezone_name,
                              imported_at=now)
            db.add(s)
            existing[key] = s
            report.created += 1
        else:
            report.updated += 1
        s.name, s.source_url = parsed.name, parsed.url
        s.weekday, s.start_minute, s.duration_min = parsed.weekday, parsed.start_minute, parsed.duration_min
        s.street, s.city, s.state, s.zip = parsed.street, parsed.city, parsed.state, parsed.zip
        s.lat, s.lng = parsed.lat, parsed.lng
        s.active, s.last_seen_at = True, now
    if prune:
        for key, s in existing.items():
            if key not in seen and s.active:
                s.active = False
                report.deactivated += 1
    db.commit()
    return report


def read_rows(path: str) -> list[dict]:
    """Rows from a .csv or .xlsx, keyed by the header row. No extra packages."""
    if path.lower().endswith(".csv"):
        with open(path, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    return _read_xlsx(path)


def _read_xlsx(path: str) -> list[dict]:
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    rel = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
    z = zipfile.ZipFile(path)
    strings: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in si.iter(ns + "t")) for si in root.findall(ns + "si")]
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    first = next(iter(workbook.iter(ns + "sheet")))
    target = rels[first.get(rel + "id")]
    sheet = ET.fromstring(z.read(target.lstrip("/") if target.startswith("/") else "xl/" + target))

    def cell_value(c):
        v = c.find(ns + "v")
        if v is None:
            inline = c.find(ns + "is")
            return "".join(t.text or "" for t in inline.iter(ns + "t")) if inline is not None else ""
        return strings[int(v.text)] if c.get("t") == "s" else (v.text or "")

    def column_of(ref: str) -> str:
        return re.sub(r"\d", "", ref or "")

    rows_xml = list(sheet.iter(ns + "row"))
    if not rows_xml:
        return []
    headers = {column_of(c.get("r")): cell_value(c).strip() for c in rows_xml[0]}
    out = []
    for r in rows_xml[1:]:
        d = {}
        for c in r:
            col = column_of(c.get("r"))
            if headers.get(col):
                d[headers[col]] = cell_value(c)
        if any(str(v).strip() for v in d.values()):
            out.append({h: d.get(h, "") for h in headers.values() if h})
    return out


# --------------------------------------------------------------------------- #
# Occurrences
# --------------------------------------------------------------------------- #

def occurrences(db: Session, series: list[MeetingSeries], since: datetime,
                until: datetime) -> list[Meeting]:
    """
    The Meeting rows for every time each series meets in [since, until),
    creating the ones that do not exist yet. Idempotent: the unique index on
    (series_id, starts_at) is the guarantee, and this checks first anyway.
    """
    if not series:
        return []
    wanted: dict[tuple[int, datetime], MeetingSeries] = {}
    for s in series:
        zone = ZoneInfo(s.timezone or "America/Detroit")
        day = since.astimezone(zone).date() - timedelta(days=1)
        last = until.astimezone(zone).date() + timedelta(days=1)
        while day <= last:
            if day.weekday() == s.weekday:
                local = datetime(day.year, day.month, day.day, s.start_minute // 60,
                                 s.start_minute % 60, tzinfo=zone)
                start = local.astimezone(timezone.utc)
                if since <= start < until:
                    wanted[(s.id, start)] = s
            day += timedelta(days=1)
    ids = list({sid for sid, _ in wanted})
    have = {(m.series_id, m.starts_at): m for m in db.scalars(
        select(Meeting).where(Meeting.series_id.in_(ids or [-1]),
                              Meeting.starts_at >= since, Meeting.starts_at < until))}
    made = []
    for (sid, start), s in wanted.items():
        if (sid, start) in have:
            continue
        m = Meeting(county_id=None, series_id=s.id, name=s.name, category=s.category,
                    source=MeetingSource.directory, address=s.address, lat=s.lat, lng=s.lng,
                    starts_at=start, duration_min=s.duration_min, radius_ft=s.radius_ft,
                    time_window_min=s.time_window_min, created_by_id=s.created_by_id)
        db.add(m)
        made.append(m)
    if made:
        db.commit()
    return list(have.values()) + made


# --------------------------------------------------------------------------- #
# Nearby, search, places
# --------------------------------------------------------------------------- #

def _box(lat: float, lng: float, miles: float) -> tuple[float, float, float, float]:
    dlat = miles * MILE_M / 111_320
    dlng = dlat / max(0.2, math.cos(math.radians(lat)))
    return lat - dlat, lat + dlat, lng - dlng, lng + dlng


def miles_between(lat1, lng1, lat2, lng2) -> float:
    return haversine_m(lat1, lng1, lat2, lng2) / MILE_M


def _visible(q, county_id: int | None):
    """The shared directory plus anything a county imported for itself."""
    if county_id is None:
        return q.where(MeetingSeries.county_id.is_(None))
    return q.where((MeetingSeries.county_id.is_(None)) | (MeetingSeries.county_id == county_id))


def nearby(db: Session, lat: float, lng: float, within_mi: float = DEFAULT_WITHIN_MI, *,
           county_id: int | None = None) -> list[tuple[MeetingSeries, float]]:
    """Active series within `within_mi` of a point, with the distance, nearest first."""
    within_mi = max(1, min(MAX_WITHIN_MI, within_mi))
    lo_lat, hi_lat, lo_lng, hi_lng = _box(lat, lng, within_mi)
    q = _visible(select(MeetingSeries).where(
        MeetingSeries.active.is_(True), MeetingSeries.lat.between(lo_lat, hi_lat),
        MeetingSeries.lng.between(lo_lng, hi_lng)), county_id)
    out = []
    for s in db.scalars(q):
        d = miles_between(lat, lng, s.lat, s.lng)
        if d <= within_mi:
            out.append((s, d))
    out.sort(key=lambda sd: sd[1])
    return out


def search(db: Session, q: str | None = None, *, weekday: int | None = None,
           near: tuple[float, float] | None = None, within_mi: float = MAX_WITHIN_MI,
           county_id: int | None = None, limit: int = 60) -> list[tuple[MeetingSeries, float | None]]:
    """Name, town or zip, optionally one weekday, optionally near a point."""
    if near:
        rows = nearby(db, near[0], near[1], within_mi, county_id=county_id)
    else:
        rows = [(s, None) for s in db.scalars(_visible(
            select(MeetingSeries).where(MeetingSeries.active.is_(True)), county_id))]
    words = [w for w in re.split(r"\s+", (q or "").strip().lower()) if w]

    def matches(s: MeetingSeries) -> bool:
        if weekday is not None and s.weekday != weekday:
            return False
        hay = " ".join(x for x in (s.name, s.street, s.city, s.zip) if x).lower()
        return all(w in hay for w in words)

    out = [(s, d) for s, d in rows if matches(s)]
    if not near:
        out.sort(key=lambda sd: (sd[0].weekday, sd[0].start_minute, sd[0].name.lower()))
    return out[:limit]


def places(db: Session, q: str | None = None, *, county_id: int | None = None,
           limit: int = 200) -> list[dict]:
    """Towns in the directory, with how many meetings each has and where its middle is."""
    stmt = _visible(select(MeetingSeries.city, MeetingSeries.state, func.count(MeetingSeries.id),
                           func.avg(MeetingSeries.lat), func.avg(MeetingSeries.lng))
                    .where(MeetingSeries.active.is_(True), MeetingSeries.city.is_not(None)), county_id)
    stmt = stmt.group_by(MeetingSeries.city, MeetingSeries.state)
    out = []
    needle = (q or "").strip().lower()
    for city, state, n, lat, lng in db.execute(stmt):
        if needle and needle not in city.lower():
            continue
        out.append({"city": city, "state": state, "label": f"{city}, {state}", "meetings": n,
                    "lat": round(lat, 5), "lng": round(lng, 5)})
    out.sort(key=lambda p: (-p["meetings"], p["city"]))
    return out[:limit]


def nearest_place(db: Session, lat: float, lng: float, *, county_id: int | None = None) -> dict | None:
    best, best_d = None, None
    for p in places(db, county_id=county_id, limit=10_000):
        d = miles_between(lat, lng, p["lat"], p["lng"])
        if best is None or d < best_d:
            best, best_d = p, d
    if best is not None:
        best = best | {"miles": round(best_d, 1)}
    return best


def stats(db: Session, *, county_id: int | None = None) -> dict:
    rows = db.execute(_visible(select(MeetingSeries.source_name, func.count(MeetingSeries.id),
                                      func.max(MeetingSeries.last_seen_at))
                               .where(MeetingSeries.active.is_(True)), county_id)
                      .group_by(MeetingSeries.source_name)).all()
    return {"meetings": sum(n for _, n, _ in rows),
            "sources": [{"name": name, "meetings": n,
                         "last_import": (seen.replace(tzinfo=timezone.utc) if seen and seen.tzinfo is None
                                         else seen).isoformat() if seen else None}
                        for name, n, seen in rows]}


def series_out(s: MeetingSeries, distance_mi: float | None = None) -> dict:
    return {
        "id": s.id, "name": s.name, "category": s.category.value, "source": s.source_name,
        "url": s.source_url, "weekday": s.weekday, "start_minute": s.start_minute,
        "duration_min": s.duration_min, "timezone": s.timezone,
        "street": s.street, "city": s.city, "state": s.state, "zip": s.zip,
        "address": s.address, "lat": s.lat, "lng": s.lng,
        "radius_ft": s.radius_ft, "time_window_min": s.time_window_min,
        "miles": round(distance_mi, 1) if distance_mi is not None else None,
    }
