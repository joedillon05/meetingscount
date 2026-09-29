# Demonstrating MeetingsCount to court staff

A six minute run through the demo county, in the order that answers an
officer's questions before they ask them. The one thing to keep saying: the
system confirms attendance, a person decides anything that hurts.

## Before you start

- Two things open: the dashboard on a laptop, the phone app on your phone.
- Sign-in screen lists the demo accounts. **Officer Chen** for most of it,
  **Admin (county)** for the last part.
- Do this first, or a live check-in will fail: **Meetings**, click the map
  where you actually are, set the start time to now, create it. The seeded
  meetings are in Ann Arbor and a check-in from anywhere else is correctly
  rejected as too far away.
- Say early that it is seeded data, ten invented people, no real records.

## The run

**1. The caseload (45 seconds).** Sign in. "Eleven people, three behind this
week, thirty-two check-ins waiting on a decision." Point at Sam Okafor: five
weeks of attendance, then nothing for two. The screen surfaces that without
anyone going looking.

**2. One person (45 seconds).** Open Sam. The chart is weeks against what the
order requires. Under it, every check-in with the reasons behind the result.
"This is the conversation you would otherwise be having from a stack of
signed slips."

**3. The review queue (2 minutes).** This is the heart of it. Open Devon
Hargrove.

- The line at the top says what the decision does: counting it completes
  their week, not counting it leaves them short. No mental arithmetic.
- The map, the distance against the limit, the timing against the window,
  how accurate the phone was.
- **At this venue**: how check-ins go there for everyone, with no other
  participant named. When weak GPS is normal in that building, the officer
  can see it is the building, not the person.
- Decide it. Note the six second undo.
- Then **Ask a question** on another one, type a real question, and switch to
  your phone: the question is waiting at the top of the app. Answer it, and
  it comes back to the top of the queue marked replied. Say plainly: while it
  is waiting, it counts as pending, never as a miss.

**4. The phone (90 seconds).** Sign out on the phone, sign back in with the
phone number and a code you generate from the participant's record. Then
check in to the meeting you made earlier, outside or near a window.

- It finds your location, keeps the best reading, and shows the result in
  plain words with a picture of you against the allowed circle.
- Say what it does not do: location is read at that tap, never in the
  background. There is no map of where anyone has been.

**5. The report (45 seconds).** Back on the participant, **Compliance
report**, open the printable one. Week by week against the order, then every
check-in with any review decision in the officer's own words. Point at the
fingerprint at the bottom and at the sentence that says a result that is not
confirmed is not evidence of absence.

**6. Accountability (30 seconds).** Sign in as the admin. **Audit log**:
every staff view of a record, every decision, every export, with who and
when. "Insider curiosity is a likelier failure than a hacker, so every look
is on the record." **Staff**: add an officer, second factors, turning an
account off.

## Questions you will get, and honest answers

**Can someone fake their location?** Yes, with a free app, and no single
check stops it. That is why there is a confidence score built from layers,
and why a human reviews anything unclear. Next up is a code the meeting
displays that changes every few minutes, which is the cheapest strong signal
and needs nothing from the meeting but a taped-up sheet.

**Does this track people?** No. Location is read at the tap, for check-in and
check-out. There is no background collection in the code, exact coordinates
are deleted after 30 days, and staff screens show outcomes, not maps.

**What about someone with no phone, or no signal?** Today they need a phone
for this to help. Do not oversell it: the honest answer is that paper stays
for those cases, and the first county pilot should measure how many people
that actually is.

**Who owns the records?** If a county contracts for it, the county does, and
we are a processor under their retention and disclosure rules. That belongs
in the contract, not a demo.

**Can it say someone did not attend?** No, and it never will. It reports what
it confirmed. Absence is an inference a human makes with other evidence.

**What does it cost to run?** About $56 a month for a county pilot on AWS.
Pricing to a county is a conversation, not a number on a slide.

**When could we start?** Ask for their security questionnaire. That document
is the real specification, and it is a better first step than a date.

## What to say about what is not finished

Be first to say it. `SECURITY.md` lists it: field-level encryption of
locations, row-level security switched on in practice, a scheduled retention
job, SMS codes, and an independent penetration test before real data. Saying
it plainly is worth more than hoping nobody asks.
