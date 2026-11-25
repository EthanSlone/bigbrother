"""Debug viewer: live annotated frames, per-camera stats, and recent events.

A development tool for judging detection quality, not the real app (that's
Phase 4). Bound to localhost by docker-compose; never expose it.

Box colours: green = confirmed event, yellow = tracking but not yet confirmed,
grey = stationary (parked), red outline = raw motion areas.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from ..events_db import EventStore

log = logging.getLogger(__name__)

GREEN, YELLOW, GREY, RED = (80, 200, 80), (40, 200, 230), (150, 150, 150), (60, 60, 220)


def draw_box(img: np.ndarray, box, color, text: str | None = None, thickness: int = 2) -> None:
    x1, y1, x2, y2 = (int(v) for v in box)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
    if text:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = y1 - 4 if y1 - th - 6 > 0 else y2 + th + 4
        cv2.rectangle(img, (x1, ty - th - 3), (x1 + tw + 4, ty + 3), color, -1)
        cv2.putText(img, text, (x1 + 2, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)


def render_live(view) -> bytes | None:
    if view.frame is None:
        return None
    img = view.frame.copy()
    for b in view.motion:
        draw_box(img, b, RED, thickness=1)
    for t in view.tracks:
        if t["missed"]:
            continue  # not seen on the latest check: don't draw where it *was*
        color = GREY if t["stationary"] else GREEN if t["confirmed"] else YELLOW
        draw_box(img, t["box"], color, f"#{t['id']} {t['label']} {t['score']:.2f}")
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75])
    return buf.tobytes() if ok else None


def render_snapshot(path: Path, box, label: str) -> bytes | None:
    img = cv2.imread(str(path))
    if img is None:
        return None
    draw_box(img, box, GREEN, label)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BigBrother · detector debug</title>
<style>
:root{--bg:#111418;--panel:#1a1f25;--line:#2a313a;--text:#e6e8eb;--dim:#8b949e;--ok:#50c850;--warn:#e6c828;--bad:#dc3c3c}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;background:var(--bg);color:var(--text)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:baseline}
h1{font-size:16px;margin:0}header span{color:var(--dim);font-size:12px}
main{padding:16px 20px;display:grid;gap:20px}
.cams{display:grid;grid-template-columns:repeat(auto-fill,minmax(420px,1fr));gap:14px}
.cam{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
.cam img{width:100%;display:block;background:#000;aspect-ratio:16/9;object-fit:contain}
.meta{padding:8px 10px;display:flex;flex-wrap:wrap;gap:4px 14px;font-size:12px;color:var(--dim)}
.meta b{color:var(--text);font-weight:600}.state-running{color:var(--ok)}.state-error{color:var(--bad)}
.err{color:var(--bad);font-size:12px;padding:0 10px 8px}
.legend{font-size:12px;color:var(--dim);display:flex;gap:14px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:middle}
th{color:var(--dim);font-weight:500;font-size:12px}td img{height:54px;border-radius:4px;cursor:zoom-in;display:block}
.tag{font-size:11px;padding:1px 6px;border-radius:9px;background:#2a313a;color:var(--dim)}.live{background:#1f3a24;color:var(--ok)}
dialog{border:0;padding:0;background:#000;max-width:95vw}dialog img{max-width:95vw;max-height:90vh;display:block}
</style></head><body>
<header><h1>BigBrother detector</h1><span>debug viewer: not the real app</span>
<div class="legend"><span><i class="sw" style="background:var(--ok)"></i>event</span><span><i class="sw" style="background:var(--warn)"></i>tracking (unconfirmed)</span><span><i class="sw" style="background:#969696"></i>stationary</span><span><i class="sw" style="background:var(--bad)"></i>motion</span></div></header>
<main><section class="cams" id="cams"></section>
<section><h2 style="font-size:14px;margin:0 0 8px">Recent events</h2>
<table><thead><tr><th>Snapshot</th><th>Camera</th><th>Object</th><th>Score</th><th>Started</th><th>Duration</th><th></th></tr></thead><tbody id="events"></tbody></table></section></main>
<dialog id="dlg" onclick="this.close()"><img id="big" alt=""></dialog>
<script>
const camsEl=document.getElementById('cams'),evEl=document.getElementById('events');
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function stats(){
  const s=await (await fetch('api/stats')).json();
  for(const c of s){
    let el=document.getElementById('cam-'+c.camera);
    if(!el){el=document.createElement('div');el.className='cam';el.id='cam-'+c.camera;
      el.innerHTML=`<img alt="${esc(c.name)}"><div class="meta"></div><div class="err"></div>`;camsEl.appendChild(el);}
    el.querySelector('.meta').innerHTML=`<b>${esc(c.name)}</b><span class="state-${esc(c.state)}">${esc(c.state)}</span>`+
      `<span>in <b>${c.fps_in}</b> fps</span><span>processed <b>${c.fps_processed}</b></span><span>detect <b>${c.detect_fps}</b> fps</span><span>model <b>${c.infer_ms??'-'}</b> ms</span>`+
      `<span>events <b>${c.events_started}</b></span><span>filtered <b>${c.filtered}</b></span><span>skipped <b>${c.skipped_frames}</b></span>`;
    el.querySelector('.err').textContent=c.error||'';
  }
}
function live(){for(const img of camsEl.querySelectorAll('.cam img')){const id=img.parentElement.id.slice(4);
  const n=new Image();n.onload=()=>{img.src=n.src};n.src=`live/${id}.jpg?t=${Date.now()}`;}}
const fmtDur=(a,b)=>{const s=Math.round(((b??Date.now()/1000))-a);return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s'};
async function events(){
  const ev=await (await fetch('api/events?limit=40')).json();
  evEl.innerHTML=ev.map(e=>`<tr><td>${e.snapshot?`<img src="snapshot/${esc(e.id)}.jpg?u=${e.updated_ts}" data-id="${esc(e.id)}">`:''}</td>`+
   `<td>${esc(e.camera)}</td><td>${esc(e.label)}</td><td>${e.median_score.toFixed(2)} / ${e.top_score.toFixed(2)}</td>`+
   `<td>${new Date(e.start_ts*1000).toLocaleTimeString()}</td><td>${fmtDur(e.start_ts,e.end_ts)}</td>`+
   `<td>${e.end_ts?'':'<span class="tag live">in view</span>'} ${e.stationary?'<span class="tag">stationary</span>':''}</td></tr>`).join('');
}
evEl.addEventListener('click',e=>{if(e.target.tagName==='IMG'){document.getElementById('big').src=e.target.src;document.getElementById('dlg').showModal();}});
stats().then(live);setInterval(stats,2000);setInterval(live,400);events();setInterval(events,3000);
</script></body></html>"""


def start_viewer(workers, store: EventStore, data_dir: Path, port: int) -> ThreadingHTTPServer:
    by_id = {w.cam.id: w for w in workers}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # quiet: the page polls constantly
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj).encode(), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            path = url.path.rstrip("/") or "/"
            try:
                if path == "/":
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if path == "/api/stats":
                    return self._json([w.stats() for w in workers])
                if path == "/api/events":
                    q = parse_qs(url.query)
                    limit = min(int(q.get("limit", ["50"])[0]), 500)
                    cam = q.get("camera", [None])[0]
                    return self._json([e.to_dict() for e in store.recent(limit, cam)])
                if path.startswith("/live/") and path.endswith(".jpg"):
                    w = by_id.get(path[len("/live/"):-4])
                    body = render_live(w.view) if w else None
                    return self._send(200, body, "image/jpeg") if body else self._send(404, b"", "text/plain")
                if path.startswith("/snapshot/") and path.endswith(".jpg"):
                    e = store.get(path[len("/snapshot/"):-4])  # id looked up in the DB, never used as a path
                    if e and e.snapshot:
                        body = render_snapshot(data_dir / e.snapshot, e.box, f"{e.label} {e.top_score:.2f}")
                        if body:
                            return self._send(200, body, "image/jpeg")
                    return self._send(404, b"", "text/plain")
                return self._send(404, b"not found", "text/plain")
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:  # noqa: BLE001
                log.exception("viewer request failed: %s", self.path)
                self._send(500, b"error", "text/plain")

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="viewer", daemon=True).start()
    log.info("debug viewer on port %d", port)
    return server
