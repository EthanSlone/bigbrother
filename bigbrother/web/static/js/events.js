import {$, $$, api, esc, state, camName, go, parseHash, isAdmin, ICON, labelName, timeOf, timeSec, dayName, duration, ALERT_TEXT, toast, showNew} from './app.js';

const PAGE = 40;
let root = null, shell = null, timer = null;
let filters = {cameras: new Set(), labels: new Set(), alert: '', day: '', review: ''};
let items = [], done = false, loading = false;
let seenBefore = Infinity;   // events after this are new to you (marked until you leave the page)

export async function mount(view, route, sh) {
  root = view; shell = sh;
  root.innerHTML = `<div class="events-layout">
    <aside class="filters" aria-label="Filters">
      <div><h3>Cameras</h3><div class="chips" id="f-cam"></div></div>
      <div><h3>Objects</h3><div class="chips" id="f-label"></div></div>
      <div><h3>Alerts</h3><div class="seg" id="f-alert">
        <button data-v="" aria-pressed="true">All</button><button data-v="notified" aria-pressed="false">Alerted</button></div></div>
      <div><h3>Your review</h3><div class="seg" id="f-review">
        <button data-v="" aria-pressed="true">All</button><button data-v="unreviewed" aria-pressed="false">To review</button><button data-v="false_alarm" aria-pressed="false">False alarms</button><button data-v="kept" aria-pressed="false">Kept</button></div></div>
      <div><h3>Day</h3><input type="date" id="f-day" aria-label="Show events from this day"></div>
      <button class="btn quiet" id="f-clear" hidden>Clear filters</button>
    </aside>
    <section><div id="feed"></div><div class="more"><button class="btn" id="more" hidden>Show older events</button></div></section>
  </div>
  <dialog class="sheet" id="sheet" aria-labelledby="sheet-title"></dialog>`;

  const labels = [...new Set(state.cameras.flatMap(c => c.labels))];
  $('#f-cam', root).innerHTML = state.cameras.map(c =>
    `<button class="chip" data-v="${esc(c.id)}" aria-pressed="false">${esc(c.name)}</button>`).join('');
  $('#f-label', root).innerHTML = labels.map(l =>
    `<button class="chip k-${esc(l)}" data-v="${esc(l)}" aria-pressed="false"><span class="kind">${esc(labelName(l))}</span></button>`).join('');
  readFilters(route.query);

  const toggle = (set, b) => { set.has(b.dataset.v) ? set.delete(b.dataset.v) : set.add(b.dataset.v); apply(); };
  for (const b of $$('#f-cam .chip', root)) b.onclick = () => toggle(filters.cameras, b);
  for (const b of $$('#f-label .chip', root)) b.onclick = () => toggle(filters.labels, b);
  for (const b of $$('#f-alert button', root)) b.onclick = () => { filters.alert = b.dataset.v; apply(); };
  for (const b of $$('#f-review button', root)) b.onclick = () => { filters.review = b.dataset.v; apply(); };
  shell.tools.innerHTML = isAdmin() ? '<a class="btn quiet" href="#/settings/feedback">Detection feedback</a>' : '';
  $('#f-day', root).onchange = e => { filters.day = e.target.value; apply(); };
  $('#f-clear', root).onclick = () => { filters = {cameras: new Set(), labels: new Set(), alert: '', day: '', review: ''}; apply(); };
  $('#more', root).onclick = () => load(false);
  $('#feed', root).addEventListener('click', e => {
    const row = e.target.closest('[data-ev]');
    if (row) go(`#/events/${row.dataset.ev}${location.hash.includes('?') ? '?' + location.hash.split('?')[1] : ''}`);
  });
  const sheet = $('#sheet', root);
  sheet.addEventListener('close', () => {
    const v = $('video', sheet); if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    if (parseHash().parts[0]) go('#/events' + query(), true);
  });
  sheet.addEventListener('click', e => { if (e.target === sheet) sheet.close(); });

  syncControls();
  try { const r = await api('/api/me/seen', {method: 'POST', body: {ts: Date.now() / 1000}}); seenBefore = r.previous ?? Infinity; }
  catch { /* still usable */ }
  showNew(0);
  await load(true);
  openFromRoute(route);
  timer = setInterval(() => { if (!document.hidden) poll(); }, 8000);
}

export function update(route) { openFromRoute(route); }

export function unmount() { clearInterval(timer); root = null; }

function readFilters(q) {
  filters.cameras = new Set((q.get('camera') || '').split(',').filter(Boolean));
  filters.labels = new Set((q.get('label') || '').split(',').filter(Boolean));
  filters.alert = q.get('alert') || '';
  filters.day = q.get('day') || '';
  filters.review = q.get('review') || '';
}

function query() {
  const q = new URLSearchParams();
  if (filters.cameras.size) q.set('camera', [...filters.cameras].join(','));
  if (filters.labels.size) q.set('label', [...filters.labels].join(','));
  if (filters.alert) q.set('alert', filters.alert);
  if (filters.day) q.set('day', filters.day);
  if (filters.review) q.set('review', filters.review);
  const s = q.toString();
  return s ? '?' + s : '';
}

function apiQuery(extra = {}) {
  const q = new URLSearchParams();
  if (filters.cameras.size) q.set('camera', [...filters.cameras].join(','));
  if (filters.labels.size) q.set('label', [...filters.labels].join(','));
  if (filters.alert) q.set('alert', filters.alert);
  if (filters.review) q.set('review', filters.review);
  if (filters.day) {
    const [y, m, d] = filters.day.split('-').map(Number);
    const start = new Date(y, m - 1, d).getTime() / 1000;
    q.set('until', start + 86400);
    q.set('since', start);
  }
  for (const [k, v] of Object.entries(extra)) q.set(k, v);
  return q.toString();
}

function syncControls() {
  for (const b of $$('#f-cam .chip', root)) b.setAttribute('aria-pressed', filters.cameras.has(b.dataset.v));
  for (const b of $$('#f-label .chip', root)) b.setAttribute('aria-pressed', filters.labels.has(b.dataset.v));
  for (const b of $$('#f-alert button', root)) b.setAttribute('aria-pressed', b.dataset.v === filters.alert);
  for (const b of $$('#f-review button', root)) b.setAttribute('aria-pressed', b.dataset.v === filters.review);
  $('#f-day', root).value = filters.day;
  $('#f-clear', root).hidden = !(filters.cameras.size || filters.labels.size || filters.alert || filters.day || filters.review);
}

function apply() {
  syncControls();
  history.replaceState(null, '', '#/events' + query());
  load(true);
}

async function load(reset) {
  if (loading) return;
  loading = true;
  try {
    if (reset) { items = []; done = false; }
    const extra = {limit: PAGE};
    if (items.length) extra.before = items[items.length - 1].start_ts;
    const got = await api('/api/events?' + apiQuery(extra));
    items = items.concat(got);
    done = got.length < PAGE;
    draw();
  } catch (e) { if (e.status !== 401) toast(e.message); }
  finally { loading = false; }
}

async function poll() {
  if (!root) return;
  // Refresh the newest page in place: new events appear, ongoing ones update.
  try {
    const fresh = await api('/api/events?' + apiQuery({limit: PAGE}));
    const ids = new Set(fresh.map(e => e.id));
    const oldest = fresh.length ? fresh[fresh.length - 1].start_ts : Infinity;
    items = fresh.concat(items.filter(e => !ids.has(e.id) && e.start_ts < oldest));
    draw();
    if (fresh.length) api('/api/me/seen', {method: 'POST', body: {ts: Date.now() / 1000}}).catch(() => {});
  } catch { /* offline for a moment */ }
}

function alertBadge(a) {
  if (!a) return '';
  const cls = {sent: 'sent', logged: 'logged', failed: 'failed', grouped: 'logged'}[a.status] || '';
  return `<span class="badge ${cls}">${esc(ALERT_TEXT[a.status] || a.status)}</span>`;
}

const VERDICT_TEXT = {correct: 'Marked correct', false_alarm: 'False alarm', wrong_label: 'Wrong object'};
function verdictBadge(f) {
  if (!f) return '';
  const extra = f.verdict === 'wrong_label' && f.label ? `: ${labelName(f.label)}` : '';
  return `<span class="badge ${f.verdict === 'correct' ? 'ok' : 'failed'}">${esc(VERDICT_TEXT[f.verdict])}${esc(extra)}</span>`;
}

function draw() {
  if (!root) return;
  const feed = $('#feed', root);
  if (!feed) return;
  if (!items.length) {
    const filtered = filters.cameras.size || filters.labels.size || filters.alert || filters.day || filters.review;
    feed.innerHTML = `<div class="empty"><h2>${filtered ? 'No events match these filters' : 'No events yet'}</h2>
      ${filtered ? 'Try another day, or clear the filters.' : 'When a camera sees a person, vehicle or animal, it shows up here with a snapshot and a clip.'}</div>`;
    $('#more', root).hidden = true;
    return;
  }
  let html = '', lastDay = '';
  for (const e of items) {
    const day = dayName(e.start_ts);
    if (day !== lastDay) { html += `${lastDay ? '</div>' : ''}<h2 class="day">${esc(day)}</h2><div class="feed">`; lastDay = day; }
    const zones = e.zones.length ? `, ${e.zones.map(z => esc(z.replace(/_/g, ' '))).join(', ')}` : '';
    const fresh = e.start_ts > seenBefore;
    html += `<button class="ev k-${esc(e.label)}${fresh ? ' new' : ''}" data-ev="${esc(e.id)}">
      ${e.has_snapshot ? `<img src="/media/snapshot/${esc(e.id)}.jpg?w=400&u=${e.updated_ts}" alt="" loading="lazy">` : '<img alt="">'}
      <span class="what"><span class="kind">${esc(labelName(e.label))}${fresh ? '<span class="newdot">New</span>' : ''}${e.kept ? `<span class="kept" title="Kept">${ICON.star}</span>` : ''}</span>
        <span class="where">${esc(camName(e.camera))}${zones}</span></span>
      <span class="when"><b>${timeOf(e.start_ts)}</b>
        ${e.end_ts ? `<span>${esc(duration(e.start_ts, e.end_ts))}</span>` : '<span class="badge live">In view now</span>'}
        ${alertBadge(e.alert)}${verdictBadge(e.feedback)}</span>
    </button>`;
  }
  feed.innerHTML = html + '</div>';
  $('#more', root).hidden = done;
}

async function openFromRoute(route) {
  const id = route.parts[0];
  const sheet = $('#sheet', root);
  if (!id) { if (sheet.open) sheet.close(); return; }
  let e;
  try { e = await api(`/api/events/${encodeURIComponent(id)}`); }
  catch { toast('That event no longer exists. Old events are removed with their recordings.'); go('#/events', true); return; }
  for (const r of $$('.ev', root)) r.setAttribute('aria-selected', r.dataset.ev === id);
  const a = e.alert;
  sheet.innerHTML = `<div class="hd"><h2 id="sheet-title"><span class="kind k-${esc(e.label)}">${esc(labelName(e.label))}</span></h2>
      <span class="muted">${esc(camName(e.camera))}</span><div class="grow"></div>
      <a class="btn quiet" href="#/recordings?cam=${encodeURIComponent(e.camera)}&t=${Math.floor(e.start_ts - 5)}">${ICON.recordings}Recordings</a>
      <button class="btn quiet" id="keep" aria-pressed="${!!e.kept}" title="Kept events stay after old recordings are removed">${ICON.star}<span>${e.kept ? 'Kept' : 'Keep'}</span></button>
      <a class="btn quiet" id="dl" href="/media/clip/${esc(e.id)}.mp4" download="${esc(e.camera)}-${esc(e.id)}.mp4">${ICON.download}Save clip</a>
      ${isAdmin() ? `<button class="btn quiet" id="del" title="Delete this event">${ICON.trash}<span class="sr">Delete</span></button>` : ''}
      <button class="btn quiet" id="x" aria-label="Close">${ICON.close}</button></div>
    <video controls playsinline ${e.has_snapshot ? `poster="/media/snapshot/${esc(e.id)}.jpg?u=${e.updated_ts}"` : ''}></video>
    <p class="note" id="clipnote" hidden></p>
    <div class="review" id="review"></div>
    <div class="facts">
      <div>When<b>${esc(dayName(e.start_ts))}, ${timeSec(e.start_ts)}</b></div>
      <div>How long<b>${e.end_ts ? esc(duration(e.start_ts, e.end_ts)) : 'Still in view'}</b></div>
      <div>Confidence<b>${Math.round(e.median_score * 100)}% typical, ${Math.round(e.top_score * 100)}% best</b></div>
      ${e.zones.length ? `<div>Zones<b>${e.zones.map(z => esc(z.replace(/_/g, ' '))).join(', ')}</b></div>` : ''}
      ${e.stationary ? '<div>Movement<b>Stationary</b></div>' : ''}
      <div>Alert<b>${a ? esc(ALERT_TEXT[a.status] || a.status) : 'None'}${a && a.reason && a.status !== 'sent' ? `: ${esc(a.reason)}` : ''}</b></div>
      ${a && a.verify_score != null ? `<div>Second opinion<b>${Math.round(a.verify_score * 100)}%</b></div>` : ''}
      ${e.kept ? `<div>Kept<b>by ${esc(e.kept.by)}, ${esc(dayName(e.kept.ts))}</b></div>` : ''}
    </div>`;
  $('#x', sheet).onclick = () => sheet.close();
  $('#keep', sheet).onclick = async ev => {
    const btn = ev.currentTarget, keep = btn.getAttribute('aria-pressed') !== 'true';
    try {
      await api(`/api/events/${encodeURIComponent(e.id)}/keep`, {method: 'PUT', body: {keep}});
      btn.setAttribute('aria-pressed', keep); $('span', btn).textContent = keep ? 'Kept' : 'Keep';
      const it = items.find(x => x.id === e.id); if (it) it.kept = keep ? {by: state.user, ts: Date.now() / 1000} : null;
      draw();
      toast(keep ? 'Kept: this event stays until you remove the star.' : 'No longer kept: it goes with old recordings.', 3500);
    } catch (ex) { toast(ex.message); }
  };
  const del = $('#del', sheet);
  if (del) del.onclick = async () => {
    if (!confirm(`Delete this ${labelName(e.label).toLowerCase()} event from ${camName(e.camera)}? Its snapshot and saved clip go too. The recording itself stays.`)) return;
    try {
      await api(`/api/events/${encodeURIComponent(e.id)}`, {method: 'DELETE'});
      items = items.filter(x => x.id !== e.id); draw();
      sheet.close(); toast('Event deleted');
    } catch (ex) { toast(ex.message); }
  };
  drawReview($('#review', sheet), e);
  if (!sheet.open) sheet.showModal();
  const video = $('video', sheet), note = $('#clipnote', sheet), dl = $('#dl', sheet);
  // Keep checking until the clip exists: recordings are saved a minute at a time.
  const tryClip = async () => {
    if (!sheet.open || parseHash().parts[0] !== e.id) return;
    let r;
    try { r = await fetch(`/media/clip/${encodeURIComponent(e.id)}.mp4`, {method: 'HEAD', cache: 'no-store'}); }
    catch { r = null; }
    if (!r || !r.ok) {
      video.controls = false; dl.hidden = true;
      note.textContent = r && r.status === 404
        ? "This event's recording isn't saved yet: recordings are written a minute at a time. The clip appears here as soon as it's ready."
        : "The clip couldn't be prepared. Retrying…";
      note.hidden = false;
      setTimeout(tryClip, 10000);
      return;
    }
    video.controls = true; dl.hidden = false;
    const partial = r.headers.get('X-Clip-Partial') === '1';
    note.textContent = partial ? "This event is still going or its last minute isn't saved yet, so the clip may stop early." : '';
    note.hidden = !partial;
    video.src = `/media/clip/${encodeURIComponent(e.id)}.mp4?v=${Date.now()}`;
    video.play().catch(() => {});
  };
  tryClip();
}

const WRONG_CHOICES = ['person', 'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'dog', 'cat', 'bird', 'other'];
function drawReview(el, e) {
  const f = e.feedback;
  el.innerHTML = `<span class="q">Was this right?</span>
    <button class="btn" data-v="correct" aria-pressed="${f && f.verdict === 'correct'}">Correct</button>
    <button class="btn" data-v="false_alarm" aria-pressed="${f && f.verdict === 'false_alarm'}">False alarm</button>
    <label class="wrong">Wrong object: it was
      <select aria-label="What it really was"><option value="">choose…</option>${WRONG_CHOICES.filter(l => l !== e.label).map(l =>
        `<option value="${l}" ${f && f.verdict === 'wrong_label' && f.label === l ? 'selected' : ''}>${l === 'other' ? 'something else' : esc(labelName(l).toLowerCase())}</option>`).join('')}</select></label>
    ${f ? '<button class="btn quiet" data-v="">Clear</button>' : ''}`;
  const send = async (verdict, label) => {
    try {
      if (!verdict) await api(`/api/events/${encodeURIComponent(e.id)}/feedback`, {method: 'DELETE'});
      else await api(`/api/events/${encodeURIComponent(e.id)}/feedback`, {method: 'PUT', body: {verdict, label}});
      e.feedback = verdict ? {verdict, label} : null;
      const it = items.find(x => x.id === e.id); if (it) it.feedback = e.feedback;
      drawReview(el, e); draw();
      toast(verdict ? 'Thanks. It\'s saved for tuning detection.' : 'Cleared');
    } catch (ex) { toast(ex.message); }
  };
  for (const b of $$('button[data-v]', el)) b.onclick = () => send(b.dataset.v);
  $('select', el).onchange = ev => { if (ev.target.value) send('wrong_label', ev.target.value); };
}
