# MeetingsCount

Verifiable attendance for court-ordered meetings. A participant taps check in,
their phone's GPS pins the location, the server scores it against the meeting's
geofence and time window, and anything the engine can't settle goes to a human
before it affects anyone's compliance.

This is v3: real schema, roles, compliance tracking, and a review queue.

## Run it

```bash
pip install -r requirements.txt
python -m app.seed                  # one county, 10 participants, 9 weeks of history
uvicorn app.main:app --reload
```

| URL | Who it's for |
|---|---|
| `/officer` | Probation officer: caseload, review queue, participant records, reports |
| `/` | Participant app (installable on a phone): consent, one-tap check-in and check-out, this week's progress, full history with every reason and every officer decision |
| `/ops` | Developer: map of every check-in, and a simulator that needs no phone |
| `/docs` | API reference |

Dev auth is on, so there's no login. On `/officer`, pick a staff member from
the dropdown in the top right. On `/`, tap the yellow **Demo** chip to switch
participants. Each page remembers its own person, so being Aisha on the phone
page never changes who you are on the dashboard.

```bash
python -m pytest                    # 40 tests
```

### With Postgres, the way production runs

```bash
docker compose up -d
export MC_DATABASE_URL=postgresql+psycopg2://mc:mc@localhost:5432/meetingscount
alembic upgrade head
python -m app.seed
uvicorn app.main:app --reload
```

## What's in the demo county

Ten participants with recognizable stories, so every state the dashboard can
show actually occurs:

| Participant | What they demonstrate |
|---|---|
| Marcus Webb, Dana Ruiz, Jenna Park | Steady compliance across two requirements |
| Sam Okafor | Attended for five weeks, then stopped. The trend chart shows the drop |
| Tyler Nowak | Meets the meeting requirement, misses community service |
| Aisha Bello | Works downtown; a third of her check-ins flag on GPS accuracy alone |
| Chris Lindqvist | One rejected check-in he has disputed, with his note attached |
| Devon Hargrove | Consistently late and near the edge of the radius: nothing fails, confidence stays low |
| Riley Chen | Enrolled four days ago, no history yet |
| Pat Moreau | Attends, but forgets to check out and once used a VPN |
| Jordan Ellis | Signed up today: sees the consent screen first, no history yet |

## How a check-in is judged

Every meeting has a lat/lng, a start time, a radius (500 ft by default, wider
downtown), and a check-in window (±60 min). When someone checks in:

1. The phone sends its coordinates and an accuracy figure.
2. The server stamps the time. The phone's clock is not trusted.
3. Each rule in `app/verification.py` scores it and writes a plain-English reason.
4. The weighted average becomes a confidence score, and the check-in becomes
   **confirmed** (all rules passed, confidence ≥ 70), **rejected** (a hard rule
   failed), or **flagged** (something looked off).

| Rule | Hard? | What it checks | Weight |
|---|---|---|---|
| distance | yes | inside `radius_ft`; score tapers toward the edge | 3 |
| time_window | yes | within ±`time_window_min` of the start; late costs points | 2 |
| gps_accuracy | no | a wide accuracy circle lowers confidence | 1.5 |
| network | no | VPN/proxy hook; neutral until an IP intelligence provider is wired in | 1.5 |
| checkout_distance | no | added at check-out when a second location sample is sent | 0 |

**Flagged and rejected are not verdicts.** They go to the review queue, an
officer decides with the evidence in front of them, and only that decision
affects compliance. Add a rule by writing a class with an `evaluate(ctx)`
method and listing it in `DEFAULT_CRITERIA`.

## Compliance

A requirement says "3 public meetings a week from Sep 1 to Dec 31". For each
period, the system counts check-ins that count:

- confirmed → counts
- flagged, approved on review → counts
- flagged, not yet reviewed → shown separately as pending, not counted
- rejected, or denied on review → does not count

One meeting counts once. Someone rejected from the parking lot who walks in
and checks in again gets the better of the two results, not two credits, and a
second tap on a meeting already checked into returns the same check-in.
Periods before a requirement began never appear in history, so no report can
show someone as short for a week their order did not cover.

Weeks run Monday to Sunday in the county's timezone, not UTC, because that is
how an officer thinks about a week. Meetings a participant created themselves
never count toward a court requirement, only toward their own tracking.

## Layout

```
app/
  config.py        settings from the environment
  models.py        the schema: counties, programs, users, participants,
                   requirements, meetings, check-ins, reviews, disputes, audit
  db.py            SQLAlchemy engine; the only file that knows the database
  auth.py          dev auth + Cognito JWT verification, roles, tenancy, audit
  verification.py  the criteria engine
  compliance.py    requirements -> is this person compliant?
  seed.py          the demo county
  routers/         me (the signed-in person's own record), meetings, checkins,
                   caseload, reviews, reports, admin
static/
  officer.html     the officer dashboard
  index.html       the participant app shell; the app is static/app/
  app/             participant.js, participant.css, lib.js (shared helpers)
  sw.js            service worker: caches the app shell, never API responses
  ops.html         map and simulator
alembic/           migrations, including Postgres row-level security
infra/             CDK stack for AWS, not deployed yet. See infra/README.md
tests/
```

## Security posture

The plan document has the full reasoning. What is actually enforced in this
code today:

| Principle | Where it lives |
|---|---|
| Location read once, at the tap, never in the background | `static/index.html`, and the consent text on that page |
| The court sees outcomes, not coordinates | `schemas.checkin_out(include_pin=...)`: pins are returned only for a check-in under review, or to the participant themselves |
| Bulk location data is admin-only and audited | `/api/ops/checkins` |
| A human decides anything that hurts | `Review`; the engine never writes a compliance failure on its own |
| The participant sees everything about themselves | `/api/checkins/mine`, `/api/my/progress` |
| Every staff view of a record is logged | `auth.audit()`, called by every caseload and detail route |
| Least privilege | `auth.require(*roles)` on every staff route |
| Tenant isolation | `auth.scope_to_county()` plus Postgres row-level security in migration `002_rls` |
| Raw pins expire | `POST /api/retention/run` nulls coordinates past `MC_PIN_RETENTION_DAYS`, keeping the verdict |
| Reports never assert absence | `routers/reports.py`: "could not verify", never "did not attend" |
| Explainable | every verdict stores its reasons; they appear in the UI and in the report |
| Consent before the first check-in | `routers/me.py` records who agreed to which wording; `checkins.require_consent` refuses a check-in without it |
| Personal meetings are the participant's alone | staff lists, the review queue, caseload, reports and the ops map all exclude them |
| No location to third parties from the phone | the participant result shows a drawn diagram, not map tiles |
| Personal data never cached | `main.cache_rules`: every `/api/` response is `no-store` |

### Before real people's data goes in

- `MC_ENV=production` refuses to boot unless `MC_DEMO=0` and `MC_DEV_AUTH=0`.
  That is the guard, but check it rather than trusting it.
- Wire Cognito: set `MC_COGNITO_USER_POOL_ID` and `MC_COGNITO_CLIENT_ID`. The
  verification code is in `auth.py` and needs `python-jose`, already in
  requirements.
- Encrypt `users.phone` at the column level with a KMS data key. The field is
  marked in `models.py`; it is stored in plain text today.
- Browsers only hand out GPS over HTTPS. For phone testing, `ngrok http 8000`.
- Connect as a non-superuser Postgres role and `SET LOCAL app.county_id` per
  request, or the row-level security policies do nothing.
- GPS can be spoofed at the OS level and the server cannot tell. Accuracy,
  network signals and attendance history are how confidence accumulates; no
  single rule is proof, which is why a human is in the loop.

## What's next

Roughly in order: Cognito wired up, the rotating meeting code (a printed sheet
with a code that changes every few minutes, which is the cheapest strong
presence signal available), an IP intelligence provider behind
`NetworkCriterion`, SMS check-out reminders through SNS, and the deploy.
