import './player.js';
import {$, $$, api, esc, state, camName, go, ICON, labelName, timeOf, dayName, duration} from './app.js';

let timers = [], root = null, shell = null;

export async function mount(view, route, sh) {
  root = view; shell = sh;
  render(route);
}

export function update(route) { render(route); }

export function unmount() {
  timers.forEach(clearInterval); timers = [];
  root = null;
}

function render(route) {
  timers.forEach(clearInterval); timers = [];
  const camId = route.parts[0];
  if (camId && state.camById[camId]) return renderFocus(camId);
  shell.setTitle('Live');
  shell.tools.innerHTML = '';
  if (!state.cameras.length) {
    root.innerHTML = `<div class="empty"><h2>No cameras yet</h2>Add cameras under Settings.</div>`;
    return;
  }
  root.innerHTML = `<div class="grid">${state.cameras.map(c => `
    <button class="tile" data-cam="${esc(c.id)}" aria-label="${esc(c.name)}: open full screen">
      <bb-live stream="${esc(c.sub || c.main)}" label="${esc(c.name)}"></bb-live>
      <span class="cap"><b>${esc(c.name)}</b><span class="st" data-st="${esc(c.id)}"></span></span>
    </button>`).join('')}</div>`;
  for (const t of $$('.tile', root)) t.onclick = () => go(`#/live/${t.dataset.cam}`);
  refreshGrid();
  timers.push(setInterval(() => { if (!document.hidden) refreshGrid(); }, 5000));
}

async function refreshGrid() {
  if (!root) return;
  let st, active;
  try {
    [st, active] = await Promise.all([api('/api/status'), api(`/api/events?since=${Date.now() / 1000 - 5}&limit=50`)]);
  } catch { return; }
  if (!root) return;
  const inView = {};
  for (const e of active) if (!e.end_ts && !e.stationary) (inView[e.camera] ||= new Set()).add(e.label);
  for (const c of state.cameras) {
    const el = $(`[data-st="${CSS.escape(c.id)}"]`, root);
    if (!el) continue;
    const s = st.cameras[c.id] || {};
    const rec = s.recording && s.recording.state;
    const seen = inView[c.id] ? [...inView[c.id]].map(labelName).join(', ') : '';
    let text = seen ? `${seen} in view` : '';
    if (rec && rec !== 'recording' && rec !== 'unknown') text = text || 'Not recording';
    el.innerHTML = `${rec === 'recording' ? '<span class="live-dot" title="Recording"></span>' : ''}${esc(text)}`;
    el.closest('.tile').classList.toggle('busy-person', !!(inView[c.id] && inView[c.id].has('person')));
  }
}

function renderFocus(camId) {
  const c = state.camById[camId];
  shell.setTitle(c.name);
  shell.tools.innerHTML = `<button class="btn quiet" id="back">${ICON.back}All cameras</button>
    <a class="btn" href="#/recordings?cam=${encodeURIComponent(c.id)}">${ICON.recordings}Recordings</a>`;
  $('#back', shell.tools).onclick = () => go('#/live');
  root.innerHTML = `<div class="focus">
    <div class="stage"><bb-live stream="${esc(c.main)}" label="${esc(c.name)}" controls></bb-live></div>
    <section class="side"><h2>Recent on this camera</h2><div class="feed" id="recent"><p class="muted">Loading…</p></div></section>
  </div>`;
  const load = async () => {
    let evs;
    try { evs = await api(`/api/events?camera=${encodeURIComponent(c.id)}&limit=8`); } catch { return; }
    if (!root) return;
    const el = $('#recent', root);
    if (!el) return;
    el.innerHTML = evs.length ? evs.map(e => `
      <a class="ev k-${esc(e.label)}" href="#/events/${esc(e.id)}" style="grid-template-columns:120px minmax(0,1fr);text-decoration:none">
        ${e.has_snapshot ? `<img src="/media/snapshot/${esc(e.id)}.jpg?w=320&u=${e.updated_ts}" alt="" loading="lazy" style="width:120px">` : '<span></span>'}
        <span class="what"><span class="kind">${esc(labelName(e.label))}</span>
          <span class="where">${dayName(e.start_ts) === 'Today' ? '' : esc(dayName(e.start_ts)) + ', '}${timeOf(e.start_ts)}, ${e.end_ts ? esc(duration(e.start_ts, e.end_ts)) : 'in view now'}</span></span>
      </a>`).join('') : '<p class="muted">Nothing detected on this camera yet.</p>';
  };
  load();
  timers.push(setInterval(() => { if (!document.hidden) load(); }, 10000));
}
