import {$, $$, api, esc, state, go, ICON, labelName, timeOf, timeSec, toast, camName} from './app.js';
import {MultiView} from './multiview.js';

const ALL = 'all';   // every camera on one timeline
let multi = null;
const remember = v => { try { localStorage.setItem('bb.rec.cam', v); } catch { /* private mode */ } };
const remembered = () => { try { return localStorage.getItem('bb.rec.cam'); } catch { return null; } };

const ZOOMS = [24, 6, 1];
let root, shell, timer;
let cam, dayStart, zoom = 24, playhead = null, tl = null;
let segs = [], si = -1, pendingOffset = 0, dragging = false;

const now = () => Date.now() / 1000;
const startOfDay = ts => { const d = new Date(ts * 1000); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000; };
const isoDay = ts => { const d = new Date(ts * 1000); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };

export async function mount(view, route, sh) {
  if (multi) { multi.destroy(); multi = null; }   // left over from a mount that was cut short
  root = view; shell = sh;
  root.innerHTML = `
    <div class="rec-bar">
      <select id="cam" aria-label="Camera">${state.cameras.length > 1 ? `<option value="${ALL}">All cameras</option>` : ''}${state.cameras.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('')}</select>
      <span class="daynav"><button class="btn quiet" id="prev" aria-label="Previous day">${ICON.back}</button>
      <input type="date" id="day" aria-label="Day">
      <button class="btn quiet" id="next" aria-label="Next day" style="transform:scaleX(-1)">${ICON.back}</button></span>
      <div class="seg" id="zoom" role="group" aria-label="Timeline zoom">${ZOOMS.map(z => `<button data-z="${z}" aria-pressed="${z === zoom}">${z} h</button>`).join('')}</div>
      <button class="btn" id="latest">Latest</button>
      <div class="spacer"></div>
      <button class="btn" id="miss" title="Something was in view here but wasn't detected">Report a missed detection</button>
      <button class="btn" id="save" title="Download 5 minutes starting at the playhead">${ICON.download}Save 5 minutes</button>
    </div>
    <dialog class="sheet" id="missdlg" style="width:min(460px,94vw)" aria-labelledby="miss-title"><form class="form" style="padding:18px" method="dialog">
      <h2 id="miss-title" style="font-size:var(--step-1);margin:0">Report a missed detection</h2>
      <p id="miss-when"></p>
      <label class="field">What was missed<select name="label">${['person', 'car', 'truck', 'motorcycle', 'bicycle', 'dog', 'cat', 'other']
        .map(l => `<option value="${l}">${l === 'other' ? 'Something else' : esc(labelName(l))}</option>`).join('')}</select></label>
      <label class="field">Note (optional)<input type="text" name="note" maxlength="300" placeholder="e.g. walked along the fence in the dark"></label>
      <div style="display:flex;gap:10px"><button class="btn primary" value="send">Report it</button><button class="btn quiet" value="cancel">Cancel</button></div>
    </form></dialog>
    <div class="player"><video id="v" playsinline controls></video><div class="multi" id="multi" hidden></div><div class="msg" id="pmsg" hidden></div></div>
    <div class="mctl" id="mctl" hidden><button class="btn" id="mplay" aria-pressed="false">${ICON.play}<span>Play</span></button>
      <div class="seg" id="mrate" role="group" aria-label="Speed">${[1, 2, 4, 8].map(r => `<button data-r="${r}" aria-pressed="${r === 1}">${r}×</button>`).join('')}</div>
      <span class="muted">Select a camera to watch it on its own.</span></div>
    <div class="clock"><b id="clock">–</b><span class="muted" id="clockday"></span></div>
    <div class="scrub" id="scrub" role="slider" tabindex="0" aria-label="Recording timeline"></div>
    <div class="axis" id="axis"></div>
    <section class="rec-events"><h2>Events in view</h2><div class="chips" id="evlist"></div></section>
    <div class="tip" id="tip" hidden></div>`;

  const q = route.query;
  const okCam = c => c && (state.camById[c] || (c === ALL && state.cameras.length > 1));
  cam = okCam(q.get('cam')) ? q.get('cam') : okCam(remembered()) ? remembered() : (state.cameras[0] || {}).id;
  if (!cam) { root.innerHTML = '<div class="empty"><h2>No cameras yet</h2>Add cameras under Settings.</div>'; return; }
  const t = Number(q.get('t')) || null;
  dayStart = startOfDay(t || now());
  $('#cam', root).value = cam;
  $('#day', root).value = isoDay(dayStart);
  $('#day', root).max = isoDay(now());

  $('#cam', root).onchange = e => { cam = e.target.value; remember(cam); syncUrl(); setMode(); reload(playhead); };
  $('#mplay', root).onclick = () => { if (multi) { multi.setPlaying(!multi.playing); showMultiPlaying(); } };
  for (const b of $$('#mrate button', root)) b.onclick = () => { if (multi) multi.setRate(+b.dataset.r);
    for (const x of $$('#mrate button', root)) x.setAttribute('aria-pressed', x === b); };
  root.addEventListener('keydown', e => { if (multi && e.key === ' ' && !e.target.closest('input,select,button,video')) {
    e.preventDefault(); multi.setPlaying(!multi.playing); showMultiPlaying(); } });
  $('#day', root).onchange = e => { const [y, m, d] = e.target.value.split('-').map(Number);
    if (!y) return; dayStart = new Date(y, m - 1, d).getTime() / 1000; syncUrl(); reload(null); };
  $('#prev', root).onclick = () => shiftDay(-1);
  $('#next', root).onclick = () => shiftDay(1);
  $('#latest', root).onclick = () => { dayStart = startOfDay(now()); $('#day', root).value = isoDay(dayStart); reload(null, true); };
  for (const b of $$('#zoom button', root)) b.onclick = () => { zoom = +b.dataset.z;
    for (const x of $$('#zoom button', root)) x.setAttribute('aria-pressed', x === b); draw(); };
  $('#save', root).onclick = save;
  $('#miss', root).onclick = () => {
    if (cam === ALL) { toast('Pick the camera first: select it in the grid.'); return; }
    if (playhead == null) { toast('Pick the moment on the timeline first.'); return; }
    const v = $('#v', root); v.pause();
    $('#miss-when', root).textContent = `${state.camById[cam].name}, ${new Date(playhead * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit'})}. 10 seconds either side are kept for testing.`;
    $('#missdlg', root).showModal();
  };
  $('#missdlg', root).addEventListener('close', async () => {
    const d = $('#missdlg', root);
    if (d.returnValue !== 'send') return;
    const f = $('form', d);
    try {
      await api('/api/misses', {method: 'POST', body: {camera: cam, ts: playhead, label: f.label.value, note: f.note.value}});
      f.reset(); toast('Reported. It will be part of the next test-set export.');
    } catch (e) { toast(e.message); }
  });

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
  v.addEventListener('error', () => {
    if (si < 0) return;
    if (v.error && v.error.code === 4) {   // format, not a damaged file: every minute will fail the same way
      showMsg("This browser can't play this camera's recordings (H.265 video). Safari, or Chrome and Edge on a computer with " +
              "H.265 hardware decoding, can. Saving 5 minutes still works.");
      return;
    }
    nextSegment();
  });

  wireScrub();
  setMode();
  await reload(t, !t);
  timer = setInterval(() => { if (!document.hidden && dayStart === startOfDay(now())) loadTimeline(); }, 30000);
}

export function update(route) {
  const q = route.query, t = Number(q.get('t')) || null, c = q.get('cam');
  if (c && (state.camById[c] || c === ALL) && (c !== cam || t)) {
    cam = c; $('#cam', root).value = cam; setMode();
    if (t) { dayStart = startOfDay(t); $('#day', root).value = isoDay(dayStart); }
    reload(t);
  }
}

export function unmount() {
  clearInterval(timer);
  if (multi) { multi.destroy(); multi = null; }
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
    if (cam === ALL) {
      const all = await Promise.all(state.cameras.map(c =>
        api(`/api/timeline?camera=${encodeURIComponent(c.id)}&start=${dayStart}&end=${dayStart + 86400}`)
          .then(r => ({...r, events: r.events.map(e => ({...e, camera: c.id}))}))));
      const recorded = all.flatMap(r => r.recorded).sort((a, b) => a[0] - b[0]);
      tl = {recorded: recorded.reduce((m, r) => {   // union, so the bar shows when any camera recorded
        const last = m[m.length - 1];
        if (last && r[0] <= last[1] + 1) last[1] = Math.max(last[1], r[1]); else m.push([...r]);
        return m; }, []), events: all.flatMap(r => r.events).sort((a, b) => a.start - b.start)};
    } else {
      tl = await api(`/api/timeline?camera=${encodeURIComponent(cam)}&start=${dayStart}&end=${dayStart + 86400}`);
    }
  } catch (e) { if (e.status !== 401) toast(e.message); tl = null; return; }
  draw();
}

// One camera with its own controls, or all of them in a grid on one clock.
function setMode() {
  const all = cam === ALL, v = $('#v', root);
  $('#multi', root).hidden = !all; v.hidden = all; $('#mctl', root).hidden = !all;
  $('.player', root).classList.toggle('grid', all);
  $('.player', root).style.setProperty('--rows', Math.ceil(state.cameras.length / 2));
  if (all && !multi) {
    v.pause(); v.removeAttribute('src'); v.load(); segs = []; si = -1;
    multi = new MultiView($('#multi', root), state.cameras, {
      onTime: t => { if (dragging || t == null) return; playhead = t;
        if (playhead >= dayStart + 86400) { dayStart = startOfDay(playhead); $('#day', root).value = isoDay(dayStart); loadTimeline(); }
        drawHead(); },
      onOpen: (c, t) => go(`#/recordings?cam=${encodeURIComponent(c)}&t=${Math.floor(t || playhead || now())}`),
      onCaughtUp: () => { showMultiPlaying(); toast('Caught up with the recordings. The newest minute appears once it has been saved.'); },
    });
  } else if (!all && multi) { multi.destroy(); multi = null; }
}
function showMultiPlaying() {
  const b = $('#mplay', root); if (!b || !multi) return;
  b.setAttribute('aria-pressed', multi.playing);
  b.innerHTML = multi.playing ? `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 5h3v14H7zM14 5h3v14h-3z" fill="currentColor"/></svg><span>Pause</span>`
                              : `${ICON.play}<span>Play</span>`;
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

  const narrow = $('#scrub', root).clientWidth < 600;   // fewer labels on phones so they don't run together
  const step = zoom === 24 ? (narrow ? 6 : 3) * 3600 : zoom === 6 ? (narrow ? 2 : 1) * 3600 : (narrow ? 1200 : 600);
  let ticks = '';
  for (let t = Math.ceil(a / step) * step; t <= b; t += step) {
    const p = pct(t); if (p < 3 || p > 97) continue;
    ticks += `<span style="left:${p}%">${timeOf(t)}</span>`;
  }
  $('#axis', root).innerHTML = ticks;
  $('#evlist', root).innerHTML = inView.length ? inView.slice(0, 60).map(e =>
    `<button class="chip k-${esc(e.label)}" data-t="${e.start}"><span class="kind">${esc(labelName(e.label))}</span> ${timeOf(e.start)}${e.camera ? `, ${esc(camName(e.camera))}` : ''}</button>`).join('')
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
  if (multi) { showMsg(''); await multi.seek(t, play); showMultiPlaying(); draw(); return; }
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
  if (cam === ALL) { toast('Pick the camera first: select it in the grid.'); return; }
  if (playhead == null) { toast('Pick a moment on the timeline first.'); return; }
  const start = Math.floor(playhead), end = start + 300;
  const a = document.createElement('a');
  a.href = `/media/export.mp4?camera=${encodeURIComponent(cam)}&start=${start}&end=${end}`;
  a.download = '';
  document.body.appendChild(a); a.click(); a.remove();
  toast('Preparing the video. The download starts in a few seconds.');
}
