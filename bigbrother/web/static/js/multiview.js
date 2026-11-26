// All cameras on one clock. The page owns the time; each camera's video follows it,
// loading whichever minute covers that moment and nudging its speed to stay in step
// (seeking only when it's more than two seconds out, so playback stays smooth).
import {api, esc} from './app.js';

const sec = () => Date.now() / 1000;

class Track {
  constructor(cam, cell, clock) {
    this.cam = cam; this.cell = cell; this.clock = clock;
    this.video = cell.querySelector('video');
    this.msg = cell.querySelector('.mmsg');
    this.segs = []; this.range = null; this.cur = null; this.loading = false; this.blankShown = false;
    this.video.muted = true;
    this.video.addEventListener('loadedmetadata', () => {
      if (!this.cur) return;
      this.video.currentTime = Math.min(Math.max(0, this.clock().t - this.cur.start), Math.max(0, this.video.duration - 0.3));
      if (this.clock().playing) this.video.play().catch(() => {});
    });
    this.video.addEventListener('error', () => {
      if (!this.cur) return;
      const unsupported = this.video.error && this.video.error.code === 4;
      this.unplayable = unsupported;
      this.blank(unsupported ? "This browser can't play this camera's video format (H.265)" : 'This minute could not be played');
    });
  }

  segAt(t) { return this.segs.find(s => s.start - 0.5 <= t && t < s.end); }
  covered(t) { return this.range && t >= this.range[0] && t <= this.range[1]; }
  nextStart(t) { const s = this.segs.find(x => x.start > t); return s ? s.start : null; }

  async fetch(t) {
    const a = t - 180, b = t + 3600;
    this.segs = await api(`/api/segments?camera=${encodeURIComponent(this.cam)}&start=${a}&end=${b}`);
    this.range = [a, Math.min(b - 60, sec() - 90)];   // near the live edge, ask again for new minutes
  }

  blank(text) {
    this.msg.textContent = text; this.msg.hidden = false; this.cell.classList.add('empty');
    if (this.blankShown) return;
    this.blankShown = true; this.cur = null;
    this.video.removeAttribute('src'); this.video.load();
  }

  async show(force = false) {
    if (this.loading || (this.unplayable && !force)) return;
    this.unplayable = false;
    const {t, playing, rate} = this.clock();
    let seg = this.segAt(t);
    if (!seg && (force || !this.covered(t))) {
      this.loading = true;
      try { await this.fetch(t); } catch { /* try again next tick */ } finally { this.loading = false; }
      seg = this.segAt(this.clock().t);
    }
    if (!seg) return this.blank(this.clock().t > sec() - 120 ? 'Still being recorded' : 'No recording at this time');
    const v = this.video;
    if (!this.cur || this.cur.id !== seg.id) {
      this.cur = seg; this.blankShown = false;
      this.msg.hidden = true; this.cell.classList.remove('empty');
      v.playbackRate = rate;
      v.src = `/media/recording/${seg.id}.mp4`;
      return;
    }
    if (v.readyState < 1) return;
    const drift = v.currentTime - (t - seg.start);
    if (force || Math.abs(drift) > 2) { v.currentTime = Math.max(0, t - seg.start); v.playbackRate = rate; }
    else v.playbackRate = rate * (Math.abs(drift) > 0.25 ? (drift > 0 ? 0.92 : 1.08) : 1);
    if (playing && v.paused && !v.ended) v.play().catch(() => {});
    if (!playing && !v.paused) v.pause();
  }

  stop() { this.video.pause(); this.video.removeAttribute('src'); this.video.load(); }
}

export class MultiView {
  constructor(el, cams, {onTime, onOpen, onCaughtUp}) {
    this.el = el; this.onTime = onTime; this.onCaughtUp = onCaughtUp;
    this.t = null; this.playing = false; this.rate = 1; this.stall = 0;
    el.innerHTML = cams.map(c => `<button class="mcell" data-cam="${esc(c.id)}" title="Open ${esc(c.name)} on its own">
      <video playsinline muted preload="auto" tabindex="-1"></video><span class="mname">${esc(c.name)}</span>
      <span class="mmsg" hidden></span></button>`).join('');
    el.style.setProperty('--n', cams.length);
    const clock = () => ({t: this.t, playing: this.playing, rate: this.rate});
    this.tracks = cams.map((c, i) => new Track(c.id, el.children[i], clock));
    el.onclick = e => { const c = e.target.closest('.mcell'); if (c) onOpen(c.dataset.cam, this.t); };
    this.last = performance.now();
    this.timer = setInterval(() => this.tick(), 250);
  }

  destroy() { clearInterval(this.timer); for (const tr of this.tracks) tr.stop(); this.el.innerHTML = ''; this.el.onclick = null; }

  async seek(t, play) {
    this.t = t; this.playing = play; this.stall = 0; this.last = performance.now();
    this.onTime(t);
    await Promise.all(this.tracks.map(tr => tr.show(true)));
  }

  setPlaying(p) { this.playing = p; this.last = performance.now(); for (const tr of this.tracks) tr.show(); }
  setRate(r) { this.rate = r; for (const tr of this.tracks) tr.video.playbackRate = r; }

  tick() {
    const now = performance.now(), dt = Math.min(1, (now - this.last) / 1000);
    this.last = now;
    if (this.t == null) return;
    if (this.playing) {
      // Wait (up to 3 s) for a camera that is still loading its minute, so it doesn't fall behind.
      const waiting = this.tracks.some(tr => tr.cur && tr.video.readyState < 3);
      this.stall = waiting ? this.stall + dt : 0;
      if (!waiting || this.stall > 3) this.t += dt * this.rate;
      if (this.t > sec() - 60) { this.playing = false; this.onCaughtUp(); }
      // Nothing recorded on any camera here: skip ahead to the next recorded moment.
      if (this.tracks.every(tr => !tr.segAt(this.t) && tr.covered(this.t))) {
        const next = Math.min(...this.tracks.map(tr => tr.nextStart(this.t) ?? Infinity));
        if (next < Infinity) { this.seek(next, true); return; }
      }
    }
    for (const tr of this.tracks) tr.show();
    this.onTime(this.t);
  }
}
