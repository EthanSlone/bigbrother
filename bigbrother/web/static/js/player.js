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
    this.video.addEventListener('playing', () => { clearTimeout(this.stallTimer); this.status(''); });
    this.video.addEventListener('waiting', () => {
      const now = performance.now();
      if (now - (this.jumpedAt || 0) < 2000) return;   // our own jump to live, not the network
      if (this.video.currentTime > 0.5 && now - this.lastStall > 1000) this.cushion = Math.min(4, this.cushion + 0.5);
      this.lastStall = now;
      this.stalls = (this.stalls || 0) + 1;
      if (this.video.currentTime > 0.5 && now - (this.jumpedAt || 0) > 2000) this.ondiag('stall', {stalls: this.stalls});
      clearTimeout(this.stallTimer);   // only say so if it lasts: brief waits aren't worth a message
      this.stallTimer = setTimeout(() => this.status('Buffering…'), 1500);
    });
  }

  liveSync(gap, end) {
    const v = this.video, now = performance.now();
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
