// The participant app: what someone under a court order opens on their phone.
//
// Three screens (Today, History, You) plus sheets for checking in, checking
// out, and reading a result. The rules it follows come from the Big Picture
// doc: location is read once, at the tap; every result carries its reasons in
// plain words; nothing recorded about the person is hidden from them.

import {
  $, bestPosition, dateTime, day, dayKey, devIdentity, duration, esc, feet,
  greeting, icon, locationPermission, makeApi, minutesUntil, plural, relativeDay,
  setTimeZone, sheet, time, toast, weekday, weekKey, weekLabel,
} from './lib.js';

const identity = devIdentity('mc_act_participant');
const api = makeApi(identity);
const app = $('#app');

const S = {
  ctx: {}, me: null, devUsers: null, meetings: [], mine: [], progress: null,
  tab: 'today', loaded: false, installPrompt: null,
  expanded: new Set(), filter: '', places: null,      // the Today lists
};

/* ---------- words ---------- */

const CATEGORY = {
  public: 'Public meeting', private: 'Private meeting',
  community_service: 'Community service', therapy: 'Therapy',
};
const REQ_NOUN = {
  public: ['public meeting', 'public meetings'],
  community_service: ['community service session', 'community service sessions'],
  private: ['private meeting', 'private meetings'],
  therapy: ['therapy session', 'therapy sessions'],
  any: ['meeting', 'meetings'],
};

/* Where a check-in stands for the participant, folding in any review. */
function standing(c) {
  const r = c.review?.decision;
  if (!c.meeting.counts) {
    return c.status === 'rejected'
      ? { key: 'rejected', label: 'Not confirmed', tone: 'bad', icon: 'x' }
      : { key: 'personal', label: 'Saved for you', tone: 'neutral', icon: 'check' };
  }
  if (r === 'approved') return { key: 'approved', label: 'Counted after review', tone: 'ok', icon: 'check' };
  if (r === 'denied') return { key: 'denied', label: 'Not counted', tone: 'bad', icon: 'x' };
  if (r === 'more_info' && c.awaiting_reply) return { key: 'question', label: 'Your officer asked a question', tone: 'warn', icon: 'note' };
  if (r === 'more_info') return { key: 'replied', label: 'Reply sent, in review', tone: 'warn', icon: 'clock' };
  if (c.status === 'confirmed') return { key: 'confirmed', label: 'Counted', tone: 'ok', icon: 'check' };
  if (c.status === 'flagged') return { key: 'flagged', label: 'In review', tone: 'warn', icon: 'clock' };
  return { key: 'rejected', label: 'Not confirmed', tone: 'bad', icon: 'x' };
}

/* The engine's reasons, rewritten for the person they are about. */
function explain(r, c) {
  const d = r.data || {};
  const m = c.meeting;
  switch (r.name) {
    case 'distance':
      return r.passed
        ? `You were ${feet(d.distance_ft)} from the meeting. The limit is ${feet(d.radius_ft)}.`
        : `Your phone placed you ${feet(d.distance_ft)} from the meeting. It needs to be within ${feet(d.radius_ft)}.`;
    case 'time_window': {
      const min = Math.round(d.minutes_from_start ?? 0);
      const w = m.time_window_min;
      if (!r.passed) return min > 0
        ? `That was ${duration(min)} after the start. Check-in closes ${w} min after the start.`
        : `That was ${duration(-min)} before the start. Check-in opens ${w} min before.`;
      if (min === 0) return 'You checked in right as it started.';
      return min < 0 ? `You checked in ${duration(-min)} before it started.`
                     : `You checked in ${duration(min)} after it started.`;
    }
    case 'gps_accuracy':
      if (d.accuracy_m == null) return 'Your phone did not say how accurate its location was.';
      return r.passed
        ? `Your phone's location was accurate to about ${Math.round(d.accuracy_m)} m.`
        : `Your phone's location was fuzzy, about ±${Math.round(d.accuracy_m)} m. That happens indoors and downtown.`;
    case 'network':
      if (/not configured/.test(r.reason)) return 'Connection check is not turned on for your county.';
      return r.passed ? 'Your connection looked normal.' : 'Your connection looked like a VPN or proxy.';
    case 'checkout_distance':
      return `At check-out you were ${feet(d.distance_ft)} from the meeting.`;
    case 'checkout_time':
      return `You checked out ${duration(d.minutes_before_end)} before the meeting ended.`;
    default:
      return r.reason;
  }
}

/* ---------- boot ---------- */

async function boot() {
  registerServiceWorker();
  watchConnection();
  window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); S.installPrompt = e; });

  try { S.ctx = await api('/api/auth/context'); } catch { S.ctx = {}; }

  if (S.ctx.dev_auth) {
    // Local development: pick a demo participant, no sign-in.
    try {
      const users = await api('/api/users');
      S.devUsers = users.filter(u => u.role === 'participant');
      if (!S.devUsers.some(u => String(u.id) === identity.get()) && S.devUsers.length) {
        identity.set(S.devUsers[0].id);
      }
    } catch { S.devUsers = null; }
  } else {
    S.devUsers = null;
    identity.clear();                      // a stale demo id is not a sign-in
  }

  try {
    S.me = await api('/api/me');
  } catch (e) {
    return e.status === 401 ? renderSignIn() : renderFatal(e);
  }
  if (S.me.role !== 'participant') return renderStaffHere();
  setTimeZone(S.me.timezone);

  enter();
}

/* What stands between a signed-in person and Today: the consent screen,
   then, for a self-serve profile, a live subscription. */
function enter() {
  if (S.me.consent.required) return renderConsent();
  if (S.me.billing && !S.me.billing.active) return renderPayWall();
  start();
}

function start() {
  if (S.started) { renderShell(); refresh(); return; }
  S.started = true;
  S.tab = ['today', 'history', 'you'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'today';
  renderShell();
  refresh();
  setInterval(() => { if (!document.hidden) renderTab(); }, 30_000);   // times and windows move
  setInterval(() => { if (!document.hidden) refresh(); }, 120_000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('hashchange', () => {
    const t = location.hash.slice(1);
    if (['today', 'history', 'you'].includes(t) && t !== S.tab) { S.tab = t; renderShell(); }
  });
}

async function refresh() {
  const now = Date.now();
  const since = new Date(now - 18 * 3600e3).toISOString();
  const until = new Date(now + 8 * 864e5).toISOString();
  try {
    const [meetings, mine, progress] = await Promise.all([
      api(`/api/meetings?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}`),
      api('/api/checkins/mine'),
      api('/api/my/progress'),
    ]);
    Object.assign(S, { meetings, mine, progress, loaded: true, loadError: null });
  } catch (e) {
    if (e.status === 428) { S.me = await api('/api/me'); return renderConsent(); }
    S.loadError = e.message;
  }
  renderTab();
}

/* ---------- shell ---------- */

function renderShell() {
  document.body.classList.remove('bare');
  app.innerHTML = `
    <header class="bar">
      <span class="brand">${logo()}<span>ActionsCount+</span></span>
      ${S.devUsers ? `<button class="chip demo" data-act="switch" type="button">Demo · ${esc(firstName(S.me.name))}</button>` : ''}
    </header>
    <div class="offline" id="offline" hidden>${icon('wifi')} You are offline. Check-in needs a connection.</div>
    <main id="view" tabindex="-1"></main>
    <nav class="tabs" aria-label="Sections">
      ${tabBtn('today', 'home', 'Today')}
      ${tabBtn('history', 'list', 'History')}
      ${tabBtn('you', 'user', 'You')}
    </nav>`;
  syncOffline();
  renderTab();
}

function tabBtn(key, ic, label) {
  return `<a href="#${key}" class="tab ${S.tab === key ? 'on' : ''}" ${S.tab === key ? 'aria-current="page"' : ''}>
    ${icon(ic)}<span>${label}</span></a>`;
}

function renderTab() {
  const v = $('#view');
  if (!v) return;
  if (!S.loaded) { v.innerHTML = S.loadError ? errorCard(S.loadError) : skeleton(); return; }
  v.innerHTML = { today: viewToday, history: viewHistory, you: viewYou }[S.tab]();
  if (S.tab === 'you') loadPeople();
}

const firstName = n => String(n || '').split(' ')[0];

/* "today", "tomorrow", or "on Mon, Jul 27", for the middle of a sentence. */
function inlineDay(d) {
  const r = relativeDay(d);
  return ['Today', 'Tomorrow', 'Yesterday'].includes(r) ? r.toLowerCase() : `on ${r}`;
}

function logo() {
  return `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="var(--brand)"/>
    <path d="M9.5 16.5l4.2 4.2 8.8-9.4" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

function skeleton() {
  return `<div class="stack"><div class="sk sk-title"></div><div class="card sk-card"></div><div class="card sk-card short"></div></div>`;
}

function errorCard(msg) {
  return `<div class="card empty">${icon('alert', 'lg muted')}<p>${esc(msg)}</p>
    <button class="btn" data-act="retry" type="button">Try again</button></div>`;
}

/* ---------- Today ---------- */

function liveState(m, now = Date.now()) {
  const opens = +new Date(m.opens_at), closes = +new Date(m.closes_at), ends = +new Date(m.ends_at);
  if (now < opens) return 'upcoming';
  if (now <= closes) return 'open';
  if (now <= ends) return 'late';
  return 'ended';
}

/* The check-in someone is sitting in right now, if any: not checked out, and
   the meeting has not been over for more than 90 minutes. */
function activeCheckIn(now = Date.now()) {
  return S.mine.find(c => c.status !== 'rejected' && !c.checked_out_at &&
    now < +new Date(c.meeting.ends_at) + 90 * 60e3 && now > +new Date(c.meeting.starts_at) - 3 * 3600e3);
}

function standingFor(meetingId) {
  return S.mine.find(c => c.meeting_id === meetingId && c.status !== 'rejected');
}

/* With the directory switched on, a town has hundreds of meetings a week.
   Each list shows a few and offers the rest, and a search box narrows them. */
const SHOW_FIRST = 5;

function viewToday() {
  const now = Date.now();
  const active = activeCheckIn(now);
  const needle = (S.filter || '').trim().toLowerCase();
  const hit = m => !needle || `${m.name} ${m.address || ''}`.toLowerCase().includes(needle);
  const ms = S.meetings.map(m => ({ ...m, state: liveState(m, now), miles: milesFromArea(m) }))
    .sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at) || (a.miles ?? 0) - (b.miles ?? 0));
  // Everything open is open now, so the nearest first. The county's own
  // meetings carry no distance and stay at the top.
  const openNow = ms.filter(m => (m.state === 'open' || m.state === 'late') && m.id !== active?.meeting_id && hit(m))
    .sort((a, b) => (a.miles ?? -1) - (b.miles ?? -1));
  const later = ms.filter(m => m.state === 'upcoming' && hit(m));
  const nextUp = ms.find(m => m.state === 'upcoming');

  const groups = {};
  later.forEach(m => (groups[dayKey(m.starts_at)] ||= []).push(m));

  return `<div class="stack">
    <div class="hello">
      <h1>${greeting()}, ${esc(firstName(S.me.name))}</h1>
      <p class="muted">${esc(day(new Date()))}</p>
    </div>

    ${questionCards()}
    ${active ? activeCard(active) : ''}
    ${progressCard()}
    ${areaBar()}
    ${S.meetings.length > 8 || needle ? `<div class="findbox">
      <input type="search" id="mfilter" placeholder="Find a meeting by name or street" value="${esc(S.filter || '')}" autocomplete="off" aria-label="Find a meeting">
    </div>` : ''}

    <section aria-labelledby="h-open">
      <h2 class="sec" id="h-open">Open for check-in</h2>
      ${openNow.length ? someOf('open', openNow) : `
        <div class="card quiet">${icon('clock', 'muted')}
          <p>${needle ? 'Nothing open matches that.' : active ? 'Nothing else is open right now.' : 'Nothing is open right now.'}
          ${!needle && nextUp ? ` Next up: <b>${esc(nextUp.name)}</b>, ${esc(inlineDay(nextUp.starts_at))} at ${time(nextUp.starts_at)}.` : ''}</p>
        </div>`}
    </section>

    ${Object.keys(groups).length ? `<section aria-labelledby="h-later">
      <h2 class="sec" id="h-later">Coming up</h2>
      ${Object.entries(groups).map(([k, list]) => `
        <h3 class="day">${esc(relativeDay(list[0].starts_at))} <span class="muted">· ${plural(list.length, 'meeting')}</span></h3>
        ${someOf(`day:${k}`, list)}`).join('')}
    </section>` : ''}

    <button class="link-row" data-act="personal" type="button">
      ${icon('plus')}<span><b>Track a personal meeting</b><br>
      <span class="muted small">Therapy or anything not on the list. Only you see these.</span></span>${icon('arrow', 'muted')}
    </button>
  </div>`;
}

/* The first few of a list, and a button for the rest. */
function someOf(key, list) {
  const all = S.expanded.has(key) || list.length <= SHOW_FIRST + 1;
  const shown = all ? list : list.slice(0, SHOW_FIRST);
  return shown.map(meetingCard).join('') + (all ? '' :
    `<button class="btn secondary block more" data-act="more" data-key="${esc(key)}" type="button">Show all ${list.length}</button>`);
}

/* ---------- the town ----------
   Nearby means within 25 miles of a town the participant picked once. Only
   the town is kept: never where the phone was. */

function milesFromArea(m) {
  const a = S.me.area;
  if (!a || m.lat == null || !m.series_id) return null;
  const R = 3958.8, toR = x => x * Math.PI / 180;
  const dLat = toR(m.lat - a.lat), dLng = toR(m.lng - a.lng);
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(toR(a.lat)) * Math.cos(toR(m.lat)) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function areaBar() {
  if (!S.ctx.directory) return '';
  const a = S.me.area;
  if (a) {
    return `<div class="area"><span>${icon('map')} Meetings near <b>${esc(a.label)}</b></span>
      <button class="linkish" data-act="area" type="button">Change</button></div>`;
  }
  return `<button class="link-row" data-act="area" type="button">${icon('map')}
    <span><b>Find meetings near you</b><br><span class="muted small">Pick your town once. Meetings within 25 miles of it show up here.</span></span>${icon('arrow', 'muted')}</button>`;
}

async function areaSheet() {
  const sh = sheet(`<div class="step left town">
    <h2>Where do you go to meetings?</h2>
    <p class="muted">Pick a town. Meetings within 25 miles of it show on Today. Only the town is kept, never where your phone was.</p>
    <input type="search" id="placeq" placeholder="Town or city" autocomplete="off" aria-label="Town">
    <ul class="pick places" id="places"><li class="muted small">Loading</li></ul>
    <button class="btn secondary block" data-locate type="button">${icon('pin')} Use my location once</button>
    ${S.me.area ? '<button class="btn block ghost" data-clear type="button">Stop showing nearby meetings</button>' : ''}
  </div>`, { label: 'Your town' });
  const body = sh.body;
  let places = S.places;
  try { places = S.places = S.places || await api('/api/directory/places'); }
  catch (e) { body.querySelector('#places').innerHTML = `<li class="muted small">${esc(e.message)}</li>`; return; }
  const draw = () => {
    const q = body.querySelector('#placeq').value.trim().toLowerCase();
    const hits = places.filter(p => !q || p.city.toLowerCase().includes(q)).slice(0, 10);
    body.querySelector('#places').innerHTML = hits.length ? hits.map(p => `<li><button class="pick-row ${S.me.area?.label === p.label ? 'on' : ''}"
        data-place="${esc(p.label)}" data-lat="${p.lat}" data-lng="${p.lng}" type="button"><b>${esc(p.label)}</b>
        <span class="muted small">${plural(p.meetings, 'meeting')} a week</span></button></li>`).join('')
      : '<li class="muted small">No town by that name in the directory yet.</li>';
  };
  draw();
  body.querySelector('#placeq').oninput = draw;
  body.querySelector('#placeq').focus();
  const choose = async payload => {
    try {
      S.me.area = await api('/api/my/area', { method: 'POST', body: JSON.stringify(payload) });
      S.expanded.clear();
      sh.close();
      toast(`Showing meetings near ${S.me.area.label}.`);
      refresh();
    } catch (e) { toast(e.message); }
  };
  body.querySelector('#places').onclick = e => {
    const b = e.target.closest('[data-place]');
    if (b) choose({ label: b.dataset.place, lat: Number(b.dataset.lat), lng: Number(b.dataset.lng) });
  };
  body.querySelector('[data-locate]').onclick = async e => {
    const b = e.currentTarget;
    b.disabled = true; b.textContent = 'Finding your location';
    try {
      const pos = await bestPosition({ goodEnoughM: 500, settleMs: 3000, hardTimeoutMs: 15000 });
      await choose({ lat: pos.coords.latitude, lng: pos.coords.longitude });
    } catch {
      b.disabled = false; b.innerHTML = `${icon('pin')} Use my location once`;
      toast('Could not read your location. Pick a town instead.');
    }
  };
  body.querySelector('[data-clear]')?.addEventListener('click', async () => {
    try { await api('/api/my/area', { method: 'DELETE' }); S.me.area = null; sh.close(); refresh(); }
    catch (e) { toast(e.message); }
  });
}

/* An officer asked something. It goes above everything else, because the
   check-in stays pending until the participant answers. */
function questionCards() {
  return S.mine.filter(c => c.awaiting_reply).map(c => `
    <section class="card question" aria-label="Question from your officer">
      <span class="badge tone-warn">${icon('note')} ${esc(c.review.by || 'Your officer')} asked a question</span>
      <p class="q-text">"${esc(c.review.note)}"</p>
      <p class="small muted">About ${esc(c.meeting.name)}, ${esc(inlineDay(c.checked_in_at))} at ${time(c.checked_in_at)}. It stays pending until you answer.</p>
      <button class="btn block" data-act="detail" data-id="${c.id}" type="button">Answer</button>
    </section>`).join('');
}

function activeCard(c) {
  const now = Date.now(), start = +new Date(c.meeting.starts_at), end = +new Date(c.meeting.ends_at);
  const pct = Math.max(0, Math.min(100, ((now - start) / (end - start)) * 100));
  const left = minutesUntil(end, now);
  const st = standing(c);
  return `<section class="card active tone-${st.tone}" aria-label="Your current check-in">
    <div class="active-top">
      <span class="badge tone-${st.tone}">${icon(st.icon)} ${c.status === 'flagged' ? 'Checked in, in review' : 'Checked in'}</span>
      <span class="muted small">since ${time(c.checked_in_at)}</span>
    </div>
    <h2 class="active-name">${esc(c.meeting.name)}</h2>
    <div class="meter" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}"
      aria-label="Meeting progress"><i style="width:${pct}%"></i></div>
    <p class="small muted">${left > 0 ? `Ends at ${time(end)} · ${duration(left)} left` : `Ended at ${time(end)}`}</p>
    <button class="btn block ${left > 10 ? 'secondary' : ''}" data-act="checkout" data-id="${c.id}" type="button">Check out</button>
    <p class="fine">Checking out adds a second location reading. It helps if a check-in is ever questioned.</p>
  </section>`;
}

function reqNoun(cat, n) { const w = REQ_NOUN[cat] || REQ_NOUN.any; return n === 1 ? w[0] : w[1]; }

function progressCard() {
  const p = S.progress;
  if (!p?.has_requirements) {
    return `<div class="card quiet">${icon('shield', 'muted')}<p>No court requirement is on file for you yet. Your officer adds it. Your check-ins are still saved.</p></div>`;
  }
  return `<section class="card progress" aria-labelledby="h-prog">
    <div class="row between"><h2 class="sec flush" id="h-prog">This ${p.summary.requirements[0].period}</h2>
      ${p.officer ? `<span class="muted small">Officer: ${esc(p.officer.replace(/^Officer /, ''))}</span>` : ''}</div>
    ${p.summary.requirements.map(reqBlock).join('<hr>')}
  </section>`;
}

function reqBlock(r) {
  const endsAt = new Date(new Date(r.ends).getTime() - 60e3);          // Sunday 23:59, not Monday
  const daysLeft = Math.max(0, Math.ceil((new Date(r.ends) - Date.now()) / 864e5));
  const need = Math.max(0, r.required - r.counted);
  let line;
  if (r.status === 'met') line = `${icon('check', 'ok')} Done for this ${r.period}.`;
  else if (r.status === 'pending') line = `${icon('clock', 'warn')} ${plural(r.pending, 'check-in')} waiting on your officer's review.`;
  else line = `${need} more by ${r.period === 'week' ? weekday(endsAt) : day(endsAt)} · ${plural(daysLeft, 'day')} left`
    + (r.pending ? ` · ${r.pending} in review` : '');

  const dots = r.required <= 8
    ? `<div class="dots" aria-hidden="true">${Array.from({ length: r.required }, (_, i) =>
        `<i class="${i < r.counted ? 'done' : i < r.counted + r.pending ? 'pend' : ''}"></i>`).join('')}</div>`
    : `<div class="meter thin" aria-hidden="true"><i style="width:${Math.min(100, r.counted / r.required * 100)}%"></i></div>`;

  return `<div class="req">
    <div class="req-top">
      <div><span class="big">${r.counted}</span><span class="of"> of ${r.required}</span>
        <div class="muted small">${esc(reqNoun(r.category, r.required))}</div></div>
      ${r.streak >= 2 ? `<span class="chip streak">${icon('flame')} ${r.streak} ${r.period}s in a row</span>` : ''}
    </div>
    ${dots}
    <p class="req-line ${r.status}">${line}</p>
    ${weekStrip(r)}
  </div>`;
}

function weekStrip(r) {
  const h = (r.history || []).slice(-8);
  if (h.length < 2) return '';
  return `<div class="strip" role="img" aria-label="Last ${h.length} ${r.period}s: ${h.filter(p => p.status === 'met').length} met">
    ${h.map((p, i) => {
      const cur = i === h.length - 1;
      const cls = p.status === 'met' ? 'met' : cur ? 'now' : p.status === 'pending' ? 'pend' : 'short';
      return `<span class="${cls}" title="${esc(`Week of ${new Date(p.starts).toLocaleDateString([], { month: 'short', day: 'numeric' })}: ${p.counted} of ${p.required}`)}"></span>`;
    }).join('')}
    <span class="strip-label muted">last ${h.length} ${r.period}s</span>
  </div>`;
}

function meetingCard(m) {
  const mine = standingFor(m.id);
  const st = m.state;
  let action;
  if (mine) {
    const s = standing(mine);
    action = `<button class="badge tone-${s.tone} as-btn" data-act="detail" data-id="${mine.id}" type="button">${icon(s.icon)} Checked in at ${time(mine.checked_in_at)}</button>`;
  } else if (st === 'open') {
    action = `<button class="btn block" data-act="checkin" data-id="${m.id}" type="button">Check in</button>
      <p class="fine center">Open until ${time(m.closes_at)}</p>`;
  } else if (st === 'late') {
    action = `<p class="fine">Check-in closed at ${time(m.closes_at)}.</p>
      <button class="btn block secondary" data-act="checkin" data-late="1" data-id="${m.id}" type="button">I'm here, check in late</button>`;
  } else {
    const mins = minutesUntil(m.opens_at);
    action = `<p class="opens">${icon('clock')} Check-in opens ${mins <= 90 ? `in ${duration(mins)}` : `at ${time(m.opens_at)}`}</p>`;
  }
  return `<article class="card meeting ${st}">
    <div class="m-head">
      <div class="m-time"><b>${time(m.starts_at)}</b><span class="muted">${duration(m.duration_min)}</span></div>
      <div class="m-main">
        <h4>${esc(m.name)}</h4>
        <p class="muted small">${esc(CATEGORY[m.category] || m.category)}${m.counts ? '' : ' · <span class="tag">Only you</span>'}${
          m.miles != null ? ` · ${m.miles < 1 ? 'under a mile' : `${Math.round(m.miles)} mi`} away` : ''}</p>
        ${m.address ? `<a class="addr small" href="${mapsUrl(m)}" target="_blank" rel="noopener">${icon('directions')} ${esc(m.address)}</a>` : ''}
      </div>
    </div>
    ${action}
  </article>`;
}

function mapsUrl(m) {
  const q = encodeURIComponent(m.address || `${m.lat},${m.lng}`);
  return /iPhone|iPad|Macintosh/.test(navigator.userAgent)
    ? `https://maps.apple.com/?q=${q}` : `https://www.google.com/maps/search/?api=1&query=${q}`;
}

/* ---------- checking in ---------- */

async function checkIn(meetingId, { late = false } = {}) {
  const m = S.meetings.find(x => x.id === meetingId);
  if (!m) return;
  const sh = sheet('', { label: 'Check in' });

  if (late && !(await confirmStep(sh, {
    title: 'Check in late?',
    body: `Check-in for this meeting closed at ${time(m.closes_at)}. You can still record that you're here. It will not count on its own, but your officer will review it and can count it.`,
    yes: 'Record that I\'m here', no: 'Cancel',
  }))) return;

  if (!(await primeLocation(sh))) return;
  await runCheckIn(sh, m, body => api('/api/checkins', { method: 'POST', body: JSON.stringify(body) }));
}

/* Before the browser's own prompt, say what we are about to do and why. */
async function primeLocation(sh) {
  const state = await locationPermission();
  if (state === 'granted') return true;
  if (state === 'denied') { sh.set(locationHelp()); return false; }
  let primed = false;
  try { primed = localStorage.getItem('mc_loc_primed') === '1'; } catch { /* ignore */ }
  if (primed && state !== 'prompt') return true;
  const ok = await confirmStep(sh, {
    icon: 'pin',
    title: 'Your location, once',
    body: 'To confirm you are at the meeting, ActionsCount+ reads your location one time, right now. Your phone will ask for permission. It is never read in the background.',
    yes: 'Continue', no: 'Not now',
  });
  try { localStorage.setItem('mc_loc_primed', '1'); } catch { /* ignore */ }
  return ok;
}

function confirmStep(sh, { icon: ic, title, body, yes, no }) {
  return new Promise(resolve => {
    sh.set(`<div class="step">
      ${ic ? `<div class="hero-ic">${icon(ic)}</div>` : ''}
      <h2>${esc(title)}</h2><p>${esc(body)}</p>
      <button class="btn block" data-yes type="button">${esc(yes)}</button>
      <button class="btn block ghost" data-no type="button">${esc(no)}</button></div>`);
    sh.body.querySelector('[data-yes]').focus();
    sh.body.querySelector('[data-yes]').onclick = () => resolve(true);
    sh.body.querySelector('[data-no]').onclick = () => { sh.close(); resolve(false); };
    sh.el.addEventListener('close', () => resolve(false), { once: true });
  });
}

async function runCheckIn(sh, m, post) {
  const locating = acc => sh.set(`<div class="step" aria-live="polite">
    <div class="pulse" aria-hidden="true">${icon('pin')}</div>
    <h2>Finding your location</h2>
    <p class="muted">${acc ? `Accurate to about ±${Math.round(acc)} m${acc > 25 ? ', getting a better fix' : ''}` : 'Keep this screen open. This takes a few seconds.'}</p>
    <p class="small muted">${esc(m.name)}</p></div>`);
  locating();

  let pos;
  try {
    pos = await bestPosition({ onProgress: locating });
  } catch (e) {
    sh.set(e.code === 1 ? locationHelp() : `<div class="step">
      <div class="hero-ic warn">${icon('alert')}</div>
      <h2>Could not get your location</h2>
      <p>${e.code === 3 ? 'Your phone did not find a location in time. Step near a door or window, or outside, and try again.' : esc(e.message || 'Your phone could not share a location.')}</p>
      <button class="btn block" data-retry type="button">Try again</button></div>`);
    const retry = sh.body.querySelector('[data-retry]');
    if (retry) retry.onclick = () => runCheckIn(sh, m, post);
    return;
  }

  sh.set(`<div class="step" aria-live="polite"><div class="pulse" aria-hidden="true">${icon('check')}</div>
    <h2>Checking you in</h2><p class="muted">Comparing your location with the meeting.</p></div>`);
  let c;
  try {
    const { latitude: lat, longitude: lng, accuracy } = pos.coords;
    c = await post({ meeting_id: m.id, lat, lng, accuracy_m: accuracy });
  } catch (e) {
    if (e.status === 428) { sh.close(); S.me = await api('/api/me'); return renderConsent(); }
    sh.set(`<div class="step"><div class="hero-ic warn">${icon('alert')}</div>
      <h2>That did not go through</h2><p>${esc(e.message)}</p>
      <button class="btn block" data-retry type="button">Try again</button></div>`);
    sh.body.querySelector('[data-retry]').onclick = () => runCheckIn(sh, m, post);
    return;
  }
  showResult(sh, c, m, post);
  refresh();
}

function showResult(sh, c, m, post) {
  const st = standing(c);
  const failed = c.reasons.filter(r => !r.passed && r.weight > 0);
  const distFail = failed.find(r => r.name === 'distance');
  const timeFail = failed.find(r => r.name === 'time_window');
  let title, lead;

  if (c.duplicate) {
    title = 'You are already checked in';
    lead = `You checked in to this meeting at ${time(c.checked_in_at)}. Nothing else to do.`;
  } else if (c.status === 'confirmed') {
    title = 'You\'re checked in';
    lead = c.meeting.counts ? 'This counts toward your requirement.' : 'Saved to your own tracking. Only you can see it.';
  } else if (c.status === 'flagged') {
    title = 'Checked in, sent for review';
    lead = 'Something in the signal was unclear, so your officer will take a look. You don\'t need to do anything else.';
  } else {
    title = 'We couldn\'t confirm this one';
    lead = distFail ? 'Your phone put you too far from the meeting. If you are there, step near a door or window, or outside, and try again.'
      : timeFail ? (timeFail.data.minutes_from_start < 0
        ? `Check-in opens at ${time(+new Date(c.meeting.starts_at) - c.meeting.time_window_min * 60e3)}. Try again then.`
        : 'This was outside the check-in window. If you were there, add a note and your officer will review it.')
      : 'Your officer will review it.';
  }

  const endsIn = minutesUntil(c.meeting.ends_at);
  sh.set(`<div class="result tone-${st.tone}">
    <div class="hero-ic tone-${st.tone}">${icon(st.icon === 'clock' ? 'clock' : st.icon)}</div>
    <h2>${esc(title)}</h2>
    <p class="lead">${esc(lead)}</p>
    <p class="small muted">${esc(c.meeting.name)} · ${time(c.checked_in_at)}</p>
    ${diagram(c)}
    <ul class="checks">${c.reasons.filter(r => !isInert(r)).map(checkLine(c)).join('')}</ul>
    ${c.status !== 'rejected' && !c.checked_out_at && endsIn > 0
      ? `<p class="callout">${icon('clock')} Remember to check out when it ends at ${time(c.meeting.ends_at)}.</p>` : ''}
    ${c.status === 'rejected' ? '<p class="small muted">This attempt is saved. Your officer sees it with the reasons above.</p>' : ''}
    ${c.status !== 'confirmed' && !c.duplicate ? noteForm(c) : ''}
    <div class="actions">
      ${distFail && post ? '<button class="btn block" data-again type="button">Try again</button>' : ''}
      <button class="btn block ${distFail ? 'ghost' : c.status === 'confirmed' || c.duplicate ? '' : 'secondary'}" data-close type="button">Done</button>
    </div>
  </div>`);
  wireNoteForm(sh.body, c);
  const again = sh.body.querySelector('[data-again]');
  if (again) again.onclick = () => runCheckIn(sh, m, post);
}

function locationHelp() {
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  return `<div class="step">
    <div class="hero-ic warn">${icon('pin')}</div>
    <h2>Location is turned off for this site</h2>
    <p>ActionsCount+ needs your location at the moment you check in. To turn it back on:</p>
    <ol class="how">${ios
      ? '<li>Open <b>Settings</b>, then <b>Privacy &amp; Security</b>, then <b>Location Services</b>.</li><li>Turn it on, then find <b>Safari Websites</b> and choose <b>While Using the App</b>.</li><li>Come back here and try again.</li>'
      : '<li>Tap the icon to the left of the web address.</li><li>Choose <b>Permissions</b> or <b>Site settings</b>, then allow <b>Location</b>.</li><li>Come back here and try again.</li>'}</ol>
    ${location.protocol !== 'https:' && location.hostname !== 'localhost'
      ? '<p class="small muted">Browsers only share location over a secure (https) connection.</p>' : ''}
    <button class="btn block" data-close type="button">OK</button></div>`;
}

/* A diagram instead of a street map: it shows what matters (you, the meeting,
   the allowed circle, how sure the phone was) and it does not send anyone's
   location to a third-party tile server. */
function diagram(c) {
  if (c.lat == null || c.meeting?.lat == null) return '';
  const m = c.meeting, HALF = 104, C = 120;
  const mPerDeg = 111_320, cos = Math.cos(m.lat * Math.PI / 180);
  const dx = (c.lng - m.lng) * mPerDeg * cos * 3.28084;          // feet east of the meeting
  const dy = (c.lat - m.lat) * mPerDeg * 3.28084;                // feet north
  const dist = Math.hypot(dx, dy), r = m.radius_ft;
  const accFt = (c.accuracy_m || 0) * 3.28084;
  // Fit both the allowed circle and the reading, so a far-off pin is not
  // lost in a corner with half the picture empty.
  const minX = Math.min(-r, dx - accFt), maxX = Math.max(r, dx + accFt);
  const minY = Math.min(-r, dy - accFt), maxY = Math.max(r, dy + accFt);
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2;
  const k = HALF / (Math.max(maxX - minX, maxY - minY, 120) / 2 * 1.08);
  const sx = fx => C + (fx - cx) * k, sy = fy => C - (fy - cy) * k;
  const mx = sx(0), my = sy(0), x = sx(dx), y = sy(dy);
  const inside = dist <= r;
  const youLblY = y - 13 < 14 ? y + 24 : y - 13;
  return `<figure class="diagram">
    <svg viewBox="0 0 240 240" role="img" aria-label="You were ${Math.round(c.distance_ft)} feet from the meeting. The allowed distance is ${Math.round(r)} feet.">
      <circle cx="${mx}" cy="${my}" r="${r * k}" class="d-zone"/>
      ${accFt ? `<circle cx="${x}" cy="${y}" r="${Math.max(accFt * k, 4)}" class="d-acc ${inside ? '' : 'out'}"/>` : ''}
      <line x1="${mx}" y1="${my}" x2="${x}" y2="${y}" class="d-line"/>
      <rect x="${mx - 7}" y="${my - 7}" width="14" height="14" rx="3.5" class="d-meet"/>
      <circle cx="${x}" cy="${y}" r="7" class="d-you ${inside ? '' : 'out'}"/>
      <text x="${mx}" y="${my + 23}" class="d-lbl" text-anchor="middle">meeting</text>
      <text x="${Math.min(Math.max(x, 24), 216)}" y="${youLblY}" class="d-lbl strong" text-anchor="middle">you</text>
    </svg>
    <figcaption><span><i class="k-you ${inside ? '' : 'out'}"></i>You, ${feet(c.distance_ft)} away</span>
      <span><i class="k-zone"></i>Allowed: ${feet(r)}</span>
      ${accFt ? '<span><i class="k-acc"></i>Phone accuracy</span>' : ''}</figcaption>
  </figure>`;
}

/* A check that is switched off, or a note added at check-out, is information,
   not a pass or a fail, and gets a neutral mark rather than a green tick. */
const isInert = r => r.name === 'network' && /not configured/.test(r.reason);
const checkLine = c => r => {
  const kind = isInert(r) || r.weight === 0 && r.name !== 'checkout_distance' ? 'info' : r.passed ? 'ok' : 'no';
  const ic = { ok: 'check', no: 'x', info: 'note' }[kind];
  return `<li class="${kind}">${icon(ic)}<span>${esc(explain(r, c))}</span></li>`;
};

function noteForm(c, answering = false) {
  return `<form class="note-form" data-note="${c.id}">
    <label for="note-${c.id}">${answering ? 'Your answer' : 'Add a note for your officer <span class="muted">(optional)</span>'}</label>
    <textarea id="note-${c.id}" rows="3" maxlength="2000" placeholder="${answering
      ? 'Answer in your own words. Your officer sees this with the check-in.'
      : 'For example: the meeting is in the basement and my signal was weak.'}"></textarea>
    <button class="btn ${answering ? '' : 'secondary'} block" type="submit">${answering ? 'Send answer' : 'Send note'}</button>
  </form>`;
}

function wireNoteForm(root, c, onSent) {
  const f = root.querySelector('form[data-note]');
  if (!f) return;
  f.onsubmit = async e => {
    e.preventDefault();
    const ta = f.querySelector('textarea'), btn = f.querySelector('button');
    const msg = ta.value.trim();
    if (msg.length < 3) { ta.focus(); toast('Write a few words first.'); return; }
    btn.disabled = true; btn.textContent = 'Sending';
    try {
      await api(`/api/checkins/${c.id}/dispute`, { method: 'POST', body: JSON.stringify({ message: msg }) });
      if (onSent) return onSent();
      f.outerHTML = `<p class="callout ok">${icon('check')} Note sent. Your officer will see it with this check-in.</p>`;
      refresh();
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Send note'; toast(err.message);
    }
  };
}

/* ---------- checking out ---------- */

async function checkOut(id) {
  const c = S.mine.find(x => x.id === id);
  if (!c) return;
  const sh = sheet('', { label: 'Check out' });
  const early = minutesUntil(c.meeting.ends_at);
  if (early > 10 && !(await confirmStep(sh, {
    icon: 'clock', title: 'Leave early?',
    body: `This meeting ends at ${time(c.meeting.ends_at)}. If you check out now, your record will show that you left ${duration(early)} early.`,
    yes: 'Check out now', no: 'Not yet',
  }))) return;

  sh.set(`<div class="step" aria-live="polite"><div class="pulse">${icon('pin')}</div>
    <h2>Checking you out</h2><p class="muted">Reading your location one more time.</p></div>`);
  let body = {};
  try {
    const pos = await bestPosition({ goodEnoughM: 50, settleMs: 4000, hardTimeoutMs: 12000 });
    body = { lat: pos.coords.latitude, lng: pos.coords.longitude };
  } catch { /* check out without a location rather than not at all */ }
  try {
    const done = await api(`/api/checkins/${id}/checkout`, { method: 'POST', body: JSON.stringify(body) });
    if (!body.lat) toast('Checked out, without a location reading.');
    await refresh();
    if (!canAsk() || !done.meeting?.counts) { sh.close(); toast('Checked out.'); return; }
    askStep(sh, id, { afterCheckout: true });
  } catch (e) {
    sh.set(`<div class="step"><div class="hero-ic warn">${icon('alert')}</div><h2>Could not check out</h2>
      <p>${esc(e.message)}</p><button class="btn block" data-close type="button">OK</button></div>`);
  }
}

/* ---------- asking someone who was there ----------
   A sponsor, the chair, a counselor. They get one text with a link, and
   their answer sits on the check-in for the participant and the officer. */

const RELATIONSHIPS = [['sponsor', 'Sponsor'], ['chair', 'Meeting chair'], ['counselor', 'Counselor'],
  ['family', 'Family'], ['friend', 'Friend'], ['other', 'Someone else who was there']];
const VOUCH = {
  confirmed: { tone: 'ok', icon: 'check' }, declined: { tone: 'bad', icon: 'x' },
  asked: { tone: 'warn', icon: 'clock' }, expired: { tone: 'neutral', icon: 'clock' },
};

/* Texting has to be switched on, or this is a demo where the link shows on screen. */
function canAsk() { return !!(S.ctx.texting || S.ctx.demo); }

function askable(c) {
  const xs = c.confirmations || [];
  return canAsk() && c.meeting.counts && c.status !== 'rejected' && !xs.some(x => x.state === 'asked')
    && xs.length < 3 && Date.now() - new Date(c.checked_in_at) < 7 * 864e5;
}

function askSheet(id) {
  const sh = sheet('', { label: 'Ask someone to confirm' });
  askStep(sh, id);
}

async function askStep(sh, id, { afterCheckout = false } = {}) {
  let people = [];
  try { people = await api('/api/my/confirmers'); } catch { people = []; }
  const c = S.mine.find(x => x.id === id);
  const xs = (c?.confirmations || []).filter(x => x.state !== 'expired');
  const waiting = xs.filter(x => x.state === 'asked');
  const answered = xs.filter(x => x.state === 'confirmed');
  // Anyone already asked about this check-in is not offered again.
  people = people.filter(k => !xs.some(x => x.confirmer_id === k.id));
  sh.set(`<div class="step left ask">
    ${afterCheckout ? `<div class="hero-ic tone-ok">${icon('check')}</div><h2>Checked out</h2>` : '<h2>Ask someone to confirm</h2>'}
    <p>Ask someone who was there to confirm it. They get one text with a link. It shows your first name and the meeting, nothing else.</p>
    ${answered.length ? `<p class="callout ok">${icon('check')} ${esc(answered.map(x => firstName(x.name)).join(' and '))} already confirmed this one.</p>` : ''}
    ${waiting.length ? `<p class="callout">${icon('clock')} You already asked ${esc(waiting.map(x => firstName(x.name)).join(' and '))}. Waiting for their answer.</p>` : ''}
    <ul class="pick people" id="askPeople">
      ${people.map(k => `<li><button class="pick-row" data-k="${k.id}" type="button"><b>${esc(k.name)}</b>
        <span class="muted small">${esc(k.relationship_words)} · ${esc(k.phone)}</span></button></li>`).join('')}
      <li><button class="pick-row" data-k="new" type="button"><b>${people.length ? 'Someone else' : 'Add someone'}</b>
        <span class="muted small">Their name and mobile number</span></button></li>
    </ul>
    <form id="askNew" class="ask-form" hidden>
      <label for="ak-name">Their name</label>
      <input id="ak-name" maxlength="120" autocomplete="off" placeholder="Kim">
      <label for="ak-phone">Their mobile number</label>
      <input id="ak-phone" type="tel" inputmode="tel" autocomplete="off" placeholder="(734) 555-0142">
      <label for="ak-rel">They are your</label>
      <select id="ak-rel">${RELATIONSHIPS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
      <label class="check"><input id="ak-save" type="checkbox" checked> Remember them for next time</label>
      <button class="btn block" type="submit">Send the text</button>
    </form>
    <button class="btn block ghost" data-close type="button">${afterCheckout ? 'Not this time' : 'Cancel'}</button>
  </div>`);
  const body = sh.body;
  body.querySelector('#askPeople').onclick = e => {
    const b = e.target.closest('[data-k]');
    if (!b) return;
    if (b.dataset.k === 'new') {
      body.querySelector('#askPeople').hidden = true;
      const f = body.querySelector('#askNew');
      f.hidden = false;
      f.querySelector('#ak-name').focus();
      return;
    }
    sendAsk(sh, id, { confirmer_id: Number(b.dataset.k) }, b.querySelector('b').textContent);
  };
  body.querySelector('#askNew').onsubmit = e => {
    e.preventDefault();
    const f = e.target;
    sendAsk(sh, id, {
      name: f.querySelector('#ak-name').value.trim(), phone: f.querySelector('#ak-phone').value.trim(),
      relationship: f.querySelector('#ak-rel').value, save: f.querySelector('#ak-save').checked,
    }, f.querySelector('#ak-name').value.trim());
  };
}

async function sendAsk(sh, id, payload, name) {
  sh.body.querySelectorAll('button').forEach(b => { b.disabled = true; });
  let r;
  try {
    r = await api(`/api/checkins/${id}/confirmation`, { method: 'POST', body: JSON.stringify(payload) });
  } catch (e) {
    sh.body.querySelectorAll('button').forEach(b => { b.disabled = false; });
    toast(e.message);
    return;
  }
  const who = esc(firstName(r.name || name));
  sh.set(`<div class="step"><div class="hero-ic tone-ok">${icon('inbox')}</div>
    <h2>We texted ${who}</h2>
    <p>You will get a text when they answer. It shows on this check-in too. The link works for 7 days.</p>
    ${r.demo_link ? `<p class="callout ok small">${icon('note')}<span>Demo: the text to ${who} would carry this link.<br>
      <a href="${esc(r.demo_link)}" target="_blank" rel="noopener">Open it as ${who}</a></span></p>` : ''}
    <button class="btn block" data-close type="button">Done</button></div>`);
  refresh();
}

/* What people said about a check-in, and the button to ask, for the detail sheet. */
function vouchBlock(c) {
  const xs = c.confirmations || [];
  const can = askable(c);
  if (!xs.length && !can) return '';
  const said = x => {
    if (x.state === 'confirmed') return `confirmed it, ${esc(relativeDay(x.answered_at).toLowerCase())} at ${time(x.answered_at)}.`;
    if (x.state === 'declined') return 'could not confirm it.';
    if (x.state === 'asked') return `was asked ${esc(relativeDay(x.asked_at).toLowerCase())}. No answer yet.`;
    return 'was asked and did not answer.';
  };
  return `<h3 class="sub">Someone who was there</h3>
    ${xs.map(x => `<p class="vouch tone-${VOUCH[x.state].tone}">${icon(VOUCH[x.state].icon)}
      <span><b>${esc(x.name)}</b> (${esc(x.relationship_words)}) ${said(x)}${x.answer_note ? `<br><span class="muted small">"${esc(x.answer_note)}"</span>` : ''}</span></p>`).join('')}
    ${can ? `<button class="btn secondary block" data-act="ask" data-id="${c.id}" type="button">${icon('inbox')} Ask someone to confirm</button>` : ''}`;
}

/* The You tab: the people a participant keeps on their list. */
async function loadPeople() {
  const ul = $('#peopleList');
  if (!ul) return;
  let people = [];
  try { people = await api('/api/my/confirmers'); } catch { ul.innerHTML = '<li class="muted small">Could not load your list.</li>'; return; }
  ul.innerHTML = people.length ? people.map(k => `<li><span class="who"><b>${esc(k.name)}</b>
      <span class="muted small">${esc(k.relationship_words)} · ${esc(k.phone)}</span></span>
      <button class="linkish small" data-act="removeperson" data-id="${k.id}" type="button">Remove</button></li>`).join('')
    : '<li class="muted small">Nobody yet. You can also add someone when you check out.</li>';
}

function addPersonSheet() {
  const sh = sheet(`<form class="step left ask-form" id="addPerson">
    <h2>Add someone</h2>
    <p class="muted">When you ask them, they get one text with a link. They see your first name and the meeting, nothing else.</p>
    <label for="ap-name">Their name</label>
    <input id="ap-name" maxlength="120" autocomplete="off" placeholder="Kim" required>
    <label for="ap-phone">Their mobile number</label>
    <input id="ap-phone" type="tel" inputmode="tel" autocomplete="off" placeholder="(734) 555-0142" required>
    <label for="ap-rel">They are your</label>
    <select id="ap-rel">${RELATIONSHIPS.map(([v, l]) => `<option value="${v}">${l}</option>`).join('')}</select>
    <button class="btn block" type="submit">Save</button>
  </form>`, { label: 'Add someone' });
  sh.body.querySelector('#ap-name').focus();
  sh.body.querySelector('#addPerson').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    try {
      await api('/api/my/confirmers', { method: 'POST', body: JSON.stringify({
        name: f.querySelector('#ap-name').value.trim(), phone: f.querySelector('#ap-phone').value.trim(),
        relationship: f.querySelector('#ap-rel').value }) });
      sh.close();
      toast('Saved.');
      loadPeople();
    } catch (err) { toast(err.message); }
  };
}

async function removePerson(id) {
  try { await api(`/api/my/confirmers/${id}`, { method: 'DELETE' }); loadPeople(); }
  catch (e) { toast(e.message); }
}

/* ---------- personal meetings ---------- */

function personalMeeting() {
  const sh = sheet(`<form class="step left" id="pm">
    <h2>Track a personal meeting</h2>
    <p class="muted">For appointments that aren't on the list, like therapy. It starts now, where you are.</p>
    <p class="callout">${icon('eye')} Only you can see these. They don't count toward your court requirement.</p>
    <label for="pm-name">What is it?</label>
    <input id="pm-name" required maxlength="120" placeholder="For example: Therapy with Dr. Patel" autocomplete="off">
    <div class="two">
      <div><label for="pm-cat">Type</label>
        <select id="pm-cat"><option value="therapy">Therapy</option><option value="private">Support group</option>
        <option value="community_service">Volunteering</option><option value="public">Other meeting</option></select></div>
      <div><label for="pm-len">Length</label>
        <select id="pm-len"><option>30</option><option selected>60</option><option>90</option><option>120</option></select></div>
    </div>
    <button class="btn block" type="submit">Start and check in</button>
  </form>`, { label: 'Track a personal meeting' });
  sh.body.querySelector('#pm-name').focus();
  sh.body.querySelector('#pm').onsubmit = async e => {
    e.preventDefault();
    const name = sh.body.querySelector('#pm-name').value.trim();
    const category = sh.body.querySelector('#pm-cat').value;
    const duration_min = Number(sh.body.querySelector('#pm-len').value);
    if (!(await primeLocation(sh))) return;
    const draft = { id: 0, name, opens_at: new Date().toISOString() };
    await runCheckIn(sh, draft, async ({ lat, lng, accuracy_m }) => {
      const m = await api('/api/meetings', { method: 'POST', body: JSON.stringify({
        name, category, lat, lng, duration_min, starts_at: new Date().toISOString() }) });
      return api('/api/checkins', { method: 'POST',
        body: JSON.stringify({ meeting_id: m.id, lat, lng, accuracy_m }) });
    });
  };
}

/* ---------- History ---------- */

function viewHistory() {
  if (!S.mine.length) {
    return `<div class="stack"><h1 class="page">History</h1>
      <div class="card empty">${icon('list', 'lg muted')}<p>Your check-ins will show up here, with the reasons behind each result.</p></div></div>`;
  }
  const weeks = {};
  S.mine.forEach(c => (weeks[weekKey(c.checked_in_at)] ||= []).push(c));
  const needsYou = S.mine.filter(c => c.awaiting_reply);

  return `<div class="stack">
    <h1 class="page">History</h1>
    ${needsYou.length ? `<div class="callout warn">${icon('note')} Your officer asked a question about ${plural(needsYou.length, 'check-in')}. Open it to answer.</div>` : ''}
    ${Object.entries(weeks).map(([k, list]) => `
      <section class="week">
        <div class="row between"><h2 class="sec flush">${esc(weekLabel(k))}</h2>${weekTally(list)}</div>
        <ul class="hist card flush">${list.map(histRow).join('')}</ul>
      </section>`).join('')}
    <p class="fine center">Exact locations are deleted after ${S.me.policy.pin_retention_days} days. Results stay with your case record.</p>
  </div>`;
}

function weekTally(list) {
  const counted = new Set(list.filter(c => c.meeting.counts && ['confirmed', 'approved'].includes(standing(c).key))
    .map(c => c.meeting_id)).size;
  return counted ? `<span class="muted small">${counted} counted</span>` : '';
}

function histRow(c) {
  const s = standing(c);
  return `<li><button class="hist-row" data-act="detail" data-id="${c.id}" type="button">
    <span class="dot tone-${s.tone}">${icon(s.icon)}</span>
    <span class="h-main"><b>${esc(c.meeting.name)}</b>
      <span class="muted small">${esc(relativeDay(c.checked_in_at))}, ${time(c.checked_in_at)}${c.meeting.counts ? '' : ' · only you'}${
        c.confirmed_by?.state === 'confirmed' ? ` · ${esc(firstName(c.confirmed_by.name))} confirmed` : ''}</span></span>
    <span class="h-state tone-${s.tone}">${esc(s.label)}</span>
    ${icon('arrow', 'muted chev')}
  </button></li>`;
}

function detail(id) {
  if (!S.mine.some(x => x.id === id)) return;
  const sh = sheet(detailHtml(id), { label: 'Check-in details' });
  wireDetail(sh, id);
  // An answer from someone asked may have come in since the last load.
  const before = JSON.stringify(S.mine.find(x => x.id === id));
  refresh().then(() => {
    if (!sh.el.open || JSON.stringify(S.mine.find(x => x.id === id)) === before) return;
    const draft = sh.body.querySelector('textarea')?.value;
    sh.set(detailHtml(id)); wireDetail(sh, id);
    if (draft) sh.body.querySelector('textarea').value = draft;
  });
}

/* After a note is sent, reload and redraw in place, so the thread shows it. */
function wireDetail(sh, id) {
  const c = S.mine.find(x => x.id === id);
  wireNoteForm(sh.body, c, async () => {
    await refresh();
    if (sh.el.open) { sh.set(detailHtml(id)); wireDetail(sh, id); toast(c.awaiting_reply ? 'Answer sent.' : 'Note sent.'); }
  });
  if (c.awaiting_reply) sh.body.querySelector('textarea')?.focus();
}

function detailHtml(id) {
  const c = S.mine.find(x => x.id === id);
  const s = standing(c);
  const asked = c.awaiting_reply;
  const canNote = c.status !== 'confirmed' || c.reviews.length;
  return `<div class="result left">
    <span class="badge tone-${s.tone}">${icon(s.icon)} ${esc(s.label)}</span>
    <h2>${esc(c.meeting.name)}</h2>
    <p class="muted">${esc(dateTime(c.checked_in_at))}${c.meeting.address ? ` · ${esc(c.meeting.address)}` : ''}</p>

    ${!c.reviews.length && c.meeting.counts && c.status === 'flagged'
      ? '<p class="callout warn">Waiting for your officer to review it. Until then it shows as pending, not missed.</p>'
      : !c.reviews.length && c.meeting.counts && c.status === 'rejected'
      ? '<p class="callout">Your officer will review this attempt along with any note you add.</p>' : ''}

    ${thread(c)}
    ${canNote ? noteForm(c, asked) : ''}
    ${vouchBlock(c)}

    ${c.pin_purged ? `<p class="callout">${icon('trash')} The exact location was deleted after ${S.me.policy.pin_retention_days} days, as promised. The result stays.</p>` : diagram(c)}

    <h3 class="sub">What we checked</h3>
    <ul class="checks">${c.reasons.map(checkLine(c)).join('')}</ul>
    <p class="small muted">${c.checked_out_at ? `Checked out at ${time(c.checked_out_at)}.` : c.status === 'rejected' ? '' : 'No check-out recorded.'}
      Confidence score ${c.confidence} out of 100. The score helps your officer sort what to look at. It is not a decision.</p>
  </div>`;
}

/* Everything said about one check-in, in order: the officer's decisions and
   questions, and the participant's own notes. Nothing is left out. */
function thread(c) {
  const ev = [
    ...c.reviews.map(r => ({ at: r.at, who: 'officer', r })),
    ...c.disputes.map(d => ({ at: d.created_at, who: 'you', d })),
  ].sort((a, b) => new Date(a.at) - new Date(b.at));
  if (!ev.length) return '';
  const said = { approved: 'Counted this check-in', denied: 'Decided this does not count', more_info: 'Asked a question' };
  return `<h3 class="sub">Between you and your officer</h3><ol class="thread">${ev.map(e => e.who === 'officer'
    ? `<li class="from-officer tone-${{ approved: 'ok', denied: 'bad', more_info: 'warn' }[e.r.decision]}">
        <b>${esc(e.r.by || 'Your officer')}: ${esc(said[e.r.decision])}</b>
        ${e.r.note ? `<p>"${esc(e.r.note)}"</p>` : ''}<span class="small muted">${esc(dateTime(e.at))}</span></li>`
    : `<li class="from-you"><b>You</b><p>${esc(e.d.message)}</p><span class="small muted">${esc(dateTime(e.at))}</span></li>`
  ).join('')}</ol>`;
}

/* ---------- You ---------- */

function viewYou() {
  const me = S.me, pol = me.policy;
  const years = Math.round(pol.record_retention_days / 365);
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone;
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  return `<div class="stack">
    <h1 class="page">${esc(me.name)}</h1>
    <section class="card">
      <dl class="kv">
        ${me.case_number ? `<dt>Case</dt><dd>${esc(me.case_number)}</dd>` : ''}
        ${me.program ? `<dt>Program</dt><dd>${esc(me.program)}</dd>` : ''}
        ${me.officer ? `<dt>Officer</dt><dd>${esc(me.officer)}</dd>` : ''}
        ${me.county ? `<dt>County</dt><dd>${esc(me.county)}</dd>` : ''}
      </dl>
    </section>

    ${billingCard()}

    <section class="card" aria-labelledby="h-priv">
      <h2 class="sec flush" id="h-priv">${icon('shield')} Your privacy</h2>
      <ul class="plain">
        <li>Your location is read only when you tap check in or check out. Never in the background.</li>
        <li>Your officer sees whether each check-in was confirmed, not a map of where you go.</li>
        <li>Exact locations are deleted after ${pol.pin_retention_days} days. Results stay with your case for up to ${plural(years, 'year')}.</li>
        <li>Personal meetings you track are visible only to you.</li>
        <li>You can add a note to any result, and a person reviews anything unclear.</li>
      </ul>
      ${me.consent.accepted_at ? `<p class="fine">You agreed to this on ${esc(day(me.consent.accepted_at))}.</p>` : ''}
    </section>

    ${canAsk() ? `<section class="card" aria-labelledby="h-people">
      <h2 class="sec flush" id="h-people">${icon('users')} People who can confirm you were there</h2>
      <p class="small muted">A sponsor, a chair, a counselor. When you ask, they get one text with a link. They see your first name and the meeting, nothing else.</p>
      <ul class="plain people-list" id="peopleList"><li class="muted small">Loading</li></ul>
      <button class="btn secondary block" data-act="addperson" type="button">${icon('plus')} Add someone</button>
    </section>` : ''}

    <button class="link-row" data-act="export" type="button">${icon('download')}
      <span><b>Download my record</b><br><span class="muted small">Every check-in, reason and note, as a file you keep.</span></span>${icon('arrow', 'muted')}</button>

    ${!standalone && (S.installPrompt || ios) ? `<button class="link-row" data-act="install" type="button">${icon('install')}
      <span><b>Add to your home screen</b><br><span class="muted small">Opens like an app, one tap from your home screen.</span></span>${icon('arrow', 'muted')}</button>` : ''}

    ${S.ctx.dev_auth ? '' : `<button class="link-row" data-act="signout" type="button">${icon('back')}
      <span><b>Sign out</b><br><span class="muted small">You will need a new code to sign back in.</span></span></button>`}

    ${S.devUsers ? `<section class="card dev">
      <h2 class="sec flush">Demo mode</h2>
      <p class="small muted">Dev auth is on, so there is no sign-in. Switch between the demo participants to see each story.</p>
      <button class="btn secondary block" data-act="switch" type="button">Switch participant</button>
    </section>` : ''}
    <p class="fine center">ActionsCount+ · <a href="/officer">Staff sign-in</a></p>
  </div>`;
}

async function exportRecord() {
  try {
    const r = await fetch('/api/my/export', { headers: { 'X-MC-User': identity.get() } });
    if (!r.ok) throw new Error('Could not prepare your record. Try again.');
    const blob = await r.blob();
    const name = (r.headers.get('content-disposition') || '').match(/filename="([^"]+)"/)?.[1] || 'actionscount-record.json';
    const a = Object.assign(document.createElement('a'), { href: URL.createObjectURL(blob), download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
    toast('Your record is downloading.');
  } catch (e) { toast(e.message); }
}

async function install() {
  if (S.installPrompt) {
    S.installPrompt.prompt();
    await S.installPrompt.userChoice;
    S.installPrompt = null;
    renderTab();
    return;
  }
  sheet(`<div class="step left"><h2>Add to your home screen</h2>
    <ol class="how"><li>Tap the <b>Share</b> button at the bottom of Safari.</li>
    <li>Scroll down and tap <b>Add to Home Screen</b>.</li><li>Tap <b>Add</b>.</li></ol>
    <button class="btn block" data-close type="button">Got it</button></div>`, { label: 'Add to home screen' });
}

function switchUser() {
  const sh = sheet(`<div class="step left"><h2>Switch participant</h2>
    <p class="small muted">Demo only. In production each person signs in with their own phone number.</p>
    <ul class="pick">${S.devUsers.map(u => `<li><button class="pick-row ${String(u.id) === identity.get() ? 'on' : ''}"
      data-uid="${u.id}" type="button">${esc(u.name)}${String(u.id) === identity.get() ? ' <span class="muted small">(you)</span>' : ''}</button></li>`).join('')}</ul></div>`,
  { label: 'Switch participant' });
  sh.body.addEventListener('click', e => {
    const b = e.target.closest('[data-uid]');
    if (!b) return;
    identity.set(b.dataset.uid);
    location.hash = 'today';
    location.reload();
  });
}

/* ---------- consent ---------- */

function renderConsent() {
  document.body.classList.add('bare');
  const me = S.me, pol = me.policy, years = Math.round(pol.record_retention_days / 365);
  const officer = me.officer || (me.billing ? 'Whoever you share your record with' : 'Your officer');
  app.innerHTML = `<main class="consent">
    <div class="consent-head">${logo()}<p class="muted">ActionsCount+${me.county ? ` for ${esc(me.county)}` : ''}</p></div>
    <h1>Before your first check&#8209;in</h1>
    <p class="lead">Here is exactly what this app records and who sees it. It takes a minute to read.</p>
    <ol class="promises">
      <li>${icon('pin')}<div><b>Your location, only when you tap</b>
        <p>We read your phone's location once, when you check in or out. Never in the background, never between meetings.</p></div></li>
      <li>${icon('eye')}<div><b>${esc(officer)} sees results, not a map</b>
        <p>They see whether each check-in was confirmed. Your exact location is shown to them only if a check-in needs a closer look.</p></div></li>
      <li>${icon('trash')}<div><b>Deleted on a schedule</b>
        <p>Exact locations are deleted after ${pol.pin_retention_days} days. The result (confirmed or not, distance, time) stays with your case for up to ${plural(years, 'year')}${me.county ? `, under ${esc(me.county)} policy` : ''}.</p></div></li>
      <li>${icon('shield')}<div><b>You can see and question everything</b>
        <p>Every result comes with its reasons. You can add a note to any of them, and a person reviews anything unclear before it affects you.</p></div></li>
      <li>${icon('inbox')}<div><b>Texts, only about your check-ins</b>
        <p>A sign-in code when you ask for one, and a text when someone you chose confirms you were at a meeting. Reply STOP to any of them to stop.</p></div></li>
    </ol>
    <button class="btn block big" id="agree" type="button">I understand, continue</button>
    <p class="fine center">You can reread this any time under <b>You</b>. ${S.devUsers ? `<button class="linkish" data-act="switch" type="button">Demo: switch participant</button>` : ''}</p>
  </main>`;
  $('#agree').onclick = async () => {
    $('#agree').disabled = true;
    try {
      await api('/api/my/consent', { method: 'POST', body: JSON.stringify({ version: me.consent.version }) });
      S.me = await api('/api/me');
      enter();
    } catch (e) { $('#agree').disabled = false; toast(e.message); }
  };
}

/* ---------- paying for your own profile ---------- */

/* Only a self-serve profile ever sees this. Someone a county enrolled has
   no billing at all. */
function renderPayWall() {
  document.body.classList.add('bare');
  const b = S.me.billing;
  app.innerHTML = `<main class="consent">
    <div class="consent-head">${logo()}<p class="muted">ActionsCount+</p></div>
    <h1>${b.status === 'unpaid_yet' ? 'One more step' : 'Your subscription needs attention'}</h1>
    <p class="lead">${esc(b.words)}</p>
    ${b.price ? `<p class="callout">${icon('note')} <span>${b.trial ? `${esc(b.trial)}, then ${esc(b.price.label)}` : esc(b.price.label)}. Cancel any time from here.</span></p>` : ''}
    ${b.payment ? `<button class="btn block big" data-act="${b.can_pay ? 'pay' : 'portal'}" type="button">${b.can_pay ? 'Continue to payment' : 'Manage billing'}</button>`
      : `<p class="callout warn">${icon('alert')} Payment is not set up on this server yet.</p>`}
    ${b.payment && b.can_pay ? `<details class="promo">
      <summary>Have a code?</summary>
      <label for="code">Promotion code</label>
      <input id="code" name="code" autocomplete="off" autocapitalize="characters" maxlength="40" placeholder="For example FREEYEAR">
      <p class="fine">Applied on the next page. With a code that covers the whole cost, no card is asked for.</p>
    </details>` : ''}
    <button class="btn block ghost" data-act="signout" type="button">Sign out</button>
    <p class="fine center">Check-ins are paused until the subscription is active. Your record is kept.</p>
  </main>`;
}

/* Hand the person to a Stripe page. The link is minted for this profile only. */
async function stripePage(path) {
  const btn = document.querySelector(`[data-act="${path.endsWith('checkout') ? 'pay' : 'portal'}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Opening'; }
  const code = ($('#code')?.value || '').trim();
  try {
    const r = await api(path, { method: 'POST', body: JSON.stringify(code ? { code } : {}) });
    location.href = r.url;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = path.endsWith('checkout') ? 'Continue to payment' : 'Manage billing'; }
    toast(e.message, { ms: 5000 });
  }
}

function billingCard() {
  const b = S.me.billing;
  if (!b) return '';
  return `<section class="card" aria-labelledby="h-bill">
    <h2 class="sec flush" id="h-bill">${icon('file')} Your subscription</h2>
    <p class="small" style="margin-top:6px">${esc(b.words)}${b.price ? ` <span class="muted">${esc(b.price.label)}.</span>` : ''}</p>
    ${b.payment && b.can_pay ? `<button class="btn block" data-act="pay" type="button" style="margin-top:12px">Continue to payment</button>` : ''}
    ${b.payment && b.can_manage ? `<button class="btn secondary block" data-act="portal" type="button" style="margin-top:12px">Manage billing</button>` : ''}
  </section>`;
}

/* ---------- signing in ---------- */

/* Phone number, then a one-time code. No password to forget, and a new phone
   only needs a new code. */
function renderSignIn(phone = '') {
  document.body.classList.add('bare');
  S.started = false;
  app.innerHTML = `<main class="signin">
    <div class="signin-head">${logo()}<b>ActionsCount+</b></div>
    <h1>Sign in</h1>
    <p class="lead">Enter the mobile number on your account. We will check a one-time code, not a password.</p>
    <form id="phoneForm" novalidate>
      <label for="phone">Phone number</label>
      <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel"
        placeholder="(734) 555-0101" value="${esc(phone)}" required>
      <p class="err" id="signinErr" role="alert" hidden></p>
      <button class="btn block big" type="submit">Continue</button>
    </form>
    <p class="fine center">Trouble signing in? Your officer can read you a code.</p>
    ${S.ctx.self_signup ? `<p class="fine center">No profile yet? <a href="/join">Create one</a></p>` : ''}
  </main>`;
  const input = $('#phone');
  input.focus();
  $('#phoneForm').onsubmit = async e => {
    e.preventDefault();
    const value = input.value.trim();
    if (value.replace(/\D/g, '').length < 10) return signInError('Enter the full phone number, including the area code.');
    const btn = e.target.querySelector('button');
    btn.disabled = true; btn.textContent = 'Checking';
    try {
      const r = await api('/api/auth/code/request', { method: 'POST', body: JSON.stringify({ phone: value }) });
      renderCodeStep(value, r);
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Continue';
      signInError(err.message);
    }
  };
}

function renderCodeStep(phone, sent) {
  app.innerHTML = `<main class="signin">
    <div class="signin-head">${logo()}<b>ActionsCount+</b></div>
    <h1>Enter your code</h1>
    <p class="lead">${sent.delivered
      ? esc(sent.message || `We sent a six-digit code to ${phone}.`)
      : `Your officer can read you a six-digit code. It works for ${sent.minutes || 60} minutes.`}</p>
    ${sent.demo_code ? `<p class="callout ok">${icon('note')} Demo: your code is <b class="code">${esc(sent.demo_code)}</b></p>` : ''}
    <form id="codeForm" novalidate>
      <label for="code">Six-digit code</label>
      <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*"
        maxlength="6" class="code-input" placeholder="000000" required>
      <p class="err" id="signinErr" role="alert" hidden></p>
      <button class="btn block big" type="submit">Sign in</button>
      <button class="btn block ghost" type="button" data-back>Use a different number</button>
    </form>
  </main>`;
  const input = $('#code');
  input.focus();
  input.addEventListener('input', () => {
    input.value = input.value.replace(/\D/g, '').slice(0, 6);
    if (input.value.length === 6) $('#codeForm').requestSubmit();
  });
  app.querySelector('[data-back]').onclick = () => renderSignIn(phone);
  $('#codeForm').onsubmit = async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    btn.disabled = true; btn.textContent = 'Checking';
    try {
      await api('/api/auth/code/verify', { method: 'POST', body: JSON.stringify({ phone, code: input.value }) });
      location.reload();
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Sign in';
      input.value = ''; input.focus();
      signInError(err.message);
    }
  };
}

function signInError(message) {
  const el = $('#signinErr');
  if (el) { el.textContent = message; el.hidden = false; }
}

async function signOut() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* going anyway */ }
  location.href = '/';
}

function renderStaffHere() {
  document.body.classList.add('bare');
  app.innerHTML = `<main class="consent center">${logo()}
    <h1>This is the participant app</h1>
    <p class="lead">You are signed in as staff. The officer dashboard is where you want to be.</p>
    <a class="btn block" href="/officer">Open the officer dashboard</a>
    <button class="btn block ghost" data-act="signout" type="button">Sign out</button></main>`;
}

function renderFatal(e) {
  document.body.classList.add('bare');
  app.innerHTML = `<main class="consent center">${icon('alert', 'lg')}
    <h1>ActionsCount+ could not load</h1><p class="lead">${esc(e.message)}</p>
    <button class="btn block" data-act="reload" type="button">Try again</button></main>`;
}

/* ---------- plumbing ---------- */

app.addEventListener('click', e => {
  const t = e.target.closest('[data-act]');
  if (!t) return;
  const id = Number(t.dataset.id);
  ({
    checkin: () => checkIn(id, { late: !!t.dataset.late }),
    checkout: () => checkOut(id),
    detail: () => detail(id),
    personal: personalMeeting,
    ask: () => { document.querySelector('dialog[open]')?.close(); askSheet(id); },
    area: areaSheet,
    more: () => { S.expanded.add(t.dataset.key); renderTab(); },
    addperson: addPersonSheet,
    removeperson: () => removePerson(id),
    export: exportRecord,
    install,
    switch: switchUser,
    signout: signOut,
    pay: () => stripePage('/api/billing/checkout'),
    portal: () => stripePage('/api/billing/portal'),
    retry: () => { S.loadError = null; renderTab(); refresh(); },
    reload: () => location.reload(),
  })[t.dataset.act]?.();
});

/* The search box on Today. Re-drawing the lists must not lose the caret. */
document.addEventListener('input', e => {
  if (e.target.id !== 'mfilter') return;
  S.filter = e.target.value;
  S.expanded.clear();
  renderTab();
  const f = $('#mfilter');
  if (f) { f.focus(); f.setSelectionRange(f.value.length, f.value.length); }
});

function watchConnection() {
  window.addEventListener('online', () => { syncOffline(); refresh(); });
  window.addEventListener('offline', syncOffline);
}
function syncOffline() { const o = $('#offline'); if (o) o.hidden = navigator.onLine !== false; }

function registerServiceWorker() {
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* the app works without it */ });
  }
}

boot();
