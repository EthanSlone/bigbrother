import {$, $$, api, esc, state, go, ICON, labelName, timeOf, timeSec, toast} from './app.js';

const ZOOMS = [24, 6, 1];
let root, shell, timer;
let cam, dayStart, zoom = 24, playhead = null, tl = null;
let segs = [], si = -1, pendingOffset = 0, dragging = false;

const now = () => Date.now() / 1000;
const startOfDay = ts => { const d = new Date(ts * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000; };
const isoDay = ts => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };

export async function mount(view, route, sh) {
  root = view; shell = sh;
  root.innerHTML = `
    <div class="rec-bar">
      <select id="cam" aria-label="Camera">${state.cameras.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>
      <button class="btn quiet" id="prev" aria-label="Previous day">${ICON.back}</button>
      <input type="date" id="day" aria-label="Day">
      <button class="btn quiet" id="next" aria-label="Next day" style="transform:scaleX(-1)">${ICON.back}</button>
      <div class="seg" id="zoom" role="group" aria-label="Timeline zoom">${ZOOMS.map(z => `<button data-z="${z}" aria-pressed="${z === zoom}">${z} h</button>`).join('')}</div>
      <button class="btn" id="latest">Latest</button>
      <div style="flex:1"></div>
      <button class="btn" id="save" title="Download 5 minutes starting at the playhead">${ICON.download}Save 5 minutes</button>
    </div>
    <div class="player"><video id="v" playsinline controls></video><div class="msg" id="pmsg" hidden></div></div>
    <div class="clock"><b id="clock">–</b><span class="muted" id="clockday"></span></div>
    <div class="scrub" id="scrub" role="slider" tabindex="0" aria-label="Recording timeline"></div>
    <div class="axis" id="axis"></div>
    <section class="rec-events"><h2>Events in view</h2><div class="chips" id="evlist"></div></section>
    <div class="tip" id="tip" hidden></div>`;

  const q = route.query;
  cam = state.camById[q.get('cam')] ? q.get('cam') : (state.cameras[0] || {}).id;
  if (!cam) { root.innerHTML = '<div class="empty"><h2>No cameras yet</h2>Add cameras under Settings.</div>'; return; }
  const t = Number(q.get('t')) || null;
  dayStart = startOfDay(t || now());
  $('#cam', root).value = cam;
  $('#day', root).value = isoDay(dayStart);
  $('#day', root).max = isoDay(now());

  $('#cam', root).onchange = e => { cam = e.target.value; syncUrl(); reload(playhead); };
  $('#day', root).onchange = e => { const [y, m, d] = e.target.value.split('-').map(Number);
    if (!y) return; dayStart = new Date(y, m - 1, d).getTime() / 1000; syncUrl(); reload(null); };
  $('#prev', root).onclick = () => shiftDay(-1);
  $('#next', root).onclick = () => shiftDay(1);
  $('#latest', root).onclick = () => { dayStart = startOfDay(now()); $('#day', root).value = isoDay(dayStart); reload(null, true); };
  for (const b of $$('#zoom button', root)) b.onclick = () => { zoom = +b.dataset.z;
    for (const x of $$('#zoom button', root)) x.setAttribute('aria-pressed', x === b); draw(); };
  $('#save', root).onclick = save;

  const v = $('#v', root);
  v.addEventListener('loadedmetadata', () => {
    if (pendingOffset) { v.currentTime = Math.min(pendingOffset, Math.max(0, v.duration - 0.3)); pendingOffset = 0; }
  });
  v.addEventListener('ended', nextSegment);
  v.addEventListener('timeupdate', () => {
    if (!root || si < 0 || dragging) return;
    playhead = segs[si].start + v.currentTime;
    if (playhead >= dayStart + 86400) { dayStart = startOfDay(playhead); $('#day', root).value = isoDay(dayStart); loadTimeline(); }
    drawHead();
  });
  v.addEventListener('error', () => { if (si >= 0) nextSegment(); });

  wireScrub();
  await reload(t, !t);
  timer = setInterval(() => { if (!document.hidden && dayStart === startOfDay(now())) loadTimeline(); }, 30000);
}

export function update(route) {
  const q = route.query, t = Number(q.get('t')) || null, c = q.get('cam');
  if (c && state.camById[c] && (c !== cam || t)) {
    cam = c; $('#cam', root).value = cam;
    if (t) { dayStart = startOfDay(t); $('#day', root).value = isoDay(dayStart); }
    reload(t);
  }
}

export function unmount() {
  clearInterval(timer);
  const v = root && $('#v', root);
  if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
  root = null;
}

function syncUrl() {
  history.replaceState(null, '', `#/recordings?cam=${encodeURIComponent(cam)}`);
}

function shiftDay(n) {
  const d = new Date(dayStart * 1000); d.setDate(d.getDate() + n);
  const s = d.getTime() / 1000;
  if (s > now()) return;
  dayStart = s; $('#day', root).value = isoDay(dayStart); reload(null);
}

async function reload(t, latest = false) {
  segs = []; si = -1;
  await loadTimeline();
  if (!tl) return;
  if (t && t >= dayStart && t < dayStart + 86400) return seek(t, true);
  if (latest || !tl.recorded.length) {
    const last = tl.recorded.length ? tl.recorded[tl.recorded.length - 1][1] : null;
    if (last) return seek(Math.max(tl.recorded[tl.recorded.length - 1][0], last - 30), false);
  } else if (tl.recorded.length) {
    return seek(tl.recorded[0][0], false);
  }
  playhead = null;
  showMsg(tl.recorded.length ? '' : 'Nothing was recorded on this camera that day.');
  draw();
}

async function loadTimeline() {
  try {
    tl = await api(`/api/timeline?camera=${encodeURIComponent(cam)}&start=${dayStart}&end=${dayStart + 86400}`);
  } catch (e) { if (e.status !== 401) toast(e.message); tl = null; return; }
  draw();
}

function win() {
  const span = zoom * 3600;
  if (zoom === 24 || playhead == null) return [dayStart, dayStart + span];
  let a = playhead - span / 2;
  a = Math.max(dayStart, Math.min(a, dayStart + 86400 - span));
  return [a, a + span];
}

function draw() {
  if (!root || !tl) return;
  const [a, b] = win(), pct = t => ((t - a) / (b - a) * 100);
  const clip = (s, e) => [Math.max(s, a), Math.min(e, b)];
  let html = '';
  for (const [s, e] of tl.recorded) {
    const [x, y] = clip(s, e); if (y <= x) continue;
    html += `<i class="rec" style="left:${pct(x)}%;width:${Math.max(0.12, pct(y) - pct(x))}%"></i>`;
  }
  const inView = tl.events.filter(e => e.start < b && (e.end ?? now()) > a);
  for (const e of inView) {
    const [x, y] = clip(e.start, e.end ?? now());
    html += `<button class="tick k-${esc(e.label)}" data-t="${e.start}" tabindex="-1" aria-label="${esc(labelName(e.label))} at ${timeOf(e.start)}"
      style="left:${pct(x)}%;width:${Math.max(0, pct(y) - pct(x))}%"></button>`;
  }
  html += '<i class="hover" hidden></i><i class="head" hidden></i>';
  $('#scrub', root).innerHTML = html;

  const step = zoom === 24 ? 3 * 3600 : zoom === 6 ? 3600 : 600;
  let ticks = '';
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) {
    const p = pct(t); if (p < 3 || p > 97) continue;
    ticks += `<span style="left:${p}%">${timeOf(t)}</span>`;
  }
  $('#axis', root).innerHTML = ticks;
  $('#evlist', root).innerHTML = inView.length ? inView.slice(0, 60).map(e =>
    `<button class="chip k-${esc(e.label)}" data-t="${e.start}"><span class="kind">${esc(labelName(e.label))}</span> ${timeOf(e.start)}</button>`).join('')
    : '<span class="muted">No events in this stretch.</span>';
  for (const c of $$('#evlist .chip', root)) c.onclick = () => seek(+c.dataset.t - 3, true);
  drawHead();
}

function drawHead() {
  if (!root) return;
  const head = $('#scrub .head', root);
  if (!head) return;
  const [a, b] = win();
  if (playhead == null || playhead < a || playhead > b) {
    head.hidden = true;
    if (playhead != null && zoom !== 24) draw();
  } else {
    head.hidden = false;
    head.style.left = `${(playhead - a) / (b - a) * 100}%`;
  }
  $('#clock', root).textContent = playhead == null ? '–' : timeSec(playhead);
  $('#clockday', root).textContent = playhead == null ? '' :
    new Date(playhead * 1000).toLocaleDateString([], {weekday: 'long', month: 'long', day: 'numeric'});
  const s = $('#scrub', root);
  if (playhead != null) s.setAttribute('aria-valuetext', timeSec(playhead));
}

function timeAt(x) {
  const r = $('#scrub', root).getBoundingClientRect(), [a, b] = win();
  return a + Math.min(1, Math.max(0, (x - r.left) / r.width)) * (b - a);
}

function wireScrub() {
  const s = $('#scrub', root), tip = $('#tip', root);
  s.addEventListener('pointerdown', e => {
    dragging = true; s.setPointerCapture(e.pointerId);
    const t = e.target.dataset.t ? +e.target.dataset.t - 3 : timeAt(e.clientX);
    playhead = t; drawHead();
  });
  s.addEventListener('pointermove', e => {
    const t = timeAt(e.clientX), hov = $('.hover', s), r = s.getBoundingClientRect();
    hov.hidden = false; hov.style.left = `${e.clientX - r.left}px`;
    tip.hidden = false; tip.textContent = e.target.dataset.t ? `${labelName((e.target.className.match(/k-(\w+)/) || [])[1] || '')} ${timeOf(+e.target.dataset.t)}` : timeSec(t);
    tip.style.left = `${e.clientX + 12}px`; tip.style.top = `${r.top - 30}px`;
    if (dragging) { playhead = t; drawHead(); }
  });
  s.addEventListener('pointerleave', () => { tip.hidden = true; const h = $('.hover', s); if (h) h.hidden = true; });
  const end = () => { if (!dragging) return; dragging = false; seek(playhead, true); };
  s.addEventListener('pointerup', end);
  s.addEventListener('pointercancel', end);
  s.addEventListener('keydown', e => {
    const step = e.shiftKey ? 600 : 10;
    if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      e.preventDefault();
      seek((playhead ?? dayStart) + (e.key === 'ArrowRight' ? step : -step), true);
    }
  });
}

function showMsg(text) {
  const m = $('#pmsg', root);
  m.textContent = text; m.hidden = !text;
}

async function seek(t, play) {
  playhead = t; drawHead();
  const v = $('#v', root);
  let got;
  try { got = await api(`/api/segments?camera=${encodeURIComponent(cam)}&start=${t - 180}&end=${t + 6 * 3600}`); }
  catch (e) { if (e.status !== 401) toast(e.message); return; }
  segs = got;
  si = segs.findIndex(s => s.end > t);
  if (si < 0) {
    v.removeAttribute('src'); v.load();
    showMsg(t > now() - 120 ? "That's the latest minute: it's still being recorded. It'll be ready shortly."
                            : 'Nothing was recorded at that time.');
    return;
  }
  showMsg('');
  pendingOffset = Math.max(0, t - segs[si].start);
  if (segs[si].start > t) playhead = segs[si].start;   // jumped over a gap
  v.src = `/media/recording/${segs[si].id}.mp4`;
  if (play) v.play().catch(() => {});
  draw();
}

async function nextSegment() {
  if (!root) return;
  const v = $('#v', root);
  if (si + 1 < segs.length) {
    si++; pendingOffset = 0;
    v.src = `/media/recording/${segs[si].id}.mp4`;
    v.play().catch(() => {});
    return;
  }
  // Reached the end of what we fetched: look for newer segments (live edge).
  const after = si >= 0 ? segs[si].end : playhead;
  try {
    const more = await api(`/api/segments?camera=${encodeURIComponent(cam)}&start=${after - 1}&end=${after + 6 * 3600}`);
    const fresh = more.filter(s => s.start >= after - 1 && (si < 0 || s.id !== segs[si].id));
    if (fresh.length) { segs = fresh; si = 0; pendingOffset = 0; v.src = `/media/recording/${segs[0].id}.mp4`; v.play().catch(() => {}); return; }
  } catch { /* fall through */ }
  showMsg('Caught up with the recording. The newest minute appears once it has been saved.');
}

function save() {
  if (playhead == null) { toast('Pick a moment on the timeline first.'); return; }
  const start = Math.floor(playhead), end = start + 300;
  const a = document.createElement('a');
  a.href = `/media/export.mp4?camera=${encodeURIComponent(cam)}&start=${start}&end=${end}`;
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('Preparing the video. The download starts in a few seconds.');
}
