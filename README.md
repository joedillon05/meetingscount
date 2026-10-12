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
| `/officer` | Probation officer: caseload, review queue, participant records, reports. Admins also get the audit log |
| `/` | Participant app (installable on a phone): consent, one-tap check-in and check-out, this week's progress, full history with every reason and every officer decision |
| `/confirm/<token>` | The person a participant asked to confirm a check-in: one link from a text, no sign-in |
| `/ops` | Developer: map of every check-in, and a simulator that needs no phone |
| `/docs` | API reference |

Dev auth is on, so there's no login. On `/officer`, pick a staff member from
the dropdown in the top right. On `/`, tap the yellow **Demo** chip to switch
participants. Each page remembers its own person, so being Aisha on the phone
page never changes who you are on the dashboard.

```bash
python -m pytest                    # 54 tests
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

## The review queue

Three tabs: **To decide**, **Waiting on participant**, and **Decided** (last
14 days). Opening an item shows the map, each signal against its limit, what
counting or not counting would do to that person's week, how check-ins
usually go at that venue for everyone (aggregates only, nobody else named),
and the person's own recent record.

Three decisions, and the participant sees every note:

| Decision | Note | What happens |
|---|---|---|
| Count it | optional | counts toward the requirement |
| Don't count it | required | does not count |
| Ask a question | required, it is the question | pending, never a miss. The item waits in the second tab; when the participant answers on their phone it comes back to the top of **To decide** |

Reviews are append-only (`app/reviewing.py`). Changing a decision needs a
reason and adds a row; the earlier decision stays, and the participant sees
both. Decisions wait six seconds before they are sent, with an Undo, so a
stray keystroke cannot count or fail anyone; anything still waiting is sent
at once if the page closes. Keyboard: `j`/`k` to move, `a`/`d`/`q` to decide,
`x` to select several for one bulk decision, `?` for the list.

## Confirmation by text

At check-out, a participant can ask someone who was there (a sponsor, the
chair, a counselor) to confirm it. That person gets one text with a link, or
they can just reply YES or NO. What they say sits on the check-in: the
participant sees it, the review queue shows it next to the GPS, and the
report says who confirmed and when. It is one more piece of evidence, never a
decision, and the engine's verdict does not change.

The person asked sees a first name, the meeting and the time. Nothing about
why. The participant chose them, and a participant keeps a short list of
people under **You** so the next ask is one tap. Never their own number, at
most three people per check-in, and never a number that has replied STOP.

Texts go through Twilio. Set `MC_TWILIO_ACCOUNT_SID`, `MC_TWILIO_AUTH_TOKEN`,
`MC_TWILIO_FROM` (a number, or a Messaging Service SID) and `MC_PUBLIC_URL`.
With those set, sign-in codes go by text as well. Without them nothing is
sent: codes are read out by an officer, and in demo mode the confirmation
link shows on the participant's screen so the whole loop can be walked
through on one laptop. A demo with Twilio set up texts only the numbers in
`MC_SMS_ALLOW`, since anyone can sign in to a demo; every other number gets
its code or link on screen. Every text is logged in `sms_log` with the link token
blanked out, and a county admin reads that log, and sends a test text, from
the dashboard's **Texts** page. The Twilio side is in `DEPLOY.md`.

## The meeting directory

Weekly meetings imported from an area's published list, so a participant
picks a real meeting instead of their officer typing each one in. The
Southeast Michigan AA list ships with the app in `data/`, 1,331 meetings
across 128 towns, and goes into the directory on first boot. Any list with a
header row works:

```bash
python tools/import_meetings.py data/semi-aa-meetings-2026-01-28.csv
python tools/import_meetings.py "another area.xlsx" --prune    # retire what the file no longer lists
```

Columns are matched by name (meeting name, page URL, day, start and end
times, street, city, zip, latitude, longitude). Rows with no street address
are skipped: nobody can check in to a Zoom meeting. A meeting is recognised
next time by its page URL, so re-running a file changes nothing, and a
corrected row updates the meeting in place. Nothing is ever deleted, because
check-ins may point at it.

A directory meeting is a `MeetingSeries`: "Thursdays at 7, 306 N Division".
The week's instances become `Meeting` rows as phones ask for them, so every
check-in still points at one date, and the compliance maths never hears
about recurrence. Directory meetings count toward a requirement the same as
staff-made ones.

On the phone, a participant picks their town once and sees the meetings
within 25 miles of it, with a search box for the long evenings. Only the town
is kept, never where the phone was. Officers search the directory from the
Meetings page.

## Compliance

A requirement says "3 public meetings a week from Sep 1 to Dec 31". For each
period, the system counts check-ins that count:

- confirmed → counts
- flagged or rejected, approved on review → counts
- flagged, not yet reviewed → shown separately as pending, not counted
- officer asked a question → pending until they decide, never a miss
- rejected and undecided, or denied on review → does not count

The latest review is the one that counts.

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
  reviewing.py     where each check-in stands with a human; every screen reads it
  confirming.py    asking someone who was there, and the texts themselves
  sms.py           Twilio, the text log, opt-outs, webhook signatures
  directory.py     the meeting directory: importing a list, weekly meetings
                   into dated ones, what is near a town
  seed.py          the demo county
  routers/         me (the signed-in person's own record), meetings, checkins,
                   caseload, reviews, reports, admin, confirmations (asks,
                   the public answer page, Twilio webhooks), directory
tools/import_meetings.py   bring an area's meeting list into the directory
data/              the meeting lists that ship with the app
static/
  officer.html     the staff dashboard shell
  index.html       the participant app shell
  confirm.html     the page a sponsor opens from a text (app/confirm.js)
  app/             tokens.css + base.css (shared design system), lib.js
                   (shared helpers), participant.*, officer.js + officer.css,
                   queue.js (the review queue)
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
| The court sees outcomes, not coordinates | `schemas.checkin_out(include_pin=...)`: the caseload, record and queue list carry no pins. `GET /api/reviews/{id}` returns one check-in's pin and writes `view_review_item` to the audit log. The participant always sees their own |
| Bulk location data is admin-only and audited | `/api/ops/checkins` |
| A human decides anything that hurts | `Review`; the engine never writes a compliance failure on its own. A denial or a changed decision must carry a reason (`reviewing.note_required`) |
| The participant sees everything about themselves | `/api/checkins/mine`, `/api/my/progress` |
| Every staff view of a record is logged | `auth.audit()`, called by every caseload and detail route |
| Least privilege | `auth.require(*roles)` on every staff route |
| Tenant isolation | `auth.scope_to_county()` plus Postgres row-level security in migration `002_rls` |
| Raw pins expire | `POST /api/retention/run` nulls coordinates past `MC_PIN_RETENTION_DAYS`, keeping the verdict |
| Reports never assert absence | `routers/reports.py`: "could not verify", never "did not attend" |
| Explainable | every verdict stores its reasons; they appear in the UI and in the report |
| Consent before the first check-in | `routers/me.py` records who agreed to which wording; `checkins.require_consent` refuses a check-in without it |
| Personal meetings are the participant's alone | staff lists, the review queue, caseload, reports and the ops map all exclude them |
| The person asked to confirm sees the minimum | `routers/confirmations.py`: a first name, the meeting, the time, and a single-use link. Only the link token's hash is stored |
| No location to third parties from the phone | the participant result shows a drawn diagram, not map tiles |
| Personal data never cached | `main.cache_rules`: every `/api/` response is `no-store` |

Security posture in full, with the gaps: see `SECURITY.md`.

Deploying: see `DEPLOY.md`. A preview with the demo county behind a shared
password is ready to go (`fly.toml`); real participants need sign-in first.

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

Roughly in order: the rest of the state's meeting lists in the directory,
the rotating meeting code (a printed sheet with a code that changes every
few minutes, which is the cheapest strong presence signal available), an IP
intelligence provider behind `NetworkCriterion`, and check-out reminders by
text.
