// The page a sponsor or a meeting chair opens from a text. No sign-in: the
// token in the link is the key, and it works once.
//
// It says as little as it can. A first name, a meeting, a time, two buttons.
// Nothing about why the participant is keeping track.

import { $, esc, icon, setTimeZone, shortDay, time } from './lib.js';

const token = decodeURIComponent(location.pathname.split('/').pop() || '');
const app = $('#app');

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { 'Content-Type': 'application/json' } });
  if (!r.ok) {
    let msg = `Something went wrong (${r.status}).`;
    try { const b = await r.json(); if (typeof b.detail === 'string') msg = b.detail; } catch { /* not JSON */ }
    throw Object.assign(new Error(msg), { status: r.status });
  }
  return r.json();
}

function logo() {
  return `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="var(--brand)"/>
    <path d="M9.5 16.5l4.2 4.2 8.8-9.4" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}

const head = () => `<div class="brand">${logo()}<span>ActionsCount+</span></div>`;

async function boot() {
  let d;
  try {
    d = await api(`/api/confirm/${encodeURIComponent(token)}`);
  } catch (e) {
    return render(`${head()}<h1>This link does not work</h1>
      <p>${esc(e.message)}</p>
      <p class="fine">If someone asked you to confirm a meeting, use the link in their text exactly as it came.</p>`);
  }
  setTimeZone(d.timezone || 'America/Detroit');
  if (d.state === 'confirmed' || d.state === 'declined') return done(d, false);
  if (d.state === 'expired') {
    return render(`${head()}<h1>This link has expired</h1>
      <p>Links work for a week. ${esc(d.participant)} can send a new one from their app.</p>`);
  }
  question(d);
}

function question(d) {
  const m = d.meeting;
  render(`${head()}
    <h1>Hi ${esc(d.asked)}. Were they there?</h1>
    <p class="lead"><b>${esc(d.participant)}</b> says they were at <b>${esc(m.name)}</b>
      on ${esc(shortDay(m.starts_at))} at ${time(m.starts_at)}${m.address ? `, ${esc(m.address)}` : ''}.</p>
    <p class="muted small">They checked in at ${time(d.checked_in_at)}${d.checked_out_at ? ` and out at ${time(d.checked_out_at)}` : ''}.</p>
    <div class="answers">
      <button class="btn block big" data-answer="yes" type="button">${icon('check')} Yes, I saw them there</button>
      <button class="btn block secondary big" data-answer="no" type="button">I can't confirm that</button>
    </div>
    <label for="note">Anything to add <span class="muted">(optional)</span></label>
    <textarea id="note" rows="2" maxlength="400" placeholder="For example: they stayed the whole hour."></textarea>
    <p class="fine">You got a text because ${esc(d.participant)} entered your number. This page does not read your location.
      Reply STOP to any text from ActionsCount+ to stop them.</p>`);
  app.querySelectorAll('[data-answer]').forEach(b => {
    b.onclick = async () => {
      app.querySelectorAll('button').forEach(x => { x.disabled = true; });
      try {
        const r = await api(`/api/confirm/${encodeURIComponent(token)}`, {
          method: 'POST', body: JSON.stringify({ answer: b.dataset.answer, note: $('#note').value.trim() || null }) });
        done(r, true);
      } catch (e) {
        app.querySelectorAll('button').forEach(x => { x.disabled = false; });
        const err = $('#err') || app.appendChild(Object.assign(document.createElement('p'), { id: 'err', className: 'err' }));
        err.textContent = e.message;
      }
    };
  });
}

function done(d, justNow) {
  const yes = d.state === 'confirmed';
  render(`${head()}
    <div class="hero-ic tone-${yes ? 'ok' : 'neutral'}">${icon(yes ? 'check' : 'note')}</div>
    <h1>${justNow ? 'Thank you' : 'Already answered'}</h1>
    <p class="lead">${justNow
      ? `${esc(d.participant)} will get a text saying you ${yes ? 'confirmed it' : 'could not confirm it'}.`
      : `This one was answered ${d.answered_at ? `on ${esc(shortDay(d.answered_at))}` : 'already'}. There is nothing else to do.`}</p>
    <p class="fine">You can close this page.</p>`);
}

function render(html) {
  app.innerHTML = html;
}

boot();
