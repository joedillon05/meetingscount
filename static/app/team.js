// The founders' back office: tasks, the hours log, timesheets, expenses.
// Mounted inside the staff dashboard for superusers only. Every rule lives
// on the server (app/routers/team.py); this file draws and asks.

import { $, $$, ago, esc, icon, plural, toast } from './lib.js';

const T = {
  ctx: null, root: null, tab: 'tasks', overview: null,
  tasks: [], filter: 'open', scope: 'all',
  who: null, hours: [], sheets: [], expenses: [],
};

const TABS = [['tasks', 'Tasks'], ['hours', 'Hours'], ['money', 'Money']];
const STATUS = { open: ['Open', 'ok', 'check'], waiting: ['Waiting', 'warn', 'clock'], done: ['Done', 'neutral', 'check'] };
const SHEET = {
  submitted: ['Awaiting approval', 'warn', 'clock'], approved: ['Approved, unpaid', 'ok', 'check'],
  paid: ['Paid', 'neutral', 'check'], returned: ['Sent back', 'bad', 'x'],
};

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const money = c => usd.format((c || 0) / 100);
const hrs = m => `${+((m || 0) / 60).toFixed(2)} h`;
const first = name => (name || '').split(' ')[0];
const badge = ([label, tone, ic]) => `<span class="badge tone-${tone} sm">${icon(ic)} ${label}</span>`;
const me = () => T.overview.people.find(p => p.is_you) || { balance: {} };
const api = (...a) => T.ctx.api(...a);

/* Calendar dates ("2026-10-22") are days, not instants: shown as typed and
   never shifted through a time zone. */
const asDate = s => new Date(`${s}T12:00:00`);
const dayText = s => !s ? '' : new Intl.DateTimeFormat([], { month: 'short', day: 'numeric',
  year: asDate(s).getFullYear() === new Date().getFullYear() ? undefined : 'numeric' }).format(asDate(s));
const todayKey = () => { const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const isPast = s => !!s && s < todayKey();
const toCents = v => Math.round(Number(String(v).replace(/[$,\s]/g, '')) * 100);
const toMinutes = v => Math.round(Number(v) * 60);
const idOrNull = v => v ? Number(v) : null;
const showErr = (form, msg) => { const el = form.querySelector('.err'); el.textContent = msg; el.hidden = false; };

export function mountTeam(ctx, root, { tab = 'tasks' } = {}) {
  T.ctx = ctx; T.root = root;
  T.tab = TABS.some(([k]) => k === tab) ? tab : 'tasks';
  T.who = T.who || ctx.me.id;
  draw();
}

export function unmountTeam() { T.root = null; }

/* ---------- the frame: tiles and tabs ---------- */

async function draw() {
  const v = T.root;
  if (!v) return;
  v.innerHTML = '<div class="sk sk-title"></div><div class="card sk-card"></div>';
  try { T.overview = await api('/api/team/overview'); } catch (e) { return failed(v, e); }
  if (!T.root) return;
  const o = T.overview, b = me().balance;
  v.innerHTML = `
    <div class="page-head"><div><h1>Team</h1>
      <p class="muted">Tasks, hours and money for the people who run ActionsCount+. No county can see this page.</p></div></div>
    <div class="tiles">
      ${tile(o.my_open_tasks, 'open tasks for you', '', '#team/tasks')}
      ${tile(o.open_tasks, 'open tasks in all', '', '#team/tasks')}
      ${tile(hrs(b.unsubmitted_minutes), 'not yet on a timesheet', '', '#team/hours')}
      ${tile(money(b.owed_cents), 'owed to you now', b.owed_cents ? 'ok' : '', '#team/money')}
      ${tile(o.awaiting_me, 'timesheets waiting on you', o.awaiting_me ? 'warn' : '', '#team/money')}
    </div>
    <div class="tabbar">${TABS.map(([k, l]) => `<a href="#team/${k}" class="${T.tab === k ? 'on' : ''}">${l}</a>`).join('')}</div>
    <div id="teamBody" class="stack"></div>`;
  ({ tasks: drawTasks, hours: drawHours, money: drawMoney })[T.tab]();
}

function tile(value, label, tone = '', href = '') {
  const inner = `<span class="tv">${value ?? 'n/a'}</span><span class="tk">${label}</span>`;
  return href ? `<a class="tile ${tone}" href="${href}">${inner}</a>` : `<div class="tile ${tone}">${inner}</div>`;
}

function failed(el, e) {
  el.innerHTML = `<div class="card empty">${icon('alert', 'lg')}<p>${esc(e.message)}</p>
    <button class="btn" type="button" data-retry>Try again</button></div>`;
  el.querySelector('[data-retry]').onclick = draw;
}

/* ---------- tasks ---------- */

async function drawTasks() {
  const b = $('#teamBody');
  try { T.tasks = await api('/api/team/tasks'); } catch (e) { return failed(b, e); }
  b.innerHTML = `<section class="card flush">
    <div class="toolbar">
      <div class="seg" id="tfilter">${[['open', 'Open'], ['waiting', 'Waiting'], ['done', 'Done'], ['', 'All']].map(([k, l]) =>
        `<button data-f="${k}" class="${T.filter === k ? 'on' : ''}" type="button">${l}</button>`).join('')}</div>
      <div class="seg" id="tscope">
        <button data-s="all" class="${T.scope === 'all' ? 'on' : ''}" type="button">Everyone</button>
        <button data-s="mine" class="${T.scope === 'mine' ? 'on' : ''}" type="button">Mine</button></div>
      <span class="spacer"></span><span class="muted small" id="tcount"></span>
      <button class="btn small-btn" data-add type="button">${icon('plus')} Add task</button>
    </div>
    <div class="table-wrap"><table class="grid">
      <thead><tr><th>Task</th><th>For</th><th>Due</th><th>Status</th><th></th></tr></thead>
      <tbody id="trows"></tbody></table></div>
  </section>`;
  renderTasks();
  $('#tfilter').onclick = e => { const x = e.target.closest('[data-f]'); if (!x) return;
    T.filter = x.dataset.f; $$('#tfilter button').forEach(y => y.classList.toggle('on', y === x)); renderTasks(); };
  $('#tscope').onclick = e => { const x = e.target.closest('[data-s]'); if (!x) return;
    T.scope = x.dataset.s; $$('#tscope button').forEach(y => y.classList.toggle('on', y === x)); renderTasks(); };
  b.querySelector('[data-add]').onclick = () => taskSheet();
  $('#trows').onclick = e => {
    const done = e.target.closest('[data-done]');
    if (done) return finishTask(Number(done.dataset.done));
    const tr = e.target.closest('tr[data-id]');
    if (tr) taskSheet(T.tasks.find(t => t.id === Number(tr.dataset.id)));
  };
  $('#trows').onkeydown = e => { const tr = e.target.closest('tr[data-id]');
    if (tr && e.key === 'Enter') taskSheet(T.tasks.find(t => t.id === Number(tr.dataset.id))); };
}

function renderTasks() {
  const rows = T.tasks.filter(t => (!T.filter || t.status === T.filter) &&
    (T.scope === 'all' || t.assignee_id === T.ctx.me.id));
  $('#tcount').textContent = plural(rows.length, 'task');
  $('#trows').innerHTML = rows.map(t => {
    const last = t.notes[t.notes.length - 1];
    return `<tr data-id="${t.id}" tabindex="0" class="${t.status === 'done' ? 'off' : ''}">
      <td><b>${esc(t.title)}</b>${t.county ? ` <span class="tag">${esc(t.county)}</span>` : ''}
        ${last ? `<div class="muted small">${plural(t.notes.length, 'note')}, last from ${esc(first(last.author))} ${esc(ago(last.at))}</div>` : ''}</td>
      <td>${t.assignee ? esc(first(t.assignee)) : '<span class="muted">anyone</span>'}</td>
      <td class="nowrap ${t.status !== 'done' && isPast(t.due_on) ? 'overdue' : ''}">${dayText(t.due_on)}</td>
      <td>${badge(STATUS[t.status])}</td>
      <td class="right">${t.status === 'done' ? '' : `<button class="btn ghost sm-btn" data-done="${t.id}" type="button">Done</button>`}</td>
    </tr>`;
  }).join('') || `<tr><td colspan="5" class="empty-row">${T.filter === 'open' ? 'Nothing open.' : 'Nothing here.'}</td></tr>`;
}

async function finishTask(id) {
  try {
    await api(`/api/team/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'done' }) });
  } catch (e) { return toast(e.message); }
  toast('Done.', { action: 'Undo', onAction: async () => {
    try { await api(`/api/team/tasks/${id}`, { method: 'PATCH', body: JSON.stringify({ status: 'open' }) }); } catch { /* it stays done */ }
    draw();
  } });
  draw();
}

function taskSheet(t = null) {
  const o = T.overview;
  const opt = (rows, sel) => rows.map(r =>
    `<option value="${r.id}" ${r.id === sel ? 'selected' : ''}>${esc(r.name)}</option>`).join('');
  const sh = T.ctx.sheet(`<form class="step left" id="taskForm">
    <h2>${t ? 'Task' : 'Add a task'}</h2>
    <label>What needs doing<input name="title" required maxlength="200" value="${esc(t?.title || '')}"
      placeholder="Call Washtenaw back about the pilot"></label>
    <label>Details<textarea name="details" maxlength="5000" rows="3"
      placeholder="Whatever the other of you would need to pick this up">${esc(t?.details || '')}</textarea></label>
    <div class="two">
      <label>For<select name="assignee_id"><option value="">Anyone</option>${opt(o.people, t?.assignee_id)}</select></label>
      <label>Due<input name="due_on" type="date" value="${esc(t?.due_on || '')}"></label>
    </div>
    <label>County, if it is about one<select name="county_id"><option value="">None</option>${opt(o.counties, t?.county_id)}</select></label>
    ${t ? `<label>Status<select name="status">${['open', 'waiting', 'done'].map(s =>
      `<option value="${s}" ${t.status === s ? 'selected' : ''}>${STATUS[s][0]}</option>`).join('')}</select></label>` : ''}
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">${t ? 'Save' : 'Add task'}</button>
    ${t ? `<div class="notes"><h3>Notes</h3>
      ${t.notes.length ? `<ul class="tl">${t.notes.map(n => `<li class="t-note">
        <span class="t-when">${esc(first(n.author))} · ${esc(ago(n.at))}</span><p>${esc(n.body)}</p></li>`).join('')}</ul>`
        : '<p class="muted small">No notes yet.</p>'}
      <div class="note-form"><label>Add a note<textarea name="note" rows="2" maxlength="5000" placeholder="Left a voicemail"></textarea></label>
        <button class="btn secondary small-btn" data-note type="button">Add note</button></div>
      <p class="fine">Added by ${esc(t.created_by)} ${esc(ago(t.created_at))}${t.done_at ? `, done ${esc(ago(t.done_at))}` : ''}</p>
    </div>` : ''}
  </form>`, { label: t ? 'Task' : 'Add a task' });
  const form = sh.body.querySelector('#taskForm');
  form.title.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    const body = { title: form.title.value.trim(), details: form.details.value.trim() || null,
      assignee_id: idOrNull(form.assignee_id.value), county_id: idOrNull(form.county_id.value),
      due_on: form.due_on.value || null };
    if (t) body.status = form.status.value;
    try {
      await api(t ? `/api/team/tasks/${t.id}` : '/api/team/tasks',
        { method: t ? 'PATCH' : 'POST', body: JSON.stringify(body) });
      sh.close(); toast(t ? 'Saved.' : 'Task added.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
  const noteBtn = form.querySelector('[data-note]');
  if (noteBtn) noteBtn.onclick = async () => {
    const text = form.note.value.trim();
    if (!text) return form.note.focus();
    try {
      const fresh = await api(`/api/team/tasks/${t.id}/notes`, { method: 'POST', body: JSON.stringify({ body: text }) });
      sh.close(); taskSheet(fresh);
      T.tasks = T.tasks.map(x => x.id === fresh.id ? fresh : x); renderTasks();
    } catch (err) { showErr(form, err.message); }
  };
}

/* ---------- the hours log ---------- */

async function drawHours() {
  const b = $('#teamBody');
  const people = T.overview.people;
  const mine = T.who === T.ctx.me.id;
  try {
    [T.hours, T.tasks] = await Promise.all([api(`/api/team/hours?user_id=${T.who}`), api('/api/team/tasks')]);
  } catch (e) { return failed(b, e); }
  const person = people.find(p => p.id === T.who) || me();
  const bal = person.balance;
  const free = T.hours.filter(h => h.editable);
  const taskOpts = T.tasks.filter(t => t.status !== 'done').map(t =>
    `<option value="${t.id}">${esc(t.title)}</option>`).join('');
  b.innerHTML = `
    ${people.length > 1 ? `<div class="seg" id="hwho">${people.map(p =>
      `<button data-w="${p.id}" class="${p.id === T.who ? 'on' : ''}" type="button">${p.is_you ? 'You' : esc(first(p.name))}</button>`).join('')}</div>` : ''}
    ${mine ? `<section class="card"><form class="logform" id="logForm">
        <label>Day<input name="worked_on" type="date" value="${todayKey()}" max="${todayKey()}" required></label>
        <label>Hours<input name="hours" type="number" step="0.25" min="0.25" max="24" placeholder="2.5" required></label>
        <label class="grow">What you did<input name="note" maxlength="300" placeholder="Cook mode: the timers" required></label>
        <label>Task<select name="task_id"><option value="">None</option>${taskOpts}</select></label>
        <button class="btn" type="submit">${icon('plus')} Log it</button>
      </form><p class="err" role="alert" hidden></p></section>` : ''}
    <section class="card flush">
      <div class="toolbar"><b>${mine ? 'Your hours' : `${esc(first(person.name))}'s hours`}</b>
        <span class="muted small">${hrs(bal.unsubmitted_minutes)} not yet on a timesheet${
          person.rate ? '' : '. No hourly rate is set yet, so nothing has a value'}${
          person.rate && bal.unpriced_minutes ? `. ${hrs(bal.unpriced_minutes)} of it is from before the rate started` : ''}</span>
        <span class="spacer"></span>
        ${mine && free.length ? `<button class="btn secondary small-btn" data-submit type="button">${icon('file')} Submit a timesheet</button>` : ''}
      </div>
      <div class="table-wrap"><table class="grid">
        <thead><tr><th>Day</th><th>Hours</th><th>What</th><th>Task</th><th>Worth</th><th>Timesheet</th><th></th></tr></thead>
        <tbody>${T.hours.map(h => `<tr>
          <td class="nowrap">${dayText(h.worked_on)}</td><td class="num">${hrs(h.minutes)}</td><td>${esc(h.note)}</td>
          <td class="muted small">${esc(h.task || '')}</td>
          <td class="num">${h.cents == null ? '<span class="muted">no rate</span>' : money(h.cents)}</td>
          <td>${h.timesheet_status ? badge(SHEET[h.timesheet_status]) : '<span class="muted small">not yet</span>'}</td>
          <td class="right">${mine && h.editable ? `<button class="btn ghost sm-btn" data-edit="${h.id}" type="button">Edit</button>` : ''}</td>
        </tr>`).join('') || `<tr><td colspan="7" class="empty-row">${mine ? 'Nothing logged yet.' : 'Nothing logged in the last few months.'}</td></tr>`}
        </tbody></table></div>
    </section>`;
  const who = $('#hwho');
  if (who) who.onclick = e => { const x = e.target.closest('[data-w]'); if (!x) return; T.who = Number(x.dataset.w); drawHours(); };
  const form = $('#logForm');
  if (form) {
    form.hours.focus();
    form.onsubmit = async e => {
      e.preventDefault();
      const body = { worked_on: form.worked_on.value, minutes: toMinutes(form.hours.value),
        note: form.note.value.trim(), task_id: idOrNull(form.task_id.value) };
      if (!body.minutes) return showErr(form.parentElement, 'How long? A quarter hour is the smallest entry.');
      try {
        await api('/api/team/hours', { method: 'POST', body: JSON.stringify(body) });
        toast('Logged.'); draw();
      } catch (err) { showErr(form.parentElement, err.message); }
    };
  }
  b.querySelectorAll('[data-edit]').forEach(x => x.onclick = () =>
    hoursSheet(T.hours.find(h => h.id === Number(x.dataset.edit)), taskOpts));
  const submit = b.querySelector('[data-submit]');
  if (submit) submit.onclick = () => timesheetSheet(free, person);
}

function hoursSheet(h, taskOpts) {
  const sh = T.ctx.sheet(`<form class="step left" id="hForm">
    <h2>Edit this entry</h2>
    <div class="two">
      <label>Day<input name="worked_on" type="date" value="${esc(h.worked_on)}" max="${todayKey()}" required></label>
      <label>Hours<input name="hours" type="number" step="0.25" min="0.25" max="24" value="${+(h.minutes / 60).toFixed(2)}" required></label>
    </div>
    <label>What you did<input name="note" maxlength="300" value="${esc(h.note)}" required></label>
    <label>Task<select name="task_id"><option value="">None</option>${taskOpts}</select></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Save</button>
    <button class="btn block ghost danger-link" data-del type="button">Delete this entry</button>
  </form>`, { label: 'Edit hours' });
  const form = sh.body.querySelector('#hForm');
  if (h.task_id) form.task_id.value = String(h.task_id);
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api(`/api/team/hours/${h.id}`, { method: 'PATCH', body: JSON.stringify({
        worked_on: form.worked_on.value, minutes: toMinutes(form.hours.value),
        note: form.note.value.trim(), task_id: idOrNull(form.task_id.value) }) });
      sh.close(); toast('Saved.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
  form.querySelector('[data-del]').onclick = async () => {
    try { await api(`/api/team/hours/${h.id}`, { method: 'DELETE' }); sh.close(); toast('Deleted.'); draw(); }
    catch (err) { showErr(form, err.message); }
  };
}

function timesheetSheet(free, person) {
  const days = free.map(h => h.worked_on).sort();
  const other = T.overview.people.find(p => !p.is_you);
  const sh = T.ctx.sheet(`<form class="step left" id="tsForm">
    <h2>Submit a timesheet</h2>
    <p class="muted">Your unsubmitted hours in this range go on it, priced at your rate on each day.
      ${other ? `${esc(first(other.name))} approves it and marks it paid.` : ''}</p>
    <div class="two">
      <label>From<input name="starts_on" type="date" value="${days[0]}" required></label>
      <label>To<input name="ends_on" type="date" value="${days[days.length - 1]}" required></label>
    </div>
    <p class="callout" id="tsPreview"></p>
    <label>Note<input name="note" maxlength="400" placeholder="Optional. What this stretch was mostly about"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Submit</button>
  </form>`, { label: 'Submit a timesheet' });
  const form = sh.body.querySelector('#tsForm');
  const preview = () => {
    const inRange = free.filter(h => h.worked_on >= form.starts_on.value && h.worked_on <= form.ends_on.value);
    const mins = inRange.reduce((n, h) => n + h.minutes, 0);
    const priced = inRange.every(h => h.cents != null);
    const cents = inRange.reduce((n, h) => n + (h.cents || 0), 0);
    $('#tsPreview', form).innerHTML = inRange.length
      ? `${icon('clock')} <span>${plural(inRange.length, 'entry', 'entries')}, ${hrs(mins)}, ${priced ? money(cents) : 'some days have no rate yet'}</span>`
      : `${icon('alert')} <span>No unsubmitted hours between those dates.</span>`;
  };
  preview();
  form.starts_on.onchange = form.ends_on.onchange = preview;
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      const made = await api('/api/team/timesheets', { method: 'POST', body: JSON.stringify({
        starts_on: form.starts_on.value, ends_on: form.ends_on.value, note: form.note.value.trim() || null }) });
      sh.close(); toast(`Timesheet submitted: ${hrs(made.minutes)}, ${money(made.amount_cents)}.`);
      location.hash = 'team/money';
    } catch (err) { showErr(form, err.message); }
  };
}

/* ---------- money ---------- */

async function drawMoney() {
  const b = $('#teamBody');
  try {
    [T.sheets, T.expenses] = await Promise.all([api('/api/team/timesheets'), api('/api/team/expenses')]);
  } catch (e) { return failed(b, e); }
  const myId = T.ctx.me.id;
  const owedExp = T.expenses.filter(e => e.owed).reduce((n, e) => n + e.amount_cents, 0);
  b.innerHTML = `
    <div class="people">${T.overview.people.map(personCard).join('')}</div>
    <section class="card flush">
      <div class="toolbar"><b>Timesheets</b>
        <span class="muted small">Attach the invoice you sent for yours. Approve or send back the other founder's, pay from the link, then mark it paid.</span></div>
      <div class="table-wrap"><table class="grid">
        <thead><tr><th>Who</th><th>Period</th><th>Hours</th><th>Amount</th><th>Status</th><th></th></tr></thead>
        <tbody>${T.sheets.map(s => `<tr class="${s.status === 'returned' ? 'off' : ''}">
          <td><b>${esc(first(s.person))}</b></td>
          <td class="nowrap">${dayText(s.starts_on)} to ${dayText(s.ends_on)}${s.note ? `<div class="muted small">${esc(s.note)}</div>` : ''}</td>
          <td class="num">${hrs(s.minutes)}</td>
          <td class="num">${money(s.amount_cents)}${s.status === 'paid' && s.paid_cents !== s.amount_cents ? `<div class="muted small">paid ${money(s.paid_cents)}</div>` : ''}${
            s.invoice_ref || s.invoice_url ? `<div class="muted small">${invoiceLabel(s)}</div>` : ''}</td>
          <td>${badge(SHEET[s.status])}<div class="muted small">${sheetWhen(s)}</div></td>
          <td class="right nowrap">
            <button class="btn ghost sm-btn" data-view="${s.id}" type="button">Entries</button>
            ${s.user_id === myId && (s.status === 'submitted' || s.status === 'approved') ? `<button class="btn ghost sm-btn" data-invoice="${s.id}" type="button">${s.invoice_ref || s.invoice_url ? 'Edit invoice' : 'Add invoice'}</button>` : ''}
            ${s.user_id !== myId && s.status === 'submitted' ? `<button class="btn secondary sm-btn" data-approve="${s.id}" type="button">Approve</button>` : ''}
            ${s.user_id !== myId && s.status === 'approved' && s.invoice_url ? `<a class="btn secondary sm-btn" href="${esc(s.invoice_url)}" target="_blank" rel="noopener">Pay invoice${s.invoice_ref ? ` ${esc(s.invoice_ref)}` : ''}</a>` : ''}
            ${s.user_id !== myId && s.status === 'approved' ? `<button class="btn sm-btn" data-pay="${s.id}" type="button">Mark paid</button>` : ''}
            ${s.user_id !== myId && (s.status === 'submitted' || s.status === 'approved') ? `<button class="btn ghost sm-btn" data-return="${s.id}" type="button">Send back</button>` : ''}
          </td></tr>`).join('') || '<tr><td colspan="6" class="empty-row">No timesheets yet. They are submitted from the Hours tab.</td></tr>'}
        </tbody></table></div>
    </section>
    <section class="card flush">
      <div class="toolbar"><b>Expenses</b>
        <span class="muted small">${owedExp ? `${money(owedExp)} paid personally and not yet paid back` : 'Hosting, texting, the domain: whatever the business cost'}</span>
        <span class="spacer"></span>
        <button class="btn small-btn" data-addexp type="button">${icon('plus')} Add expense</button></div>
      <div class="table-wrap"><table class="grid">
        <thead><tr><th>Day</th><th>Vendor</th><th>Amount</th><th>Paid by</th><th>Status</th><th></th></tr></thead>
        <tbody>${T.expenses.map(e => `<tr>
          <td class="nowrap">${dayText(e.spent_on)}</td>
          <td><b>${esc(e.vendor)}</b>${e.note ? `<div class="muted small">${esc(e.note)}</div>` : ''}</td>
          <td class="num">${money(e.amount_cents)}</td>
          <td>${e.paid_by ? esc(first(e.paid_by)) : '<span class="muted">the business</span>'}</td>
          <td>${e.owed ? badge(['To pay back', 'warn', 'clock'])
            : e.reimbursed_at ? `${badge(['Paid back', 'neutral', 'check'])}<div class="muted small">by ${esc(first(e.reimbursed_by))} ${esc(ago(e.reimbursed_at))}${e.reimbursed_note ? `, ${esc(e.reimbursed_note)}` : ''}</div>`
            : '<span class="muted small">nothing owed</span>'}</td>
          <td class="right nowrap">
            ${e.owed && e.paid_by_id !== myId ? `<button class="btn secondary sm-btn" data-reimburse="${e.id}" type="button">Mark paid back</button>` : ''}
            ${e.reimbursed_at ? '' : `<button class="btn ghost sm-btn" data-editexp="${e.id}" type="button">Edit</button>`}
          </td></tr>`).join('') || '<tr><td colspan="6" class="empty-row">No expenses logged.</td></tr>'}
        </tbody></table></div>
    </section>`;
  b.querySelectorAll('[data-rate]').forEach(x => x.onclick = () =>
    rateSheet(T.overview.people.find(p => p.id === Number(x.dataset.rate))));
  b.querySelectorAll('[data-view]').forEach(x => x.onclick = () => entriesSheet(Number(x.dataset.view)));
  b.querySelectorAll('[data-approve]').forEach(x => x.onclick = () => decideSheet(sheetById(x.dataset.approve), 'approve'));
  b.querySelectorAll('[data-return]').forEach(x => x.onclick = () => decideSheet(sheetById(x.dataset.return), 'return'));
  b.querySelectorAll('[data-pay]').forEach(x => x.onclick = () => paySheet(sheetById(x.dataset.pay)));
  b.querySelectorAll('[data-invoice]').forEach(x => x.onclick = () => invoiceSheet(sheetById(x.dataset.invoice)));
  b.querySelector('[data-addexp]').onclick = () => expenseSheet();
  b.querySelectorAll('[data-editexp]').forEach(x => x.onclick = () =>
    expenseSheet(T.expenses.find(e => e.id === Number(x.dataset.editexp))));
  b.querySelectorAll('[data-reimburse]').forEach(x => x.onclick = () =>
    reimburseSheet(T.expenses.find(e => e.id === Number(x.dataset.reimburse))));
}

const sheetById = id => T.sheets.find(s => s.id === Number(id));

/* "Invoice 1042", linked when there is somewhere to pay it. */
function invoiceLabel(s) {
  const text = s.invoice_ref ? `Invoice ${esc(s.invoice_ref)}` : 'Invoice';
  return s.invoice_url ? `<a href="${esc(s.invoice_url)}" target="_blank" rel="noopener">${text}</a>` : text;
}

function invoiceSheet(s) {
  const has = s.invoice_ref || s.invoice_url;
  const sh = T.ctx.sheet(`<form class="step left" id="invForm">
    <h2>${has ? 'The invoice for this timesheet' : 'Attach the invoice'}</h2>
    <p class="muted">${dayText(s.starts_on)} to ${dayText(s.ends_on)}, ${money(s.amount_cents)}. Send the invoice from Found as usual,
      then put its number and payment link here. ${esc(first(T.overview.people.find(p => !p.is_you)?.name || 'The other founder'))} pays from the link and marks the sheet paid.</p>
    <label>Invoice number<input name="invoice_ref" maxlength="60" value="${esc(s.invoice_ref || '')}" placeholder="1042"></label>
    <label>Payment link<input name="invoice_url" type="url" maxlength="500" value="${esc(s.invoice_url || '')}" placeholder="https://"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Save</button>
    ${has ? '<button class="btn block ghost danger-link" data-remove type="button">Remove the invoice</button>' : ''}
  </form>`, { label: 'Invoice' });
  const form = sh.body.querySelector('#invForm');
  form.invoice_ref.focus();
  const send = async body => {
    try {
      await api(`/api/team/timesheets/${s.id}/invoice`, { method: 'POST', body: JSON.stringify(body) });
      sh.close(); toast(body.invoice_ref || body.invoice_url ? 'Invoice attached.' : 'Invoice removed.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
  form.onsubmit = e => {
    e.preventDefault();
    const ref = form.invoice_ref.value.trim(), url = form.invoice_url.value.trim();
    if (!ref && !url) return showErr(form, 'A number, a link, or both.');
    send({ invoice_ref: ref || null, invoice_url: url || null });
  };
  const remove = form.querySelector('[data-remove]');
  if (remove) remove.onclick = () => send({ invoice_ref: null, invoice_url: null });
}

function sheetWhen(s) {
  if (s.status === 'paid') return `${esc(first(s.paid_by))} on ${dayText(s.paid_on)}${s.paid_note ? `, ${esc(s.paid_note)}` : ''}`;
  if (s.status === 'returned') return `${esc(first(s.decided_by))}: ${esc(s.decision_note || '')}`;
  if (s.status === 'approved') return `by ${esc(first(s.decided_by))} ${esc(ago(s.decided_at))}`;
  return `submitted ${esc(ago(s.submitted_at))}`;
}

function personCard(p) {
  const b = p.balance;
  return `<section class="card person">
    <div class="row between">
      <h2 class="h2 flush">${esc(p.name)}${p.is_you ? ' <span class="muted small">you</span>' : ''}</h2>
      <button class="btn ghost sm-btn" data-rate="${p.id}" type="button">${p.rate ? `${money(p.rate.cents_per_hour)} an hour` : 'Set an hourly rate'}</button>
    </div>
    <div class="owed"><span class="tv ${b.owed_cents ? 'ok' : ''}">${money(b.owed_cents)}</span><span class="tk">owed to ${p.is_you ? 'you' : esc(first(p.name))} now</span></div>
    <dl class="kv bal">
      <dt>Not yet submitted</dt><dd>${hrs(b.unsubmitted_minutes)}, ${money(b.unsubmitted_cents)}${b.unpriced_minutes ? ` <span class="warn">(${hrs(b.unpriced_minutes)} with no rate)</span>` : ''}</dd>
      <dt>Awaiting approval</dt><dd>${money(b.submitted_cents)}</dd>
      <dt>Approved, unpaid</dt><dd>${money(b.approved_cents)}</dd>
      <dt>Expenses to pay back</dt><dd>${money(b.expenses_owed_cents)}</dd>
      <dt>Paid to date</dt><dd>${money(b.paid_cents)} for ${hrs(b.paid_minutes)}</dd>
      <dt>Earned to date</dt><dd>${money(b.earned_cents)}</dd>
    </dl>
  </section>`;
}

async function rateSheet(p) {
  let history = [];
  try { history = await api(`/api/team/rates?user_id=${p.id}`); } catch { /* the form still works */ }
  const sh = T.ctx.sheet(`<form class="step left" id="rateForm">
    <h2>${p.is_you ? 'Your' : `${esc(first(p.name))}'s`} hourly rate</h2>
    <p class="muted">A new rate starts on a date and runs until the next one. Hours already on a timesheet keep the amount they were submitted at.</p>
    <div class="two">
      <label>Dollars an hour<input name="rate" inputmode="decimal" placeholder="50" value="${p.rate ? (p.rate.cents_per_hour / 100) : ''}" required></label>
      <label>From<input name="effective_on" type="date" value="${todayKey()}" required></label>
    </div>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Save rate</button>
    ${history.length ? `<h3 class="sub">So far</h3><ul class="plain">${history.map(r =>
      `<li>${money(r.cents_per_hour)} an hour from ${dayText(r.effective_on)}<span class="muted"> · set by ${esc(first(r.set_by))}</span></li>`).join('')}</ul>` : ''}
  </form>`, { label: 'Hourly rate' });
  const form = sh.body.querySelector('#rateForm');
  form.rate.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    const cents = toCents(form.rate.value);
    if (!(cents >= 0)) return showErr(form, 'Enter the rate as a number of dollars.');
    try {
      await api('/api/team/rates', { method: 'POST', body: JSON.stringify({
        user_id: p.id, cents_per_hour: cents, effective_on: form.effective_on.value }) });
      sh.close(); toast('Rate saved.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
}

async function entriesSheet(id) {
  let s;
  try { s = await api(`/api/team/timesheets/${id}`); } catch (e) { return toast(e.message); }
  T.ctx.sheet(`<div class="step left">
    <h2>${esc(first(s.person))}, ${dayText(s.starts_on)} to ${dayText(s.ends_on)}</h2>
    <p class="muted">${hrs(s.minutes)} for ${money(s.amount_cents)}. ${badge(SHEET[s.status])}</p>
    ${s.note ? `<p class="callout">${icon('note')} <span>${esc(s.note)}</span></p>` : ''}
    ${s.invoice_ref || s.invoice_url ? `<p class="callout">${icon('file')} <span>${invoiceLabel(s)}</span></p>` : ''}
    ${s.decision_note && s.status === 'returned' ? `<p class="callout warn">${icon('alert')} <span>Sent back: ${esc(s.decision_note)}</span></p>` : ''}
    <ul class="tl">${s.hours.map(h => `<li class="t-ok"><span class="t-when">${dayText(h.worked_on)} · ${hrs(h.minutes)}</span>
      <p>${esc(h.note)}${h.task ? `<br><span class="muted small">${esc(h.task)}</span>` : ''}</p></li>`).join('')}</ul>
  </div>`, { label: 'Timesheet entries' });
}

function decideSheet(s, action) {
  const approving = action === 'approve';
  const sh = T.ctx.sheet(`<form class="step left" id="decForm">
    <h2>${approving ? 'Approve' : 'Send back'} ${esc(first(s.person))}'s timesheet?</h2>
    <p class="muted">${dayText(s.starts_on)} to ${dayText(s.ends_on)}: ${hrs(s.minutes)} for ${money(s.amount_cents)}.
      ${approving ? 'Approving says the company owes this. Paying it is a separate step.' : 'The hours go back to their log to fix and resubmit.'}</p>
    <label>${approving ? 'Note, if any' : 'What should change'}<input name="note" maxlength="400" ${approving ? '' : 'required'}
      placeholder="${approving ? 'Optional' : 'Half an hour for a typo?'}"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block ${approving ? '' : 'danger'}" type="submit">${approving ? `Approve ${money(s.amount_cents)}` : 'Send it back'}</button>
  </form>`, { label: approving ? 'Approve timesheet' : 'Send back timesheet' });
  const form = sh.body.querySelector('#decForm');
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api(`/api/team/timesheets/${s.id}/${action}`, { method: 'POST',
        body: JSON.stringify({ note: form.note.value.trim() || null }) });
      sh.close(); toast(approving ? 'Approved.' : 'Sent back.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
}

function paySheet(s) {
  const sh = T.ctx.sheet(`<form class="step left" id="payForm">
    <h2>Mark ${esc(first(s.person))}'s timesheet paid</h2>
    <p class="muted">This records that the money went. Nothing is moved from here.</p>
    ${s.invoice_url ? `<p class="callout">${icon('file')} <span>Not paid yet? <a href="${esc(s.invoice_url)}" target="_blank" rel="noopener">Open ${s.invoice_ref ? `invoice ${esc(s.invoice_ref)}` : 'the invoice'}</a> and pay it first.</span></p>` : ''}
    <div class="two">
      <label>Amount paid<input name="amount" inputmode="decimal" value="${(s.amount_cents / 100).toFixed(2)}" required></label>
      <label>On<input name="paid_on" type="date" value="${todayKey()}" required></label>
    </div>
    <label>How<input name="note" maxlength="200" value="${s.invoice_ref ? `Invoice ${esc(s.invoice_ref)}` : ''}" placeholder="Zelle, check 1042"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Mark paid</button>
  </form>`, { label: 'Mark paid' });
  const form = sh.body.querySelector('#payForm');
  form.onsubmit = async e => {
    e.preventDefault();
    const cents = toCents(form.amount.value);
    if (!(cents >= 0)) return showErr(form, 'Enter the amount as dollars.');
    try {
      await api(`/api/team/timesheets/${s.id}/pay`, { method: 'POST', body: JSON.stringify({
        paid_cents: cents, paid_on: form.paid_on.value, note: form.note.value.trim() || null }) });
      sh.close(); toast('Marked paid.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
}

function expenseSheet(ex = null) {
  const people = T.overview.people;
  const sh = T.ctx.sheet(`<form class="step left" id="expForm">
    <h2>${ex ? 'Expense' : 'Add an expense'}</h2>
    <div class="two">
      <label>Day<input name="spent_on" type="date" value="${esc(ex?.spent_on || todayKey())}" required></label>
      <label>Amount<input name="amount" inputmode="decimal" placeholder="12.34" value="${ex ? (ex.amount_cents / 100).toFixed(2) : ''}" required></label>
    </div>
    <label>Who was paid<input name="vendor" maxlength="120" value="${esc(ex?.vendor || '')}" placeholder="Fly.io" required></label>
    <label>Paid by<select name="paid_by_id"><option value="">The business</option>${people.map(p =>
      `<option value="${p.id}" ${(ex ? ex.paid_by_id : T.ctx.me.id) === p.id ? 'selected' : ''}>${esc(p.name)}${p.is_you ? ' (you)' : ''}</option>`).join('')}</select></label>
    <label>Note<input name="note" maxlength="300" value="${esc(ex?.note || '')}" placeholder="October hosting"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">${ex ? 'Save' : 'Add expense'}</button>
    ${ex ? '<button class="btn block ghost danger-link" data-del type="button">Delete this expense</button>' : ''}
  </form>`, { label: ex ? 'Expense' : 'Add an expense' });
  const form = sh.body.querySelector('#expForm');
  form.vendor.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    const cents = toCents(form.amount.value);
    if (!(cents > 0)) return showErr(form, 'Enter the amount as dollars.');
    try {
      await api(ex ? `/api/team/expenses/${ex.id}` : '/api/team/expenses', { method: ex ? 'PATCH' : 'POST',
        body: JSON.stringify({ spent_on: form.spent_on.value, vendor: form.vendor.value.trim(), amount_cents: cents,
          paid_by_id: idOrNull(form.paid_by_id.value), note: form.note.value.trim() || null }) });
      sh.close(); toast(ex ? 'Saved.' : 'Expense added.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
  const del = form.querySelector('[data-del]');
  if (del) del.onclick = async () => {
    try { await api(`/api/team/expenses/${ex.id}`, { method: 'DELETE' }); sh.close(); toast('Deleted.'); draw(); }
    catch (err) { showErr(form, err.message); }
  };
}

function reimburseSheet(ex) {
  const sh = T.ctx.sheet(`<form class="step left" id="reimForm">
    <h2>Paid ${esc(first(ex.paid_by))} back for ${esc(ex.vendor)}?</h2>
    <p class="muted">${money(ex.amount_cents)} from ${dayText(ex.spent_on)}. This records it; nothing is moved from here.</p>
    <label>How<input name="note" maxlength="200" placeholder="Venmo"></label>
    <p class="err" role="alert" hidden></p>
    <button class="btn block" type="submit">Mark paid back</button>
  </form>`, { label: 'Mark paid back' });
  const form = sh.body.querySelector('#reimForm');
  form.onsubmit = async e => {
    e.preventDefault();
    try {
      await api(`/api/team/expenses/${ex.id}/reimburse`, { method: 'POST',
        body: JSON.stringify({ note: form.note.value.trim() || null }) });
      sh.close(); toast('Marked paid back.'); draw();
    } catch (err) { showErr(form, err.message); }
  };
}
