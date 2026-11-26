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
    this.video.addEventListener('playing', () => this.status(''));
    this.video.addEventListener('waiting', () => this.status('Buffering…'));
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

  onclose() {
    this.status('Reconnecting…');
    return super.onclose();
  }

  static get observedAttributes() { return ['stream']; }
  attributeChangedCallback(name, _old, value) {
    if (name === 'stream' && value) {
      this.src = new URL(`/go2rtc/api/ws?src=${encodeURIComponent(value)}`, location.href).toString();
    }
  }
}

if (!customElements.get('bb-live')) customElements.define('bb-live', BBLive);
