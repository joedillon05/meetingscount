const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const COLOR = { confirmed:'#0ca30c', flagged:'#fab219', rejected:'#d03b3b' };
const ICON = { confirmed:'✓', flagged:'!', rejected:'✕' };
const FT = 3.28084, M_DEG = 111320;
let checkins = [], meetings = [], byId = {}, people = [];
let layer, simLayer, markers = {}, pin = null, where = '0', when = 'now', ip = '76.120.44.9';
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));

function actingId() { try { return localStorage.getItem('mc_act_ops') || ''; } catch (e) { return ''; } }
async function api(p, o = {}) {
  const h = { 'Content-Type':'application/json', ...(o.headers || {}) };
  if (actingId()) h['X-MC-User'] = actingId();
  const r = await fetch(p, { ...o, headers:h });
  if (!r.ok) throw new Error((await r.text()).slice(0, 200));
  return r.status === 204 ? null : r.json();
}

const map = L.map('map').setView([42.28, -83.74], 11);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution:'© OpenStreetMap' }).addTo(map);
map.on('contextmenu', e => { pin = e.latlng; setWhere('map'); });

async function load() {
  let users = [];
  try { users = await api('/api/users'); } catch (e) {}
  const admin = users.find(u => u.role === 'admin');
  if (admin && !actingId()) { try { localStorage.setItem('mc_act_ops', admin.id); } catch (e) {} }
  people = users.filter(u => u.role === 'participant');

  try {
    [checkins, meetings] = await Promise.all([api('/api/ops/checkins'), api('/api/meetings')]);
  } catch (e) {
    $('#badge').textContent = 'admin role required';
    if (admin) { try { localStorage.setItem('mc_act_ops', admin.id); } catch (_) {} location.reload(); }
    return;
  }
  byId = Object.fromEntries(meetings.map(m => [m.id, m]));
  $('#badge').textContent = 'admin view';

  const f = $('#meetingFilter'), cur = f.value;
  f.innerHTML = '<option value="">All meetings</option>' +
    meetings.map(m => `<option value="${m.id}">${esc(m.name)}, ${new Date(m.starts_at).toLocaleDateString()}</option>`).join('');
  f.value = cur;

  const s = $('#simMeeting'), cs = s.value;
  const sorted = [...meetings].sort((a, b) =>
    (a.window_state === 'open' ? 0 : 1) - (b.window_state === 'open' ? 0 : 1) ||
    new Date(b.starts_at) - new Date(a.starts_at));
  s.innerHTML = sorted.map(m => `<option value="${m.id}">${esc(m.name)} (${m.window_state.replace('_',' ')})</option>`).join('');
  if (cs && byId[cs]) s.value = cs;

  const su = $('#simUser'), cu = su.value;
  su.innerHTML = people.map(u => `<option value="${u.id}">${esc(u.name)}</option>`).join('');
  if (cu) su.value = cu;

  draw(); drawSim();
}

function visible() {
  const mid = $('#meetingFilter').value, u = $('#userFilter').value.trim().toLowerCase();
  return checkins.filter(c => (!mid || c.meeting_id == mid) &&
    (!u || c.participant.name.toLowerCase().includes(u)));
}

function draw() {
  if (layer) map.removeLayer(layer);
  layer = L.featureGroup().addTo(map); markers = {};
  const mid = $('#meetingFilter').value;
  meetings.filter(m => !mid || m.id == mid).forEach(m => {
    L.circle([m.lat, m.lng], { radius:m.radius_ft / FT, color:'#111', weight:1, dashArray:'4', fillOpacity:.04 }).addTo(layer);
    L.circleMarker([m.lat, m.lng], { radius:6, color:'#111', fillOpacity:1 }).addTo(layer)
      .bindPopup(`<b>${esc(m.name)}</b><br>${new Date(m.starts_at).toLocaleString()}<br>${m.radius_ft} ft · ±${m.time_window_min} min`);
  });
  const cs = visible();
  cs.forEach(c => {
    if (c.lat == null) return;
    if (c.accuracy_m) L.circle([c.lat, c.lng], { radius:c.accuracy_m, color:COLOR[c.status], weight:1, fillOpacity:.05 }).addTo(layer);
    markers[c.id] = L.circleMarker([c.lat, c.lng], { radius:6, color:COLOR[c.status], fillColor:COLOR[c.status], fillOpacity:.85, weight:2 })
      .addTo(layer).bindPopup(`<b>${esc(c.participant.name)}</b> ${ICON[c.status]} ${c.status} ${c.confidence}/100<br>
        ${esc(c.meeting.name)}<br>${Math.round(c.distance_ft)} ft · ±${c.accuracy_m ? Math.round(c.accuracy_m) : '?'} m<br>
        ${new Date(c.checked_in_at).toLocaleString()}
        <ul>${c.reasons.map(r => `<li>${r.passed ? '✓' : '✕'} ${esc(r.reason)}</li>`).join('')}</ul>`);
  });
  $('#count').textContent = `${cs.length} of ${checkins.length} check-ins`;
  if (layer.getLayers().length) map.fitBounds(layer.getBounds().pad(0.15));
}

function setWhere(v) { where = v; $$('#simWhere button').forEach(b => b.classList.toggle('on', b.dataset.ft === v)); drawSim(); }
$('#simWhere').onclick = e => e.target.dataset.ft && setWhere(e.target.dataset.ft);
$('#simWhen').onclick = e => { if (e.target.dataset.min === undefined) return;
  when = e.target.dataset.min; $$('#simWhen button').forEach(b => b.classList.toggle('on', b === e.target)); drawSim(); };
$('#simNet').onclick = e => { if (!e.target.dataset.ip) return;
  ip = e.target.dataset.ip; $$('#simNet button').forEach(b => b.classList.toggle('on', b === e.target)); drawSim(); };
$('#simAcc').oninput = () => { $('#accV').textContent = $('#simAcc').value; drawSim(); };
$('#simMeeting').onchange = drawSim;
$('#meetingFilter').onchange = () => { draw(); };
$('#userFilter').oninput = () => { draw(); };
$('#refresh').onclick = load;

function simPos() {
  const m = byId[$('#simMeeting').value]; if (!m) return null;
  if (where === 'map') return pin ? { lat:pin.lat, lng:pin.lng } : null;
  const d = Number(where) / FT / Math.SQRT2;
  return { lat: m.lat + d / M_DEG, lng: m.lng + d / (M_DEG * Math.cos(m.lat * Math.PI / 180)) };
}
function simTime() {
  const m = byId[$('#simMeeting').value];
  return (!m || when === 'now') ? null : new Date(new Date(m.starts_at).getTime() + Number(when) * 60000).toISOString();
}
function distFt(a, b) {
  const R = 6371008.8, p1 = a.lat * Math.PI / 180, p2 = b.lat * Math.PI / 180;
  const dp = p2 - p1, dl = (b.lng - a.lng) * Math.PI / 180;
  const h = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h)) * FT;
}
function drawSim() {
  if (simLayer) map.removeLayer(simLayer);
  simLayer = L.featureGroup().addTo(map);
  const m = byId[$('#simMeeting').value], p = simPos();
  if (!m || !p) { $('#simPreview').textContent = where === 'map' ? 'Right-click the map to place them.' : ''; return; }
  const acc = Number($('#simAcc').value);
  L.circle([p.lat, p.lng], { radius:acc, color:'#7c3aed', weight:1, fillOpacity:.1 }).addTo(simLayer);
  L.circleMarker([p.lat, p.lng], { radius:8, color:'#7c3aed', fillColor:'#7c3aed', fillOpacity:.9 }).addTo(simLayer).bindPopup('Simulated');
  const t = simTime();
  $('#simPreview').innerHTML = `Sending <b>${distFt(p, m).toFixed(0)} ft</b> from "${esc(m.name)}" (limit ${m.radius_ft} ft), ±${acc} m, at ${t ? new Date(t).toLocaleString() : 'now'}${t ? '' : (m.window_state === 'open' ? ' (window open)' : ` (window ${m.window_state.replace('_',' ')}, expect a rejection)`)}.`;
}

$('#simRun').onclick = async () => {
  const m = byId[$('#simMeeting').value], p = simPos(), uid = $('#simUser').value;
  if (!m || !p || !uid) return $('#simResult').innerHTML = '<div class="result flagged">Pick a participant, a meeting and a position.</div>';
  const body = { meeting_id:m.id, lat:p.lat, lng:p.lng, accuracy_m:Number($('#simAcc').value), sim_ip:ip };
  const at = simTime(); if (at) body.at = at;
  $('#simRun').disabled = true;
  try {
    // Send as the participant: the check-in belongs to them, not to the admin.
    const r = await fetch('/api/checkins', { method:'POST',
      headers:{ 'Content-Type':'application/json', 'X-MC-User': uid }, body: JSON.stringify(body) });
    const c = await r.json();
    if (!r.ok) throw new Error(c.detail || r.statusText);
    $('#simResult').innerHTML = `<div class="result ${c.status}">
      <h3>${ICON[c.status]} ${c.status} · ${c.confidence}/100</h3>
      ${Math.round(c.distance_ft)} ft from the meeting
      <ul>${c.reasons.map(x => `<li>${x.passed ? '✓' : '✕'} <b>${esc(x.name)}</b>: ${esc(x.reason)}</li>`).join('')}</ul>
      ${c.status !== 'confirmed' ? '<div style="margin-top:6px">This one lands in the review queue.</div>' : ''}</div>`;
    await load();
    markers[c.id] && markers[c.id].openPopup();
  } catch (e) { $('#simResult').innerHTML = `<div class="result rejected">Error: ${esc(e.message)}</div>`; }
  finally { $('#simRun').disabled = false; }
};

$('#seed').onclick = async () => {
  if (!confirm('This wipes the database and rebuilds the full demo county. Continue?')) return;
  await api('/api/demo/seed', { method:'POST' });
  $('#simResult').innerHTML = ''; load();
};

load();
