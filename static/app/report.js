// The attendance report: the page an officer prints or saves as a PDF and
// puts in a file. It is written for someone who has never seen this system,
// including a judge, so every term is explained where it is used.
//
// Careful wording throughout: MeetingsCount confirms attendance, it never
// asserts absence. "Could not be verified" is not "did not attend".

import { $, dateTime, esc, feet, icon, makeApi, devIdentity, plural, setTimeZone, shortDay, time } from './lib.js';

const api = makeApi(devIdentity('mc_act_staff'));
const app = $('#app');

const RESULT = {
  confirmed: 'Confirmed', flagged: 'Needed review', rejected: 'Could not be verified',
};
const OUTCOME = {
  counted: ['Counts', 'ok'], pending: ['Awaiting a decision', 'warn'], 'not counted': ['Does not count', 'bad'],
};
const CATEGORY = { public: 'public meeting', community_service: 'community service',
  private: 'private meeting', therapy: 'therapy', any: 'meeting' };
const REQ_NOUN = { public: 'public meetings', community_service: 'community service sessions',
  private: 'private meetings', therapy: 'therapy sessions', any: 'meetings' };

async function boot() {
  const params = new URLSearchParams(location.search);
  const pid = params.get('participant');
  const days = Number(params.get('days') || 90);
  if (!pid) return fail('No participant was named in the link.');
  let d;
  try {
    d = await api(`/api/reports/participant/${pid}?days=${days}`);
  } catch (e) {
    return fail(e.status === 401 ? 'Sign in on the dashboard first, then open this report again.' : e.message);
  }
  setTimeZone(d.timezone || 'America/Detroit');
  document.title = `Attendance report, ${d.participant}`;
  render(d);
}

function fail(message) {
  app.innerHTML = `<div class="sheetlike"><h1>Report not available</h1><p>${esc(message)}</p>
    <p><a href="/officer">Back to the dashboard</a></p></div>`;
}

function render(d) {
  const req = d.requirements[0];
  app.innerHTML = `
    <div class="toolbar no-print">
      <a class="btn secondary" href="/officer#participant/${new URLSearchParams(location.search).get('participant')}">${icon('back')} Back to the record</a>
      <span class="spacer"></span>
      <span class="muted small">Print, or save as PDF from the print dialog.</span>
      <button class="btn" id="print" type="button">${icon('file')} Print</button>
    </div>

    <article class="sheet-page">
      <header class="rep-head">
        <div>
          <h1>Attendance report</h1>
          <p class="muted">${esc(d.county || '')}${d.program ? ` · ${esc(d.program)}` : ''}</p>
        </div>
        <div class="rep-brand">${logo()}<b>MeetingsCount</b></div>
      </header>

      <section class="rep-who">
        <dl>
          <div><dt>Participant</dt><dd>${esc(d.participant)}</dd></div>
          <div><dt>Case number</dt><dd>${esc(d.case_number || 'not recorded')}</dd></div>
          <div><dt>Supervising officer</dt><dd>${esc(d.officer || 'not recorded')}</dd></div>
          <div><dt>Enrolled</dt><dd>${esc(shortDay(d.enrolled_on))}</dd></div>
          <div><dt>Period covered</dt><dd>${esc(shortDay(d.period_from))} to ${esc(shortDay(d.generated_at))} (${d.period_days} days)</dd></div>
          <div><dt>Prepared</dt><dd>${esc(dateTime(d.generated_at))}${d.generated_by ? ` by ${esc(d.generated_by)}` : ''}</dd></div>
        </dl>
      </section>

      ${req ? `<section>
        <h2>What the order requires</h2>
        <p class="rep-order"><b>${req.count_required} ${esc(REQ_NOUN[req.category] || req.category)} per ${esc(req.period)}</b>${
          req.order_reference ? `, order ${esc(req.order_reference)}` : ''}, from ${esc(shortDay(req.starts_on))}${
          req.ends_on ? ` to ${esc(shortDay(req.ends_on))}` : ''}.</p>
        <p class="rep-summary">In the ${plural(req.periods_covered, req.period)} covered by this report,
          <b>${req.periods_met} of ${req.periods_covered}</b> met the requirement in full.</p>
        ${periodTable(req)}
      </section>` : '<section><h2>What the order requires</h2><p>No requirement was on file for this period.</p></section>'}

      <section>
        <h2>Check-ins in this period</h2>
        ${d.check_ins.length ? checkInTable(d) : '<p>No check-ins were recorded in this period.</p>'}
      </section>

      <section class="rep-note">
        <h2>How to read this report</h2>
        <p>${esc(d.note)}</p>
        <ul>
          <li><b>Confirmed</b> means the phone was inside the meeting's allowed distance, within the check-in window, with a good location reading.</li>
          <li><b>Needed review</b> means something was unclear, usually a weak location reading indoors. A person decided it, and their decision is shown.</li>
          <li><b>Could not be verified</b> means a check-in was recorded outside the allowed distance or time. It is not evidence that the participant was absent.</li>
          <li>Meetings a participant recorded for themselves are private to them and are not included here.</li>
        </ul>
      </section>

      <footer class="rep-foot">
        <p><b>Verification.</b> This report is a copy of the record as it stood on
          ${esc(dateTime(d.generated_at))}. Its fingerprint is:</p>
        <p class="hash">${esc(d.content_hash)}</p>
        <p class="muted small">Producing the same report again from an unchanged record gives the same fingerprint.
          A different fingerprint means the record changed after this copy was made, for example a review decision.</p>
      </footer>
    </article>`;
  $('#print').onclick = () => window.print();
}

function periodTable(req) {
  return `<table class="rep-table">
    <thead><tr><th>Week beginning</th><th>Required</th><th>Counted</th><th>Awaiting a decision</th><th>Outcome</th></tr></thead>
    <tbody>${req.history.map(h => `<tr${h.partial ? ' class="partial"' : ''}>
      <td>${esc(shortDay(h.starts))}${h.partial ? '<div class="muted">part week under the order</div>' : ''}</td>
      <td>${h.required}</td>
      <td>${h.counted}</td>
      <td>${h.pending || ''}</td>
      <td class="${h.partial ? '' : `s-${h.status}`}">${h.partial ? 'Not counted either way'
        : { met: 'Met', pending: 'Could still be met', short: 'Short' }[h.status]}</td>
    </tr>`).join('')}</tbody>
  </table>
  ${req.partial_periods ? `<p class="fine">A part week is one the order began or ended in the middle of.
    Those are shown for completeness and left out of the tally above.</p>` : ''}`;
}

function checkInTable(d) {
  return `<table class="rep-table">
    <thead><tr><th>Date and time</th><th>Meeting</th><th>Result</th><th>Distance</th><th>Counts</th></tr></thead>
    <tbody>${d.check_ins.map(c => {
      const [word, tone] = OUTCOME[c.outcome] || ['', ''];
      return `<tr>
        <td class="nowrap">${esc(shortDay(c.date))}, ${esc(time(c.date))}</td>
        <td>${esc(c.meeting)}${c.address ? `<span class="muted"> · ${esc(c.address)}</span>` : ''}
          ${c.review ? `<div class="rep-review">${esc(c.review_words)} by ${esc(c.reviewed_by || 'an officer')},
            ${esc(shortDay(c.reviewed_at))}${c.review_note ? `: "${esc(c.review_note)}"` : ''}</div>` : ''}</td>
        <td>${esc(RESULT[c.result] || c.result)}</td>
        <td class="nowrap">${esc(feet(c.distance_ft))}</td>
        <td class="s-${tone}">${esc(word)}</td>
      </tr>`;
    }).join('')}</tbody>
  </table>`;
}

function logo() {
  return `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="var(--brand)"/>
    <path d="M9.5 16.5l4.2 4.2 8.8-9.4" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

boot();
