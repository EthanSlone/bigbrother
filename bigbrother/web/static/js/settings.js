import {$, $$, api, esc, state, go, toast, labelName, loadCameras, camName, ICON, isAdmin, promptInstall, isInstalled, installPrompt} from './app.js';

// admin: only admins see the section (the server enforces it too).
export const SECTIONS = {
  status: {title: 'System status', render: renderStatus},
  cameras: {title: 'Cameras', render: renderCameras, admin: true},
  zones: {title: 'Zones and masks', render: renderZones, admin: true},
  alerts: {title: 'Alerts', render: renderAlerts, admin: true},
  recording: {title: 'Recording', render: renderRecording, admin: true},
  feedback: {title: 'Detection feedback', render: renderFeedback, admin: true},
  users: {title: 'People', render: renderUsers, admin: true},
  account: {title: 'Your account', render: renderAccount},
};
const visible = () => Object.fromEntries(Object.entries(SECTIONS).filter(([, v]) => !v.admin || isAdmin()));

let root, shell, timer, cleanup = null;

export async function mount(view, route, sh) {
  root = view; shell = sh;
  root.innerHTML = `<div class="settings"><nav class="subnav" aria-label="Settings sections">
      ${Object.entries(visible()).map(([k, s]) => `<a href="#/settings/${k}" data-s="${k}">${esc(s.title)}</a>`).join('')}
    </nav><div class="panel" id="panel"></div></div>`;
  show(route);
}
export function update(route) { show(route); }
export function unmount() { clearInterval(timer); if (cleanup) cleanup(); root = null; }

function show(route) {
  clearInterval(timer);
  if (cleanup) { cleanup(); cleanup = null; }
  const key = visible()[route.parts[0]] ? route.parts[0] : 'status';
  for (const a of $$('.subnav a', root)) {
    if (a.dataset.s === key) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  const panel = $('#panel', root);
  panel.innerHTML = '';
  panel.classList.toggle('wide', key === 'zones');
  SECTIONS[key].render(panel, route, t => { timer = t; });
}

// ---------- shared ----------
let cfgCache = null;
async function config(fresh = false) {
  if (!cfgCache || fresh) cfgCache = await api('/api/config');
  return cfgCache;
}
const SVC = {go2rtc: 'live streams', recorder: 'recording', detector: 'detection', alerter: 'alerts'};
async function save(method, path, body, what = 'Saved') {
  const r = await api(path, {method, body});
  cfgCache = null;
  await loadCameras();
  const parts = r.restarted.map(s => SVC[s] || s);
  toast(parts.length ? `${what}. Restarting ${parts.join(', ')} to apply it (about 20 seconds).` : `${what}.`, 4500);
  return r;
}
function formError(el, ex) { el.textContent = ex.message; el.hidden = false; }
const chip = (v, on, text, cls = '') => `<button type="button" class="chip ${cls}" data-v="${esc(v)}" aria-pressed="${!!on}">${text}</button>`;
function chipGroup(el) {
  for (const b of $$('.chip', el)) b.onclick = () => b.setAttribute('aria-pressed', b.getAttribute('aria-pressed') !== 'true');
}
const picked = el => $$('.chip[aria-pressed=true]', el).map(b => b.dataset.v);
const objChips = (all, on) => all.map(o => chip(o, on.includes(o), `<span class="kind">${esc(labelName(o))}</span>`, `k-${o}`)).join('');

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
// Load average vs CPUs: past about 85% the server is short of CPU, and live video and recording
// are the first things to suffer.
function loadText({avg, cpus}) {
  const share = avg[0] / cpus, busy = share > 0.85;
  return `<span class="dot ${busy ? 'warn' : 'ok'}"></span>${Math.round(share * 100)}% of ${cpus} CPUs in use over the last minute
    (${Math.round(avg[2] / cpus * 100)}% over 15 minutes).${busy ? ' The server is short of CPU: live video may stutter. Detection can be lightened per camera (fewer frames per second, or Strictness).' : ''}`;
}
async function renderStatus(panel, _r, setTimer) {
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
        <td class="num">${x.events_today ?? 0}</td></tr>`;
    }).join('');
    const svc = Object.entries(s.services || {}).map(([k, v]) =>
      `<span style="margin-right:22px"><span class="dot ${v === 'online' ? 'ok' : 'bad'}"></span>${esc(k.charAt(0).toUpperCase() + k.slice(1))}</span>`).join('');
    const probs = s.problems || [];
    el.innerHTML = `
      ${s.problems == null ? '<p class="muted" style="margin-bottom:18px">Health checks start once the alert service is running.</p>'
        : probs.length ? `<div class="problems" role="alert">${probs.map(p => `<div class="problem">${esc(p.message)}
            <span>Since ${new Date(p.since * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'})}${p.notified ? ', alert sent' : ''}</span></div>`).join('')}</div>`
        : '<p style="margin-bottom:18px"><span class="dot ok"></span>Everything is working.</p>'}
      <table class="health"><thead><tr><th>Camera</th><th>Recording</th><th>Detection</th>
        <th class="num" title="How long the model takes per frame">Model</th>
        <th class="num" title="Time a frame waits behind other cameras. If this stays high, the server needs more CPU.">Queue</th>
        <th class="num">Events today</th></tr></thead><tbody>${rows}</tbody></table>
      ${d ? `<div style="margin-top:26px"><h3>Storage</h3>
        <p>${GB(d.used)} of ${GB(d.total)} used (${d.percent}%). 24/7 recordings are kept ${d.retention_days} days and events ${d.event_retention_days} days; if the disk passes ${d.limit_percent}%, the oldest recordings are deleted first.</p>
        <div class="meter" style="margin-top:10px" role="img" aria-label="Disk ${d.percent}% full"><i style="width:${d.percent}%"></i><b style="left:${d.limit_percent}%"></b></div></div>` : ''}
      ${svc ? `<div style="margin-top:26px"><h3>Services</h3><p>${svc}</p></div>` : ''}
      ${s.load ? `<div style="margin-top:26px"><h3>Server load</h3><p>${loadText(s.load)}</p></div>` : ''}`;
  };
  await draw();
  setTimer(setInterval(() => { if (!document.hidden) draw(); }, 5000));
}

// ---------- cameras ----------
const HIK = /^rtsp:\/\/\$\{(\w+)\}:\$\{(\w+)\}@([^:/]+)(?::(\d+))?\/Streaming\/Channels\/(\d)01$/;
const PROTECT = /^rtsp:\/\/([^:/]+):7447\/\$\{(\w+)\}$/;
function detectKind(streams) {
  const m = (streams.main || '').match(HIK);
  if (m) {
    const s = (streams.sub || '').match(HIK);
    if (!streams.sub || (s && s[3] === m[3] && s[5] === m[5]))
      return {kind: 'hik', user: m[1], pass: m[2], host: m[3], port: m[4] || '554', channel: m[5], sub: !!streams.sub};
  }
  const p = (streams.main || '').match(PROTECT);
  if (p) {
    const s = (streams.sub || '').match(PROTECT);
    return {kind: 'protect', host: p[1], token: p[2], subtoken: s ? s[2] : ''};
  }
  return {kind: 'url', main: streams.main || '', sub: streams.sub || ''};
}
function buildStreams(f) {
  if (f.kind === 'hik') {
    const base = `rtsp://\${${f.user}}:\${${f.pass}}@${f.host}${f.port && f.port !== '554' ? ':' + f.port : ':554'}/Streaming/Channels/${f.channel || '1'}`;
    return {main: base + '01', sub: f.sub ? base + '02' : null};
  }
  if (f.kind === 'protect') return {main: `rtsp://${f.host}:7447/\${${f.token}}`, sub: f.subtoken ? `rtsp://${f.host}:7447/\${${f.subtoken}}` : null};
  return {main: f.main.trim(), sub: f.sub.trim() || null};
}

async function renderCameras(panel, route) {
  const cfg = await config(true);
  const cams = cfg.raw.cameras || {};
  const editing = route.parts[1];
  if (editing) return cameraForm(panel, cfg, editing === 'new' ? null : editing);
  panel.innerHTML = `<div style="display:flex;align-items:center;gap:12px"><h2 style="flex:1">Cameras</h2>
      <a class="btn primary" href="#/settings/cameras/new">Add camera</a></div>
    <div class="rows">${Object.entries(cams).map(([id, c]) => {
      const k = detectKind(c.streams || {});
      const where = k.kind === 'hik' ? `Hikvision/Annke at ${k.host}` : k.kind === 'protect' ? `UniFi Protect via ${k.host}` : 'Custom stream';
      const rec = !(c.record && c.record.enabled === false), det = !(c.detect && c.detect.enabled === false);
      return `<div class="row"><div class="t"><b>${esc(c.name || id)}</b>
        <span>${esc(where)}. ${rec ? 'Recording' : 'Not recording'}, ${det ? 'detection on' : 'detection off'}${c.streams && c.streams.sub ? '' : ', no substream'}.</span></div>
        <a class="btn" href="#/settings/cameras/${esc(id)}">Edit</a></div>`;
    }).join('')}</div>`;
}

function cameraForm(panel, cfg, id) {
  const raw = id ? (cfg.raw.cameras || {})[id] : null;
  if (id && !raw) { go('#/settings/cameras', true); return; }
  const c = raw || {streams: {}};
  const k = id ? detectKind(c.streams || {}) : {kind: 'hik', user: 'CAM_USER', pass: 'CAM_PASSWORD', host: '', port: '554', channel: '1', sub: true};
  const det = c.detect || {}, labels = det.labels || cfg.default_labels;
  panel.innerHTML = `<form class="form" id="camf" novalidate>
    <h2>${id ? esc(c.name || id) : 'Add camera'}</h2>
    <label class="field">Name<input type="text" name="name" value="${esc(c.name || '')}" required placeholder="Front door"></label>
    ${id ? '' : '<label class="field">Short id (used in file names; lowercase, no spaces)<input type="text" name="id" required placeholder="front_door" pattern="[a-z][a-z0-9_]*"></label>'}
    <label class="field">Camera type<select name="kind">
      <option value="hik">Hikvision or Annke</option><option value="protect">UniFi Protect</option><option value="url">Other (enter stream addresses)</option></select></label>
    <div data-kind="hik" class="form" style="gap:12px">
      <div class="two"><label class="field">IP address<input type="text" name="host" value="${esc(k.host || '')}" placeholder="10.10.15.18"></label>
        <label class="field">RTSP port<input type="text" name="port" value="${esc(k.port || '554')}"></label></div>
      <div class="two"><label class="field">Username variable<input type="text" name="user" value="${esc(k.user || 'CAM_USER')}"></label>
        <label class="field">Password variable<input type="text" name="pass" value="${esc(k.pass || 'CAM_PASSWORD')}"></label></div>
      <label class="check"><input type="checkbox" name="hsub" ${k.kind !== 'hik' || k.sub ? 'checked' : ''}> Use the substream for detection (recommended)</label>
    </div>
    <div data-kind="protect" class="form" style="gap:12px">
      <label class="field">UniFi console address<input type="text" name="phost" value="${esc(k.kind === 'protect' ? k.host : '192.168.0.1')}"></label>
      <div class="two"><label class="field">High-quality stream token variable<input type="text" name="token" value="${esc(k.token || '')}" placeholder="BACKYARD_TOKEN"></label>
        <label class="field">Low/Medium stream token variable<input type="text" name="subtoken" value="${esc(k.subtoken || '')}" placeholder="optional"></label></div>
      <p>In Protect, open the camera's settings, turn on RTSP for the streams, and put each token from the address in <code>.env</code> under the variable name you enter here.</p>
    </div>
    <div data-kind="url" class="form" style="gap:12px">
      <label class="field">Main stream (recorded)<input type="text" name="main" value="${esc(k.kind === 'url' ? k.main : '')}" placeholder="rtsp://\${CAM_USER}:\${CAM_PASSWORD}@10.0.0.5:554/stream1"></label>
      <label class="field">Substream (detection, optional)<input type="text" name="sub" value="${esc(k.kind === 'url' ? k.sub : '')}"></label>
    </div>
    <p>Passwords never go here. They stay in the <code>.env</code> file on the server, and the camera refers to them by variable name. ${cfg.env_vars.length ? `Variables in use: ${cfg.env_vars.map(v => `<code>${esc(v)}</code>`).join(', ')}.` : ''}</p>
    <label class="check"><input type="checkbox" name="record" ${!(c.record && c.record.enabled === false) ? 'checked' : ''}> Record 24/7</label>
    <label class="check"><input type="checkbox" name="detect" ${det.enabled !== false ? 'checked' : ''}> Detect objects</label>
    <div><h3>Objects to detect</h3><div class="chips" id="labels">${objChips(cfg.objects, labels)}</div></div>
    <div class="form" style="gap:12px"><h3>Detection tuning</h3>
      <div class="two"><label class="field">Motion sensitivity
        <select name="msens">${[['very_low', 'Very low (rain, trees, busy street)'], ['low', 'Low'], ['normal', 'Normal'], ['high', 'High (small or far-away movement)']]
          .map(([v, t]) => `<option value="${v}" ${(det.motion_sensitivity || 'normal') === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <label class="field">How sure before it's an event
        <select name="strict">${[[0.5, 'Relaxed (catches more, more false alarms)'], [0.6, 'Normal'], [0.7, 'Strict (fewer false alarms)'], [0.8, 'Very strict']]
          .map(([v, t]) => `<option value="${v}" ${Math.abs((det.threshold ?? 0.6) - v) < 0.001 ? 'selected' : ''}>${t}</option>`).join('')}
          ${[0.5, 0.6, 0.7, 0.8].some(v => Math.abs((det.threshold ?? 0.6) - v) < 0.001) ? '' : `<option value="${det.threshold}" selected>Custom (${det.threshold})</option>`}</select></label></div>
      <label class="check"><input type="checkbox" name="zoom" ${det.zoom !== false ? 'checked' : ''}> Take a closer look at small moving things (finds far-away people; uses more CPU when something small moves)</label>
      <label class="check"><input type="checkbox" name="gate" ${det.motion_gate !== false ? 'checked' : ''}> Only start tracking objects where something moved (ignores look-alikes such as a coat on a chair)</label>
    </div>
    <p class="error" role="alert" hidden></p>
    <div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn primary" type="submit">${id ? 'Save camera' : 'Add camera'}</button>
      <a class="btn quiet" href="#/settings/cameras">Cancel</a><span style="flex:1"></span>
      ${id ? '<button class="btn" type="button" id="del">Remove camera</button>' : ''}</div>
  </form>`;
  const f = $('#camf', panel), err = $('.error', f);
  f.kind.value = k.kind;
  const showKind = () => { for (const d of $$('[data-kind]', f)) d.hidden = d.dataset.kind !== f.kind.value; };
  f.kind.onchange = showKind; showKind();
  chipGroup($('#labels', f));
  f.onsubmit = async e => {
    e.preventDefault(); err.hidden = true;
    const v = Object.fromEntries(new FormData(f));
    const t = x => (x || '').trim();
    const streams = v.kind === 'hik'
      ? buildStreams({kind: 'hik', user: t(v.user), pass: t(v.pass), host: t(v.host), port: t(v.port), channel: k.channel || '1', sub: !!v.hsub})
      : v.kind === 'protect'
        ? buildStreams({kind: 'protect', host: t(v.phost), token: t(v.token), subtoken: t(v.subtoken)})
        : {main: t(v.main), sub: t(v.sub) || null};
    if ((v.kind === 'hik' && !t(v.host)) || (v.kind === 'protect' && (!t(v.phost) || !t(v.token))) || !streams.main) {
      err.textContent = 'Fill in where to find the camera.'; err.hidden = false; return;
    }
    const lab = picked($('#labels', f));
    if (!lab.length) { err.textContent = 'Pick at least one object to detect.'; err.hidden = false; return; }
    const body = {name: v.name.trim(), streams, record: {enabled: !!v.record}, detect: {enabled: !!v.detect, labels: lab,
      motion_sensitivity: v.msens, threshold: +v.strict, zoom: !!v.zoom, motion_gate: !!v.gate}};
    try {
      if (id) await save('PUT', `/api/config/cameras/${encodeURIComponent(id)}`, body, 'Camera saved');
      else await save('POST', '/api/config/cameras', {id: (v.id || '').trim(), ...body}, 'Camera added');
      go('#/settings/cameras');
    } catch (ex) { formError(err, ex); }
  };
  const del = $('#del', f);
  if (del) del.onclick = async () => {
    if (!confirm(`Remove ${c.name || id}? Its existing recordings and events stay until they age out.`)) return;
    try { await save('DELETE', `/api/config/cameras/${encodeURIComponent(id)}`, undefined, 'Camera removed'); go('#/settings/cameras'); }
    catch (ex) { formError(err, ex); }
  };
}

// ---------- zones & masks ----------
const COLORS = {zone: '#5EC2D6', motion: '#93A6B4', objects: '#E5604D'};
const r4 = v => Math.round(v * 10000) / 10000;

async function renderZones(panel, route) {
  const cfg = await config(true);
  const cams = cfg.raw.cameras || {};
  const id = cams[route.parts[1]] ? route.parts[1] : Object.keys(cams)[0];
  if (!id) { panel.innerHTML = '<div class="empty"><h2>No cameras yet</h2></div>'; return; }
  const c = cams[id];
  const ed = {
    zones: Object.entries(c.zones || {}).map(([name, z]) => ({kind: 'zone', name, objects: z.objects || [], points: z.points})),
    motion: ((c.masks || {}).motion || []).map(p => ({kind: 'motion', points: p})),
    objects: ((c.masks || {}).objects || []).map(p => ({kind: 'objects', points: p})),
  };
  let shapes = [...ed.zones, ...ed.motion, ...ed.objects], drawing = null, sel = null;
  panel.innerHTML = `<h2>Zones and masks</h2>
    <p><b>Zones</b> are areas you name, like "porch". Alerts can require an object to enter one.
      <b>Motion masks</b> hide areas that move but don't matter (trees, the timestamp).
      <b>Object masks</b> drop detections standing in an area (a parked car, a statue). Objects count as inside an area where they touch the ground.</p>
    <div class="chips" id="camsel">${Object.entries(cams).map(([cid, x]) => `<a class="chip" href="#/settings/zones/${esc(cid)}" aria-pressed="${cid === id}" style="text-decoration:none">${esc(x.name || cid)}</a>`).join('')}</div>
    <div class="editor">
      <div><div class="stage"><img id="zimg" alt="Current view of ${esc(c.name || id)}"><svg id="zsvg" viewBox="0 0 1000 1000" preserveAspectRatio="none" aria-label="Drawing area"></svg></div>
        <p class="muted" id="zhint" style="margin-top:8px">Pick what to add, then click on the picture to place corners. Click the first corner to finish. Drag corners to adjust.</p></div>
      <div class="form" style="gap:14px">
        <div style="display:flex;gap:8px;flex-wrap:wrap"><button class="btn" data-add="zone">Add zone</button><button class="btn" data-add="motion">Add motion mask</button><button class="btn" data-add="objects">Add object mask</button></div>
        <div class="shapes" id="shapes"></div>
        <div id="shapeopts"></div>
        <p class="error" role="alert" hidden></p>
        <div style="display:flex;gap:10px"><button class="btn primary" id="zsave">Save</button><button class="btn quiet" id="zreset">Undo changes</button></div>
      </div></div>`;
  const svg = $('#zsvg', panel), err = $('.error', panel);
  const img = $('#zimg', panel);
  img.src = `/media/frame/${encodeURIComponent(id)}.jpg?q=low&t=${Date.now()}`;
  img.onerror = () => { $('#zhint', panel).textContent = "Couldn't get a picture from this camera right now. You can still draw, but check the camera is online."; };

  const label = s => s.kind === 'zone' ? (s.name || 'unnamed zone') : s.kind === 'motion' ? 'Motion mask' : 'Object mask';
  const pt = e => { const b = svg.getBoundingClientRect(); return [r4(Math.min(1, Math.max(0, (e.clientX - b.left) / b.width))), r4(Math.min(1, Math.max(0, (e.clientY - b.top) / b.height)))]; };
  function draw() { drawCanvas(); drawOpts(); }
  function drawCanvas() {
    let h = '';
    for (const [i, s] of shapes.entries()) {
      const col = COLORS[s.kind], on = sel === i;
      const p = s.points.map(([x, y]) => `${x * 1000},${y * 1000}`).join(' ');
      h += `<polygon data-i="${i}" points="${p}" fill="${col}" fill-opacity="${s.kind === 'zone' ? (on ? .28 : .14) : (on ? .5 : .35)}" stroke="${col}" stroke-width="${on ? 3 : 2}" vector-effect="non-scaling-stroke" style="cursor:pointer"/>`;
      if (s.kind === 'zone') { const [x, y] = s.points[0]; h += `<text x="${x * 1000 + 8}" y="${y * 1000 + 30}" fill="#fff" font-size="26" font-family="Atkinson, sans-serif" paint-order="stroke" stroke="#000" stroke-width="4">${esc(s.name || '')}</text>`; }
      if (on) for (const [j, [x, y]] of s.points.entries()) h += `<circle data-i="${i}" data-j="${j}" cx="${x * 1000}" cy="${y * 1000}" r="9" fill="#fff" stroke="${col}" stroke-width="3" vector-effect="non-scaling-stroke" style="cursor:grab"/>`;
    }
    if (drawing) {
      const col = COLORS[drawing.kind];
      h += `<polyline points="${drawing.points.map(([x, y]) => `${x * 1000},${y * 1000}`).join(' ')}" fill="none" stroke="${col}" stroke-width="3" stroke-dasharray="8 6" vector-effect="non-scaling-stroke"/>`;
      for (const [j, [x, y]] of drawing.points.entries()) h += `<circle cx="${x * 1000}" cy="${y * 1000}" r="${j === 0 ? 12 : 7}" fill="${j === 0 ? '#fff' : col}" stroke="${col}" stroke-width="3" vector-effect="non-scaling-stroke"/>`;
    }
    svg.innerHTML = h;
    $('#shapes', panel).innerHTML = shapes.length ? shapes.map((s, i) => `<div class="shape" ${sel === i ? 'style="background:var(--deck2)"' : ''}>
        <span class="sw" style="background:${COLORS[s.kind]}"></span><button class="btn quiet grow" data-pick="${i}" style="justify-content:flex-start">${esc(label(s))}</button>
        <button class="btn quiet" data-del="${i}" aria-label="Delete ${esc(label(s))}">${ICON.close}</button></div>`).join('')
      : '<p class="muted">Nothing drawn on this camera.</p>';
  }
  function drawOpts() {
    const opts = $('#shapeopts', panel);
    const s = shapes[sel];
    if (s && s.kind === 'zone') {
      const labels = (c.detect && c.detect.labels) || cfg.default_labels;
      opts.innerHTML = `<label class="field">Zone name<input type="text" id="zname" value="${esc(s.name || '')}" placeholder="porch"></label>
        <div style="margin-top:10px"><h3>Only count these objects (none picked = all)</h3><div class="chips" id="zobj">${objChips(labels, s.objects || [])}</div></div>`;
      $('#zname', opts).oninput = e => { s.name = e.target.value.trim().toLowerCase().replace(/\s+/g, '_'); drawCanvas(); };
      for (const b of $$('#zobj .chip', opts)) b.onclick = () => {
        b.setAttribute('aria-pressed', b.getAttribute('aria-pressed') !== 'true'); s.objects = picked($('#zobj', opts)); };
    } else opts.innerHTML = '';
  }

  let drag = null;
  svg.addEventListener('pointerdown', e => {
    const t = e.target;
    if (drawing) {
      const p = pt(e);
      if (t.tagName === 'circle' && drawing.points.length >= 3 && t === svg.querySelector('circle')) return finish();
      drawing.points.push(p); drawCanvas(); return;
    }
    if (t.dataset.j !== undefined) { drag = {i: +t.dataset.i, j: +t.dataset.j}; svg.setPointerCapture(e.pointerId); return; }
    if (t.dataset.i !== undefined) { sel = +t.dataset.i; draw(); return; }
    sel = null; draw();
  });
  svg.addEventListener('pointermove', e => { if (!drag) return; shapes[drag.i].points[drag.j] = pt(e); drawCanvas(); });
  svg.addEventListener('pointerup', () => { drag = null; });
  function finish() {
    if (drawing.points.length < 3) return;
    shapes.push(drawing); sel = shapes.length - 1; drawing = null;
    $('#zhint', panel).textContent = shapes[sel].kind === 'zone' ? 'Give the zone a name, then save.' : 'Added. Save when you\'re done.';
    draw();
    const n = $('#zname', panel); if (n) n.focus();
  }
  const keys = e => {
    if (!drawing) return;
    if (e.key === 'Enter') finish();
    if (e.key === 'Escape') { drawing = null; draw(); }
    if (e.key === 'Backspace') { drawing.points.pop(); drawCanvas(); }
  };
  document.addEventListener('keydown', keys);
  cleanup = () => document.removeEventListener('keydown', keys);
  for (const b of $$('[data-add]', panel)) b.onclick = () => {
    drawing = {kind: b.dataset.add, points: [], name: '', objects: []}; sel = null;
    $('#zhint', panel).textContent = 'Click to place corners. Click the first corner (or press Enter) to finish; Esc cancels.';
    draw();
  };
  $('#shapes', panel).addEventListener('click', e => {
    const p = e.target.closest('[data-pick]'), d = e.target.closest('[data-del]');
    if (p) { sel = +p.dataset.pick; draw(); }
    if (d) { shapes.splice(+d.dataset.del, 1); sel = null; draw(); }
  });
  $('#zreset', panel).onclick = () => renderZones(panel, route);
  $('#zsave', panel).onclick = async () => {
    err.hidden = true;
    const zones = {}, motion = [], objects = [];
    for (const s of shapes) {
      if (s.kind === 'zone') {
        if (!/^[a-z][a-z0-9_]{0,31}$/.test(s.name || '')) { err.textContent = 'Each zone needs a name: lowercase letters, digits and _, starting with a letter.'; err.hidden = false; return; }
        if (zones[s.name]) { err.textContent = `Two zones are named ${s.name}.`; err.hidden = false; return; }
        zones[s.name] = {points: s.points, objects: s.objects && s.objects.length ? s.objects : null};
      } else (s.kind === 'motion' ? motion : objects).push(s.points);
    }
    // Alert rules can't point at a zone that no longer exists.
    const alertZones = ((c.alert || {}).zones || []).filter(z => zones[z]);
    const body = {zones: Object.keys(zones).length ? zones : null, masks: motion.length || objects.length ? {motion, objects} : null};
    if (c.alert && c.alert.zones) body.alert = {zones: alertZones.length ? alertZones : null};
    try { await save('PUT', `/api/config/cameras/${encodeURIComponent(id)}`, body, 'Zones saved'); }
    catch (ex) { formError(err, ex); }
  };
  draw();
}

// ---------- alerts ----------
const DAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun'];
const DAY_NAMES = {mon: 'Mon', tue: 'Tue', wed: 'Wed', thu: 'Thu', fri: 'Fri', sat: 'Sat', sun: 'Sun'};
const COOLDOWNS = [[30, '30 seconds'], [60, '1 minute'], [120, '2 minutes'], [300, '5 minutes'], [600, '10 minutes'], [1800, '30 minutes']];

async function renderAlerts(panel) {
  const cfg = await config(true);
  const raw = cfg.raw, al = raw.alerts || {}, defAlert = (raw.defaults || {}).alert || {};
  const tz = al.timezone || 'UTC';
  let tzs = Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
  if (!tzs.includes(tz)) tzs = [tz, ...tzs];   // browsers list some zones under another name
  const ns = await api('/api/notify/settings');
  panel.innerHTML = `<h2>Alerts</h2>
    <p>The lamp at the top turns all phone alerts on or off. These rules decide which events become alerts while it's on.</p>
    <form class="form" id="po"><h3>Pushover</h3>
      <p>Alerts go to phones through <a href="https://pushover.net" target="_blank" rel="noopener">Pushover</a>
        (one-time purchase per person; iPhone and Android). On pushover.net, create an application named BigBrother and paste its API token here.
        Each person then adds their own user key under Your account.</p>
      <p>${ns.token_source === 'app' ? '<span class="dot ok"></span>App token saved.' : ns.token_source === 'env'
        ? '<span class="dot ok"></span>Using the token from the server\'s .env file.' : '<span class="dot warn"></span>No app token yet: alerts are only logged.'}
        ${ns.people.length ? `Alerts go to ${ns.people.map(r => `<b>${esc(r.name)}</b>${r.cameras ? ` (${r.cameras.length} camera${r.cameras.length === 1 ? '' : 's'})` : ''}`).join(', ')}.`
          : 'Nobody has added their Pushover key yet.'}</p>
      <label class="field">App token<input type="password" name="token" autocomplete="off" spellcheck="false"
        placeholder="${ns.token_source === 'app' ? 'saved; paste a new one to replace it' : '30 letters and numbers'}"></label>
      <p class="error" role="alert" hidden></p>
      <div style="display:flex;gap:10px"><button class="btn" type="submit">Save token</button>
        ${ns.token_source === 'app' ? '<button class="btn quiet" type="button" id="clear">Remove token</button>' : ''}</div></form>
    <form class="form" id="auto"><h3>Turn alerts on and off automatically</h3>
      <p>Alerts turn on when a window starts and off when it ends, for example overnight and while you're at work.
        The lamp still works in between: if you turn alerts off by hand, they stay off until the next start.</p>
      <label class="field">Schedule<select name="mode"><option value="off">Off: only the lamp turns alerts on and off</option>
        <option value="on" ${(al.arm_schedule || []).length ? 'selected' : ''}>On during these hours</option></select></label>
      <div data-g="sched" ${(al.arm_schedule || []).length ? '' : 'hidden'}>${((al.arm_schedule || []).length ? al.arm_schedule
        : [{days: DAYS, start: '22:00', end: '07:00'}]).map(schedRow).join('')}
        <button type="button" class="btn quiet" data-addwin>Add another time window</button></div>
      <p class="error" role="alert" hidden></p>
      <div><button class="btn" type="submit">Save schedule</button></div></form>
    <form class="form" id="glob"><h3>For all cameras</h3>
      <label class="check"><input type="checkbox" name="verify" ${al.verify !== false ? 'checked' : ''}> Double-check with a second, larger model before alerting (fewer false alarms)</label>
      <label class="field">Time zone for schedules and alert times<select name="timezone">${tzs.map(t => `<option ${t === tz ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></label>
      <label class="field">One alert per visit<select name="group">${[[0, 'Off: alert for every camera'], [30, 'Same object on another camera within 30 seconds'],
        [90, 'Same object on another camera within 90 seconds'], [180, 'Same object on another camera within 3 minutes']].map(([v, t]) =>
        `<option value="${v}" ${(al.group_seconds ?? 90) === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <label class="field">Address of this app from outside, for the link in each alert
        <input type="url" name="public_url" value="${esc(al.public_url || '')}" placeholder="https://nvr.example.com" autocomplete="off"></label>
      <p class="error" role="alert" hidden></p>
      <div><button class="btn" type="submit">Save</button></div></form>
    ${Object.entries(raw.cameras || {}).map(([id, c]) => alertCard(id, c, defAlert, cfg)).join('')}`;
  const g = $('#glob', panel);
  g.onsubmit = async e => {
    e.preventDefault();
    try { await save('PUT', '/api/config/settings', {alerts: {verify: g.verify.checked, timezone: g.timezone.value,
      group_seconds: +g.group.value, public_url: g.public_url.value.trim().replace(/\/+$/, '')}}, 'Alert settings saved'); }
    catch (ex) { formError($('.error', g), ex); }
  };
  for (const f of $$('form[data-cam]', panel)) wireAlertCard(f, cfg);
  const au = $('#auto', panel), auSched = $('[data-g=sched]', au);
  chipGroup(au);
  au.mode.onchange = () => { auSched.hidden = au.mode.value !== 'on'; };
  auSched.addEventListener('click', e => {
    if (e.target.closest('[data-addwin]')) { e.target.closest('[data-addwin]').insertAdjacentHTML('beforebegin', schedRow({start: '09:00', end: '17:00'})); chipGroup(auSched); }
    const rm = e.target.closest('[data-rmwin]'); if (rm && $$('.win', auSched).length > 1) rm.closest('.win').remove();
  });
  au.onsubmit = async e => {
    e.preventDefault(); const err = $('.error', au); err.hidden = true;
    const wins = au.mode.value === 'on' ? $$('.win', auSched).map(w => ({days: picked(w), start: $('[data-k=start]', w).value, end: $('[data-k=end]', w).value})) : null;
    if (wins && wins.some(w => !w.days.length || !w.start || !w.end)) { formError(err, new Error('Each time window needs days and both times.')); return; }
    try { await save('PUT', '/api/config/settings', {alerts: {arm_schedule: wins}}, wins ? 'Schedule saved: alerts follow it from now' : 'Schedule turned off'); }
    catch (ex) { formError(err, ex); }
  };
  const po = $('#po', panel);
  const putToken = async token => {
    try { await api('/api/notify/settings', {method: 'PUT', body: {token}}); toast(token ? 'App token saved' : 'App token removed'); renderAlerts(panel); }
    catch (ex) { formError($('.error', po), ex); }
  };
  po.onsubmit = e => { e.preventDefault(); if (po.token.value.trim()) putToken(po.token.value.trim()); };
  const clr = $('#clear', po);
  if (clr) clr.onclick = () => confirm('Remove the app token? Alerts will only be logged until a new one is added.') && putToken('');
}

function alertCard(id, c, defAlert, cfg) {
  const a = {...defAlert, ...(c.alert || {})};
  const labels = (c.detect && c.detect.labels) || cfg.default_labels;
  const alertLabels = a.labels || ['person'];
  const zones = Object.keys(c.zones || {});
  const sched = a.schedule || [];
  const detOff = c.detect && c.detect.enabled === false;
  return `<form class="form" data-cam="${esc(id)}" style="border-top:1px solid var(--rule);padding-top:20px;max-width:none">
    <h3>${esc(c.name || id)}</h3>
    ${detOff ? '<p>Detection is off for this camera, so it never alerts. Turn it on under Cameras.</p>' : ''}
    <label class="check"><input type="checkbox" name="enabled" ${a.enabled !== false ? 'checked' : ''}> Send alerts from this camera</label>
    <div><h3 style="font-size:var(--step-0);font-weight:400;color:var(--mist)">Alert for</h3><div class="chips" data-g="labels">${objChips(labels, alertLabels)}</div></div>
    <div><h3 style="font-size:var(--step-0);font-weight:400;color:var(--mist)">Only when entering a zone</h3>
      ${zones.length ? `<div class="chips" data-g="zones">${zones.map(z => chip(z, (a.zones || []).includes(z), esc(z.replace(/_/g, ' ')))).join('')}</div>
        <p style="margin-top:6px">None picked: anywhere in view.</p>` : `<p>This camera has no zones. <a href="#/settings/zones/${esc(id)}">Draw zones</a> to alert only for certain areas.</p>`}</div>
    <div class="two" style="max-width:520px"><label class="field">Wait before alerting again for the same kind of object
      <select name="cooldown">${COOLDOWNS.map(([s, t]) => `<option value="${s}" ${s === (a.cooldown_seconds ?? 60) ? 'selected' : ''}>${t}</option>`).join('')}
        ${COOLDOWNS.some(([s]) => s === (a.cooldown_seconds ?? 60)) ? '' : `<option value="${a.cooldown_seconds}" selected>${a.cooldown_seconds} seconds</option>`}</select></label>
      <label class="field">When<select name="when"><option value="any">Any time</option><option value="sched" ${sched.length ? 'selected' : ''}>Only during set hours</option></select></label></div>
    <div data-g="sched" ${sched.length ? '' : 'hidden'}>${(sched.length ? sched : [{days: DAYS, start: '22:00', end: '06:30'}]).map(schedRow).join('')}
      <button type="button" class="btn quiet" data-addwin>Add another time window</button></div>
    <p class="error" role="alert" hidden></p>
    <div><button class="btn" type="submit">Save ${esc(c.name || id)}</button></div>
  </form>`;
}
const schedRow = w => `<div class="win" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px">
  <div class="chips">${DAYS.map(d => chip(d, (w.days || DAYS).includes(d), DAY_NAMES[d])).join('')}</div>
  <label class="muted">From <input type="time" value="${esc(w.start || '22:00')}" data-k="start" style="width:auto;margin-left:6px"></label>
  <label class="muted">to <input type="time" value="${esc(w.end || '06:30')}" data-k="end" style="width:auto;margin-left:6px"></label>
  <button type="button" class="btn quiet" data-rmwin aria-label="Remove this window">${ICON.close}</button></div>`;

function wireAlertCard(f, cfg) {
  const id = f.dataset.cam, err = $('.error', f);
  chipGroup(f);
  const schedEl = $('[data-g=sched]', f);
  f.when.onchange = () => { schedEl.hidden = f.when.value !== 'sched'; };
  schedEl.addEventListener('click', e => {
    if (e.target.closest('[data-addwin]')) { e.target.closest('[data-addwin]').insertAdjacentHTML('beforebegin', schedRow({})); chipGroup(schedEl); }
    const rm = e.target.closest('[data-rmwin]'); if (rm && $$('.win', schedEl).length > 1) rm.closest('.win').remove();
  });
  f.onsubmit = async e => {
    e.preventDefault(); err.hidden = true;
    const labels = picked($('[data-g=labels]', f));
    if (!labels.length && f.enabled.checked) { err.textContent = 'Pick at least one object to alert for, or turn alerts off for this camera.'; err.hidden = false; return; }
    const zEl = $('[data-g=zones]', f);
    const schedule = f.when.value === 'sched' ? $$('.win', schedEl).map(w => ({
      days: picked(w), start: $('[data-k=start]', w).value, end: $('[data-k=end]', w).value})) : null;
    if (schedule && schedule.some(w => !w.days.length || !w.start || !w.end)) { err.textContent = 'Each time window needs days and both times.'; err.hidden = false; return; }
    const body = {alert: {enabled: f.enabled.checked, labels: labels.length ? labels : ['person'],
      zones: zEl && picked(zEl).length ? picked(zEl) : null, cooldown_seconds: +f.cooldown.value, schedule}};
    try { await save('PUT', `/api/config/cameras/${encodeURIComponent(id)}`, body, 'Alert rules saved'); }
    catch (ex) { formError(err, ex); }
  };
}

// ---------- recording ----------
async function renderRecording(panel) {
  const cfg = await config(true);
  const st = cfg.raw.storage || {};
  let disk = null;
  try { disk = (await api('/api/status')).disk; } catch { /* shown without */ }
  panel.innerHTML = `<h2>Recording</h2>
    <p>Every camera with recording on is saved 24/7 in 1-minute files. Old recordings are deleted automatically.</p>
    <form class="form" id="recf">
      <label class="field">Keep 24/7 recordings for (days)<input type="number" name="days" min="1" max="3650" value="${esc(st.retention_days ?? 30)}"></label>
      <label class="field">Keep events, with their clips and snapshots, for (days)<input type="number" name="edays" min="1" max="3650" value="${esc(st.event_retention_days ?? 30)}"></label>
      <p>Each event's clip is saved separately as soon as it's recorded, so events can be kept longer than the 24/7 footage at a fraction of the space.</p>
      <label class="field">Delete the oldest early if the disk gets fuller than (%)<input type="number" name="pct" min="10" max="99" value="${esc(st.max_usage_percent ?? 90)}"></label>
      ${disk ? `<p>The disk is ${disk.percent}% full: ${GB(disk.used)} of ${GB(disk.total)}.</p>` : ''}
      <p class="error" role="alert" hidden></p>
      <div><button class="btn primary" type="submit">Save</button></div>
    </form>`;
  const f = $('#recf', panel);
  f.onsubmit = async e => {
    e.preventDefault();
    try { await save('PUT', '/api/config/settings', {storage: {retention_days: +f.days.value, max_usage_percent: +f.pct.value, event_retention_days: +f.edays.value}}, 'Recording settings saved'); }
    catch (ex) { formError($('.error', f), ex); }
  };
}

// ---------- detection feedback ----------
async function renderFeedback(panel, _r, setTimer) {
  let counts, misses;
  try { [counts, misses] = await Promise.all([api('/api/feedback'), api('/api/misses')]); } catch { return; }
  const total = counts.correct + counts.false_alarm + counts.wrong_label;
  panel.innerHTML = `<h2>Detection feedback</h2>
    <p>Your verdicts on events, and detections you report as missed, are how detection gets tuned to your cameras.
      Mark events from the Events page (open one: "Was this right?"). Report misses from Recordings.</p>
    <div class="rows">
      <div class="row"><div class="t"><b>${total} event${total === 1 ? '' : 's'} reviewed</b>
        <span>${counts.correct} correct, ${counts.false_alarm} false alarm${counts.false_alarm === 1 ? '' : 's'}, ${counts.wrong_label} wrong object</span></div>
        <a class="btn" href="#/events?review=unreviewed">Review events</a></div>
      <div class="row"><div class="t"><b>${counts.missed} missed detection${counts.missed === 1 ? '' : 's'} reported</b>
        <span>Each keeps 20 seconds of video, even after the 24/7 recording is deleted.</span></div>
        <a class="btn" href="#/recordings">Open recordings</a></div>
    </div>
    <form class="form" id="exp">
      <h3>Export a test set</h3>
      <p>Builds a zip of every reviewed event and reported miss: snapshots, alert pictures and small clips (960 px, 5 fps).
        No passwords or camera addresses are included. Attach it in the chat to have detection changes measured on your own footage.</p>
      <label class="check"><input type="checkbox" name="extra" checked> Also include the 50 most recent events you haven't reviewed</label>
      <p class="error" role="alert" hidden></p>
      <div style="display:flex;gap:12px;align-items:center"><button class="btn primary" type="submit">Build export</button><span id="expstate" class="muted"></span></div>
    </form>
    ${misses.length ? `<div><h3>Reported misses</h3><div class="rows">${misses.map(m => `<div class="row"><div class="t">
        <b>${esc(labelName(m.label))} on ${esc(camName(m.camera))}</b>
        <span>${new Date(m.ts * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'})}${m.note ? `. ${esc(m.note)}` : ''}${m.has_clip ? '' : '. Saving its clip…'}</span></div>
        <div style="display:flex;gap:6px">${m.has_clip ? `<a class="btn quiet" href="/media/missed/${esc(m.id)}.mp4" target="_blank" rel="noopener">${ICON.play}Play</a>` : ''}
          <button class="btn quiet" data-del="${esc(m.id)}" aria-label="Delete this report">${ICON.close}</button></div></div>`).join('')}</div></div>` : ''}`;
  for (const b of $$('[data-del]', panel)) b.onclick = async () => {
    await api(`/api/misses/${encodeURIComponent(b.dataset.del)}`, {method: 'DELETE'}).catch(e => toast(e.message));
    renderFeedback(panel, _r, setTimer);
  };
  const f = $('#exp', panel), st = $('#expstate', panel);
  const follow = job => {
    const tick = async () => {
      let j;
      try { j = await api(`/api/testset/${job.id}`); } catch { return; }
      if (!$('#expstate', panel)) return;
      if (j.state === 'running') { st.textContent = `Building… ${j.done} of ${j.total || '?'}`; return; }
      clearInterval(t);
      $('button', f).disabled = false;
      if (j.state === 'failed') { st.textContent = `Export failed: ${j.error}`; return; }
      st.innerHTML = `Ready: <a href="/media/testset/${esc(j.id)}.zip" download>${esc(j.file)}</a> (${(j.size / 1e6).toFixed(1)} MB)`;
    };
    const t = setInterval(tick, 1500); setTimer(t); tick();
  };
  f.onsubmit = async e => {
    e.preventDefault();
    $('button', f).disabled = true; st.textContent = 'Starting…';
    try { follow(await api('/api/testset', {method: 'POST', body: {include_unreviewed: f.extra.checked ? 50 : 0}})); }
    catch (ex) { $('button', f).disabled = false; formError($('.error', f), ex); }
  };
}

// ---------- people (admin) ----------
const ROLE_TEXT = {admin: 'Admin: everything, including settings and people',
                   member: 'Member: watch, review events, turn alerts on and off'};
const ago = ts => {
  if (!ts) return 'never';
  const m = Math.round((Date.now() / 1000 - ts) / 60);
  return m < 2 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
};
async function renderUsers(panel) {
  let users, sec;
  try { [users, sec] = await Promise.all([api('/api/users'), api('/api/security')]); } catch { return; }
  panel.innerHTML = `<h2>People</h2>
    <p>Everyone in the house can have their own sign-in. Members can watch, review events and turn alerts on and off;
      only admins can change settings or manage people.</p>
    <form class="form" id="policy"><label class="field">Two-factor sign-in is required for
      <select name="require">${[['admins', 'Admins (recommended at least)'], ['everyone', 'Everyone'], ['off', 'Nobody: it\'s optional']].map(([v, t]) =>
        `<option value="${v}" ${sec.require_2fa === v ? 'selected' : ''}>${t}</option>`).join('')}</select></label>
      <p class="hint muted">Anyone required who hasn't set it up is taken through setup right after their password, on their next visit.</p></form>
    <div class="rows">${users.map(u => `<div class="row"><div class="t"><b>${esc(u.username)}${u.username === state.user ? ' (you)' : ''}</b>
        <span>${u.role === 'admin' ? 'Admin' : 'Member'}. ${u.twofa ? 'Two-factor on' : u.twofa_required ? 'Two-factor required, set up at next visit' : 'Two-factor off'}.
          ${u.devices ? `Signed in on ${u.devices} device${u.devices > 1 ? 's' : ''}, last active ${ago(u.last_seen)}.` : 'Not signed in anywhere.'}</span></div>
        <button class="btn" data-edit="${esc(u.username)}">Manage</button></div>`).join('')}</div>
    <form class="form" id="adduser" autocomplete="off">
      <h3>Add a person</h3>
      <div class="two"><label class="field">Username<input type="text" name="username" required autocapitalize="none" spellcheck="false"></label>
        <label class="field">Starting password<input type="text" name="password" required minlength="10" autocomplete="off"></label></div>
      <label class="field">Role<select name="role"><option value="member">${ROLE_TEXT.member}</option><option value="admin">${ROLE_TEXT.admin}</option></select></label>
      <p>Give them the password in person; they can change it under Your account.</p>
      <p class="error" role="alert" hidden></p>
      <div><button class="btn primary" type="submit">Add person</button></div>
    </form>
    <dialog class="sheet" id="udlg" style="width:min(520px,94vw)"></dialog>`;
  const pol = $('#policy', panel);
  pol.require.onchange = async () => {
    if (pol.require.value === 'off' && !confirm('Make two-factor optional for everyone? Admin accounts could then be reached with a password alone.')) {
      pol.require.value = sec.require_2fa; return;
    }
    try { await api('/api/security', {method: 'PUT', body: {require_2fa: pol.require.value}}); toast('Saved'); renderUsers(panel); }
    catch (ex) { toast(ex.message); }
  };
  const f = $('#adduser', panel);
  f.onsubmit = async e => {
    e.preventDefault();
    try {
      await api('/api/users', {method: 'POST', body: {username: f.username.value.trim(), password: f.password.value, role: f.role.value}});
      toast(`${f.username.value.trim()} added`); renderUsers(panel);
    } catch (ex) { formError($('.error', f), ex); }
  };
  const dlg = $('#udlg', panel);
  for (const b of $$('[data-edit]', panel)) b.onclick = () => {
    const u = users.find(x => x.username === b.dataset.edit);
    dlg.innerHTML = `<form class="form" style="padding:20px" method="dialog">
      <h2 style="font-size:var(--step-1);margin:0">${esc(u.username)}</h2>
      <label class="field">Role<select name="role">${['member', 'admin'].map(r => `<option value="${r}" ${u.role === r ? 'selected' : ''}>${ROLE_TEXT[r]}</option>`).join('')}</select></label>
      <label class="field">Set a new password (optional)<input type="text" name="password" minlength="10" autocomplete="off" placeholder="leave empty to keep theirs"></label>
      ${u.twofa ? '<label class="check"><input type="checkbox" name="reset2fa"> Turn off their two-factor sign-in (lost phone). Signs them out everywhere.</label>' : ''}
      <p class="error" role="alert" hidden></p>
      <div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn primary" value="save">Save</button><button class="btn quiet" value="cancel">Cancel</button>
        <span style="flex:1"></span>${u.username !== state.user ? '<button class="btn" type="button" id="rm">Remove</button>' : ''}</div></form>`;
    const df = $('form', dlg), err = $('.error', df);
    df.onsubmit = async e => {
      if (e.submitter && e.submitter.value === 'cancel') return;
      e.preventDefault();
      const body = {};
      if (df.role.value !== u.role) body.role = df.role.value;
      if (df.password.value) body.password = df.password.value;
      if (df.reset2fa && df.reset2fa.checked) body.reset_twofa = true;
      try { if (Object.keys(body).length) await api(`/api/users/${encodeURIComponent(u.username)}`, {method: 'PUT', body});
        dlg.close(); toast('Saved'); renderUsers(panel); }
      catch (ex) { formError(err, ex); }
    };
    const rm = $('#rm', df);
    if (rm) rm.onclick = async () => {
      if (!confirm(`Remove ${u.username}? They're signed out immediately.`)) return;
      try { await api(`/api/users/${encodeURIComponent(u.username)}`, {method: 'DELETE'}); dlg.close(); renderUsers(panel); }
      catch (ex) { formError(err, ex); }
    };
    dlg.showModal();
  };
}

// ---------- your account ----------
function appSection() {
  const ios = /iPhone|iPad/.test(navigator.userAgent);
  const how = isInstalled() ? "<p><span class=\"dot ok\"></span>You're using the installed app.</p>"
    : installPrompt ? '<p>Install it to open BigBrother from your home screen, full screen, like any other app.</p><div><button class="btn primary" id="install">Install BigBrother</button></div>'
    : ios ? '<p>To get an app icon: in Safari, tap Share, then <b>Add to Home Screen</b>.</p>'
    : '<p>To get an app icon: in Chrome, open the ⋮ menu and choose <b>Install app</b> (or <b>Add to Home screen</b>).</p>';
  return `${how}<p class="hint muted">With "Keep me signed in", this device stays signed in as long as you open BigBrother at least once
    every 30 days. Two-factor codes are only asked for when signing in on a new device. Always use the same address
    (your public one) at home and away: each address signs in separately.</p>`;
}

function phoneAlerts(m) {
  const all = m.cameras == null;
  return `<form class="form" id="phone"><h3>Phone alerts</h3>
    ${m.ready ? '' : `<p><span class="dot warn"></span>${isAdmin() ? 'First add the Pushover app token under <a href="#/settings/alerts">Alerts</a>.'
      : 'An admin needs to finish setting up Pushover before alerts can reach your phone. You can add your key now.'}</p>`}
    <p>Install the Pushover app on your phone and sign in. Your user key is at the top of
      <a href="https://pushover.net" target="_blank" rel="noopener">pushover.net</a> once you're signed in.</p>
    <label class="field">Your Pushover user key<input type="text" name="key" value="${esc(m.pushover_user)}" autocomplete="off"
      spellcheck="false" autocapitalize="none" placeholder="30 letters and numbers"></label>
    <fieldset class="field"><legend>Cameras that alert me</legend>
      <label class="check"><input type="checkbox" name="all" ${all ? 'checked' : ''}> Every camera, including ones added later</label>
      <div class="chips" id="pcams" ${all ? 'hidden' : ''}>${state.cameras.map(c => chip(c.id, all || m.cameras.includes(c.id), esc(c.name))).join('')}</div>
    </fieldset>
    <label class="check"><input type="checkbox" name="health" ${m.health ? 'checked' : ''}> Also tell me when something's wrong (a camera stops recording, the disk is filling up)</label>
    <p class="hint muted">The lamp at the top still turns alerts on and off for everyone; the rules for each camera are under Alerts.</p>
    <p class="error" role="alert" hidden></p>
    <div style="display:flex;gap:10px;flex-wrap:wrap"><button class="btn primary" type="submit">Save</button>
      <button class="btn" type="button" id="ptest" ${m.pushover_user && m.ready ? '' : 'disabled'}>Send me a test</button></div></form>`;
}

function wirePhoneAlerts(f, m) {
  const err = $('.error', f), cams = $('#pcams', f);
  chipGroup(cams);
  f.all.onchange = () => { cams.hidden = f.all.checked; };
  f.onsubmit = async e => {
    e.preventDefault(); err.hidden = true;
    const body = {pushover_user: f.key.value.trim(), health: f.health.checked, cameras: f.all.checked ? null : picked(cams)};
    if (body.cameras && !body.cameras.length && body.pushover_user) { formError(err, new Error('Pick at least one camera, or remove your key to stop alerts.')); return; }
    try {
      const r = await api('/api/me/notify', {method: 'PUT', body});
      $('#ptest', f).disabled = !(r.pushover_user && r.ready);
      toast(r.pushover_user ? `Saved${r.note && r.note.startsWith('devices') ? ` (${r.note})` : ''}` : 'Saved. Alerts won\'t reach your phone without a key.', 4000);
    } catch (ex) { formError(err, ex); }
  };
  $('#ptest', f).onclick = async ev => {
    err.hidden = true; ev.target.disabled = true;
    try { await api('/api/me/notify/test', {method: 'POST'}); toast('Test sent. It should arrive in a few seconds.'); }
    catch (ex) { formError(err, ex); }
    finally { setTimeout(() => { ev.target.disabled = false; }, 10000); }
  };
}

async function renderAccount(panel) {
  let devices = [], mine = null;
  try { [devices, mine] = await Promise.all([api('/api/me/devices'), api('/api/me/notify')]); } catch { /* shown without */ }
  const ua = s => /iPhone|iPad/.test(s) ? 'iPhone or iPad' : /Android/.test(s) ? 'Android phone' : /Mac OS/.test(s) ? 'Mac'
    : /Windows/.test(s) ? 'Windows PC' : /Linux/.test(s) ? 'Linux computer' : 'Browser';
  panel.innerHTML = `<h2>Your account</h2>
    <p>Signed in as <b>${esc(state.user)}</b> (${state.role === 'admin' ? 'admin' : 'member'}).</p>
    ${mine ? phoneAlerts(mine) : ''}
    <section class="form" id="twofa">
      <h3>Two-factor sign-in</h3>
      ${state.twofa
        ? `<p><span class="dot ok"></span>On. Signing in on a new device asks for a code from your authenticator app.
             ${state.twofaRequired ? 'It\'s required for your account.' : ''}</p>
           <form class="form" id="move" style="gap:12px"><label class="field">New phone? Your password, to move two-factor to it
             <input type="password" name="password" autocomplete="current-password" required></label>
             <div><button class="btn" type="submit">Move to a new phone</button></div></form>
           ${state.twofaRequired ? '' : `<form class="form" id="off" style="gap:12px"><label class="field">Your password, to turn it off<input type="password" name="password" autocomplete="current-password" required></label>
             <p class="error" role="alert" hidden></p><div><button class="btn" type="submit">Turn off two-factor</button></div></form>`}`
        : `<p>Recommended now that BigBrother can be reached from the internet: even with your password, nobody can sign in without your phone.
             Works with any authenticator app (Google Authenticator, Microsoft Authenticator, 1Password, Authy…).</p>
           <div><button class="btn primary" id="on" type="button">Set up two-factor</button></div>`}
    </section>
    <form class="form" id="pw" autocomplete="on">
      <h3>Change password</h3>
      <input type="text" name="username" value="${esc(state.user)}" autocomplete="username" hidden>
      <label class="field">Current password<input type="password" name="current" autocomplete="current-password" required></label>
      <label class="field">New password<input type="password" name="new" autocomplete="new-password" required minlength="10"></label>
      <label class="field">Repeat new password<input type="password" name="new2" autocomplete="new-password" required></label>
      <p class="hint muted">At least 10 characters. Changing it signs you out on every other device.</p>
      <p class="error" role="alert" hidden></p>
      <div><button class="btn primary" type="submit">Change password</button></div>
    </form>
    <section id="app-sec"><h3>BigBrother on this device</h3>${appSection()}</section>
    <section><h3>Where you're signed in</h3>
      <div class="rows">${devices.map(d => `<div class="row"><div class="t"><b>${esc(ua(d.user_agent))}${d.current ? ' (this device)' : ''}</b>
        <span>Last active ${ago(d.last_seen)}${d.ip ? `, from ${esc(d.ip)}` : ''}</span></div><span></span></div>`).join('')}</div>
      <div style="display:flex;gap:10px;margin-top:12px;flex-wrap:wrap">
        ${devices.length > 1 ? '<button class="btn" id="others">Sign out other devices</button>' : ''}
        <button class="btn" id="logout">Sign out here</button></div></section>`;
  if (mine) wirePhoneAlerts($('#phone', panel), mine);
  const inst = $('#install', panel);
  if (inst) inst.onclick = async () => { if (await promptInstall()) toast('Installed: open BigBrother from your home screen'); renderAccount(panel); };
  const pw = $('#pw', panel), err = $('.error', pw);
  pw.onsubmit = async e => {
    e.preventDefault();
    const f = Object.fromEntries(new FormData(pw));
    err.hidden = true;
    if (f.new !== f.new2) { err.textContent = "The new passwords don't match."; err.hidden = false; return; }
    try { await api('/api/password', {method: 'POST', body: {current: f.current, new: f.new}}); pw.reset(); toast('Password changed'); }
    catch (ex) { formError(err, ex); }
  };
  const others = $('#others', panel);
  if (others) others.onclick = async () => { const r = await api('/api/me/devices/sign-out-others', {method: 'POST'});
    toast(`Signed out ${r.signed_out} other device${r.signed_out === 1 ? '' : 's'}`); renderAccount(panel); };
  $('#logout', panel).onclick = async () => { await api('/api/logout', {method: 'POST'}).catch(() => {}); location.hash = ''; location.reload(); };
  const off = $('#off', panel);
  if (off) off.onsubmit = async e => {
    e.preventDefault();
    try { await api('/api/me/2fa/disable', {method: 'POST', body: {password: off.password.value}}); state.twofa = false; toast('Two-factor sign-in is off'); renderAccount(panel); }
    catch (ex) { formError($('.error', off), ex); }
  };
  const sec = $('#twofa', panel);
  const run = async password => {
    const {enrol} = await import('./twofa.js');
    try { await enrol(sec, {password, heading: password ? 'Move two-factor to a new phone' : 'Set up two-factor sign-in',
                           onDone: () => { state.twofa = true; renderAccount(panel); }}); }
    catch (ex) { toast(ex.message); }
  };
  const on = $('#on', panel);
  if (on) on.onclick = () => run();
  const move = $('#move', panel);
  if (move) move.onsubmit = e => { e.preventDefault(); run(move.password.value); };
}
