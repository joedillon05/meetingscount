// Create your own profile and pay for it. Four short screens: who you are
// and what the order says, the code we texted, payment on Stripe's page,
// done. Someone who is already signed in lands on whichever screen is next.
import { $, esc, icon, makeApi, toast } from './lib.js';

const api = makeApi({ get: () => '' });          // no dev identity on this page
const app = $('#app');
const S = { ctx: {}, me: null };

function logo() {
  return `<svg class="logo" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="9" fill="var(--brand)"/>
    <path d="M9.5 16.5l4.2 4.2 8.8-9.4" fill="none" stroke="#fff" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`;
}
const head = () => `<div class="join-head">${logo()}<b>ActionsCount+</b></div>`;
const steps = n => `<div class="steps" aria-hidden="true">${[1, 2, 3].map(i => `<i class="${i <= n ? 'on' : ''}"></i>`).join('')}</div>`;

async function boot() {
  try { S.ctx = await api('/api/signup/context'); } catch { S.ctx = {}; }
  const q = new URLSearchParams(location.search);
  try { S.me = await api('/api/me'); } catch (e) { S.me = null; if (e.status && e.status !== 401) return fatal(e); }

  if (S.me && S.me.role !== 'participant') return staffHere();
  if (S.me) {
    if (q.get('paid') && q.get('session_id')) return confirmPayment(q.get('session_id'));
    return next({ canceled: !!q.get('canceled') });
  }
  if (!S.ctx.open) return closed();
  renderProfile();
}

/* Where a signed-in person goes: pay, or into the app. */
function next({ canceled = false, created = false } = {}) {
  const b = S.me.billing;
  if (!b) return allSet({ county: true });
  if (b.active) return allSet({ created });
  renderPay({ canceled, created });
}

/* ---------- 1. who you are ---------- */

function renderProfile() {
  const today = new Date(); today.setMinutes(today.getMinutes() - today.getTimezoneOffset());
  app.innerHTML = `<main class="join">
    ${head()}${steps(1)}
    <h1>Create your profile</h1>
    <p class="lead">For people whose court or program is not on ActionsCount+ yet. You pay for it yourself and keep your own verified record of every meeting.</p>
    <form id="profileForm" novalidate>
      <label for="name">Your name <span class="muted">as your officer knows it</span></label>
      <input id="name" name="name" autocomplete="name" maxlength="120" required>
      <label for="phone">Mobile number</label>
      <input id="phone" name="phone" type="tel" inputmode="tel" autocomplete="tel" placeholder="(734) 555-0101" required>
      <label for="email">Email <span class="muted">for receipts, optional</span></label>
      <input id="email" name="email" type="email" inputmode="email" autocomplete="email" maxlength="200">
      <label for="perWeek">Meetings a week your order requires</label>
      <select id="perWeek" name="per_week">${[1, 2, 3, 4, 5, 6, 7].map(n =>
        `<option value="${n}" ${n === 3 ? 'selected' : ''}>${n} a week</option>`).join('')}</select>
      <label for="starts">Counting from</label>
      <input id="starts" name="starts_on" type="date" value="${today.toISOString().slice(0, 10)}" required>
      <p class="err" id="err" role="alert" hidden></p>
      <button class="btn block big" type="submit">Text me a code</button>
    </form>
    <p class="fine">We text a six-digit code to prove the number is yours. Sign-in codes and confirmation replies come by text too. Reply STOP to any of them to stop.</p>
    <p class="fine center">Already have a profile? <a href="/">Sign in</a></p>
  </main>`;
  $('#name').focus();
  $('#profileForm').onsubmit = async e => {
    e.preventDefault();
    const f = e.target;
    const phone = f.phone.value.trim();
    if (f.name.value.trim().length < 2) return showError('Enter your name.');
    if (phone.replace(/\D/g, '').length < 10) return showError('Enter the full mobile number, including the area code.');
    const btn = f.querySelector('button');
    btn.disabled = true; btn.textContent = 'Sending';
    const body = { name: f.name.value.trim(), phone, email: f.email.value.trim() || null,
                   per_week: Number(f.per_week.value), starts_on: f.starts_on.value ? `${f.starts_on.value}T12:00:00Z` : null };
    try {
      const r = await api('/api/signup/start', { method: 'POST', body: JSON.stringify(body) });
      renderCode(phone, r);
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Text me a code';
      showError(err.message);
    }
  };
}

/* ---------- 2. the code ---------- */

function renderCode(phone, sent) {
  app.innerHTML = `<main class="join">
    ${head()}${steps(2)}
    <h1>Enter your code</h1>
    <p class="lead">${esc(sent.message)}</p>
    ${sent.demo_code ? `<p class="callout ok">${icon('note')} Demo: your code is <b class="code">${esc(sent.demo_code)}</b></p>` : ''}
    <form id="codeForm" novalidate>
      <label for="code">Six-digit code</label>
      <input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]*"
        maxlength="6" class="code-input" placeholder="000000" required>
      <p class="err" id="err" role="alert" hidden></p>
      <button class="btn block big" type="submit">Continue</button>
      <button class="btn block ghost" type="button" data-back>Go back</button>
    </form>
  </main>`;
  const input = $('#code');
  input.focus();
  input.addEventListener('input', () => {
    input.value = input.value.replace(/\D/g, '').slice(0, 6);
    if (input.value.length === 6) $('#codeForm').requestSubmit();
  });
  app.querySelector('[data-back]').onclick = renderProfile;
  $('#codeForm').onsubmit = async e => {
    e.preventDefault();
    const btn = e.target.querySelector('button');
    btn.disabled = true; btn.textContent = 'Checking';
    try {
      const r = await api('/api/signup/verify', { method: 'POST', body: JSON.stringify({ phone, code: input.value }) });
      S.me = await api('/api/me');
      if (!r.created) toast('That number already had a profile, so you are signed in to it.', { ms: 5000 });
      history.replaceState(null, '', '/join');
      next({ created: r.created });
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Continue';
      input.value = ''; input.focus();
      showError(err.message);
    }
  };
}

/* ---------- 3. payment ---------- */

function renderPay({ canceled = false, created = false } = {}) {
  const b = S.me.billing, price = b.price || S.ctx.price;
  const trial = b.trial || S.ctx.trial;
  app.innerHTML = `<main class="join">
    ${head()}${steps(3)}
    <h1>${created ? `Welcome, ${esc(firstName(S.me.name))}` : 'Set up payment'}</h1>
    <p class="lead">${canceled ? 'The payment was not finished, so nothing was charged. Pick up where you left off whenever you are ready.'
      : created ? 'Your profile exists. One more step and you can start checking in.'
      : esc(b.words)}</p>
    <div class="price">
      <b>${trial ? esc(trial) : price ? esc(price.label) : 'Subscription'}</b>
      ${trial ? `<span>Then ${price ? esc(price.label) : 'the subscription price'}. Nothing is charged until the free month ends.</span>` : ''}
      <span class="small">${icon('check')} Cancel any time from the app.</span>
      <span class="small">${icon('check')} Your card is entered on Stripe's secure page, never here.</span>
    </div>
    ${b.payment ? `<button class="btn block big" id="pay" type="button">${b.can_pay ? 'Continue to payment' : 'Manage billing'}</button>`
      : `<p class="callout warn">${icon('alert')} Payment is not set up on this server yet. Ask whoever runs it.</p>`}
    ${b.payment && b.can_pay ? `<details class="promo" id="promo">
      <summary>Have a code?</summary>
      <label for="code">Promotion code</label>
      <input id="code" name="code" autocomplete="off" autocapitalize="characters" maxlength="40" placeholder="For example FREEYEAR">
      <p class="fine">A code from ActionsCount+ is applied on the next page. With a code that covers the whole cost, no card is asked for.</p>
    </details>` : ''}
    <p class="err" id="err" role="alert" hidden></p>
    <button class="btn block ghost" type="button" data-signout>Not now, sign out</button>
  </main>`;
  const btn = $('#pay');
  if (btn) btn.onclick = () => go(btn, b.can_pay ? '/api/billing/checkout' : '/api/billing/portal');
  app.querySelector('[data-signout]').onclick = signOut;
}

/* Hand the person to a Stripe page. The link is minted for this profile only. */
async function go(btn, path) {
  btn.disabled = true; btn.textContent = 'Opening';
  const code = ($('#code')?.value || '').trim();
  try {
    const r = await api(path, { method: 'POST', body: JSON.stringify(code ? { code } : {}) });
    location.href = r.url;
  } catch (err) {
    btn.disabled = false; btn.textContent = path.endsWith('checkout') ? 'Continue to payment' : 'Manage billing';
    showError(err.message);
  }
}

/* Back from Stripe with a session id: record the result now, not when the webhook lands. */
async function confirmPayment(sessionId) {
  app.innerHTML = `<main class="join center">${head()}
    <div class="pulse">${icon('clock')}</div>
    <h1>Confirming your payment</h1><p class="lead">One moment.</p></main>`;
  try {
    const r = await api('/api/billing/sync', { method: 'POST', body: JSON.stringify({ session_id: sessionId }) });
    S.me.billing = r.subscription;
  } catch (err) {
    toast(err.message, { ms: 6000 });
    try { S.me = await api('/api/me'); } catch { /* keep what we have */ }
  }
  history.replaceState(null, '', '/join');
  next();
}

/* ---------- 4. done ---------- */

function allSet({ county = false, created = false } = {}) {
  const b = S.me.billing;
  app.innerHTML = `<main class="join center">${head()}
    <div class="hero-ic tone-ok">${icon('check')}</div>
    <h1>${created ? 'You are all set' : `Hi, ${esc(firstName(S.me.name))}`}</h1>
    <p class="lead">${county ? 'Your county covers ActionsCount+ for you, so there is nothing to pay.'
      : esc(b.words)}</p>
    <ul class="plain" style="text-align:left">
      <li>Open the app and read the short consent screen.</li>
      <li>Pick your town to see meetings near you.</li>
      <li>Tap check in when you arrive. One tap, no paper.</li>
    </ul>
    <a class="btn block big" href="/">Open ActionsCount+</a>
    <button class="btn block ghost" type="button" data-signout>Sign out</button>
  </main>`;
  app.querySelector('[data-signout]').onclick = signOut;
}

/* ---------- other states ---------- */

function closed() {
  app.innerHTML = `<main class="join">${head()}
    <h1>Not open here yet</h1>
    <p class="lead">Profiles on this ActionsCount+ are set up by your probation officer or program. Ask them to enrol you, and they will give you a sign-in code.</p>
    <a class="btn block" href="/">Go to sign-in</a>
  </main>`;
}

function staffHere() {
  app.innerHTML = `<main class="join center">${head()}
    <h1>You are signed in as staff</h1>
    <p class="lead">Profiles here are for participants. Officers enrol people from the dashboard.</p>
    <a class="btn block" href="/officer">Open the officer dashboard</a>
    <button class="btn block ghost" type="button" data-signout>Sign out</button>
  </main>`;
  app.querySelector('[data-signout]').onclick = signOut;
}

function fatal(e) {
  app.innerHTML = `<main class="join center">${icon('alert', 'lg')}
    <h1>ActionsCount+ could not load</h1><p class="lead">${esc(e.message)}</p>
    <button class="btn block" type="button" onclick="location.reload()">Try again</button></main>`;
}

async function signOut() {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* going anyway */ }
  location.href = '/join';
}

function showError(message) {
  const el = $('#err');
  if (el) { el.textContent = message; el.hidden = false; }
}

const firstName = s => String(s || '').trim().split(/\s+/)[0] || 'there';

boot();
