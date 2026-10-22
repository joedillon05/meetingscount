// Shared browser helpers: API calls, escaping, time formatting, geolocation.
// No framework and no build step, so a cheap phone loads this in one request.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ---------- identity ----------
   Dev auth sends X-MC-User. Each surface keeps its own key so signing in as a
   participant on the phone page never changes who you are on /officer. */
export function devIdentity(key) {
  return {
    get() { try { return localStorage.getItem(key) || ''; } catch { return ''; } },
    set(v) { try { localStorage.setItem(key, String(v)); } catch { /* private mode */ } },
    clear() { try { localStorage.removeItem(key); } catch { /* private mode */ } },
  };
}

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

/* Wraps fetch with the identity header and turns FastAPI's error bodies into
   one readable sentence. */
export function makeApi(identity) {
  return async function api(path, opts = {}) {
    const headers = { 'Content-Type': 'application/json', ...(opts.headers || {}) };
    const who = identity.get();
    if (who) headers['X-MC-User'] = who;
    let r;
    try {
      r = await fetch(path, { ...opts, headers });
    } catch {
      throw new ApiError(0, navigator.onLine === false
        ? 'You are offline. Connect to the internet and try again.'
        : 'Could not reach ActionsCount+. Check your connection and try again.');
    }
    if (!r.ok) {
      let msg = `Something went wrong (${r.status}).`;
      try {
        const body = await r.json();
        if (typeof body.detail === 'string') msg = body.detail;
        else if (Array.isArray(body.detail) && body.detail[0]?.msg) msg = body.detail[0].msg;
      } catch { /* not JSON */ }
      throw new ApiError(r.status, msg);
    }
    return r.status === 204 ? null : r.json();
  };
}

/* Download a file from the API. A plain link cannot carry the identity
   header, so fetch it, then hand the browser the bytes. */
export function makeDownload(identity) {
  return async function download(path, fallbackName = 'download') {
    const headers = {};
    if (identity.get()) headers['X-MC-User'] = identity.get();
    const r = await fetch(path, { headers });
    if (!r.ok) throw new ApiError(r.status, 'Could not prepare the file. Try again.');
    const name = (r.headers.get('content-disposition') || '').match(/filename="([^"]+)"/)?.[1] || fallbackName;
    const a = Object.assign(document.createElement('a'),
      { href: URL.createObjectURL(await r.blob()), download: name });
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  };
}

/* ---------- time ----------
   Everything shows in the county's timezone, because that is where the
   meetings are and where the week starts and ends. */
let TZ;
export function setTimeZone(tz) { TZ = tz; }

const fmt = (opts) => new Intl.DateTimeFormat([], { timeZone: TZ, ...opts });
export const time = d => fmt({ hour: 'numeric', minute: '2-digit' }).format(new Date(d));
export const day = d => fmt({ weekday: 'long', month: 'long', day: 'numeric' }).format(new Date(d));
export const shortDay = d => fmt({ weekday: 'short', month: 'short', day: 'numeric' }).format(new Date(d));
export const monthDay = d => fmt({ month: 'short', day: 'numeric' }).format(new Date(d));
export const weekday = d => fmt({ weekday: 'long' }).format(new Date(d));
export const longDate = d => fmt({ month: 'long', day: 'numeric', year: 'numeric' }).format(new Date(d));
export const dateTime = d => `${shortDay(d)}, ${time(d)}`;

/* "19:05" in the county's zone, for a <input type="time">. */
export function wallClock(d = new Date()) {
  const p = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hourCycle: 'h23', hour: '2-digit', minute: '2-digit' })
    .formatToParts(new Date(d));
  const get = t => p.find(x => x.type === t).value;
  return `${String(Number(get('hour')) % 24).padStart(2, '0')}:${get('minute')}`;
}

/* The instant that is `hhmm` on the calendar day `dateKey` (YYYY-MM-DD) in
   the county's zone. Two passes settle the offset across a DST change. */
export function fromZoned(dateKey, hhmm) {
  const [y, mo, d] = dateKey.split('-').map(Number);
  const [hh, mm] = hhmm.split(':').map(Number);
  const wall = Date.UTC(y, mo - 1, d, hh, mm);
  let utc = wall;
  for (let i = 0; i < 2; i++) utc = wall - zoneOffsetMs(utc);
  return new Date(utc);
}
function zoneOffsetMs(ms) {
  const p = new Intl.DateTimeFormat('en-US', { timeZone: TZ, hourCycle: 'h23', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(ms));
  const g = t => Number(p.find(x => x.type === t).value);
  return Date.UTC(g('year'), g('month') - 1, g('day'), g('hour') % 24, g('minute'), g('second')) - ms;
}

/* Calendar parts in the county's zone. Always en-US here, because these are
   keys for grouping, not text anyone reads. */
function parts(d) {
  const p = new Intl.DateTimeFormat('en-US', { timeZone: TZ, year: 'numeric', month: '2-digit',
    day: '2-digit', weekday: 'short' }).formatToParts(new Date(d));
  const get = t => p.find(x => x.type === t).value;
  return { y: +get('year'), m: +get('month'), d: +get('day'), wd: get('weekday') };
}
const iso = ms => new Date(ms).toISOString().slice(0, 10);

/* Calendar day in the county's zone, as YYYY-MM-DD. */
export function dayKey(d) {
  const { y, m, d: dd } = parts(d);
  return iso(Date.UTC(y, m - 1, dd));
}

/* The Monday that starts the week containing `d`, as YYYY-MM-DD. Weeks run
   Monday to Sunday, the same as the compliance maths on the server. */
export function weekKey(d) {
  const { y, m, d: dd, wd } = parts(d);
  const back = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(wd);
  return iso(Date.UTC(y, m - 1, dd - back));
}

export function weekLabel(key, now = new Date()) {
  if (key === weekKey(now)) return 'This week';
  if (key === weekKey(new Date(now.getTime() - 7 * 864e5))) return 'Last week';
  const start = new Date(key + 'T12:00:00Z'), end = new Date(start.getTime() + 6 * 864e5);
  const f = o => new Intl.DateTimeFormat([], { timeZone: 'UTC', ...o });
  const sameMonth = start.getUTCMonth() === end.getUTCMonth();
  return `${f({ month: 'short', day: 'numeric' }).format(start)} to ${
    sameMonth ? f({ day: 'numeric' }).format(end) : f({ month: 'short', day: 'numeric' }).format(end)}`;
}

export function relativeDay(d, now = new Date()) {
  const k = dayKey(d), today = dayKey(now);
  if (k === today) return 'Today';
  if (k === dayKey(new Date(now.getTime() + 864e5))) return 'Tomorrow';
  if (k === dayKey(new Date(now.getTime() - 864e5))) return 'Yesterday';
  return shortDay(d);
}

/* "just now", "12 min ago", "3 h ago", "2 days ago". */
export function ago(d, now = Date.now()) {
  if (!d) return 'never';
  const m = Math.round((now - new Date(d).getTime()) / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m} min ago`;
  if (m < 1440) return `${Math.round(m / 60)} h ago`;
  const days = Math.round(m / 1440);
  return days === 1 ? '1 day ago' : `${days} days ago`;
}

export function minutesUntil(d, now = Date.now()) {
  return Math.round((new Date(d).getTime() - now) / 60000);
}

export function duration(min) {
  min = Math.max(0, Math.round(min));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60), m = min % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

export function greeting(now = new Date()) {
  const h = Number(fmt({ hour: 'numeric', hourCycle: 'h23' }).format(now));
  return h < 12 ? 'Good morning' : h < 17 ? 'Good afternoon' : 'Good evening';
}

export const feet = ft => `${Math.round(ft).toLocaleString()} ft`;
export const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;

/* ---------- geolocation ----------
   The first fix indoors is often poor and gets better over a few seconds.
   Watch briefly, keep the best reading, and stop. Still one reading sent,
   still only while the person is looking at the screen. */
export function bestPosition({ goodEnoughM = 25, settleMs = 8000, hardTimeoutMs = 20000,
                               onProgress = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    if (!('geolocation' in navigator)) {
      reject(Object.assign(new Error('This browser cannot share a location.'), { code: 'unsupported' }));
      return;
    }
    let best = null, settleTimer = null, done = false;
    const finish = (fn, v) => {
      if (done) return;
      done = true;
      navigator.geolocation.clearWatch(id);
      clearTimeout(settleTimer); clearTimeout(hard);
      fn(v);
    };
    const id = navigator.geolocation.watchPosition(pos => {
      if (!best || pos.coords.accuracy < best.coords.accuracy) best = pos;
      onProgress(best.coords.accuracy);
      if (best.coords.accuracy <= goodEnoughM) finish(resolve, best);
      else if (!settleTimer) settleTimer = setTimeout(() => finish(resolve, best), settleMs);
    }, err => {
      if (best) finish(resolve, best);
      else finish(reject, err);
    }, { enableHighAccuracy: true, maximumAge: 0, timeout: hardTimeoutMs });
    const hard = setTimeout(() => best ? finish(resolve, best)
      : finish(reject, Object.assign(new Error('timeout'), { code: 3 })), hardTimeoutMs);
  });
}

export async function locationPermission() {
  try {
    const s = await navigator.permissions.query({ name: 'geolocation' });
    return s.state;                      // granted | prompt | denied
  } catch {
    return 'unknown';
  }
}

/* ---------- small UI pieces ---------- */
let toastTimer;
/* A short message at the bottom of the screen. With `action`, it carries one
   button (Undo) and stays up for `ms`. */
export function toast(msg, { action, onAction, ms = 2800 } = {}) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast'; el.className = 'toast'; el.setAttribute('role', 'status');
    document.body.appendChild(el);
  }
  el.innerHTML = `<span>${esc(msg)}</span>${action ? `<button type="button">${esc(action)}</button>` : ''}`;
  el.classList.toggle('has-action', !!action);
  if (action) {
    el.querySelector('button').onclick = () => { el.classList.remove('show'); clearTimeout(toastTimer); onAction?.(); };
  }
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

/* A bottom sheet on phones, a centred dialog on wider screens. Built on
   <dialog> so focus, Escape and the backdrop come from the browser. */
export function sheet(html, { onClose, label = 'Details', wide = false } = {}) {
  const d = document.createElement('dialog');
  d.className = 'sheet' + (wide ? ' wide' : '');
  d.setAttribute('aria-label', label);
  d.innerHTML = `<div class="sheet-grip" aria-hidden="true"></div>
    <button class="sheet-x" type="button" aria-label="Close" data-close>${icon('x')}</button>
    <div class="sheet-body">${html}</div>`;
  document.body.appendChild(d);
  const close = () => d.close();
  d.addEventListener('click', e => {
    if (e.target === d || e.target.closest('[data-close]')) close();
  });
  d.addEventListener('close', () => { d.remove(); onClose?.(); });
  d.showModal();
  return { el: d, body: d.querySelector('.sheet-body'), close,
           set(html2) { d.querySelector('.sheet-body').innerHTML = html2; } };
}

/* Inline icons, drawn with currentColor so they follow the text. */
const PATHS = {
  check: '<path d="M5 12.5l4.2 4.2L19 7"/>',
  alert: '<path d="M12 8v5"/><path d="M12 16.5v.5"/><circle cx="12" cy="12" r="9"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  pin: '<path d="M12 21s-6.5-5.6-6.5-11A6.5 6.5 0 0 1 18.5 10c0 5.4-6.5 11-6.5 11z"/><circle cx="12" cy="10" r="2.4"/>',
  shield: '<path d="M12 3l7 3v5.5c0 4.5-3 8-7 9.5-4-1.5-7-5-7-9.5V6z"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  trash: '<path d="M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13"/>',
  note: '<path d="M5 4h14v12l-4 4H5z"/><path d="M15 20v-4h4"/><path d="M8.5 9h7M8.5 12.5h5"/>',
  home: '<path d="M3.5 11L12 4l8.5 7"/><path d="M6 9.5V20h12V9.5"/>',
  list: '<path d="M8 6.5h12M8 12h12M8 17.5h12"/><circle cx="4" cy="6.5" r=".6"/><circle cx="4" cy="12" r=".6"/><circle cx="4" cy="17.5" r=".6"/>',
  user: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20c1.2-3.8 4-5.6 7.5-5.6s6.3 1.8 7.5 5.6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  arrow: '<path d="M9 5l7 7-7 7"/>',
  download: '<path d="M12 4v11M7 10.5l5 5 5-5M5 20h14"/>',
  flame: '<path d="M12 21c-3.9 0-6.5-2.6-6.5-6.2 0-3.4 2.5-5.3 3.6-8.3.3 1.6 1 2.7 2 3.3.2-2.9 1.6-5.4 3.9-6.8-.3 2.6.7 4.4 2 6 1 1.3 1.5 2.6 1.5 4.1 0 4.2-2.6 7.9-6.5 7.9z"/>',
  wifi: '<path d="M3 3l18 18"/><path d="M8.5 16.2a5 5 0 0 1 7 0M5.2 12.8a9.7 9.7 0 0 1 4-2.2M12 8.2c2.8 0 5.3 1 7.2 2.8M12 20h.01"/>',
  directions: '<path d="M12 2.8l9.2 9.2-9.2 9.2L2.8 12z"/><path d="M9.5 14v-2.5h5M12.5 9l2 2.5-2 2.5"/>',
  install: '<rect x="6" y="2.5" width="12" height="19" rx="2.5"/><path d="M12 7v7M9 11l3 3 3-3"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3"/><path d="M19.5 4.5v4h-4"/>',
  inbox: '<path d="M3.5 13.5l2.6-7.3A2 2 0 0 1 8 5h8a2 2 0 0 1 1.9 1.2l2.6 7.3V18a1.5 1.5 0 0 1-1.5 1.5H5A1.5 1.5 0 0 1 3.5 18z"/><path d="M3.5 13.5H8l1.5 2.5h5l1.5-2.5h4.5"/>',
  users: '<circle cx="9" cy="8.5" r="3.3"/><path d="M3 19.5c.9-3.3 3.2-5 6-5s5.1 1.7 6 5"/><path d="M15.5 5.6a3.2 3.2 0 0 1 0 6M17.5 14.8c1.8.6 3 2.2 3.5 4.7"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  scroll: '<path d="M7 4h11a2 2 0 0 1 2 2v2h-4M7 4a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8"/><path d="M9 9h5M9 12.5h6M9 16h4"/>',
  question: '<circle cx="12" cy="12" r="9"/><path d="M9.6 9.3a2.5 2.5 0 1 1 3.6 2.3c-.8.4-1.2 1-1.2 1.9v.3"/><path d="M12 17v.2"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6 10h.01M9.5 10h.01M13 10h.01M16.5 10h.01M7.5 14h9"/>',
  back: '<path d="M15 5l-7 7 7 7"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
  map: '<path d="M9 4.5l6 2.5 5.5-2.2v14.7L15 21.7l-6-2.5-5.5 2.2V6.7z"/><path d="M9 4.5v14.7M15 7v14.7"/>',
};
export function icon(name, cls = '') {
  return `<svg class="ic ${cls}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor"
    stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">${PATHS[name] || ''}</svg>`;
}
