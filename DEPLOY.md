# Deploying MeetingsCount

Two different things, and the order matters.

1. **A preview**: the demo county on a real https URL, behind one shared
   password, so it can be opened on a phone and shown to people. Ready now,
   about half an hour.
2. **A pilot**: real participants under real court orders. Needs sign-in
   first, and a few other things listed at the bottom. Not the same evening.

## Sign-in

Staff sign in with an email and a password, and an authenticator app if they
turn one on. Participants sign in with their phone number and a six-digit
code that their officer reads out from the participant's record. Sessions are
cookies: staff sessions last 12 hours and end after 30 idle minutes,
a participant's phone stays signed in for 30 days.

`MC_DEV_AUTH=1` is the old header shortcut, for local development only. It is
off in `fly.toml` and must stay off anywhere else.

Set `MC_SECRET_KEY` in any deployment. Without it, a restart invalidates
sign-in codes people are holding.

## Why a preview still cannot hold real people

`MC_DEMO=1` lists the demo staff accounts and their shared password on the
sign-in screen, and shows a participant their code on screen instead of
waiting for an officer to read it out. That is what makes a demo easy and it
is exactly wrong for real data: anyone who knows a participant's phone number
could sign in as them.

So the preview runs with demo data only. Not one real name, phone number,
case number or address. Before real people, `MC_DEMO=0`, and then the list
below.

## Preview on Fly.io

The image is the same one Fargate would run, so nothing here is throwaway.

```bash
brew install flyctl
fly auth login                      # your own account, in the browser

cd "~/Downloads/meetingscount v3"
fly launch --no-deploy --copy-config --name <pick-a-name> --region ord
fly volumes create mc_data --size 1 --region ord --yes
fly secrets set MC_ACCESS_PASSWORD='<a long shared password>'
fly deploy
fly open
```

`fly.toml` is already written: Chicago region, https forced, a 1 GB volume at
`/data` for the SQLite database, demo seed on first boot, and the machine
suspends when nobody is using it.

Useful afterwards:

```bash
fly logs                                    # what the server is doing
fly ssh console -C "python -m app.seed"     # reset the demo data
fly status                                  # machine, volume, health checks
```

Roughly $3 to $6 a month for a shared-cpu-1x machine and the volume, less if
it suspends most of the time.

### Testing it on a phone

The demo meetings are at real addresses in Ann Arbor and Ypsilanti, so a
check-in from anywhere else is correctly rejected as too far away. To test a
real check-in where you actually are:

1. Open `/officer`, go to **Meetings**, and click the map at your location.
2. Set the start time to now, create the meeting.
3. Open `/` on the phone, and check in to it.

Two other things worth doing on the phone: add it to the home screen from the
share menu, and check in from inside a building to see the accuracy warning
and the review queue pick it up.

## Before real participants

In order, because each one depends on the last:

1. **Turn the demo helpers off** (`MC_DEMO=0`) and wire code delivery to SMS
   (Twilio or SNS) so a participant on a new phone is not stuck waiting for
   an officer. Passkeys after that, which is also the device binding the
   Big Picture doc asks for.
2. **Postgres, not SQLite**, with the app connecting as a non-superuser role
   that sets `app.county_id` per request, so the row-level security in
   migration 002 actually does something. Plus automated backups, and a
   restore you have tested.
3. **Field encryption** for `checkins.lat/lng` and `users.phone` with a KMS
   key, so a database dump is not a map of where people in recovery have been.
4. **Rate limits, security headers, error tracking**, and the retention job on
   a nightly schedule instead of a button.
5. **The paperwork**: privacy policy, terms, a data processing agreement, and
   the county's security questionnaire. Their IT office's questionnaire is the
   real specification, so ask for it before building to a guess.
6. **A map provider.** The staff maps currently load OpenStreetMap's free
   tiles, which their usage policy does not allow for a production service.

Nothing on that list is exotic, but it is more than an evening, and every item
protects someone whose housing, job or custody could turn on this record.

## AWS, when the pilot is real

`infra/` holds the CDK stack: Fargate, RDS Postgres, an ALB, Cognito, and a
KMS key, about $56 a month. See `infra/README.md` for the line items and for
why App Runner, a NAT gateway and Aurora Serverless are deliberately not in
it. Moving from Fly to that stack is a database dump, a restore, and a DNS
change.
