import {$, $$, api, esc, state, camName, go, toast} from './app.js';

// Settings sections. Each has a title and a render(panel) function; stage 3 adds
// cameras, zones and alert rules.
export const SECTIONS = {
  status: {title: 'System status', render: renderStatus},
  account: {title: 'Account', render: renderAccount},
};

let root, shell, timer;

export async function mount(view, route, sh) {
  root = view; shell = sh;
  root.innerHTML = `<div class="settings"><nav class="subnav" aria-label="Settings sections">
      ${Object.entries(SECTIONS).map(([k, s]) => `<a href="#/settings/${k}" data-s="${k}">${esc(s.title)}</a>`).join('')}
    </nav><div class="panel" id="panel"></div></div>`;
  show(route);
}
export function update(route) { show(route); }
export function unmount() { clearInterval(timer); root = null; }

function show(route) {
  clearInterval(timer);
  const key = SECTIONS[route.parts[0]] ? route.parts[0] : Object.keys(SECTIONS)[0];
  for (const a of $$('.subnav a', root)) {
    if (a.dataset.s === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  const panel = $('#panel', root);
  panel.innerHTML = '';
  SECTIONS[key].render(panel, route, t => { timer = t; });
}

// ---------- status ----------
const GB = b => (b / 1e9 >= 1000 ? `${(b / 1e12).toFixed(1)} TB` : `${Math.round(b / 1e9)} GB`);

function recCell(r) {
  if (r == null) return '<span class="muted">Off</span>';
  const ok = r.state === 'recording';
  const label = {recording: 'Recording', starting: 'Starting', error: 'Error', stopped: 'Stopped', unknown: 'No report yet'}[r.state] || r.state;
  return `<span class="dot ${ok ? 'ok' : r.state === 'unknown' || r.state === 'starting' ? 'warn' : 'bad'}"></span>${esc(label)}
    ${!ok && r.detail ? `<div class="muted">${esc(r.detail)}</div>` : ''}`;
}
function detCell(d) {
  if (d == null) return '<span class="muted">Off</span>';
  if (d.state !== 'running') return `<span class="dot ${d.state === 'unknown' ? 'warn' : 'bad'}"></span>${esc(d.state === 'unknown' ? 'No report yet' : d.state)}
    ${d.error ? `<div class="muted">${esc(d.error)}</div>` : ''}`;
  return `<span class="dot ok"></span>Watching, ${d.fps_analysed ?? 0} fps`;
}

async function renderStatus(panel, _route, setTimer) {
  panel.innerHTML = `<h2>System status</h2><div id="st"><p class="muted">Loading…</p></div>`;
  const draw = async () => {
    let s;
    try { s = await api('/api/status'); } catch { return; }
    const el = $('#st', panel); if (!el) return;
    const d = s.disk;
    const rows = state.cameras.map(c => {
      const x = s.cameras[c.id] || {}, det = x.detection;
      return `<tr><td>${esc(c.name)}</td><td>${recCell(x.recording)}</td><td>${detCell(det)}</td>
        <td class="num">${det && det.infer_ms != null ? Math.round(det.infer_ms) + ' ms' : '–'}</td>
        <td class="num">${det && det.wait_ms != null ? Math.round(det.wait_ms) + ' ms' : '–'}</td>
        <td class="num">${x.events_today}</td></tr>`;
    }).join('');
    const svc = Object.entries(s.services || {}).map(([k, v]) =>
      `<span style="margin-right:22px"><span class="dot ${v === 'online' ? 'ok' : 'bad'}"></span>${esc(k.charAt(0).toUpperCase() + k.slice(1))}</span>`).join('');
    el.innerHTML = `
      <table class="health"><thead><tr><th>Camera</th><th>Recording</th><th>Detection</th>
        <th class="num" title="How long the model takes per frame">Model</th>
        <th class="num" title="Time a frame waits behind other cameras. If this stays high, the server needs more CPU.">Queue</th>
        <th class="num">Events today</th></tr></thead><tbody>${rows}</tbody></table>
      ${d ? `<div style="margin-top:26px"><h3>Storage</h3>
        <p>${GB(d.used)} of ${GB(d.total)} used (${d.percent}%). Recordings are kept ${d.retention_days} days; if the disk passes ${d.limit_percent}%, the oldest are deleted first.</p>
        <div class="meter" style="margin-top:10px" role="img" aria-label="Disk ${d.percent}% full"><i style="width:${d.percent}%"></i><b style="left:${d.limit_percent}%"></b></div></div>` : ''}
      ${svc ? `<div style="margin-top:26px"><h3>Services</h3><p>${svc}</p></div>` : ''}`;
  };
  await draw();
  setTimer(setInterval(() => { if (!document.hidden) draw(); }, 5000));
}

// ---------- account ----------
function renderAccount(panel) {
  panel.innerHTML = `<h2>Account</h2>
    <p>Signed in as <b>${esc(state.user)}</b>.</p>
    <form class="form" id="pw" autocomplete="on">
      <h3>Change password</h3>
      <input type="text" name="username" value="${esc(state.user)}" autocomplete="username" hidden>
      <label class="field">Current password<input type="password" name="current" autocomplete="current-password" required></label>
      <label class="field">New password<input type="password" name="new" autocomplete="new-password" required minlength="10"></label>
      <label class="field">Repeat new password<input type="password" name="new2" autocomplete="new-password" required></label>
      <p>At least 10 characters. Changing it signs you out on every other device.</p>
      <p class="error" role="alert" hidden></p>
      <div><button class="btn primary" type="submit">Change password</button></div>
    </form>
    <div><h3>Sign out</h3><p style="margin-bottom:10px">Sign out of BigBrother on this device.</p>
      <button class="btn" id="logout">Sign out</button></div>`;
  const form = $('#pw', panel), err = $('.error', form);
  form.onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(form));
    err.hidden = true;
    if (f.new !== f.new2) { err.textContent = "The new passwords don't match."; err.hidden = false; return; }
    try {
      await api('/api/password', {method: 'POST', body: {current: f.current, new: f.new}});
      form.reset(); toast('Password changed');
    } catch (ex) { err.textContent = ex.message; err.hidden = false; }
  };
  $('#logout', panel).onclick = async () => {
    await api('/api/logout', {method: 'POST'}).catch(() => {});
    location.hash = ''; location.reload();
  };
}
