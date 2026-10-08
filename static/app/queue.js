// The review queue. Every check-in the engine could not settle waits here,
// and nothing counts or fails until an officer decides.
//
// Built for getting through a queue quickly without deciding carelessly:
// the list says why each item is here, the detail shows the evidence, what
// the decision would do to the person's week, and how check-ins usually go
// at that venue. Keyboard: j and k to move, a / d / q to decide, x to select.

import {
  $, $$, ago, dateTime, duration, esc, feet, icon, plural, shortDay, time, toast,
} from './lib.js';

const CATEGORY = { public: 'public meetings', community_service: 'community service',
  private: 'private meetings', therapy: 'therapy', any: 'meetings' };
const DECISION = {
  approved: { label: 'Counted', tone: 'ok', icon: 'check', verb: 'Counted this check-in' },
  denied: { label: 'Not counted', tone: 'bad', icon: 'x', verb: 'Decided it does not count' },
  more_info: { label: 'Asked a question', tone: 'warn', icon: 'question', verb: 'Asked a question' },
};
const TABS = [
  ['open', 'To decide'],
  ['waiting', 'Waiting on participant'],
  ['decided', 'Decided, last 14 days'],
];
const FILTERS = [
  ['distance', 'Too far'], ['time', 'Timing'], ['gps', 'Weak GPS'],
  ['note', 'Has a note'], ['week', 'This week'],
];

const Q = {
  tab: 'open', scope: 'all', filters: new Set(), search: '',
  items: [], counts: {}, sel: null, detail: null, picked: new Set(),
  map: null, mounted: false, ctx: null, drafts: {},
};

/* Decisions wait this long before they are sent, so a slip of the keyboard
   can be undone. Anything still waiting is sent at once if the page closes. */
const UNDO_MS = 6000;
const PENDING = new Set();                          // { ids, send, timer, done }

/* ---------- mounting ---------- */

export function mountQueue(ctx, root, { tab = 'open', id = null } = {}) {
  Q.ctx = ctx;
  Q.mounted = true;
  if (tab !== Q.tab) { Q.picked.clear(); Q.sel = null; }
  Q.tab = TABS.some(t => t[0] === tab) ? tab : 'open';
  if (id) Q.sel = id;
  root.innerHTML = frame();
  wireFrame(root);
  load();
}

export function unmountQueue() {
  PENDING.forEach(job => flush(job));
  Q.mounted = false;
  if (Q.map) { Q.map.remove(); Q.map = null; }
}

function frame() {
  return `<div class="page-head">
      <div><h1>Review queue</h1>
        <p class="muted">Check-ins the engine could not settle. Nothing counts or fails until you decide.</p></div>
      <button class="btn secondary small-btn" data-q="keys" type="button">${icon('keyboard')} Shortcuts</button>
    </div>
    <div class="qbar">
      <div class="tabbar" role="tablist">${TABS.map(([k, label]) => `
        <a role="tab" href="#queue/${k}" class="${Q.tab === k ? 'on' : ''}" aria-selected="${Q.tab === k}">
          ${label}<span class="n" data-count="${k}">${Q.counts[k] ?? ''}</span></a>`).join('')}</div>
      <div class="qtools">
        <div class="seg" role="group" aria-label="Whose participants">
          <button data-scope="all" class="${Q.scope === 'all' ? 'on' : ''}" type="button">Everyone</button>
          <button data-scope="mine" class="${Q.scope === 'mine' ? 'on' : ''}" type="button">My caseload</button>
        </div>
        <div class="filters" role="group" aria-label="Filters">${FILTERS.map(([k, label]) =>
          `<button class="fchip ${Q.filters.has(k) ? 'on' : ''}" data-filter="${k}" type="button" aria-pressed="${Q.filters.has(k)}">${label}</button>`).join('')}</div>
        <input type="search" id="qsearch" placeholder="Search by name" value="${esc(Q.search)}" aria-label="Search by name">
      </div>
    </div>
    <div class="qgrid">
      <section class="qlist card flush" id="qlist" aria-label="Items"></section>
      <section class="qdetail card" id="qdetail" aria-live="polite"></section>
    </div>`;
}

function wireFrame(root) {
  root.querySelector('.seg').onclick = e => {
    const b = e.target.closest('[data-scope]'); if (!b) return;
    Q.scope = b.dataset.scope;
    $$('.seg [data-scope]', root).forEach(x => x.classList.toggle('on', x === b));
    Q.picked.clear(); load();
  };
  root.querySelector('.filters').onclick = e => {
    const b = e.target.closest('[data-filter]'); if (!b) return;
    const k = b.dataset.filter;
    Q.filters.has(k) ? Q.filters.delete(k) : Q.filters.add(k);
    b.classList.toggle('on', Q.filters.has(k)); b.setAttribute('aria-pressed', Q.filters.has(k));
    renderList(); ensureSelection();
  };
  root.querySelector('#qsearch').oninput = e => { Q.search = e.target.value; renderList(); ensureSelection(); };
  root.querySelector('[data-q="keys"]').onclick = showKeys;
  root.querySelector('#qlist').addEventListener('click', onListClick);
  root.querySelector('#qdetail').addEventListener('click', onDetailClick);
}

async function load() {
  try {
    const r = await Q.ctx.api(`/api/reviews/queue?state=${Q.tab}${Q.scope === 'mine' ? '&mine_only=true' : ''}`);
    const held = pendingIds();
    Q.items = sortItems(r.items.filter(i => !held.has(i.id)));
    Q.counts = r.counts;
    if (Q.tab !== 'decided') Q.counts[Q.tab] -= r.items.length - Q.items.length;
  } catch (e) {
    $('#qlist').innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  }
  for (const id of [...Q.picked]) if (!Q.items.some(i => i.id === id)) Q.picked.delete(id);
  syncCounts();
  renderList();
  ensureSelection();
}

/* Answered questions first: the officer asked, the participant replied, and
   the officer is the one being waited on now. Then oldest first. */
function sortItems(items) {
  const newest = Q.tab === 'decided';
  return [...items].sort((a, b) => (Q.tab === 'open' && b.replied - a.replied) ||
    (newest ? b.since.localeCompare(a.since) : a.since.localeCompare(b.since)));
}

function pendingIds() {
  const out = new Set();
  PENDING.forEach(j => j.ids.forEach(id => out.add(id)));
  return out;
}

function syncCounts() {
  $$('[data-count]').forEach(el => { el.textContent = Q.counts[el.dataset.count] ?? ''; });
  Q.ctx.onChange?.(Q.counts);
}

/* ---------- the list ---------- */

function visible() {
  const term = Q.search.trim().toLowerCase();
  return Q.items.filter(i => {
    if (term && !i.participant.name.toLowerCase().includes(term)) return false;
    const kinds = new Set(signals(i).map(s => s.kind));
    for (const f of Q.filters) {
      if (f === 'note' && !(i.notes_count || i.replied)) return false;
      if (f === 'week' && !i.this_week) return false;
      if (['distance', 'time', 'gps'].includes(f) && !kinds.has(f)) return false;
    }
    return true;
  });
}

/* Why an item is here, in a few words: the hard failures if there are any,
   otherwise the weakest soft signals. "Low confidence" tells nobody anything. */
function signals(i) {
  const out = [];
  const say = (r) => {
    const d = r.data || {};
    if (r.name === 'distance') return { kind: 'distance', text: r.passed ? `${feet(d.distance_ft)}, limit ${feet(d.radius_ft)}` : `${feet(d.distance_ft)} away` };
    if (r.name === 'time_window') {
      const m = Math.round(d.minutes_from_start || 0);
      return { kind: 'time', text: m >= 0 ? `${duration(m)} late` : `${duration(-m)} early` };
    }
    if (r.name === 'gps_accuracy') return { kind: 'gps', text: d.accuracy_m == null ? 'No GPS accuracy' : `GPS ±${Math.round(d.accuracy_m)} m` };
    if (r.name === 'network' && !/not configured/.test(r.reason)) return { kind: 'network', text: 'VPN or proxy' };
    return null;
  };
  const scored = i.reasons.filter(r => r.weight > 0);
  scored.filter(r => !r.passed).forEach(r => { const s = say(r); if (s) out.push({ ...s, tone: r.hard ? 'bad' : 'warn' }); });
  if (!out.length) {
    scored.filter(r => r.score < 0.8).sort((a, b) => a.score - b.score).slice(0, 2)
      .forEach(r => { const s = say(r); if (s) out.push({ ...s, tone: 'warn' }); });
  }
  if (!out.length) out.push({ kind: 'low', text: `Confidence ${i.confidence}`, tone: 'warn' });
  return out;
}

/* Someone the participant asked said yes or no. Worth a tag in the list. */
function vouchTag(i) {
  const x = i.confirmed_by;
  if (!x || (x.state !== 'confirmed' && x.state !== 'declined')) return '';
  const first = String(x.name).split(' ')[0];
  return x.state === 'confirmed'
    ? `<span class="sig ok">${icon('check')} ${esc(first)} said yes</span>`
    : `<span class="sig bad">${icon('x')} ${esc(first)} said no</span>`;
}

function renderList() {
  const el = $('#qlist');
  if (!el) return;
  const rows = visible();
  const canPick = Q.tab !== 'decided';
  if (!rows.length) {
    el.innerHTML = `<div class="empty">${icon(Q.tab === 'open' ? 'check' : 'inbox', 'lg')}
      <p>${Q.items.length ? 'Nothing matches these filters.' : {
        open: 'Nothing to decide. Every check-in has been settled.',
        waiting: 'No open questions to participants.',
        decided: 'No decisions in the last 14 days.' }[Q.tab]}</p></div>`;
    return;
  }
  const allPicked = canPick && rows.every(r => Q.picked.has(r.id));
  el.innerHTML = `
    <div class="qlist-head">
      ${canPick ? `<label class="pickall"><input type="checkbox" data-pickall ${allPicked ? 'checked' : ''}> Select all</label>` : '<span></span>'}
      <span class="muted small">${rows.length === Q.items.length ? plural(rows.length, 'item') : `${rows.length} of ${Q.items.length}`}</span>
    </div>
    <ul role="listbox" aria-label="Check-ins to review">${rows.map(i => listItem(i, canPick)).join('')}</ul>`;
}

function listItem(i, canPick) {
  const sel = i.id === Q.sel;
  const old = Q.tab !== 'decided' && Date.now() - new Date(i.since) > 3 * 864e5;
  let extra = '';
  if (Q.tab === 'waiting') extra = `<p class="qi-quote">You asked: "${esc(i.latest.note)}"</p>`;
  if (Q.tab === 'decided') {
    const d = DECISION[i.latest.decision];
    extra = `<p class="qi-dec"><span class="badge tone-${d.tone} sm">${icon(d.icon)} ${d.label}</span>
      <span class="muted small">${esc(i.latest.by || '')}</span></p>`;
  }
  return `<li role="option" aria-selected="${sel}" class="qi ${sel ? 'on' : ''} ${i.status}" data-id="${i.id}">
    ${canPick ? `<input type="checkbox" class="qi-pick" data-pick="${i.id}" ${Q.picked.has(i.id) ? 'checked' : ''} aria-label="Select ${esc(i.participant.name)}">` : ''}
    <div class="qi-body">
      <div class="qi-top"><b>${esc(i.participant.name)}</b>
        <span class="qi-age ${old ? 'old' : ''}" title="${esc(dateTime(i.since))}">${esc(ago(i.since).replace(' ago', ''))}</span></div>
      <p class="qi-sub">${esc(i.meeting.name)} · ${esc(shortDay(i.checked_in_at))}, ${time(i.checked_in_at)}</p>
      <div class="qi-tags">
        ${Q.tab === 'decided' ? '' : signals(i).map(s => `<span class="sig ${s.tone}">${esc(s.text)}</span>`).join('')}
        ${i.replied ? `<span class="sig info">${icon('note')} Replied</span>` : i.notes_count ? `<span class="sig info">${icon('note')} Note</span>` : ''}
        ${vouchTag(i)}
        ${i.this_week && Q.tab !== 'decided' ? '<span class="sig week">This week</span>' : ''}
      </div>
      ${extra}
    </div>
  </li>`;
}

function onListClick(e) {
  const all = e.target.closest('[data-pickall]');
  if (all) {
    visible().forEach(i => all.checked ? Q.picked.add(i.id) : Q.picked.delete(i.id));
    renderList(); renderDetail(); return;
  }
  const pick = e.target.closest('[data-pick]');
  if (pick) { togglePick(Number(pick.dataset.pick)); return; }
  const li = e.target.closest('li[data-id]');
  if (li) select(Number(li.dataset.id));
}

function togglePick(id) {
  Q.picked.has(id) ? Q.picked.delete(id) : Q.picked.add(id);
  renderList(); renderDetail();
}

function ensureSelection() {
  const rows = visible();
  if (!rows.some(r => r.id === Q.sel)) { Q.sel = rows[0]?.id ?? null; Q.changing = false; }
  $$('#qlist li[data-id]').forEach(li => {
    const on = Number(li.dataset.id) === Q.sel;
    li.classList.toggle('on', on); li.setAttribute('aria-selected', on);
  });
  fetchDetail();
}

function select(id) {
  Q.sel = id;
  Q.changing = false;
  $$('#qlist li[data-id]').forEach(li => {
    const on = Number(li.dataset.id) === id;
    li.classList.toggle('on', on); li.setAttribute('aria-selected', on);
    if (on) li.scrollIntoView({ block: 'nearest' });
  });
  history.replaceState(null, '', `#queue/${Q.tab}/${id}`);
  fetchDetail();
}

/* ---------- the detail ---------- */

async function fetchDetail() {
  const el = $('#qdetail');
  if (!el) return;
  if (Q.picked.size) return renderDetail();
  if (!Q.sel) { Q.detail = null; return renderDetail(); }
  const want = Q.sel;
  el.classList.add('loading');
  try {
    const d = await Q.ctx.api(`/api/reviews/${want}`);
    if (want !== Q.sel) return;                     // they moved on while it loaded
    Q.detail = d;
  } catch (e) {
    Q.detail = null;
    el.innerHTML = `<div class="empty">${esc(e.message)}</div>`;
    return;
  } finally { el.classList.remove('loading'); }
  renderDetail();
}

function renderDetail() {
  const el = $('#qdetail');
  if (!el) return;
  if (Q.map) { Q.map.remove(); Q.map = null; }
  if (Q.picked.size) { el.innerHTML = bulkPanel(); return; }
  const d = Q.detail;
  if (!d) {
    el.innerHTML = `<div class="empty">${icon('inbox', 'lg')}<p>Pick an item to see the evidence.</p></div>`;
    return;
  }
  const p = d.participant;
  const first = p.name.split(' ')[0];
  el.innerHTML = `
    <header class="qd-head">
      <div>
        <h2><a href="#participant/${p.participant_id}">${esc(p.name)}</a></h2>
        <p class="muted small">${[p.case_number && `Case ${esc(p.case_number)}`, p.officer && esc(p.officer),
          `${esc(d.meeting.name)}`, `${esc(dateTime(d.checked_in_at))}`].filter(Boolean).join(' · ')}</p>
      </div>
      <span class="badge tone-${{ confirmed: 'ok', flagged: 'warn', rejected: 'bad' }[d.status]}">
        ${icon({ confirmed: 'check', flagged: 'alert', rejected: 'x' }[d.status])}
        Engine: ${{ confirmed: 'confirmed', flagged: 'flagged', rejected: 'rejected' }[d.status]}</span>
    </header>

    ${impactBanner(d, first)}

    <div class="qd-evidence">
      <figure class="qd-map"><div id="qmap" role="img" aria-label="Map of the meeting and the reported position"></div>
        <figcaption><span><i class="k meet"></i>Meeting, ${feet(d.meeting.radius_ft)} circle</span>
          <span><i class="k you ${d.status}"></i>Reported position</span>
          ${d.checkout ? '<span><i class="k out"></i>At check-out</span>' : ''}</figcaption></figure>
      <dl class="facts">${facts(d)}</dl>
    </div>

    <div class="qd-context">${venueCard(d, first)}${personCard(d, first)}</div>

    ${timeline(d, first)}
    ${decidePanel(d, first)}`;
  drawMap(d);
  wireDecide(el, d);
}

function impactBanner(d, first) {
  if (!d.impact.length || d.state === 'decided') return '';
  return d.impact.map(i => {
    const { now, if_approved: yes, if_denied: no } = i;
    const req = i.requirement, n = req.count_required;
    const noun = CATEGORY[req.category] || 'meetings';
    const when = isThisPeriod(now) ? `this ${req.period}` : `the ${req.period} of ${esc(shortDay(now.starts))}`;
    let tone = 'neutral', text;
    if (yes.counted === no.counted) {
      text = yes.status === 'met'
        ? `${first}'s ${noun} for ${when} are already met (${now.counted} of ${n}). This decision does not change that.`
        : `Another check-in at this meeting already decides it. This does not change ${first}'s ${noun} for ${when}.`;
    } else if (yes.status === 'met' && no.status !== 'met') {
      tone = 'ok';
      text = `Counting it completes ${first}'s ${noun} for ${when}: ${yes.counted} of ${n}. Not counting it leaves ${no.counted} of ${n}.`;
    } else {
      tone = 'warn';
      text = `${first}'s ${noun} for ${when}: ${yes.counted} of ${n} if you count it, ${no.counted} of ${n} if you don't.`;
    }
    return `<p class="impact tone-${tone}">${icon('calendar')}<span>${text}</span></p>`;
  }).join('');
}

function isThisPeriod(p) {
  const now = Date.now();
  return new Date(p.starts) <= now && now < new Date(p.ends);
}

function reason(d, name) { return d.reasons.find(r => r.name === name); }

function facts(d) {
  const row = (label, value, r, extra = '') => {
    const kind = !r ? 'info' : r.passed ? (r.score < 0.8 && r.weight > 0 ? 'warn' : 'ok') : r.hard ? 'bad' : 'warn';
    const ic = { ok: 'check', warn: 'alert', bad: 'x', info: 'note' }[kind];
    return `<div class="fact ${kind}"><dt>${label}</dt><dd>${icon(ic)}<span>${value}${extra ? `<small>${extra}</small>` : ''}</span></dd></div>`;
  };
  const dist = reason(d, 'distance'), tw = reason(d, 'time_window'), gps = reason(d, 'gps_accuracy');
  const net = reason(d, 'network'), outD = reason(d, 'checkout_distance'), outT = reason(d, 'checkout_time');
  const m = Math.round(tw?.data?.minutes_from_start ?? 0);
  return [
    row('Distance', feet(d.distance_ft), dist, `limit ${feet(d.meeting.radius_ft)}`),
    row('Timing', m === 0 ? 'At the start' : m > 0 ? `${duration(m)} after the start` : `${duration(-m)} before the start`,
      tw, `window ±${d.meeting.time_window_min} min`),
    row('GPS accuracy', d.accuracy_m == null ? 'Not reported' : `±${Math.round(d.accuracy_m)} m`, gps,
      d.accuracy_m != null && d.accuracy_m * 3.28084 > d.meeting.radius_ft ? 'wider than the circle' : ''),
    row('Check-out', d.checked_out_at ? time(d.checked_out_at) : 'None recorded',
      d.checked_out_at ? (outT || outD || { passed: true, score: 1, weight: 0 }) : { passed: false, hard: false, weight: 0 },
      [outD && `${feet(outD.data.distance_ft)} away`, outT && `left ${duration(outT.data.minutes_before_end)} early`].filter(Boolean).join(', ')),
    row('Network', net && !/not configured/.test(net.reason) ? (net.passed ? 'Looks normal' : 'VPN or proxy') : 'Not checked',
      net && !/not configured/.test(net.reason) ? net : null, d.ip ? esc(d.ip) : ''),
    vouchRow(d, row),
    `<div class="fact info"><dt>Confidence</dt><dd><span class="conf"><i style="width:${d.confidence}%"></i></span><span>${d.confidence} of 100</span></dd></div>`,
  ].join('');
}

/* What the person the participant asked said, as one more signal. It is
   evidence from someone the participant chose, so it never decides anything
   on its own, but an officer should see it next to the GPS. */
function vouchRow(d, row) {
  const x = d.confirmed_by;
  if (!x) return '';
  const who = `${esc(x.name)}, ${esc(x.relationship_words)}`;
  const how = x.answered_via === 'text' ? 'by text reply' : 'from the link';
  if (x.state === 'confirmed') return row('Confirmed by', 'Said yes', { passed: true, score: 1, weight: 1 }, `${who}, ${how}, ${time(x.answered_at)}`);
  if (x.state === 'declined') return row('Confirmed by', 'Said no', { passed: false, hard: true, weight: 1 }, `${who}, ${how}, ${time(x.answered_at)}`);
  if (x.state === 'asked') return row('Confirmed by', 'Asked, no answer yet', { passed: false, hard: false, weight: 1 }, who);
  return row('Confirmed by', 'Asked, did not answer', null, who);
}

function venueCard(d, first) {
  const v = d.context.venue, e = v.everyone, p = v.person;
  const pct = x => x == null ? 'n/a' : `${Math.round(x * 100)}%`;
  const hint = e.weak_gps_share >= 0.2
    ? `<p class="hint">${icon('alert')} Weak GPS is common here, for everyone. A fuzzy fix at this venue says more about the building than about ${esc(first)}.</p>` : '';
  return `<section class="ctx">
    <h3>${icon('map')} At this venue</h3>
    <p class="muted small">${esc(v.address || v.name)}</p>
    <dl class="mini">
      <div><dt>Everyone</dt><dd>${plural(e.checkins, 'check-in')} from ${plural(e.people, 'person', 'people')}</dd></div>
      <div><dt>Not confirmed</dt><dd>${pct(e.flagged_share)}</dd></div>
      <div><dt>Weak GPS</dt><dd>${pct(e.weak_gps_share)}</dd></div>
      <div><dt>Typical accuracy</dt><dd>${e.median_accuracy_m == null ? 'n/a' : `±${e.median_accuracy_m} m`}</dd></div>
    </dl>
    <p class="small"><b>${esc(first)} here:</b> ${plural(p.checkins, 'check-in')}, ${p.confirmed} confirmed, ${p.flagged} flagged,
      ${p.rejected} rejected${p.approved || p.denied ? `. Reviewed: ${p.approved} counted, ${p.denied} not.` : '.'}
      ${p.median_distance_ft != null ? ` Usually ${feet(p.median_distance_ft)} from the meeting.` : ''}</p>
    ${hint}
  </section>`;
}

function personCard(d, first) {
  const c = d.context.person;
  if (!c) return '';
  const l = c.last_90_days;
  const strips = c.requirements.map(r => `<div class="strip-row"><span class="muted small">${esc(CATEGORY[r.category] || r.category)}</span>
    <span class="wk">${r.history.map(h => `<i class="${h.status}" title="${esc(`Week of ${shortDay(h.starts)}: ${h.counted} of ${r.required}`)}"></i>`).join('')}</span></div>`).join('');
  return `<section class="ctx">
    <h3>${icon('users')} ${esc(first)}'s record</h3>
    <p class="muted small">Enrolled ${esc(shortDay(c.enrolled_on))}. Last ${plural(Math.min(8, c.requirements[0]?.history.length || 0), 'week')}:</p>
    ${strips || '<p class="small muted">No requirement on file.</p>'}
    <p class="small">Last 90 days: ${plural(l.checkins, 'check-in')}, ${l.confirmed} confirmed, ${l.flagged} flagged, ${l.rejected} rejected.
      ${l.approved || l.denied ? `Reviews: ${l.approved} counted, ${l.denied} not.` : ''}
      ${l.not_checked_out ? `${plural(l.not_checked_out, 'time')} without a check-out.` : ''}</p>
  </section>`;
}

function timeline(d, first) {
  const said = e => {
    if (e.kind === 'checkin') return `<b>Checked in.</b> The engine ${e.status === 'flagged' ? 'flagged it' : e.status === 'rejected' ? 'rejected it' : 'confirmed it'} at ${e.confidence} confidence.`;
    if (e.kind === 'checkout') return '<b>Checked out.</b>';
    if (e.kind === 'note') return `<b>${esc(first)} wrote:</b> "${esc(e.message)}"`;
    if (e.kind === 'asked') return `<b>${esc(first)} asked ${esc(e.name)} (${esc(e.relationship_words)}) to confirm,</b> by text.`;
    if (e.kind === 'answered') return `<b>${esc(e.name)} ${e.yes ? 'confirmed it' : 'could not confirm it'}</b>${
      e.via === 'text' ? ', by text reply' : ''}.${e.note ? ` "${esc(e.note)}"` : ''}`;
    const dec = DECISION[e.decision];
    return `<b>${esc(e.by || 'An officer')}: ${dec.verb.toLowerCase()}.</b>${e.note ? ` "${esc(e.note)}"` : ''}`;
  };
  const tone = e => e.kind === 'review' ? DECISION[e.decision].tone : e.kind === 'note' ? 'note'
    : e.kind === 'answered' ? (e.yes ? 'ok' : 'bad') : 'neutral';
  return `<section class="qd-thread"><h3>What has happened</h3><ol class="tl">${d.timeline.map(e =>
    `<li class="t-${tone(e)}"><span class="t-when">${esc(dateTime(e.at))}</span><p>${said(e)}</p></li>`).join('')}</ol></section>`;
}

/* ---------- deciding ---------- */

function suggestions(d, first) {
  const s = new Set();
  const failed = new Set(d.reasons.filter(r => !r.passed && r.weight > 0).map(r => r.name));
  const weakGps = failed.has('gps_accuracy') || (d.context.venue.everyone.weak_gps_share || 0) >= 0.2;
  const vouch = d.confirmed_by;
  if (vouch?.state === 'confirmed') s.add(['approved', `${vouch.name} (${vouch.relationship_words}) confirmed by text.`]);
  if (vouch?.state === 'declined') s.add(['denied', `${vouch.name} could not confirm attendance.`]);
  if (weakGps) s.add(['approved', 'Known weak GPS at this venue.']);
  if (d.timeline.some(e => e.kind === 'note')) s.add(['approved', `${first}'s note checks out.`]);
  s.add(['approved', 'Facilitator confirmed attendance.']);
  if (failed.has('distance')) s.add(['denied', 'Too far from the meeting with a good GPS fix.']);
  if (failed.has('time_window')) s.add(['denied', 'Outside the check-in window.']);
  if (d.state === 'waiting') s.add(['denied', 'No answer to my question.']);
  s.add(['more_info', 'Can the facilitator confirm you were there?']);
  if (failed.has('time_window')) s.add(['more_info', 'Why was the check-in so far from the start time?']);
  return [...s];
}

function decidePanel(d, first) {
  const latest = d.latest;
  if (d.state === 'decided' && !Q.changing) {
    const dec = DECISION[latest.decision];
    return `<section class="decided tone-${dec.tone}">
      <div><span class="badge tone-${dec.tone}">${icon(dec.icon)} ${dec.label}</span>
        <span class="muted small">${esc(latest.by || '')} · ${esc(dateTime(latest.at))}</span></div>
      ${latest.note ? `<p>"${esc(latest.note)}"</p>` : ''}
      <button class="btn secondary" data-change type="button">Change decision</button>
    </section>`;
  }
  const changing = d.state === 'decided';
  const options = [
    ['approved', 'Count it', 'a', ''],
    ['denied', 'Don\'t count it', 'd', 'secondary'],
    ['more_info', `Ask ${first} a question`, 'q', 'secondary'],
  ].filter(o => !(changing && o[0] === latest.decision));
  return `<section class="decide" aria-label="Decision">
    ${d.state === 'waiting' ? `<p class="callout warn">${icon('question')} You asked ${esc(ago(latest.at))}: "${esc(latest.note)}" ${esc(first)} has not answered yet. You can still decide.</p>` : ''}
    ${d.replied ? `<p class="callout ok">${icon('note')} ${esc(first)} answered your question. It is in the timeline above.</p>` : ''}
    ${changing ? `<p class="callout warn">${icon('alert')} Changing a decision adds to the record. The earlier one stays, and ${esc(first)} sees both.</p>` : ''}
    <label for="qnote">Note <span class="muted">${esc(first)} will see this${changing ? '. Say why it is changing.' : '.'}</span></label>
    <textarea id="qnote" rows="2" maxlength="2000" placeholder="What you checked, who you spoke to">${esc(Q.drafts[d.id] || '')}</textarea>
    <div class="quick" aria-label="Suggested notes">${suggestions(d, first).map(([k, t]) =>
      `<button type="button" class="qchip ${DECISION[k].tone}" data-say="${esc(t)}">${esc(t)}</button>`).join('')}</div>
    <p class="err" role="alert" hidden></p>
    <div class="decide-btns">${options.map(([k, label, key, cls]) =>
      `<button class="btn ${cls} dec-${k}" data-decide="${k}" type="button">${icon(DECISION[k].icon)} ${label}<kbd>${key.toUpperCase()}</kbd></button>`).join('')}
      ${changing ? '<button class="btn ghost" data-cancel-change type="button">Cancel</button>' : ''}</div>
  </section>`;
}

function wireDecide(el, d) {
  el.querySelectorAll('[data-say]').forEach(b => b.onclick = () => {
    const ta = el.querySelector('#qnote');
    ta.value = ta.value.trim() ? `${ta.value.trim()} ${b.dataset.say}` : b.dataset.say;
    ta.focus();
  });
  el.querySelector('[data-change]')?.addEventListener('click', () => { Q.changing = true; renderDetail(); el.querySelector('#qnote')?.focus(); });
  el.querySelector('[data-cancel-change]')?.addEventListener('click', () => { Q.changing = false; renderDetail(); });
}

function onDetailClick(e) {
  const b = e.target.closest('[data-decide]');
  if (b) { decide(b.dataset.decide); return; }
  const bulk = e.target.closest('[data-bulk]');
  if (bulk) { decideBulk(bulk.dataset.bulk); return; }
  if (e.target.closest('[data-clear]')) { Q.picked.clear(); renderList(); fetchDetail(); }
}

function needsNote(decision, d) {
  const first = d.participant.name.split(' ')[0];
  if (decision === 'denied') return `Say why it does not count. ${first} will see this note.`;
  if (decision === 'more_info') return `Write the question for ${first}.`;
  if (d.state === 'decided') return 'Say why the decision is changing.';
  return null;
}

async function decide(decision) {
  const d = Q.detail;
  if (!d || !$('#qnote')) return;
  const note = $('#qnote').value.trim();
  const why = needsNote(decision, d);
  if (why && !note) {
    const err = $('#qdetail .err'); err.textContent = why; err.hidden = false;
    $('#qnote').focus(); return;
  }
  const item = Q.items.find(i => i.id === d.id);
  const leaves = Q.tab !== 'decided';
  Q.drafts[d.id] = note;
  Q.changing = false;
  if (leaves && item) {
    Q.sel = neighbour(d.id);
    Q.items = Q.items.filter(i => i.id !== d.id);
    Q.counts[Q.tab] = Math.max(0, (Q.counts[Q.tab] || 1) - 1);
    syncCounts(); renderList(); ensureSelection();
  }
  later([d.id], outcomeMessage(decision, d),
    keepalive => Q.ctx.api(`/api/reviews/${d.id}`, { method: 'POST', keepalive,
      body: JSON.stringify({ decision, note: note || null }) }),
    () => {
      if (leaves && item) {
        Q.items = sortItems([...Q.items, item]);
        Q.counts[Q.tab] = (Q.counts[Q.tab] || 0) + 1;
        syncCounts();
      }
      Q.sel = d.id; renderList(); ensureSelection();
    });
  if (!leaves) renderDetail();
}

/* Send after the undo window, or at once when `flush` is called. */
function later(ids, message, send, undo) {
  const job = { ids, send, done: false };
  job.timer = setTimeout(() => flush(job), UNDO_MS);
  PENDING.add(job);
  toast(message, { action: 'Undo', ms: UNDO_MS, onAction: () => {
    if (job.done) return;
    clearTimeout(job.timer); PENDING.delete(job); undo();
  } });
}

async function flush(job, { keepalive = false } = {}) {
  if (job.done) return;
  job.done = true;
  clearTimeout(job.timer);
  PENDING.delete(job);
  try {
    const r = await job.send(keepalive);
    job.ids.forEach(id => delete Q.drafts[id]);
    if (r?.skipped?.length) toast(`${plural(r.skipped.length, 'item')} skipped, already decided.`);
  } catch (e) {
    toast(`Not saved: ${e.message}`);
  }
  if (Q.mounted && !keepalive) load();
}

window.addEventListener('pagehide', () => PENDING.forEach(job => flush(job, { keepalive: true })));

/* The next item to look at once this one leaves the list. */
function neighbour(id) {
  const rows = visible(), i = rows.findIndex(r => r.id === id);
  return (rows[i + 1] || rows[i - 1] || {}).id ?? null;
}

function outcomeMessage(decision, d) {
  const first = d.participant.name.split(' ')[0];
  if (decision === 'more_info') return `Question sent to ${first}.`;
  const i = d.impact[0];
  if (decision === 'approved') {
    return i && i.if_approved.status === 'met' && i.now.status !== 'met'
      ? `Counted. ${first}'s ${i.requirement.period} is now met.` : 'Counted.';
  }
  return 'Marked as not counted.';
}

/* ---------- bulk ---------- */

function bulkPanel() {
  const picked = Q.items.filter(i => Q.picked.has(i.id));
  const people = {};
  picked.forEach(i => { people[i.participant.name] = (people[i.participant.name] || 0) + 1; });
  return `<section class="bulk">
    <h2>${plural(picked.length, 'check-in')} selected</h2>
    <ul class="people">${Object.entries(people).map(([n, k]) => `<li>${esc(n)}${k > 1 ? ` <span class="muted">×${k}</span>` : ''}</li>`).join('')}</ul>
    <p class="muted small">One decision and one note for all of them. Use it for a run of the same situation, like a venue with known bad signal.
      Each person sees the note on their own check-in.</p>
    <label for="qnote">Note</label>
    <textarea id="qnote" rows="2" maxlength="2000" placeholder="Why this decision fits all of them"></textarea>
    <p class="err" role="alert" hidden></p>
    <div class="decide-btns">
      <button class="btn" data-bulk="approved" type="button">${icon('check')} Count all ${picked.length}</button>
      <button class="btn secondary dec-denied" data-bulk="denied" type="button">${icon('x')} Don't count</button>
      <button class="btn secondary" data-bulk="more_info" type="button">${icon('question')} Ask each a question</button>
      <button class="btn ghost" data-clear type="button">Clear selection</button>
    </div>
  </section>`;
}

function decideBulk(decision) {
  const note = $('#qnote').value.trim();
  if (decision !== 'approved' && !note) {
    const err = $('#qdetail .err');
    err.textContent = decision === 'denied' ? 'Say why these do not count. Each participant will see the note.' : 'Write the question. Each participant will see it.';
    err.hidden = false; $('#qnote').focus(); return;
  }
  const ids = [...Q.picked];
  const removed = Q.items.filter(i => Q.picked.has(i.id));
  Q.items = Q.items.filter(i => !Q.picked.has(i.id));
  Q.counts[Q.tab] = Math.max(0, (Q.counts[Q.tab] || 0) - removed.length);
  Q.picked.clear(); Q.sel = null;
  syncCounts(); renderList(); ensureSelection();
  later(ids, `${{ approved: 'Counted', denied: 'Marked as not counted', more_info: 'Asked' }[decision]} ${plural(ids.length, 'check-in')}.`,
    keepalive => Q.ctx.api('/api/reviews/bulk', { method: 'POST', keepalive,
      body: JSON.stringify({ checkin_ids: ids, decision, note: note || null }) }),
    () => {
      Q.items = sortItems([...Q.items, ...removed]);
      Q.counts[Q.tab] = (Q.counts[Q.tab] || 0) + removed.length;
      ids.forEach(id => Q.picked.add(id));
      syncCounts(); renderList(); renderDetail();
    });
}

/* ---------- map ---------- */

function drawMap(d) {
  const L = window.L;
  const box = $('#qmap');
  if (!L || !box) return;
  const cs = getComputedStyle(document.documentElement);
  const c = n => cs.getPropertyValue(n).trim();
  Q.map = L.map(box, { scrollWheelZoom: false, zoomControl: true });
  Q.map.attributionControl.setPrefix(false);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19, attribution: '&copy; OpenStreetMap contributors' }).addTo(Q.map);
  const m = d.meeting, layers = [];
  L.circle([m.lat, m.lng], { radius: m.radius_ft / 3.28084, color: c('--ink'), weight: 1.5, dashArray: '5 5', fillOpacity: 0.04 }).addTo(Q.map);
  layers.push(L.circleMarker([m.lat, m.lng], { radius: 7, color: c('--surface'), weight: 2, fillColor: c('--ink'), fillOpacity: 1 })
    .addTo(Q.map).bindTooltip(esc(m.name)));
  const tone = { confirmed: '--ok-strong', flagged: '--warn-strong', rejected: '--bad-strong' }[d.status];
  if (d.lat != null) {
    if (d.accuracy_m) L.circle([d.lat, d.lng], { radius: d.accuracy_m, stroke: false, fillColor: c(tone), fillOpacity: 0.15 }).addTo(Q.map);
    layers.push(L.circleMarker([d.lat, d.lng], { radius: 8, color: c('--surface'), weight: 2.5, fillColor: c(tone), fillOpacity: 1 })
      .addTo(Q.map).bindTooltip(`Reported position, ${feet(d.distance_ft)} away`));
  }
  if (d.checkout) {
    layers.push(L.circleMarker([d.checkout.lat, d.checkout.lng], { radius: 6, color: c('--brand'), weight: 2.5, fillOpacity: 0 })
      .addTo(Q.map).bindTooltip('Position at check-out'));
  }
  Q.map.fitBounds(L.featureGroup(layers).getBounds().pad(0.6), { maxZoom: 17 });
  setTimeout(() => Q.map?.invalidateSize(), 60);
}

/* ---------- keyboard ---------- */

document.addEventListener('keydown', e => {
  if (!Q.mounted || e.metaKey || e.ctrlKey || e.altKey) return;
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if (e.key === 'Escape' && typing) { document.activeElement.blur(); return; }
  if (typing || document.querySelector('dialog[open]')) return;
  const rows = visible(), i = rows.findIndex(r => r.id === Q.sel);
  const k = e.key.toLowerCase();
  if (k === 'j' || e.key === 'ArrowDown') { if (rows[i + 1]) select(rows[i + 1].id); e.preventDefault(); }
  else if (k === 'k' || e.key === 'ArrowUp') { if (rows[i - 1]) select(rows[i - 1].id); e.preventDefault(); }
  // preventDefault on every shortcut: deciding can move focus into the note
  // box, and the keystroke must not land there as a stray letter.
  else if (k === 'x' && Q.sel && Q.tab !== 'decided') { e.preventDefault(); togglePick(Q.sel); }
  else if (k === 'a') { e.preventDefault(); decide('approved'); }
  else if (k === 'd') { e.preventDefault(); decide('denied'); }
  else if (k === 'q') { e.preventDefault(); decide('more_info'); }
  else if (k === 'n') { $('#qnote')?.focus(); e.preventDefault(); }
  else if (e.key === '/') { $('#qsearch')?.focus(); e.preventDefault(); }
  else if (e.key === '?') showKeys();
});

function showKeys() {
  Q.ctx.sheet(`<div class="step left"><h2>Keyboard shortcuts</h2>
    <dl class="keys">
      <div><dt><kbd>J</kbd> <kbd>K</kbd></dt><dd>Next and previous item</dd></div>
      <div><dt><kbd>A</kbd></dt><dd>Count it</dd></div>
      <div><dt><kbd>D</kbd></dt><dd>Don't count it (needs a note)</dd></div>
      <div><dt><kbd>Q</kbd></dt><dd>Ask a question (needs a note)</dd></div>
      <div><dt><kbd>N</kbd></dt><dd>Write a note</dd></div>
      <div><dt><kbd>X</kbd></dt><dd>Select for a bulk decision</dd></div>
      <div><dt><kbd>/</kbd></dt><dd>Search by name</dd></div>
      <div><dt><kbd>Esc</kbd></dt><dd>Leave the note box</dd></div>
    </dl></div>`, { label: 'Keyboard shortcuts' });
}
