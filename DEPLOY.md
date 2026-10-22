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
code. With Twilio set up (below) the code goes to them by text; without it,
their officer reads it out from the participant's record. Sessions are
cookies: staff sessions last 12 hours and end after 30 idle minutes,
a participant's phone stays signed in for 30 days.

`MC_DEV_AUTH=1` is the old header shortcut, for local development only. It is
off in `fly.toml` and must stay off anywhere else.

Set `MC_SECRET_KEY` in any deployment. Without it, a restart invalidates
sign-in codes people are holding.

## Texts, through Twilio

What goes by text: sign-in codes, the ask to a sponsor or chair to confirm
a check-in (plus a short text back to the participant with the answer), a
log a participant sends to someone, and the "your account is ready" text to
people imported from the old site. The sending number has to be a Twilio
number with toll-free verification or 10DLC registration approved; a
personal cell cannot send through Twilio. Nothing is sent until these are set. `fly secrets import` reads
them from the keyboard, so the auth token never lands in your shell history
or a file. Type one per line, then Ctrl-D:

```bash
fly secrets import
```

```
MC_TWILIO_ACCOUNT_SID=AC...
MC_TWILIO_AUTH_TOKEN=...
MC_TWILIO_FROM=+1...
MC_SMS_ALLOW=+1734...,+1313...
```

`MC_TWILIO_FROM` is the number to send from, or a Messaging Service SID
(`MG...`) if you set one up in Twilio. `MC_PUBLIC_URL` is already in
`fly.toml`: links in texts are built from it, and so is the check on
incoming webhooks, so it has to be the address people actually open.

`MC_SMS_ALLOW` matters on the preview. Demo mode lists its accounts on the
sign-in screen, so anyone who finds the URL can sign in as a participant
and type in a number to be texted. In demo mode, texts go only to the
numbers on this list: yours, and whoever is in the room. Every other number
gets the old demo treatment, its code or link on screen, and the Texts page
logs it as not sent. Empty means nobody. Outside demo mode it is ignored.
To add someone before a demo, set it again with the whole list.

Then, in the Twilio console:

1. **A number that can send to US phones.** A toll-free number needs
   toll-free verification before carriers pass its texts; a local number
   needs A2P 10DLC registration. Both are forms about who is sending what to
   whom. The answers: appointment and attendance confirmations, sent to
   people who gave their number to the service or were named by someone who
   did, with STOP honoured.
2. **Point incoming messages at the app.** On the number (or the Messaging
   Service), set "A message comes in" to `https://<your app>/api/sms/inbound`,
   HTTP POST. That is how a reply of YES, NO or STOP reaches the app.
   Delivery reports arrive at `/api/sms/status` on their own; the app asks
   for them on every message it sends.
3. **Turn on Advanced Opt-Out** if you use a Messaging Service, so Twilio
   answers STOP and HELP itself. The app records STOP as well and never texts
   that number again, whichever side hears it first.

Both webhooks check Twilio's signature against the auth token and refuse
anything else. They are the only paths, besides the health check, that
`MC_ACCESS_PASSWORD` lets through, because Twilio cannot type a password.

The dashboard's **Texts** page (county admins) shows the two webhook
addresses to paste, sends a test text to a number you hold, and lists every
text with what Twilio said about it. That is the first place to look when
someone says a text never came.

Expect about a cent per message segment and a few dollars a month for the
number, at Twilio's current prices. A confirmation ask is two segments.

## Email, through SendGrid

A participant can send their log by email as well as by text. Email goes
through SendGrid, which is Twilio's email service, so it lives in the same
Twilio account. Until the two settings below are set, the email option does
not show in the app (demo mode shows the link on screen instead).

In the SendGrid console:

1. **Settings → Sender Authentication.** Either verify the one address
   (Single Sender Verification: a confirmation email to
   `attendance@actionscountplus.com`, click the link) or, better,
   **Authenticate Your Domain** for `actionscountplus.com`: SendGrid gives
   you three CNAME records to add at GoDaddy, and after that mail from any
   address at the domain passes the spam checks that a single sender does
   not always pass.
2. **Settings → API Keys → Create API Key.** Restricted access, with
   **Mail Send** only. Copy it: it is shown once.

Then, one line each, never in a file or in chat:

```bash
fly secrets set MC_SENDGRID_API_KEY=SG....
```

```bash
fly secrets set MC_EMAIL_FROM=attendance@actionscountplus.com
```

`MC_EMAIL_FROM_NAME` is the name beside the address and defaults to
ActionsCount+. Every email sent lands in the `email_log` table with
SendGrid's answer: subject and status, never the body. SendGrid's free plan
sends 100 emails a day, which is far more than log shares need; click and
open tracking are switched off in every message so the one link in it is
never rewritten.

## Payments, through Stripe

`/join` lets someone create their own profile and pay for it. Until the
three Stripe settings below are set, that page works only in demo mode
(free "demo" profiles, code shown on screen) and is closed everywhere else.
It also needs Twilio, because the phone number is proven with a text.

In the Stripe dashboard, with **Test mode** on until the real launch:

1. **Products → Add product.** One product ("MeetingsCount"), one recurring
   price (for example $12 a month). Copy the price id, `price_...`. The
   amount lives in Stripe; changing it is a new price, and a new id here.
2. **Developers → API keys.** Copy the secret key, `sk_test_...` (later
   `sk_live_...`). Never the publishable key: the app does not use it.
3. **Developers → Webhooks → Add endpoint.** URL
   `https://actionscountplus.com/api/billing/webhook`. Events:
   `checkout.session.completed`, `customer.subscription.created`,
   `customer.subscription.updated`, `customer.subscription.deleted`,
   `customer.subscription.paused`, `customer.subscription.resumed`. Copy the
   signing secret, `whsec_...`. Every call is checked against it and refused
   otherwise, and the webhook is the one path besides Twilio's that
   `MC_ACCESS_PASSWORD` lets through.
4. **Settings → Billing → Customer portal.** Turn it on, and allow customers
   to update payment methods and cancel subscriptions. The app's **Manage
   billing** button opens this portal.

Then, the same way as Twilio, one per line and Ctrl-D:

```bash
fly secrets import
```

```
MC_STRIPE_SECRET_KEY=sk_test_...
MC_STRIPE_WEBHOOK_SECRET=whsec_...
MC_STRIPE_PRICE_ID=price_...
```

`MC_STRIPE_TRIAL_DAYS` (in `fly.toml`, 30 today) makes the first month free:
checkout takes the card and Stripe charges nothing until the trial ends.
The join page says "First month free, then $10 a month" on its own from
these two settings. `MC_SELF_SIGNUP=0` closes `/join` entirely on a
deployment that is for one county only.

### Changing the price

A Stripe price never changes; you make a new one. On the product, **Add
another price**, set the new amount, save, copy its id, then:

```bash
fly secrets set MC_STRIPE_PRICE_ID=price_...
```

People already subscribed stay on the price they signed up at. Archive the
old price in Stripe once nobody new should get it.

### What the Stripe page looks like

Checkout and the billing portal are Stripe's pages, and what they show at
the top comes from the Stripe dashboard, not from this app. To take an old
logo or name off them: **Settings → Business → Branding** for the icon, the
logo and the colours (the app's icon is `static/icons/icon-512.png`);
**Settings → Business → Business details** for the public business name that
appears on the page and on receipts; and **Product catalog** for the product
name beside the price. Changing those changes every future checkout; no
deploy is needed.

### Promotion codes

A free year for a demo, or any discount, is a Stripe coupon with a
promotion code on it. The payment step has a **Have a code?** field; the app
checks the code with Stripe, applies it, skips the free month, and if the
code covers the whole cost Stripe asks for no card.

1. **Product catalog → Coupons → Create coupon.** Percentage off 100, duration
   **Multiple months**, 12 months. Name it "Free year".
2. On the coupon, **Promotion codes → Create.** The code is what people type,
   such as `FREEYEAR`. Set a redemption limit or an expiry if you like.

Redemptions show on the coupon in Stripe, and the audit log records which
code each checkout used. When a free year ends Stripe bills the card on
file; with no card, the subscription goes past due and then unpaid, and the
app pauses check-ins until the person adds one under **Manage billing**.

To try it in test mode, open `/join` on the preview, create a profile with
a number on `MC_SMS_ALLOW`, and pay with Stripe's test card `4242 4242 4242
4242`, any future date, any CVC. The dashboard's **Customers** page shows
the person; `/officer` → **Audit log** shows every webhook that arrived and
what it did. Cancelling from the portal, or from the Stripe dashboard,
switches check-in off within seconds.

Going live is the same three secrets from the live-mode dashboard, a new
webhook endpoint made in live mode, and a live price id. Stripe's fee is
2.9% plus 30 cents a charge at the time of writing, and the subscription
amount is yours to set.

Note that no card number, expiry or CVC ever reaches this app. Checkout and
the portal are Stripe pages on Stripe's domain; the app stores a customer
id, a subscription id and a status word.

## Moving people over from the old site

People can come over two ways, and both end up the same: a self-pay profile
with a weekly requirement.

1. **They sign up themselves** at `/join` with their phone number.
2. **You import a list.** A `.csv` or `.xlsx` with a header row: a name (or
   first and last name) and a mobile number are needed; an email, meetings a
   week and a start date are used when present. Copy it up and run:

```bash
fly ssh sftp shell                      # put people.csv /data/
fly ssh console -C "python tools/import_people.py /data/people.csv"
```

That only shows what would happen. Add `--yes` to make the profiles. Add
`--covered` if these people already paid on the old site, so no payment is
asked of them, and `--text` to text each one that their account is ready,
with the link. A probation end date column is used when the sheet has one.
A number that already has an account is left alone, so the same list can be
run again. Delete the file from `/data` afterwards: it is a list of names
and phone numbers.

## The meeting directory

`fly.toml` names `MC_DIRECTORY_FILE`, and the container imports that list
into the directory the first time it boots with an empty one. To load a new
or updated list later, copy it up and run the importer on the machine:

```bash
fly ssh sftp shell                      # put "another area.xlsx" /data/
fly ssh console -C "python tools/import_meetings.py /data/another\ area.xlsx --prune"
```

`--prune` retires meetings from that source that the new file no longer
lists. Each list is its own source, named after the site it came from, so
one area's file never touches another's.

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

### The domain

The app lives at `https://app.actionscountplus.com`. The bare domain is
saved for a homepage of its own; until that exists, it and `www` forward
every page to the app. GoDaddy holds the domain and runs its DNS:

```
CNAME app   mdn1rq1.meetingscount-preview.fly.dev
A     @     66.241.125.2                 (until the homepage moves in)
AAAA  @     2a09:8280:1::19f:b0da:0      (until the homepage moves in)
CNAME www   @
```

Fly holds a certificate for each name (`fly certs list`) and renews it on
its own. `MC_PUBLIC_URL` in `fly.toml` is the address texts link to, and
`MC_REDIRECT_FROM` lists the addresses whose pages forward to it. Only pages
forward: an API call never does, so a Twilio or Stripe webhook still pointed
at an old address keeps working, and Twilio's signature is checked against
both. When the homepage is ready, point `@` and `www` at wherever it is
hosted, and take them out of `MC_REDIRECT_FROM`.

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

## Going live

The order matters: the demo county holds staff accounts whose password is
printed in the README, so it has to go before the demo helpers do, and you
need an account of your own before it goes.

`fly.toml` already carries the live settings: `MC_ENV=production`,
`MC_DEMO=0`, `MC_SEED_ON_BOOT=0`. With `MC_ENV=production` the app refuses
to boot if either demo switch is turned back on.

1. **Deploy the live settings.**

   ```bash
   fly deploy
   ```

2. **Make your own account.** A superuser sees every county, including the
   self-pay one, so you can review a self-serve person's flagged check-ins
   and read the audit log. The first password is printed once; the dashboard
   makes you choose your own at first sign-in.

   ```bash
   fly ssh console -u appuser -C "python tools/make_staff.py --name 'Joe Dillon' --email you@example.com --role superuser"
   ```

   Sign in at `https://actionscountplus.com/officer` and change the password
   before the next step.

3. **Delete the demo county.** First see what would go, then delete it. The
   meeting directory and the self-pay county are untouched.

   ```bash
   fly ssh console -u appuser -C "python tools/retire_demo.py --dry-run"
   ```

   ```bash
   fly ssh console -u appuser -C "python tools/retire_demo.py --yes"
   ```

   The published demo password now opens nothing. Any county that signs up
   later gets its own admin account from `make_staff.py` with `--role admin
   --county "Their County"`.

What changes with demo mode off: the sign-in screen lists no accounts and
shows no codes; sign-in and sign-up codes go by text to any number, so
`MC_SMS_ALLOW` no longer applies and the Twilio number has to be registered
to reach US carriers (see Texts, above); `/join` is open only while Stripe
and Twilio are both configured; the simulator on `/ops` can no longer send
fake timestamps; and `/docs` is switched off.

What a live preview still is not: see the list below, in particular backups.
The SQLite file on the Fly volume gets a daily snapshot that Fly keeps for a
few days, and nothing else. Run `fly volumes snapshots list mc_data` to see
them, and take a copy before anything risky:

```bash
fly ssh sftp get /data/meetingscount.db ./backup-$(date +%F).db
```

## Demo or live

The site can go back and forth. Three lines in `fly.toml`, then `fly deploy`:

| | demo | live |
|---|---|---|
| `MC_ENV` | `demo` | `production` |
| `MC_DEMO` | `1` | `0` |
| `MC_PAYMENTS` | `0` | `1` |

In demo: sign-in and sign-up codes show on screen for any number not on
`MC_SMS_ALLOW`, so nobody is stuck waiting on a text while the Twilio
number is being registered; `/join` makes free profiles with no card and
nobody is ever blocked for not paying; the Stripe secrets stay where they
are, unused. Nothing is seeded, so there are no demo accounts unless you
add the demo county back with `python -m app.seed`, which also wipes the
database on SQLite, so do not.

In live: codes go by text to any number, `/join` takes real payments, and a
profile without a live subscription cannot check in. Flip the three lines
back and deploy. Profiles made while payments were off have no
subscription yet, so the next time they open the app they are asked to pay.

## Before real participants

In order, because each one depends on the last:

1. **Turn the demo helpers off** (`MC_DEMO=0`) and set up Twilio (above) so
   codes go by text and a participant on a new phone is not stuck waiting for
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
