// The staff dashboard: caseload, review queue, participant records, meetings,
// and (for admins) the audit log. Same design system as the phone app.
//
// Every view that opens a person's record is logged on the server; nothing
// here fetches more than the view on screen needs.

import {
  $, $$, ago, dateTime, devIdentity, esc, feet, icon, makeApi, makeDownload, monthDay,
  plural, setTimeZone, sheet, shortDay, time, toast,
} from './lib.js';
import { mountQueue, unmountQueue } from './queue.js';

const identity = devIdentity('mc_act_staff');
const api = makeApi(identity);
const download = makeDownload(identity);
const app = $('#app');

const S = {
  ctx: {}, me: null, staff: null, view: null, cases: [], stats: {},
  scope: 'all', status: '', search: '', sortKey: null, sortAsc: true, mmap: null, pin: null,
  pickers: null,
};

const OVERALL = {
  met: ['On track', 'ok', 'check'], short: ['Behind', 'bad', 'x'],
  pending: ['Awaiting review', 'warn', 'clock'], none: ['No requirement', 'neutral', 'note'],
};
const CATEGORY = { public: 'Public', community_service: 'Community service', private: 'Private', therapy: 'Therapy', any: 'Any' };
const REQ_NOUN = { public: 'public meetings', community_service: 'community service', private: 'private meetings', therapy: 'therapy sessions', any: 'meetings' };

const pill = (key) => { const [label, tone, ic] = OVERALL[key] || OVERALL.none;
  return `<span class="badge tone-${tone}">${icon(ic)} ${label}</span>`; };
const engine = s => ({ confirmed: ['Confirmed', 'ok', 'check'], flagged: ['Flagged', 'warn', 'alert'], rejected: ['Rejected', 'bad', 'x'] })[s];

/* ---------- boot ---------- */

async function boot() {
  try { S.ctx = await api('/api/auth/context'); } catch { S.ctx = {}; }

  if (S.ctx.dev_auth) {
    try {
      S.staff = (await api('/api/users')).filter(u => u.role !== 'participant');
      if (!S.staff.some(u => String(u.id) === identity.get())) {
        identity.set((S.staff.find(u => u.role === 'officer') || S.staff[0]).id);
      }
    } catch { S.staff = null; }
  } else {
    S.staff = null;
    identity.clear();
  }

  try { S.me = await api('/api/me'); } catch (e) { return e.status === 401 ? signIn() : fatal(e); }
  if (S.me.role === 'participant') return participantHere();
  setTimeZone(S.me.timezone);
  renderShell();
  window.addEventListener('hashchange', route);
  route();
  refreshBadge();
  setInterval(() => { if (!document.hidden) refreshBadge(); }, 60_000);
}

const ctx = {
  api, sheet,
  onChange: counts => setBadge(counts.open),
};

function renderShell() {
  const admin = S.me.role === 'admin' || S.me.role === 'superuser';
  app.innerHTML = `
    <header class="top">
      <a class="brand" href="#caseload">${logo()}<span>MeetingsCount</span></a>
      ${S.me.county ? `<span class="org">${esc(S.me.county)}</span>` : ''}
      <nav class="nav" aria-label="Sections">
        <a data-nav="caseload" href="#caseload">${icon('users')}<span>Caseload</span></a>
        <a data-nav="queue" href="#queue">${icon('inbox')}<span>Review queue</span><span class="count" id="qcount" hidden></span></a>
        <a data-nav="meetings" href="#meetings">${icon('calendar')}<span>Meetings</span></a>
        ${admin ? `<a data-nav="audit" href="#audit">${icon('scroll')}<span>Audit log</span></a>` : ''}
      </nav>
      <span class="spacer"></span>
      ${S.staff ? `<a class="navlink" href="/ops">Ops</a><a class="navlink" href="/">Phone app</a>
        <button class="chip demo" data-act="switch" type="button">Demo · ${esc(S.me.name)}</button>`
        : `<button class="chip who" data-act="account" type="button">${icon('user')} ${esc(S.me.name)}</button>`}
    </header>
    <main id="view" tabindex="-1"></main>`;
  app.addEventListener('click', e => {
    if (e.target.closest('[data-act="switch"]')) switchUser();
    if (e.target.closest('[data-act="account"]')) accountSheet();
  });
}

function logo() {
  return `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="var(--brand)"/>
    <path d="M9.5 16.5l4.2 4.2 8.8-9.4" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function route() {
  const [view = 'caseload', a, b] = (location.hash.slice(1) || 'caseload').split('/');
  if (S.view === 'queue' && view !== 'queue') unmountQueue();
  if (S.mmap) { S.mmap.remove(); S.mmap = null; }
  S.view = view;
  $$('[data-nav]').forEach(n => n.classList.toggle('on', n.dataset.nav === (view === 'participant' ? 'caseload' : view)));
  const v = $('#view');
  window.scrollTo(0, 0);
  if (view === 'queue') return mountQueue(ctx, v, { tab: a || 'open', id: b ? Number(b) : null });
  if (view === 'participant') return viewParticipant(Number(a));
  if (view === 'meetings') return viewMeetings();
  if (view === 'audit') return viewAudit();
  return viewCaseload();
}

async function refreshBadge() {
  try { S.stats = await api('/api/stats'); setBadge(S.stats.pending_reviews); } catch { /* next time */ }
}
function setBadge(n) {
  const b = $('#qcount');
  if (!b) return;
  b.hidden = !n; b.textContent = n;
}

function loading(v) { v.innerHTML = '<div class="sk sk-title"></div><div class="card sk-card"></div>'; }
function failed(v, e) {
  v.innerHTML = `<div class="card empty">${icon('alert', 'lg')}<p>${esc(e.message)}</p>
    <button class="btn" type="button" data-retry>Try again</button></div>`;
  v.querySelector('[data-retry]').onclick = route;
}

/* ---------- caseload ---------- */

async function viewCaseload() {
  const v = $('#view');
  loading(v);
  try {
    [S.stats, S.cases] = await Promise.all([api('/api/stats'), api('/api/caseload')]);
  } catch (e) { return failed(v, e); }
  setBadge(S.stats.pending_reviews);
  const st = S.stats, cs = S.cases;
  const behind = cs.filter(c => c.overall === 'short').length;
  const tile = (value, label, tone = '', href = '') => {
    const inner = `<span class="tv">${value ?? 'n/a'}</span><span class="tk">${label}</span>`;
    return href ? `<a class="tile ${tone}" href="${href}">${inner}</a>` : `<div class="tile ${tone}">${inner}</div>`;
  };
  v.innerHTML = `
    <div class="page-head"><div><h1>Caseload</h1>
      <p class="muted">${esc(S.me.county || '')}${S.me.county ? ' · ' : ''}week of ${esc(monthDay(mondayOf(new Date())))}</p></div>
      <button class="btn" data-enrol type="button">${icon('plus')} Add participant</button></div>
    <div class="tiles">
      ${tile(st.participants, 'participants')}
      ${tile(cs.filter(c => c.overall === 'met').length, 'on track this week', 'ok')}
      ${tile(behind, 'behind this week', behind ? 'bad' : '')}
      ${tile(st.pending_reviews, 'waiting for a decision', st.pending_reviews ? 'warn' : '', '#queue')}
      ${tile(st.checkins_this_week, 'check-ins in the last 7 days')}
      ${tile(st.avg_confidence, 'average confidence')}
      ${tile(st.not_checked_out, 'without a check-out', st.not_checked_out ? 'warn' : '')}
    </div>
    <section class="card flush">
      <div class="toolbar">
        <div class="seg" id="scopeSeg">
          <button data-scope="all" class="${S.scope === 'all' ? 'on' : ''}" type="button">Everyone</button>
          <button data-scope="mine" class="${S.scope === 'mine' ? 'on' : ''}" type="button">My caseload</button>
        </div>
        <div class="seg" id="statusSeg">${[['', 'All'], ['short', 'Behind'], ['pending', 'Awaiting review'], ['met', 'On track']]
          .map(([k, l]) => `<button data-st="${k}" class="${S.status === k ? 'on' : ''}" type="button">${l}</button>`).join('')}</div>
        <input type="search" id="csearch" placeholder="Search name or case" value="${esc(S.search)}" aria-label="Search name or case">
        <span class="spacer"></span><span class="muted small" id="ccount"></span>
      </div>
      <div class="table-wrap"><table class="grid" id="ctable">
        <thead><tr>
          <th data-k="name" scope="col">Participant</th><th data-k="officer" scope="col">Officer</th>
          <th scope="col">This week</th><th data-k="last_checkin_at" scope="col">Last check-in</th>
          <th data-k="pending_reviews" scope="col">To decide</th><th data-k="overall" scope="col">Status</th>
        </tr></thead><tbody id="crows"></tbody></table></div>
    </section>`;
  renderCases();
  $('#scopeSeg').onclick = e => { const b = e.target.closest('[data-scope]'); if (!b) return;
    S.scope = b.dataset.scope; $$('#scopeSeg button').forEach(x => x.classList.toggle('on', x === b)); renderCases(); };
  $('#statusSeg').onclick = e => { const b = e.target.closest('[data-st]'); if (!b) return;
    S.status = b.dataset.st; $$('#statusSeg button').forEach(x => x.classList.toggle('on', x === b)); renderCases(); };
  $('#csearch').oninput = e => { S.search = e.target.value; renderCases(); };
  $$('#ctable th[data-k]').forEach(th => th.onclick = () => {
    S.sortAsc = S.sortKey === th.dataset.k ? !S.sortAsc : true;
    S.sortKey = th.dataset.k; renderCases();
  });
  v.querySelector('[data-enrol]').onclick = enrolSheet;
  $('#crows').onclick = e => { const tr = e.target.closest('tr[data-id]'); if (tr) location.hash = `participant/${tr.dataset.id}`; };
  $('#crows').onkeydown = e => { const tr = e.target.closest('tr[data-id]'); if (tr && e.key === 'Enter') location.hash = `participant/${tr.dataset.id}`; };
}

function mondayOf(d) {
  const x = new Date(d); const back = (x.getDay() + 6) % 7;
  x.setDate(x.getDate() - back); return x;
}

function renderCases() {
  const term = S.search.trim().toLowerCase();
  let rows = S.cases.filter(c =>
    (S.scope === 'all' || c.officer_id === S.me.id) &&
    (!S.status || c.overall === S.status) &&
    (!term || c.name.toLowerCase().includes(term) || (c.case_number || '').toLowerCase().includes(term)));
  if (S.sortKey) {
    rows = [...rows].sort((a, b) => {
      const x = a[S.sortKey] ?? '', y = b[S.sortKey] ?? '';
      return (x > y ? 1 : x < y ? -1 : 0) * (S.sortAsc ? 1 : -1);
    });
  }
  $$('#ctable th[data-k]').forEach(th => {
    const on = th.dataset.k === S.sortKey;
    th.setAttribute('aria-sort', on ? (S.sortAsc ? 'ascending' : 'descending') : 'none');
  });
  $('#ccount').textContent = rows.length === S.cases.length ? plural(rows.length, 'participant') : `${rows.length} of ${S.cases.length}`;
  $('#crows').innerHTML = rows.map(c => {
    const stale = c.last_checkin_at && Date.now() - new Date(c.last_checkin_at) > 10 * 864e5;
    return `<tr data-id="${c.participant_id}" tabindex="0">
      <td><b>${esc(c.name)}</b><div class="muted small">${esc(c.case_number || '')}</div></td>
      <td>${esc((c.officer || '').replace(/^Officer /, ''))}</td>
      <td><div class="reqs">${c.requirements.length ? c.requirements.map(progress).join('') : '<span class="muted small">none</span>'}</div></td>
      <td class="${stale ? 'stale' : ''}" title="${esc(c.last_checkin_at ? dateTime(c.last_checkin_at) : '')}">${stale ? icon('alert') : ''}${esc(ago(c.last_checkin_at))}</td>
      <td>${c.pending_reviews ? `<span class="badge tone-warn sm">${c.pending_reviews}</span>` : '<span class="muted">0</span>'}</td>
      <td>${pill(c.overall)}</td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" class="empty-row">No participants match.</td></tr>';
}

function progress(r) {
  const pct = n => Math.min(100, (n / Math.max(r.required, 1)) * 100);
  return `<div class="prog" title="${r.counted} counted, ${r.pending} waiting on a decision, needs ${r.required}">
    <span class="bar"><i class="done" style="width:${pct(r.counted)}%"></i><i class="pend" style="left:${pct(r.counted)}%;width:${pct(r.pending)}%"></i></span>
    <span class="n">${r.counted}/${r.required}${r.pending ? ` <span class="muted">+${r.pending}</span>` : ''}</span>
    <span class="cat">${esc(CATEGORY[r.category] || r.category)}</span></div>`;
}

/* ---------- participant ---------- */

let tlLimit = 20;

async function viewParticipant(id) {
  const v = $('#view');
  loading(v);
  let d;
  try { d = await api(`/api/participants/${id}`); } catch (e) { return failed(v, e); }
  const s = d.summary;
  v.innerHTML = `
    <a class="back" href="#caseload">${icon('back')} Caseload</a>
    <section class="card phead">
      <div class="phead-main"><h1>${esc(d.name)}</h1>
        <p class="muted">${[d.case_number && `Case ${esc(d.case_number)}`, d.officer && esc(d.officer),
          `enrolled ${esc(shortDay(d.enrolled_on))}`].filter(Boolean).join(' · ')}</p></div>
      ${pill(s.overall)}
      <button class="btn secondary" data-edit type="button">${icon('note')} Edit</button>
      <button class="btn secondary" data-code type="button">${icon('keyboard')} Sign-in code</button>
      <button class="btn secondary" data-report type="button">${icon('file')} Compliance report</button>
    </section>

    <section class="card">
      <div class="row between"><h2 class="h2 flush">Requirements</h2>
        <button class="btn secondary sm-btn" data-addreq type="button">${icon('plus')} Add requirement</button></div>
      ${d.requirements.length ? d.requirements.map(r => `
        <div class="req-block">
          <div class="row wrap"><b>${r.count_required} ${esc(REQ_NOUN[r.category] || r.category)} a ${esc(r.period)}</b>
            <span class="muted small">${[r.order_reference, `from ${shortDay(r.starts_on)}`, r.ends_on && `to ${shortDay(r.ends_on)}`].filter(Boolean).map(esc).join(' · ')}</span>
            <span class="spacer"></span>
            <button class="linkish small" data-editreq="${r.id}" type="button">Amend</button>
            <button class="linkish small danger-link" data-delreq="${r.id}" type="button">Remove</button></div>
          <div class="chart">${trendChart(r)}</div>
        </div>`).join('') : '<p class="muted small">No requirement on file. Add what the order says.</p>'}
    </section>

    <section class="card">
      <div class="row between"><h2 class="h2 flush">Notes</h2>
        <span class="muted small">Only staff see these. Changes are logged.</span></div>
      <textarea id="pnotes" rows="3" maxlength="4000" placeholder="Anything the next officer should know">${esc(d.notes || '')}</textarea>
      <button class="btn secondary sm-btn" data-savenotes type="button" style="margin-top:8px">Save notes</button>
    </section>

    <section class="card">
      <div class="row between"><h2 class="h2 flush">Check-in history</h2>
        <span class="muted small">${plural(d.timeline.length, 'check-in')}, newest first. Personal meetings are private to the participant and never shown here.</span></div>
      <ol class="ptl">${d.timeline.slice(0, tlLimit).map(ev => ptlItem(ev, d)).join('') || '<li class="muted small">No check-ins yet.</li>'}</ol>
      ${d.timeline.length > tlLimit ? `<button class="btn ghost" data-more type="button">Show all ${d.timeline.length}</button>` : ''}
    </section>`;
  v.querySelector('[data-report]').onclick = () => openReport(d.participant_id, d.name);
  v.querySelector('[data-code]').onclick = () => accessCode(d.participant_id, d.name);
  v.querySelector('[data-edit]').onclick = () => editSheet(d);
  v.querySelector('[data-addreq]').onclick = () => requirementSheet(d.participant_id);
  v.querySelector('[data-savenotes]').onclick = async e => {
    e.target.disabled = true;
    try {
      await api(`/api/participants/${d.participant_id}`, { method: 'PATCH',
        body: JSON.stringify({ notes: $('#pnotes').value }) });
      toast('Notes saved.');
    } catch (err) { toast(err.message); }
    e.target.disabled = false;
  };
  v.querySelectorAll('[data-editreq]').forEach(b => b.onclick = () =>
    requirementSheet(d.participant_id, d.requirements.find(r => r.id === Number(b.dataset.editreq))));
  v.querySelectorAll('[data-delreq]').forEach(b => b.onclick = () => removeRequirement(b.dataset.delreq, id));
  v.querySelector('[data-more]')?.addEventListener('click', () => { tlLimit = 9999; viewParticipant(id); });
  tlLimit = 20;
}

function ptlItem(ev, d) {
  const [label, tone, ic] = engine(ev.status);
  const failed = ev.reasons.filter(r => !r.passed && r.weight > 0);
  const state = ev.review_state;
  const dec = ev.review && { approved: ['Counted', 'ok'], denied: ['Not counted', 'bad'], more_info: ['Asked a question', 'warn'] }[ev.review.decision];
  const action = state === 'open' ? `<a class="btn secondary sm-btn" href="#queue/open/${ev.id}">Decide</a>`
    : state === 'waiting' ? `<a class="btn secondary sm-btn" href="#queue/waiting/${ev.id}">Waiting on ${esc(d.name.split(' ')[0])}</a>`
    : state === 'decided' ? `<a class="linkish small" href="#queue/decided/${ev.id}">Review</a>` : '';
  return `<li class="ev ${ev.status}">
    <div class="ev-top">
      <b>${esc(ev.meeting.name)}</b>
      <span class="badge tone-${tone} sm">${icon(ic)} ${label}</span>
      ${dec ? `<span class="badge tone-${dec[1]} sm">${dec[0]}</span>` : ''}
      <span class="spacer"></span><span class="muted small">${esc(dateTime(ev.checked_in_at))}</span>
    </div>
    <p class="muted small">${feet(ev.distance_ft)} away · confidence ${ev.confidence} ·
      ${ev.checked_out_at ? `checked out ${time(ev.checked_out_at)}` : ev.status === 'rejected' ? 'no check-out needed' : '<span class="warn">no check-out</span>'}
      ${ev.pin_purged ? ' · location deleted by retention' : ''}</p>
    ${failed.length && ev.status !== 'confirmed' ? `<ul class="why">${failed.map(r => `<li>${icon('x')} ${esc(r.reason)}</li>`).join('')}</ul>` : ''}
    ${ev.disputes.map(n => `<div class="note"><b>${esc(d.name.split(' ')[0])} wrote, ${esc(shortDay(n.created_at))}</b>${esc(n.message)}</div>`).join('')}
    ${ev.reviews.map(r => `<div class="note"><b>${esc(r.by)}, ${esc(shortDay(r.at))}: ${esc({ approved: 'counted it', denied: 'did not count it', more_info: 'asked a question' }[r.decision])}</b>${esc(r.note || '')}</div>`).join('')}
    ${action ? `<div class="ev-act">${action}</div>` : ''}
  </li>`;
}

/* Attendance against the requirement, period by period. Every bar carries its
   number, so colour is never the only thing saying a week fell short. */
function trendChart(r) {
  const h = r.history;
  if (!h.length) return '<p class="muted small">No history yet.</p>';
  const W = 720, H = 140, P = { l: 26, r: 10, t: 14, b: 26 };
  const max = Math.max(r.count_required + 1, ...h.map(p => p.counted + p.pending));
  const bw = (W - P.l - P.r) / h.length, y = n => P.t + (1 - n / max) * (H - P.t - P.b);
  let g = '';
  for (let i = 0; i <= max; i++) if (max <= 8 || i % 2 === 0) {
    g += `<line class="gl" x1="${P.l}" x2="${W - P.r}" y1="${y(i)}" y2="${y(i)}"/><text class="lbl" x="${P.l - 6}" y="${y(i) + 3}" text-anchor="end">${i}</text>`;
  }
  const bars = h.map((p, i) => {
    const x = P.l + i * bw + bw * 0.2, w = bw * 0.6, top = y(p.counted), ptop = y(p.counted + p.pending);
    const cls = p.counted >= r.count_required ? 'met' : 'short';
    const label = `Week of ${shortDay(p.starts)}: ${p.counted} counted, ${p.pending} waiting on a decision, needs ${r.count_required}`;
    return `${p.pending ? `<rect class="b-pend" x="${x}" y="${ptop}" width="${w}" height="${Math.max(top - ptop, 0)}" rx="3"><title>${label}</title></rect>` : ''}
      <rect class="b-${cls}" x="${x}" y="${top}" width="${w}" height="${Math.max(y(0) - top, 0)}" rx="3"><title>${label}</title></rect>
      ${p.counted === 0 && !p.pending ? `<rect class="b-zero" x="${x}" y="${y(0) - 2}" width="${w}" height="2"><title>${label}</title></rect>` : ''}
      <text class="${y(0) - top > 15 ? 'val' : 'valo'}" x="${x + w / 2}" y="${y(0) - top > 15 ? top + 12 : top - 4}" text-anchor="middle">${p.counted}</text>
      <text class="lbl" x="${x + w / 2}" y="${H - 8}" text-anchor="middle">${esc(monthDay(p.starts))}</text>`;
  }).join('');
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(`Attendance per ${r.period} against the requirement of ${r.count_required}`)}">
    ${g}${bars}<line class="req" x1="${P.l}" x2="${W - P.r}" y1="${y(r.count_required)}" y2="${y(r.count_required)}"/>
    <text class="lbl strong" x="${W - P.r}" y="${y(r.count_required) - 5}" text-anchor="end">required ${r.count_required}</text></svg>`;
}

/* ---------- enrolling and editing ---------- */

async function pickers() {
  if (S.pickers) return S.pickers;
  const [officers, programs] = await Promise.all([api('/api/officers'), api('/api/programs')]);
  S.pickers = { officers, programs };
  return S.pickers;
}

const optionList = (rows, selected, label = r => r.name) => rows.map(r =>
  `<option value="${r.id}" ${String(r.id) === String(selected) ? 'selected' : ''}>${esc(label(r))}</option>`).join('');

async function enrolSheet() {
  const { officers, programs } = await pickers();
  const sh = sheet(`<form class="step left" id="enrolForm">
    <h2>Add a participant</h2>
    <p class="muted">The mobile number is how they sign in, so it has to be theirs. You will get a code to give them.</p>
    <label>Name<input name="name" required maxlength="120" autocomplete="off" placeholder="First and last name"></label>
    <label>Mobile number<input name="phone" type="tel" required maxlength="32" placeholder="(734) 555-0101"></label>
    <div class="two">
      <label>Case number<input name="case_number" maxlength="64" placeholder="26-1234-FH"></label>
      <label>Program<select name="program_id"><option value="">None</option>${optionList(programs, '')}</select></label>
    </div>
    <label>Officer<select name="officer_id">${optionList(officers, S.me.id)}</select></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Add participant</button>
  </form>`, { label: 'Add a participant' });
  const form = sh.body.querySelector('#enrolForm');
  form.name.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Adding';
    const body = Object.fromEntries(new FormData(form));
    body.program_id = body.program_id ? Number(body.program_id) : null;
    body.officer_id = Number(body.officer_id);
    try {
      const made = await api('/api/participants', { method: 'POST', body: JSON.stringify(body) });
      sh.set(`<div class="step">
        <div class="hero-ic tone-ok">${icon('check')}</div>
        <h2>${esc(made.name)} is enrolled</h2>
        <p class="muted">They sign in on the phone app with <b>${esc(made.phone)}</b> and this code, which works once, for ${made.code_minutes} minutes.</p>
        <p class="bigcode">${esc(made.sign_in_code.slice(0, 3))} ${esc(made.sign_in_code.slice(3))}</p>
        <a class="btn block" href="#participant/${made.participant_id}" data-close>Open their record</a>
        <button class="btn block ghost" data-close type="button">Done</button>
      </div>`);
      viewCaseload();
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Add participant';
      const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false;
    }
  };
}

async function editSheet(d) {
  const { officers, programs } = await pickers();
  const sh = sheet(`<form class="step left" id="editForm">
    <h2>Edit ${esc(d.name)}</h2>
    <label>Name<input name="name" required maxlength="120" value="${esc(d.name)}"></label>
    <label>Mobile number<input name="phone" type="tel" required maxlength="32" value="${esc(d.phone || '')}"></label>
    <div class="two">
      <label>Case number<input name="case_number" maxlength="64" value="${esc(d.case_number || '')}"></label>
      <label>Officer<select name="officer_id">${optionList(officers, d.officer_id)}</select></label>
    </div>
    <label>Program<select name="program_id"><option value="">None</option>${optionList(programs, d.program_id)}</select></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Save changes</button>
    <button class="btn block ghost danger-link" data-close-case type="button">Close this case</button>
  </form>`, { label: 'Edit participant' });
  const form = sh.body.querySelector('#editForm');
  form.onsubmit = async e => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    body.program_id = body.program_id ? Number(body.program_id) : null;
    body.officer_id = Number(body.officer_id);
    try {
      await api(`/api/participants/${d.participant_id}`, { method: 'PATCH', body: JSON.stringify(body) });
      sh.close(); toast('Saved.'); viewParticipant(d.participant_id);
    } catch (err) {
      const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false;
    }
  };
  form.querySelector('[data-close-case]').onclick = () => {
    sh.set(`<div class="step left"><h2>Close this case?</h2>
      <p class="muted">${esc(d.name)} is signed out and cannot check in again. The record stays, and the case can be reopened.</p>
      <button class="btn block danger" data-yes type="button">Close the case</button>
      <button class="btn block ghost" data-close type="button">Keep it open</button></div>`);
    sh.body.querySelector('[data-yes]').onclick = async () => {
      try {
        await api(`/api/participants/${d.participant_id}`, { method: 'PATCH',
          body: JSON.stringify({ active: false, ends_on: new Date().toISOString() }) });
        sh.close(); toast('Case closed.'); location.hash = 'caseload';
      } catch (err) { toast(err.message); }
    };
  };
}

/* Date inputs work in the local calendar. Going through toISOString would
   read "tomorrow" any evening after 8pm Eastern, which is exactly when an
   officer is catching up on paperwork. */
function localDate(d) {
  const x = new Date(d);
  return `${x.getFullYear()}-${String(x.getMonth() + 1).padStart(2, '0')}-${String(x.getDate()).padStart(2, '0')}`;
}
const dateValue = iso => iso ? localDate(iso) : '';
const dateToIso = v => v ? new Date(`${v}T12:00:00`).toISOString() : null;

function requirementSheet(pid, existing = null) {
  const r = existing || {};
  const cats = [['public', 'Public meetings'], ['community_service', 'Community service'],
    ['private', 'Private meetings'], ['any', 'Any meeting']];
  const sh = sheet(`<form class="step left" id="reqForm">
    <h2>${existing ? 'Amend the requirement' : 'Add a requirement'}</h2>
    <p class="muted">What the order says, in the court's words: how many, of what, per week or month.</p>
    <div class="two">
      <label>How many<input name="count_required" type="number" min="1" max="50" required value="${r.count_required || 3}"></label>
      <label>Per<select name="period">
        <option value="week" ${r.period === 'week' || !r.period ? 'selected' : ''}>Week</option>
        <option value="month" ${r.period === 'month' ? 'selected' : ''}>Month</option></select></label>
    </div>
    <label>Of what<select name="category">${cats.map(([v, l]) =>
      `<option value="${v}" ${(r.category || 'public') === v ? 'selected' : ''}>${l}</option>`).join('')}</select></label>
    <div class="two">
      <label>Starts<input name="starts_on" type="date" required value="${dateValue(r.starts_on) || localDate(new Date())}"></label>
      <label>Ends <span class="muted">(optional)</span><input name="ends_on" type="date" value="${dateValue(r.ends_on)}"></label>
    </div>
    <label>Court order reference<input name="order_reference" maxlength="120" value="${esc(r.order_reference || '')}" placeholder="ORD-26-123"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">${existing ? 'Save the amendment' : 'Add requirement'}</button>
    ${existing ? '<p class="fine">Amending changes what past weeks needed. The audit log keeps what it said before.</p>' : ''}
  </form>`, { label: 'Requirement' });
  const form = sh.body.querySelector('#reqForm');
  form.onsubmit = async e => {
    e.preventDefault();
    const body = Object.fromEntries(new FormData(form));
    body.count_required = Number(body.count_required);
    body.starts_on = dateToIso(body.starts_on);
    body.ends_on = dateToIso(body.ends_on);
    if (body.category === 'any') body.category = null;
    if (!body.order_reference) delete body.order_reference;
    try {
      if (existing) await api(`/api/requirements/${existing.id}`, { method: 'PATCH', body: JSON.stringify(body) });
      else await api('/api/requirements', { method: 'POST', body: JSON.stringify({ ...body, participant_id: pid }) });
      sh.close(); toast(existing ? 'Requirement amended.' : 'Requirement added.'); viewParticipant(pid);
    } catch (err) {
      const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false;
    }
  };
}

function removeRequirement(rid, pid) {
  const sh = sheet(`<div class="step left"><h2>Remove this requirement?</h2>
    <p class="muted">Use this for one entered by mistake. If the order ended, amend it with an end date instead, so the history still makes sense.</p>
    <button class="btn block danger" data-yes type="button">Remove it</button>
    <button class="btn block ghost" data-close type="button">Cancel</button></div>`, { label: 'Remove requirement' });
  sh.body.querySelector('[data-yes]').onclick = async () => {
    try { await api(`/api/requirements/${rid}`, { method: 'DELETE' }); sh.close(); toast('Requirement removed.'); viewParticipant(pid); }
    catch (err) { sh.close(); toast(err.message); }
  };
}

/* A code to read out so the participant can sign in on their phone. Until
   SMS is wired up this is how someone gets in on a new device, and it is in
   the audit log like any other access. */
async function accessCode(pid, name) {
  let r;
  try { r = await api(`/api/auth/participants/${pid}/access-code`, { method: 'POST' }); }
  catch (e) { return toast(e.message); }
  sheet(`<div class="step">
    <h2>Sign-in code for ${esc(name)}</h2>
    <p class="muted">Read this out, or write it down for them. It works once, for ${r.minutes} minutes.</p>
    <p class="bigcode">${esc(r.code.slice(0, 3))} ${esc(r.code.slice(3))}</p>
    <p class="muted">They open the phone app, enter <b>${esc(r.phone)}</b>, then this code.</p>
    <p class="fine">Issuing a code is written to the audit log.</p>
    <button class="btn block" data-close type="button">Done</button>
  </div>`, { label: 'Sign-in code' });
}

async function openReport(pid, name) {
  let d;
  try { d = await api(`/api/reports/participant/${pid}?days=90`); } catch (e) { toast(e.message); return; }
  const sh = sheet(`<div class="step left report">
    <h2>Compliance report</h2>
    <p class="muted">${esc(name)} · last 90 days · generated ${esc(dateTime(d.generated_at))}</p>
    <div class="reqs big">${d.summary.requirements.map(progress).join('') || '<p class="muted small">No active requirement.</p>'}</div>
    <p class="callout">${icon('shield')} ${esc(d.note)}</p>
    <p class="small muted">Verification hash <code>${esc(d.content_hash.slice(0, 16))}</code>. A printed copy can be checked against the record with this.</p>
    <details><summary>Everything in the report</summary><pre class="json">${esc(JSON.stringify(d, null, 2))}</pre></details>
    <button class="btn block" data-csv type="button">${icon('download')} Download CSV</button>
  </div>`, { label: 'Compliance report', wide: true });
  sh.body.querySelector('[data-csv]').onclick = () =>
    download(`/api/reports/participant/${pid}.csv?days=90`, `report-${pid}.csv`).catch(e => toast(e.message));
}

/* ---------- meetings ---------- */

async function viewMeetings() {
  const v = $('#view');
  loading(v);
  let meetings;
  try { meetings = await api('/api/meetings'); } catch (e) { return failed(v, e); }
  const STATE = { open: ['Open now', 'ok'], upcoming: ['Upcoming', 'neutral'], in_progress: ['In progress', 'warn'], ended: ['Ended', 'neutral'] };
  v.innerHTML = `
    <div class="page-head"><div><h1>Meetings</h1><p class="muted">Meetings that count toward a requirement. Participants' personal meetings are private and not listed.</p></div></div>
    <div class="msplit">
      <section class="card flush">
        <div class="toolbar"><span class="muted small">${plural(meetings.length, 'meeting')}</span></div>
        <div class="table-wrap tall"><table class="grid">
          <thead><tr><th>Name</th><th>Type</th><th>Starts</th><th>Check-in</th><th>Radius</th><th></th></tr></thead>
          <tbody>${meetings.map(m => `<tr>
            <td><b>${esc(m.name)}</b><div class="muted small">${esc(m.address || '')}</div></td>
            <td>${esc(CATEGORY[m.category] || m.category)}</td>
            <td>${esc(shortDay(m.starts_at))}, ${time(m.starts_at)}<div class="muted small">${m.duration_min} min</div></td>
            <td><span class="badge sm tone-${STATE[m.window_state][1]}">${STATE[m.window_state][0]}</span><div class="muted small">±${m.time_window_min} min</div></td>
            <td>${feet(m.radius_ft)}</td>
            <td><button class="btn ghost sm-btn" data-del="${m.id}" data-name="${esc(m.name)}" type="button" aria-label="Delete ${esc(m.name)}">${icon('trash')}</button></td>
          </tr>`).join('')}</tbody></table></div>
      </section>
      <div class="mside">
        <section class="card">
          <h2 class="h2">Add a meeting</h2>
          <form id="newMeeting" class="form">
            <label>Name<input name="name" required maxlength="200" placeholder="AA Open Meeting, St. Andrew's"></label>
            <div class="two">
              <label>Type<select name="category"><option value="public">Public</option><option value="community_service">Community service</option>
                <option value="private">Private</option></select></label>
              <label>Starts<input name="starts_at" type="datetime-local" required></label>
              <label>Length (min)<input name="duration_min" type="number" value="60" min="5" max="1440"></label>
              <label>Radius (ft)<input name="radius_ft" type="number" value="500" min="50" max="5280"></label>
              <label>Check-in window (± min)<input name="time_window_min" type="number" value="60" min="5" max="480"></label>
              <label>Address<input name="address" maxlength="300"></label>
            </div>
            <input type="hidden" name="lat"><input type="hidden" name="lng">
            <p class="small muted" id="pinHint">${icon('pin')} Click the map to place the meeting.</p>
            <button class="btn block" type="submit">Create meeting</button>
          </form>
        </section>
        <section class="card flush"><div id="mmap" class="mmap"></div></section>
      </div>
    </div>`;
  drawMeetingsMap(meetings);
  v.querySelector('#newMeeting').onsubmit = createMeeting;
  v.querySelector('tbody').onclick = e => {
    const b = e.target.closest('[data-del]');
    if (b) deleteMeeting(Number(b.dataset.del), b.dataset.name);
  };
}

function drawMeetingsMap(meetings) {
  const L = window.L;
  if (!L) return;
  S.mmap = L.map('mmap', { scrollWheelZoom: false }).setView([42.28, -83.74], 11);
  S.mmap.attributionControl.setPrefix(false);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(S.mmap);
  const ink = getComputedStyle(document.documentElement).getPropertyValue('--ink').trim();
  const group = L.featureGroup().addTo(S.mmap);
  meetings.forEach(m => {
    L.circle([m.lat, m.lng], { radius: m.radius_ft / 3.28084, color: ink, weight: 1, dashArray: '4', fillOpacity: 0.04 }).addTo(group);
    L.circleMarker([m.lat, m.lng], { radius: 5, color: ink, fillOpacity: 1 }).addTo(group).bindTooltip(esc(m.name));
  });
  if (group.getLayers().length) S.mmap.fitBounds(group.getBounds().pad(0.2));
  S.mmap.on('click', e => {
    const f = $('#newMeeting');
    f.lat.value = e.latlng.lat.toFixed(6); f.lng.value = e.latlng.lng.toFixed(6);
    if (S.pin) S.pin.remove();
    S.pin = L.marker(e.latlng).addTo(S.mmap);
    $('#pinHint').innerHTML = `${icon('check')} Placed at ${e.latlng.lat.toFixed(5)}, ${e.latlng.lng.toFixed(5)}. Click again to move it.`;
  });
  setTimeout(() => S.mmap?.invalidateSize(), 60);
}

async function createMeeting(e) {
  e.preventDefault();
  const f = e.target, b = Object.fromEntries(new FormData(f));
  if (!b.lat) { toast('Click the map to place the meeting first.'); return; }
  ['lat', 'lng', 'duration_min', 'radius_ft', 'time_window_min'].forEach(k => { b[k] = Number(b[k]); });
  b.starts_at = new Date(b.starts_at).toISOString();
  if (!b.address) delete b.address;
  try {
    await api('/api/meetings', { method: 'POST', body: JSON.stringify(b) });
    toast('Meeting created.');
    S.pin = null;
    viewMeetings();
  } catch (err) { toast(err.message); }
}

function deleteMeeting(id, name) {
  const sh = sheet(`<div class="step left"><h2>Delete this meeting?</h2>
    <p><b>${esc(name)}</b></p><p class="muted">Only meetings nobody has checked in to can be deleted. Anything with a check-in is part of someone's record.</p>
    <button class="btn block danger" data-yes type="button">Delete meeting</button>
    <button class="btn block ghost" data-close type="button">Keep it</button></div>`, { label: 'Delete meeting' });
  sh.body.querySelector('[data-yes]').onclick = async () => {
    try { await api(`/api/meetings/${id}`, { method: 'DELETE' }); sh.close(); toast('Meeting deleted.'); viewMeetings(); }
    catch (err) { sh.close(); toast(err.message); }
  };
}

/* ---------- audit ---------- */

const ACTIONS = {
  view_caseload: 'Viewed the caseload', view_participant: 'Opened a record', view_review_queue: 'Opened the review queue',
  view_review_item: 'Opened a check-in under review', decide_review: 'Decided a review', change_review: 'Changed a decision',
  export_report: 'Exported a report', view_ops_map: 'Viewed the ops map', run_retention: 'Ran the retention job',
  create_participant: 'Added a participant', create_requirement: 'Added a requirement', delete_requirement: 'Removed a requirement',
  create_meeting: 'Created a meeting', delete_meeting: 'Deleted a meeting', dispute: 'Participant added a note',
  accept_consent: 'Participant accepted consent', export_own_record: 'Participant downloaded their record',
};

async function viewAudit() {
  const v = $('#view');
  if (!['admin', 'superuser'].includes(S.me.role)) { location.hash = 'caseload'; return; }
  loading(v);
  let rows;
  try { rows = await api('/api/audit?limit=500'); } catch (e) { return failed(v, e); }
  v.innerHTML = `
    <div class="page-head"><div><h1>Audit log</h1>
      <p class="muted">Every staff view of a person's record, every decision and every export. Append-only: nothing here can be edited or deleted.</p></div>
      <button class="btn secondary" data-retention type="button">${icon('trash')} Run retention job</button></div>
    <section class="card flush">
      <div class="toolbar"><select id="aact" aria-label="Filter by action"><option value="">All actions</option>
        ${[...new Set(rows.map(r => r.action))].sort().map(a => `<option value="${esc(a)}">${esc(ACTIONS[a] || a)}</option>`).join('')}</select>
        <input type="search" id="awho" placeholder="Search person or detail" aria-label="Search person or detail">
        <span class="spacer"></span><span class="muted small" id="acount"></span></div>
      <div class="table-wrap tall"><table class="grid"><thead><tr><th>When</th><th>Who</th><th>What</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead>
        <tbody id="arows"></tbody></table></div>
    </section>`;
  const draw = () => {
    const a = $('#aact').value, q = $('#awho').value.trim().toLowerCase();
    const shown = rows.filter(r => (!a || r.action === a) &&
      (!q || `${r.actor} ${r.detail}`.toLowerCase().includes(q)));
    $('#acount').textContent = `${shown.length} of ${rows.length}`;
    $('#arows').innerHTML = shown.map(r => `<tr>
      <td class="nowrap">${esc(dateTime(r.at))}</td><td>${esc(r.actor || '')}</td>
      <td>${esc(ACTIONS[r.action] || r.action)}</td>
      <td class="muted">${esc(r.target_type || '')}${r.target_id ? ` #${r.target_id}` : ''}</td>
      <td class="small">${esc(r.detail || '')}</td><td class="muted small">${esc(r.ip || '')}</td></tr>`).join('')
      || '<tr><td colspan="6" class="empty-row">Nothing logged yet.</td></tr>';
  };
  draw();
  $('#aact').onchange = draw; $('#awho').oninput = draw;
  v.querySelector('[data-retention]').onclick = () => {
    const sh = sheet(`<div class="step left"><h2>Run the retention job?</h2>
      <p class="muted">Exact coordinates older than the retention window are deleted from check-ins. The result, distance and reasons stay. This cannot be undone. In production a schedule runs it nightly.</p>
      <button class="btn block danger" data-yes type="button">Delete old coordinates</button>
      <button class="btn block ghost" data-close type="button">Cancel</button></div>`, { label: 'Retention job' });
    sh.body.querySelector('[data-yes]').onclick = async () => {
      try {
        const r = await api('/api/retention/run', { method: 'POST' });
        sh.close(); toast(`Deleted coordinates from ${plural(r.purged, 'check-in')}.`); viewAudit();
      } catch (e) { sh.close(); toast(e.message); }
    };
  };
}

/* ---------- identity ---------- */

function switchUser() {
  const sh = sheet(`<div class="step left"><h2>Switch staff member</h2>
    <p class="small muted">Demo only. In production each person signs in with their own account and multi-factor authentication.</p>
    <ul class="pick">${S.staff.map(u => `<li><button class="pick-row ${String(u.id) === identity.get() ? 'on' : ''}" data-uid="${u.id}" type="button">
      ${esc(u.name)} <span class="muted small">${esc(u.role)}</span></button></li>`).join('')}</ul></div>`, { label: 'Switch staff member' });
  sh.body.addEventListener('click', e => {
    const b = e.target.closest('[data-uid]');
    if (b) { identity.set(b.dataset.uid); location.reload(); }
  });
}

/* ---------- signing in ---------- */

function signIn({ email = '', mfa = false, message = '' } = {}) {
  app.innerHTML = `<main class="signin-page">
    <form class="card signin-card" id="signinForm" novalidate>
      <div class="signin-head">${logo()}<b>MeetingsCount</b></div>
      <h1>${mfa ? 'One more step' : 'Staff sign-in'}</h1>
      <p class="muted">${mfa ? 'Enter the six-digit code from your authenticator app.'
        : 'For probation officers and county staff. Participants use the phone app.'}</p>
      <label>Email<input name="email" type="email" autocomplete="username" required
        value="${esc(email)}" ${mfa ? 'readonly' : ''}></label>
      <label>Password<input name="password" type="password" autocomplete="current-password" required></label>
      ${mfa ? `<label>Authenticator code<input name="code" inputmode="numeric" autocomplete="one-time-code"
        maxlength="6" class="code-input" placeholder="000000" required></label>` : ''}
      <p class="err" id="signinErr" role="alert" ${message ? '' : 'hidden'}>${esc(message)}</p>
      <button class="btn block" type="submit">Sign in</button>
      ${S.ctx.demo ? demoPanel() : ''}
    </form>
  </main>`;
  const form = $('#signinForm');
  (mfa ? form.code : form.email).focus();
  if (S.ctx.demo) loadDemoAccounts();
  form.onsubmit = async e => {
    e.preventDefault();
    const btn = form.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Signing in';
    const body = { email: form.email.value.trim(), password: form.password.value,
                   code: form.code ? form.code.value.trim() : null };
    try {
      const r = await api('/api/auth/login', { method: 'POST', body: JSON.stringify(body) });
      if (r.mfa_required) return signIn({ email: body.email, mfa: true });
      location.reload();
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Sign in';
      const el = $('#signinErr'); el.textContent = err.message; el.hidden = false;
      form.password.value = '';
      (mfa ? form.code : form.password).focus();
    }
  };
}

/* On a demo deployment the seeded accounts are listed, so a demonstration
   does not open with someone mistyping a password. MC_DEMO=0 removes it. */
function demoPanel() {
  return `<div class="demo-panel"><b>${icon('users')} Demo accounts</b>
    <p class="muted small">This is seeded data, not a real county.</p>
    <div id="demoList" class="muted small">Loading…</div></div>`;
}

async function loadDemoAccounts() {
  let d;
  try { d = await api('/api/auth/demo-accounts'); } catch { return; }
  const list = $('#demoList');
  if (!list) return;
  const nice = { admin: 'county admin', officer: 'probation officer' };
  list.innerHTML = d.staff.map(u => `<button type="button" class="demo-row" data-email="${esc(u.email)}">
      <span>${esc(u.name.replace(/ \(county\)$/, ''))}<span class="muted"> · ${esc(nice[u.role] || u.role)}</span></span>
      <span class="muted">use</span></button>`).join('')
    + `<p class="muted small">Participants sign in on the phone app with a phone number such as
       <b>${esc(d.participants[0]?.phone || '')}</b> and a code their officer reads out.</p>`;
  list.onclick = e => {
    const row = e.target.closest('[data-email]');
    if (!row) return;
    const form = $('#signinForm');
    form.email.value = row.dataset.email;
    form.password.value = d.password;
    form.querySelector('button[type=submit]').focus();
  };
}

function accountSheet() {
  const sh = sheet(`<div class="step left">
    <h2>${esc(S.me.name)}</h2>
    <p class="muted">${esc(S.me.role)}${S.me.county ? ` · ${esc(S.me.county)}` : ''}</p>
    <div class="acct-rows">
      <button class="link-row" data-a="password" type="button">${icon('shield')}
        <span><b>Change password</b><br><span class="muted small">Signs out every other device</span></span></button>
      <button class="link-row" data-a="mfa" type="button">${icon('keyboard')}
        <span><b>Authenticator app</b><br><span class="muted small" id="mfaState">Checking…</span></span></button>
      <button class="link-row" data-a="signout" type="button">${icon('back')}
        <span><b>Sign out</b></span></button>
    </div>
  </div>`, { label: 'Your account' });
  api('/api/me').then(me => {
    const el = sh.body.querySelector('#mfaState');
    if (el) el.textContent = me.mfa ? 'On. A code is required at sign-in.' : 'Off. Turn it on for a second factor.';
    sh.body.dataset.mfa = me.mfa ? '1' : '';
  }).catch(() => {});
  sh.body.onclick = e => {
    const b = e.target.closest('[data-a]');
    if (!b) return;
    if (b.dataset.a === 'signout') return signOut();
    sh.close();
    if (b.dataset.a === 'password') passwordSheet();
    else mfaSheet(sh.body.dataset.mfa === '1');
  };
}

function passwordSheet() {
  const sh = sheet(`<form class="step left" id="pwForm">
    <h2>Change password</h2>
    <p class="muted">At least 12 characters. A phrase you can remember beats a short scramble.</p>
    <label>Current password<input name="current_password" type="password" autocomplete="current-password" required></label>
    <label>New password<input name="new_password" type="password" autocomplete="new-password" required></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Change password</button>
  </form>`, { label: 'Change password' });
  const form = sh.body.querySelector('#pwForm');
  form.current_password.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/api/auth/password', { method: 'POST', body: JSON.stringify({
        current_password: form.current_password.value, new_password: form.new_password.value }) });
      sh.close();
      toast('Password changed. Other devices are signed out.');
    } catch (err) {
      const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false;
    }
  };
}

async function mfaSheet(enabled) {
  if (enabled) {
    const sh = sheet(`<form class="step left" id="offForm">
      <h2>Turn off the authenticator app</h2>
      <p class="muted">Sign-in will need only your password after this.</p>
      <label>Your password<input name="current_password" type="password" autocomplete="current-password" required></label>
      <p class="err" role="alert" hidden></p>
      <button class="btn block danger" type="submit">Turn it off</button>
    </form>`, { label: 'Authenticator app' });
    const form = sh.body.querySelector('#offForm');
    form.onsubmit = async e => {
      e.preventDefault();
      try {
        await api('/api/auth/totp/disable', { method: 'POST',
          body: JSON.stringify({ current_password: form.current_password.value }) });
        sh.close(); toast('Authenticator app turned off.');
      } catch (err) { const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false; }
    };
    return;
  }
  let start;
  try { start = await api('/api/auth/totp/start', { method: 'POST' }); }
  catch (e) { return toast(e.message); }
  const sh = sheet(`<form class="step left" id="onForm">
    <h2>Set up an authenticator app</h2>
    <p class="muted">In Google Authenticator, 1Password, Authy or similar, add an account by hand and type this key:</p>
    <p class="secret">${esc(start.formatted)}</p>
    <p class="fine">Account: ${esc(S.me.name)} · Issuer: MeetingsCount · 6 digits · 30 seconds</p>
    <label>Then enter the code it shows<input name="code" inputmode="numeric" maxlength="6"
      class="code-input" placeholder="000000" autocomplete="one-time-code" required></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Turn it on</button>
  </form>`, { label: 'Authenticator app' });
  const form = sh.body.querySelector('#onForm');
  form.code.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/api/auth/totp/enable', { method: 'POST', body: JSON.stringify({ code: form.code.value.trim() }) });
      sh.close(); toast('Authenticator app is on. You will need a code next time you sign in.');
    } catch (err) { const el = form.querySelector('.err'); el.textContent = err.message; el.hidden = false; }
  };
}

async function signOut() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* going anyway */ }
  location.href = '/officer';
}

function participantHere() {
  app.innerHTML = `<main class="center-screen">${logo()}<h1>This is the staff dashboard</h1>
    <p class="muted">You are signed in as a participant.</p><a class="btn" href="/">Open the participant app</a></main>`;
}

function fatal(e) {
  app.innerHTML = `<main class="center-screen">${icon('alert', 'lg')}<h1>MeetingsCount could not load</h1>
    <p class="muted">${esc(e.message)}</p><button class="btn" type="button" data-reload>Try again</button></main>`;
  app.querySelector('[data-reload]').onclick = () => location.reload();
}

boot();
