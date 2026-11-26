// BigBrother app shell: session, navigation, the alerts lamp, and shared helpers.

export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
export const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export async function api(path, {method = 'GET', body, signal} = {}) {
  const opts = {method, signal, credentials: 'same-origin', headers: {}};
  if (method !== 'GET') opts.headers['X-BigBrother'] = '1';
  if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = null;
  if ((r.headers.get('Content-Type') || '').includes('json')) data = await r.json();
  if (r.status === 401 && !path.startsWith('/api/login')) { showGate(); throw new ApiError(401, 'Signed out'); }
  if (!r.ok) throw new ApiError(r.status, (data && data.error) || `Request failed (${r.status})`);
  return data;
}

// ---------- formatting ----------
export const LABEL_NAMES = {person: 'Person', car: 'Car', truck: 'Truck', bus: 'Bus', motorcycle: 'Motorcycle',
  bicycle: 'Bicycle', dog: 'Dog', cat: 'Cat', bird: 'Bird'};
export const labelName = l => LABEL_NAMES[l] || (l.charAt(0).toUpperCase() + l.slice(1));
export const timeOf = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
export const timeSec = ts => new Date(ts * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit', second: '2-digit'});
export function dayName(ts) {
  const d = new Date(ts * 1000), today = new Date();
  const start = x => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((start(today) - start(d)) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return d.toLocaleDateString([], {weekday: 'long', month: 'short', day: 'numeric'});
}
export function duration(a, b) {
  const s = Math.max(0, Math.round((b ?? Date.now() / 1000) - a));
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.floor(s / 60)} min ${s % 60 ? (s % 60) + ' s' : ''}`.trim();
  return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
}
export const ALERT_TEXT = {sent: 'Alert sent', logged: 'Alert logged', failed: 'Alert failed',
  suppressed: 'No alert', pending: 'Checking', grouped: 'Same visit'};

export function toast(msg, ms = 2600) {
  const t = document.createElement('div');
  t.className = 'toast'; t.setAttribute('role', 'status'); t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

// ---------- icons ----------
const I = p => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${p}</svg>`;
export const ICON = {
  live: I('<rect x="3" y="6" width="13" height="12" rx="2"/><path d="M16 10l5-3v10l-5-3z"/>'),
  events: I('<path d="M4 6h16M4 12h16M4 18h10"/>'),
  recordings: I('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>'),
  settings: I('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  back: I('<path d="M15 18l-6-6 6-6"/>'),
  close: I('<path d="M6 6l12 12M18 6L6 18"/>'),
  play: I('<path d="M7 5l12 7-12 7z" fill="currentColor"/>'),
  download: I('<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>'),
  expand: I('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>'),
};
// The mark: a roofline over a lens whose pupil is the porch lamp.
export const MARK = `<svg viewBox="0 0 64 64" aria-hidden="true"><path d="M9 29 32 9l23 20" fill="none" stroke="#E6EDF2" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/><circle cx="32" cy="40" r="13.5" fill="none" stroke="#E6EDF2" stroke-width="5.5"/><circle cx="32" cy="40" r="6" fill="#F0A93A"/></svg>`;
// Night street: the porch lamp is the only light. Framed so the roof stays in view at any size.
const SCENE = `<svg viewBox="-90 270 900 490" preserveAspectRatio="xMidYMid slice" aria-hidden="true">
  <defs><radialGradient id="glow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#F0A93A" stop-opacity=".5"/><stop offset=".35" stop-color="#F0A93A" stop-opacity=".14"/><stop offset="1" stop-color="#F0A93A" stop-opacity="0"/></radialGradient>
    <linearGradient id="sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#121B24"/><stop offset="1" stop-color="#1B2833"/></linearGradient></defs>
  <rect x="-600" y="0" width="1800" height="1200" fill="url(#sky)"/>
  <path d="M-600 640 L90 640 L90 560 L160 505 L230 560 L230 640 L560 640 L560 585 L620 540 L680 585 L680 640 L1200 640 L1200 1200 L-600 1200Z" fill="#1E2C38"/>
  <path d="M250 690 L250 545 L375 440 L500 545 L500 690Z" fill="#223240"/>
  <path d="M232 560 L375 438 L518 560" fill="none" stroke="#2E4354" stroke-width="10" stroke-linejoin="round" stroke-linecap="round"/>
  <rect x="300" y="590" width="44" height="100" fill="#1A2631"/>
  <rect x="400" y="575" width="56" height="44" rx="3" fill="#1A2631"/>
  <circle cx="362" cy="600" r="150" fill="url(#glow)"/>
  <rect x="355" y="592" width="14" height="18" rx="4" fill="#F0A93A"/>
  <path d="M-600 690 L1200 690 L1200 1200 L-600 1200Z" fill="#14202A"/>
  <path d="M300 690 L344 690 L372 1200 L272 1200Z" fill="#F0A93A" fill-opacity=".05"/>
</svg>`;

// ---------- state ----------
export const state = {user: null, role: null, twofa: false, cameras: [], camById: {}, armed: null};
export const isAdmin = () => state.role === 'admin';

export async function loadCameras() {
  state.cameras = await api('/api/cameras');
  state.camById = Object.fromEntries(state.cameras.map(c => [c.id, c]));
  return state.cameras;
}
export const camName = id => (state.camById[id] || {}).name || id;

// ---------- login / first-run setup ----------
async function showGate() {
  stopView();
  const s = await (await fetch('/api/session')).json();
  const root = $('#root');
  root.innerHTML = `<div class="gate"><div class="scene">${SCENE}</div><main class="gate-main">
      <div class="wordmark">${MARK}<span>BigBrother</span></div><div id="gate-step"></div></main></div>`;
  if (s.setup) stepSetup(); else stepSignIn();
}

function gateForm(html, onsubmit) {
  const el = $('#gate-step');
  el.innerHTML = `<form novalidate class="gate-form">${html}<p class="error" role="alert" hidden></p></form>`;
  const form = $('form', el), err = $('.error', form);
  const first = $('input:not([type=checkbox])', form); if (first) first.focus();
  form.onsubmit = async e => {
    e.preventDefault();
    err.hidden = true;
    const btn = $('button[type=submit]', form); btn.disabled = true;
    try { await onsubmit(Object.fromEntries(new FormData(form)), form); }
    catch (ex) { err.textContent = ex.message; err.hidden = false; }
    finally { btn.disabled = false; }
  };
  return form;
}

function stepSignIn() {
  gateForm(`<h1>Sign in</h1>
    <label class="field">Username<input type="text" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
    <label class="field">Password<input type="password" name="password" autocomplete="current-password" required></label>
    <label class="check"><input type="checkbox" name="remember" checked> Keep me signed in on this device</label>
    <button class="btn primary big" type="submit">Sign in</button>`, async f => {
    const r = await api('/api/login', {method: 'POST', body: {username: f.username, password: f.password, remember: !!f.remember}});
    if (r && r.need_code) return stepCode(r.ticket);
    start();
  });
}

function stepCode(ticket, recovery = false) {
  const form = gateForm(`<h1>${recovery ? 'Use a recovery code' : 'Enter your code'}</h1>
    <p>${recovery ? 'One of the codes you saved when you turned on two-factor sign-in. Each works once.'
                  : 'Open your authenticator app and enter the 6-digit code for BigBrother.'}</p>
    <label class="field">${recovery ? 'Recovery code' : 'Code'}<input type="text" name="code" class="code" required
      ${recovery ? 'autocomplete="off" placeholder="abcd-1234"' : 'inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" placeholder="123456"'}></label>
    <button class="btn primary big" type="submit">Continue</button>
    <button class="btn quiet" type="button" id="swap">${recovery ? 'Use my authenticator app' : "I don't have my phone"}</button>`,
    async f => { await api('/api/login/code', {method: 'POST', body: {ticket, code: f.code}}); start(); });
  $('#swap', form).onclick = () => stepCode(ticket, !recovery);
  if (!recovery) $('input', form).addEventListener('input', e => { if (/^\d{6}$/.test(e.target.value)) form.requestSubmit(); });
}

function stepSetup() {
  gateForm(`<h1>Set up BigBrother</h1>
    <p>Create the admin account. The setup code is in the web service's log on the server:
      <code>docker compose logs web | grep "setup code"</code></p>
    <label class="field">Setup code<input type="text" name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" required></label>
    <label class="field">Username<input type="text" name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
    <label class="field">Password<input type="password" name="password" autocomplete="new-password" required></label>
    <label class="field">Repeat password<input type="password" name="password2" autocomplete="new-password" required></label>
    <p class="hint">At least 10 characters.</p>
    <button class="btn primary big" type="submit">Create account</button>`, async f => {
    if (f.password !== f.password2) throw new Error("The passwords don't match.");
    await api('/api/setup', {method: 'POST', body: {code: f.code, username: f.username, password: f.password}});
    start();
  });
}

// ---------- shell ----------
const VIEWS = {
  live: {title: 'Live', load: () => import('./live.js')},
  events: {title: 'Events', load: () => import('./events.js')},
  recordings: {title: 'Recordings', load: () => import('./recordings.js')},
  settings: {title: 'Settings', load: () => import('./settings.js')},
};
let current = null;   // {name, mod}

function renderShell() {
  $('#root').innerHTML = `<div class="shell">
    <nav class="rail" aria-label="Main">
      <div class="brand">${MARK}</div>
      <div class="nav">${Object.entries(VIEWS).map(([k, v]) => `<a href="#/${k}" data-view="${k}">${ICON[k]}<span>${v.title}</span></a>`).join('')}</div>
      <div class="spacer"></div>
    </nav>
    <div class="main">
      <header class="top"><h1 id="title"></h1><div class="grow"></div><div id="tools"></div>
        <button class="lamp" id="lamp" aria-pressed="false" title="Turn phone alerts on or off"><span class="bulb"></span><span class="lamp-text">Alerts</span></button>
      </header>
      <main class="view" id="view"></main>
    </div></div>`;
  $('#lamp').onclick = toggleArmed;
}

function showArmed(on) {
  state.armed = on;
  const b = $('#lamp'); if (!b) return;
  b.setAttribute('aria-pressed', on ? 'true' : 'false');
  $('.lamp-text', b).innerHTML = on ? 'Alerts on' : 'Alerts off';
}
function showProblems(n) {
  const a = $('.nav a[data-view=settings]');
  if (!a) return;
  a.classList.toggle('alarm', n > 0);
  a.title = n ? `${n} system problem${n > 1 ? 's' : ''}: see Settings → System status` : '';
}
async function toggleArmed() {
  const want = !state.armed;
  try {
    const r = await api('/api/armed', {method: 'POST', body: {armed: want}});
    showArmed(r.armed);
    toast(r.armed ? 'Alerts turned on' : 'Alerts turned off. Events are still recorded.');
  } catch (e) { toast(e.message); }
}

function stopView() {
  if (current && current.mod.unmount) current.mod.unmount();
  current = null;
}

export function parseHash() {
  const [path, query = ''] = location.hash.replace(/^#\/?/, '').split('?');
  const parts = path.split('/').filter(Boolean);
  return {view: parts[0] || 'live', parts: parts.slice(1), query: new URLSearchParams(query)};
}
export function go(hash, replace = false) {
  if (replace) { history.replaceState(null, '', hash); route(); }
  else location.hash = hash;   // fires hashchange -> route()
}

async function route() {
  if (!state.user) return;
  const r = parseHash();
  const name = VIEWS[r.view] ? r.view : 'live';
  for (const a of $$('.nav a')) {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  if (current && current.name === name && current.mod.update) { current.mod.update(r); return; }
  stopView();
  const mod = await VIEWS[name].load();
  current = {name, mod};
  $('#title').textContent = VIEWS[name].title;
  $('#tools').innerHTML = '';
  const view = $('#view');
  view.innerHTML = '';
  await mod.mount(view, r, {setTitle: t => { $('#title').textContent = t; }, tools: $('#tools')});
}

let armedTimer = null;
async function start() {
  const s = await (await fetch('/api/session')).json();
  if (!s.user) return showGate();
  state.user = s.user; state.role = s.role; state.twofa = s.twofa;
  await loadCameras();
  renderShell();
  const poll = () => api('/api/armed').then(r => { showArmed(r.armed); showProblems(r.problems); }).catch(() => {});
  poll();
  clearInterval(armedTimer);
  armedTimer = setInterval(() => { if (!document.hidden) poll(); }, 20000);
  if (!location.hash) history.replaceState(null, '', '#/live');
  route();
}

window.addEventListener('hashchange', route);
if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
start();
