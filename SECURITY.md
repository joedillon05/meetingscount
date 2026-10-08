# Security and privacy, as built

What this system actually does today, with the file that does it, so an
answer can be checked rather than taken on trust. Written for a county IT or
legal review, and kept honest: the last section is what is **not** done yet.

MeetingsCount holds three of the most sensitive things about a person at
once: that they are under court supervision, that they are in recovery, and
where they were at a given time. The design assumes a breach of any one of
those could cost someone a job, custody of a child, or their safety.

## What the system is for

A participant checks in at a meeting. Their phone's location is read once, at
that moment. The server scores it against the meeting's place and time and
records a result with its reasons. Anything unclear goes to a human before it
affects anyone's compliance. The court sees outcomes; it does not get a map.

## Access

| Control | How | Where |
|---|---|---|
| Staff sign-in | Email and password, hashed with scrypt (16 MB memory cost), never stored or logged in the clear | `app/security.py`, `app/routers/auth.py` |
| Second factor | Authenticator app (TOTP, RFC 6238), per account, required at sign-in once on | `app/security.py`, account menu |
| Participant sign-in | Phone number and a six-digit code: single use, one hour, dead after five wrong guesses, stored as a keyed hash. Sent by text when Twilio is set up, otherwise read out by the officer. The reply never says whether a number is known | `app/routers/auth.py` |
| Brute force | Account locks for 15 minutes after 5 failures; rate limits per address and per account on every sign-in path | `app/security.too_many` |
| Account enumeration | A wrong password and an unknown email give the same message; asking for a participant code never reveals whether the number is known | `app/routers/auth.py` |
| Sessions | Random token in an HttpOnly, SameSite, https-only cookie. Only the token's hash is stored, so a database dump cannot be used to sign in | `app/auth.py` |
| Session limits | Staff: 12 hours, ended after 30 idle minutes. Participants: 30 days on their own phone | `MC_STAFF_*`, `MC_PARTICIPANT_SESSION_DAYS` |
| Password changes | Sign out every other device; a password handed over by an admin must be replaced before the dashboard does anything | `app/routers/auth.py`, `app/auth.require` |
| Least privilege | Every staff route names the roles it allows. Officers see their county; the audit log is admin only; bulk location data is admin only and logged | `app/auth.require`, `app/routers/*` |
| Tenancy | Every query is scoped to the signed-in person's county, and Postgres row-level security enforces the same rule underneath | `app/auth.scope_to_county`, `alembic/versions/002_row_level_security.py` |

## Data

| Control | How | Where |
|---|---|---|
| Collected once | Location is read at the tap, in the foreground, for check-in and check-out. No background tracking exists in the code | `static/app/participant.js` |
| Nearby means a town | To show meetings nearby, the participant picks a town from the directory, or reads their location once and the nearest town is what gets kept. The app never stores where the phone was for this | `app/routers/directory.py` |
| Told plainly | A consent screen before the first check-in, recorded with a version. Check-in is refused without it | `app/routers/me.py`, `app/routers/checkins.require_consent` |
| Outcomes, not maps | The caseload, the participant record and the review list carry no coordinates. One check-in's coordinates appear only in its review view, and that view writes an audit row | `app/schemas.checkin_out`, `app/routers/reviews.py` |
| Bulk location | One endpoint, admin only, county scoped, audited every time | `app/routers/admin.ops_checkins` |
| Retention | Coordinates older than the window are deleted, keeping the result and the reasons | `POST /api/retention/run`, `MC_PIN_RETENTION_DAYS` |
| Private by default | Meetings a participant records for themselves are visible only to them: not in staff lists, the review queue, reports, or the ops map | `app/routers/meetings.py`, `reviews.py`, `reports.py` |
| Their own copy | A participant can see every result and reason about them, add a note to any of it, and download the lot as a file | `GET /api/my/export` |
| Caching | Every API response is `no-store`, so no browser or proxy keeps personal data | `app/main.cache_rules` |
| Asking someone to confirm | The person asked gets a first name, the meeting and the time, and a single-use link that stops working after 7 days. Only the hash of the link token is stored. Nothing says why the participant keeps track. The participant cannot ask their own number, asks at most three people per check-in, and cannot text a number that said STOP | `app/routers/confirmations.py` |
| Texts | Every text is logged with the link token blanked out. STOP, whether Twilio hears it or the app does, stops all texts to that number. Twilio's webhooks are checked against the account's signature and refused otherwise | `app/sms.py` |

## Decisions about people

| Control | How | Where |
|---|---|---|
| A human decides | The engine never records a failure. Flagged and rejected check-ins wait for an officer | `app/reviewing.py` |
| Reasons, always | Every verdict stores its criteria, scores and plain-English reasons, shown to staff, to the participant, and in the report | `app/verification.py` |
| Nothing is overwritten | Reviews are append-only. Changing a decision needs a written reason and keeps the earlier one | `app/reviewing.py`, `app/routers/reviews.py` |
| The participant is heard | They can add a note to any check-in; an officer's question waits for their answer and never counts as a miss meanwhile | `app/routers/checkins.py` |
| Careful language | Reports say attendance was confirmed or could not be verified. They never say someone did not attend | `app/routers/reports.py` |
| Fair periods | A week the order began or ended in the middle of is labelled and left out of the tally | `app/compliance.history` |
| Accountability | Every staff view of a record, every decision, export, sign-in and failure is written to an append-only log with who, when and from where | `app/auth.audit`, `alembic/versions/002` revokes UPDATE and DELETE |

## Transport and the browser

- HTTPS end to end; HSTS when served over https.
- `Content-Security-Policy` allows scripts and styles from this origin only.
  Map tiles are the one outside source, images only.
- `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: strict-origin-when-cross-origin` (other hosts see the
  site's origin, never a path; the map tile server needs at least that or it
  blocks the request), and a `Permissions-Policy` that allows
  geolocation for this site and switches off camera, microphone and payment.
- The interactive API browser is switched off when `MC_ENV=production`.
- Container runs as a non-root user (uid 10001).

See `app/main.py` for all of the above.

## Known gaps

Not done yet. Each one needs doing before real participant data, and none of
it is hidden from a reviewer.

1. **Field-level encryption.** `checkins.lat/lng` and `users.phone` are
   encrypted at rest by the database, not by the application. A KMS data key
   per county so a database dump alone is not a map is the next piece.
2. **Row-level security is not switched on in practice.** The policies exist
   (migration 002) but the app connects as the owner and does not set
   `app.county_id` per request. Application scoping is what is enforcing
   tenancy today.
3. **Retention runs on demand, not on a schedule.** The job exists and is
   audited; nothing calls it nightly yet.
4. **Texting needs a registered number.** The code is wired to Twilio, but a
   toll-free number has to pass verification (or a local number A2P
   registration) before carriers deliver at volume. Until then an officer
   reads sign-in codes out, which is fine for a pilot in one office.
5. **A confirmation proves a phone, not a person.** Someone with the
   sponsor's phone answered. A participant could name a friend. The officer
   sees who was asked and how the participant described them, which is why
   it is evidence beside the GPS and not a decision.
6. **Rate limits are per process.** They hold for one machine, which is the
   whole pilot. More than one needs shared state.
7. **No independent test.** A penetration test before a pilot, and backups
   with a restore that has actually been run.
8. **Demo helpers.** With `MC_DEMO=1` the sign-in screen lists demo accounts
   and shows participant codes. The app refuses to start with it on when
   `MC_ENV=production`, and it must never be on with real data.

## Reporting a problem

Email the maintainer. If it involves participant data, say so in the subject
and do not include the data itself.
