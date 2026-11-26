// <bb-live stream="driveway_sub">: live video from go2rtc over the app's own connection.
// Uses go2rtc's player (MIT, vendor/video-rtc.js) in MSE mode, falling back to HLS on
// iPhones that lack MediaSource. Stops streaming when off screen or the tab is hidden.
import {VideoRTC} from '../vendor/video-rtc.js';

class BBLive extends VideoRTC {
  constructor() {
    super();
    this.mode = 'mse,hls';
    this.media = 'video';            // camera audio stays off in the live view
    this.visibilityThreshold = 0.05; // pause tiles scrolled out of view
    this.background = false;
  }

  oninit() {
    super.oninit();
    this.video.muted = true;
    this.video.controls = this.hasAttribute('controls');
    this.video.setAttribute('aria-label', this.getAttribute('label') || 'Live video');
    this.msg = document.createElement('div');
    this.msg.className = 'msg';
    this.appendChild(this.msg);
    this.status('Connecting…');
    // Live cushion: how far behind the newest received video we play. It starts at 1 s, grows
    // by half a second each time the stream stalls (up to 4 s) and shrinks back after a calm
    // minute, so a steady LAN stays near real time and a phone on LTE stops stuttering.
    this.cushion = 1.0;
    this.lastStall = performance.now();
    this.agg = {stalls: 0, holes: 0, maxHole: 0, unstuck: 0, since: performance.now()};
    this.lastCT = -1; this.lastAdvance = performance.now();
    this.video.addEventListener('playing', () => { clearTimeout(this.stallTimer); this.status(''); });
    this.video.addEventListener('waiting', () => {
      const now = performance.now();
      if (now - (this.jumpedAt || 0) < 2000) return;   // our own jump to live, not the network
      if (this.video.currentTime > 0.5 && now - this.lastStall > 1000) this.cushion = Math.min(4, this.cushion + 0.5);
      this.lastStall = now;
      this.stalls = (this.stalls || 0) + 1;
      if (this.video.currentTime > 0.5 && now - (this.jumpedAt || 0) > 2000) this.agg.stalls++;
      clearTimeout(this.stallTimer);   // only say so if it lasts: brief waits aren't worth a message
      this.stallTimer = setTimeout(() => this.status('Buffering…'), 1500);
    });
  }

  // Stuck with video ahead: usually a tiny hole in the timeline between chunks (cameras whose
  // frame timing is off, e.g. sending 10 fps while declaring 20), which browsers won't play
  // across on their own. Jump to the next buffered video; if the decoder is stuck mid-buffer,
  // restart it near the live edge.
  unstick(end) {
    const v = this.video, now = performance.now(), t = v.currentTime, b = v.buffered;
    if (v.paused || end - t < 0.3) return false;
    if (Math.abs(t - this.lastCT) > 0.01) { this.lastCT = t; this.lastAdvance = now; return false; }
    const stuck = now - this.lastAdvance;
    if (stuck < 400) return false;
    for (let i = 0; i < b.length; i++) {
      const nextStart = t < b.start(i) ? b.start(i) : (t <= b.end(i) && b.end(i) - t < 0.3 && i + 1 < b.length ? b.start(i + 1) : null);
      if (nextStart !== null) {
        this.agg.holes++; this.agg.maxHole = Math.max(this.agg.maxHole, +(nextStart - t).toFixed(3));
        this.jumpedAt = now; this.lastAdvance = now;
        v.currentTime = nextStart + 0.05;
        return true;
      }
    }
    if (stuck > 1500) {
      this.agg.unstuck++;
      this.jumpedAt = now; this.lastAdvance = now;
      v.currentTime = Math.max(0, end - this.cushion);
      return true;
    }
    return false;
  }

  liveSync(gap, end) {
    const v = this.video, now = performance.now();
    if (now - this.agg.since > 10000) {   // one summary per camera every 10 s, only when something happened
      const a = this.agg;
      if (a.stalls || a.holes || a.unstuck) this.ondiag('summary', {stalls: a.stalls, holes_skipped: a.holes, biggest_hole: a.maxHole,
        unstuck: a.unstuck, ranges: v.buffered.length});
      this.agg = {stalls: 0, holes: 0, maxHole: 0, unstuck: 0, since: now};
    }
    if (this.unstick(end)) return;
    if (gap > this.cushion + 4) {      // far behind (tab was hidden, network burst): jump to live
      this.jumpedAt = now;
      v.currentTime = Math.max(0, end - this.cushion);
      v.playbackRate = 1;
      return;
    }
    if (now - this.lastStall > 60000 && this.cushion > 0.6) {
      this.cushion = Math.max(0.6, this.cushion - 0.25);
      this.lastStall = now - 30000;    // next step down after another 30 s
    }
    // Speed up or slow down gently to sit at the cushion: barely noticeable near it, up to 2x
    // when well behind (some cameras' clocks run fast, so video arrives quicker than real time).
    v.playbackRate = Math.min(2, Math.max(0.9, 1 + (gap - this.cushion) * 0.5));
  }

  status(text) { if (this.msg) this.msg.textContent = text; }

  onopen() {
    const r = super.onopen();
    this.onmessage['bb'] = msg => {
      if (msg.type === 'error') this.status(/codecs/.test(msg.value) ? "This browser can't play this camera's video format."
                                                                       : 'Camera unavailable. Retrying…');
    };
    return r;
  }

  onclose(ev) {
    if (this.wsState !== WebSocket.CLOSED) {   // not a close we asked for (page change, tile scrolled away)
      this.ondiag('ws-close', {code: ev ? ev.code : 0, reason: ev ? ev.reason : '', clean: ev ? ev.wasClean : null,
                               after: Math.round((Date.now() - this.connectTS) / 1000), hidden: document.hidden});
    }
    this.status('Reconnecting…');
    return super.onclose();
  }

  // Problems go to the server log (docker compose logs web), batched, so they can be diagnosed
  // without a computer attached to the phone.
  ondiag(kind, detail) {
    // Tearing the tile down (page change, scrolled away) empties the video's source, which the
    // browser reports as an error. That's not a problem worth logging.
    if (kind === 'video-error' && (this.wsState === WebSocket.CLOSED || /Empty src/.test(detail.message || ''))) return;
    const v = this.video, end = v && v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
    report({stream: this.getAttribute('stream'), kind, mode: this.mode_used || (this.mseCodecs ? 'mse' : ''),
            gap: v ? +(end - v.currentTime).toFixed(2) : null, cushion: this.cushion, rate: v ? v.playbackRate : null, ...detail});
  }

  static get observedAttributes() { return ['stream']; }
  attributeChangedCallback(name, _old, value) {
    if (name === 'stream' && value) {
      this.src = new URL(`/go2rtc/api/ws?src=${encodeURIComponent(value)}`, location.href).toString();
    }
  }
}

const queue = [];
let flushTimer = 0;
function report(ev) {
  queue.push({t: +(Date.now() / 1000).toFixed(1), ...ev});
  if (queue.length > 60) queue.shift();
  if (!flushTimer) flushTimer = setTimeout(flush, 5000);
}
function flush() {
  flushTimer = 0;
  const events = queue.splice(0);
  if (!events.length) return;
  fetch('/api/diag/live', {method: 'POST', credentials: 'same-origin', keepalive: true,
    headers: {'X-BigBrother': '1', 'Content-Type': 'application/json'}, body: JSON.stringify({events})}).catch(() => {});
}
addEventListener('pagehide', flush);

if (!customElements.get('bb-live')) customElements.define('bb-live', BBLive);
