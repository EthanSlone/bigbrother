"""Debug viewer: live annotated frames, per-camera stats, recent events, zone editor.

A development tool for judging detection quality, not the real app (that's
Phase 4). Bound to localhost by docker-compose; never expose it.

Box colours: green = confirmed event, yellow = tracking but not yet confirmed,
grey = stationary (parked), red outline = raw motion. Zones are drawn in blue,
motion masks as dark fill, object masks as orange fill.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np

from ..events_db import EventStore

log = logging.getLogger(__name__)

GREEN, YELLOW, GREY, RED = (80, 200, 80), (40, 200, 230), (150, 150, 150), (60, 60, 220)
BLUE, ORANGE, DARK = (230, 160, 60), (0, 140, 255), (30, 30, 30)
PAGE_PATH = Path(__file__).with_name("viewer.html")

# Extra GET routes registered by other features (alerts, playback): prefix -> handler(handler, url)
ROUTES: dict[str, Callable] = {}


def draw_box(img: np.ndarray, box, color, text: str | None = None, thickness: int = 2) -> None:
    x1, y1, x2, y2 = (int(v) for v in box)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
    if text:
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        ty = y1 - 4 if y1 - th - 6 > 0 else y2 + th + 4
        cv2.rectangle(img, (x1, ty - th - 3), (x1 + tw + 4, ty + 3), color, -1)
        cv2.putText(img, text, (x1 + 2, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)


def _pts(poly, w: int, h: int) -> np.ndarray:
    return np.array([[x * w, y * h] for x, y in poly], np.int32)


def draw_zones_and_masks(img: np.ndarray, cam) -> None:
    h, w = img.shape[:2]
    if cam.motion_masks or cam.object_masks:
        fill = img.copy()
        for poly in cam.motion_masks:
            cv2.fillPoly(fill, [_pts(poly, w, h)], DARK)
        for poly in cam.object_masks:
            cv2.fillPoly(fill, [_pts(poly, w, h)], ORANGE)
        cv2.addWeighted(fill, 0.45, img, 0.55, 0, dst=img)
    for zone in cam.zones.values():
        pts = _pts(zone.points, w, h)
        cv2.polylines(img, [pts], True, BLUE, 2, cv2.LINE_AA)
        x, y = (int(v) for v in pts.min(axis=0))
        (tw, th), _ = cv2.getTextSize(zone.name, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
        cv2.rectangle(img, (x, y), (x + tw + 6, y + th + 6), BLUE, -1)
        cv2.putText(img, zone.name, (x + 3, y + th + 2), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)


MAX_PREDICT = 0.6  # seconds a box may be moved forward along its velocity


def predict_box(t: dict, frame_ts: float) -> list[float]:
    """Move a box along its velocity to the preview frame's time, so it stays on the object
    between analyses."""
    if t["stationary"]:
        return t["box"]
    dt = min(max(frame_ts - t["seen_ts"], 0.0), MAX_PREDICT)
    return [b + v * dt for b, v in zip(t["box"], t["velocity"])]


def render_live(image: np.ndarray, frame_ts: float, view, cam=None, raw: bool = False) -> bytes | None:
    img = image.copy()
    if not raw:
        if cam is not None:
            draw_zones_and_masks(img, cam)
        if frame_ts - view.ts < 2.0:  # overlay only while analysis is current
            for b in view.motion:
                draw_box(img, b, RED, thickness=1)
            for t in view.tracks:
                if t["missed"]:
                    continue  # not seen on the latest check: don't draw where it *was*
                color = GREY if t["stationary"] else GREEN if t["confirmed"] else YELLOW
                label = f"#{t['id']} {t['label']} {t['score']:.2f}"
                if t.get("zones"):
                    label += " @" + ",".join(t["zones"])
                draw_box(img, predict_box(t, frame_ts), color, label)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 75 if not raw else 90])
    return buf.tobytes() if ok else None


def render_snapshot(path: Path, box, label: str) -> bytes | None:
    img = cv2.imread(str(path))
    if img is None:
        return None
    draw_box(img, box, GREEN, label)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes() if ok else None


def camera_info(w) -> dict:
    cam = w.cam
    return {
        "id": cam.id, "name": cam.name, "size": w.source.size,
        "zones": {n: {"points": [list(p) for p in z.points], "objects": list(z.objects) if z.objects else None}
                  for n, z in cam.zones.items()},
        "motion_masks": [[list(p) for p in poly] for poly in cam.motion_masks],
        "object_masks": [[list(p) for p in poly] for poly in cam.object_masks],
        "labels": list(cam.detect.labels),
    }


class ViewerHandler(BaseHTTPRequestHandler):
    workers: list = []
    by_id: dict = {}
    store: EventStore | None = None
    data_dir: Path = Path("/data")

    def log_message(self, fmt, *args):  # quiet: the page polls constantly
        pass

    def send_body(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, code: int = 200) -> None:
        self.send_body(code, json.dumps(obj).encode(), "application/json")

    def not_found(self) -> None:
        self.send_body(404, b"not found", "text/plain")

    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path.rstrip("/") or "/"
        q = parse_qs(url.query)
        try:
            if path == "/":
                return self.send_body(200, PAGE_PATH.read_bytes(), "text/html; charset=utf-8")
            if path == "/api/stats":
                return self.send_json([w.stats() for w in self.workers])
            if path == "/api/cameras":
                return self.send_json([camera_info(w) for w in self.workers])
            if path == "/api/events":
                limit = min(int(q.get("limit", ["50"])[0]), 500)
                cam = q.get("camera", [None])[0]
                return self.send_json([e.to_dict() for e in self.store.recent(limit, cam)])
            if path.startswith("/live/") and path.endswith(".jpg"):
                # Long-poll: wait (briefly) for a frame newer than ?after=, so the page
                # receives each decoded frame once, at the decoder's pace.
                w = self.by_id.get(path[len("/live/"):-4])
                if not w:
                    return self.not_found()
                after = int(q.get("after", ["0"])[0] or 0)
                frame = w.source.wait_frame(after, timeout=1.0)
                if frame is None:
                    return self.send_body(204, b"", "text/plain")
                body = render_live(frame.image, frame.ts, w.view, w.cam, raw=q.get("raw") == ["1"])
                if not body:
                    return self.send_body(500, b"", "text/plain")
                return self.send_body(200, body, "image/jpeg", {"X-Seq": str(frame.seq)})
            if path.startswith("/snapshot/") and path.endswith(".jpg"):
                e = self.store.get(path[len("/snapshot/"):-4])  # looked up in the DB, never used as a path
                if e and e.snapshot:
                    body = render_snapshot(self.data_dir / e.snapshot, e.box, f"{e.label} {e.top_score:.2f}")
                    if body:
                        return self.send_body(200, body, "image/jpeg")
                return self.not_found()
            for prefix, handler in ROUTES.items():
                if path.startswith(prefix):
                    return handler(self, url)
            return self.not_found()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            log.exception("viewer request failed: %s", self.path)
            try:
                self.send_body(500, b"error", "text/plain")
            except Exception:  # noqa: BLE001
                pass

    do_HEAD = do_GET


def start_viewer(workers, store: EventStore, data_dir: Path, port: int) -> ThreadingHTTPServer:
    handler = type("Handler", (ViewerHandler,), {
        "workers": workers, "by_id": {w.cam.id: w for w in workers}, "store": store, "data_dir": data_dir,
    })
    server = ThreadingHTTPServer(("0.0.0.0", port), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="viewer", daemon=True).start()
    log.info("debug viewer on port %d", port)
    return server
