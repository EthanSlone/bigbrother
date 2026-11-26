"""JSON API and media routes behind the login."""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from functools import lru_cache
from pathlib import Path

import aiohttp
from aiohttp import web

from .app import CTX, SESSION, WebContext

log = logging.getLogger(__name__)


def _float(q, name: str) -> float | None:
    v = q.get(name)
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"{name} must be a number") from exc


def _list(q, name: str) -> list[str] | None:
    v = q.get(name)
    return [s for s in v.split(",") if s] if v else None


# -- cameras & status --------------------------------------------------------------------

def camera_dict(cam) -> dict:
    return {
        "id": cam.id, "name": cam.name,
        "main": cam.main_stream, "sub": cam.sub_stream,
        "record": cam.record.enabled, "detect": cam.detect.enabled,
        "labels": list(cam.detect.labels),
        "alert": {"enabled": cam.alert.enabled, "labels": list(cam.alert.labels), "zones": list(cam.alert.zones)},
        "zones": sorted(cam.zones),
    }


async def cameras(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response([camera_dict(c) for c in ctx.cfg.cameras.values()])


async def _detector_stats(ctx: WebContext) -> dict[str, dict]:
    try:
        async with ctx.session.get(f"{ctx.detector_url}/api/stats",
                                   timeout=aiohttp.ClientTimeout(total=2)) as r:
            if r.status == 200:
                return {s["camera"]: s for s in await r.json()}
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        pass
    return {}


async def status(request: web.Request) -> web.Response:
    """Per-camera health: recording state (from the recorder over MQTT), detection stats
    (from the detector), events today, plus disk usage and service availability."""
    ctx = request.app[CTX]
    det = await _detector_stats(ctx)
    now = time.time()
    lt = time.localtime(now)
    midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    counts = await asyncio.to_thread(ctx.queries.counts_by_camera, midnight)
    cams = {}
    for cam in ctx.cfg.cameras.values():
        rec = ctx.status.get(f"recorder/{cam.id}/state")
        d = det.get(cam.id)
        cams[cam.id] = {
            "recording": None if not cam.record.enabled else (rec or {"state": "unknown"}),
            "detection": None if not cam.detect.enabled else (
                {k: d.get(k) for k in ("state", "error", "fps_in", "fps_analysed", "detect_fps",
                                        "infer_ms", "wait_ms", "skipped_frames", "tracks")} if d
                else {"state": "unknown"}),
            "events_today": counts.get(cam.id, 0),
        }
    disk = None
    try:
        du = shutil.disk_usage(ctx.recordings_root)
        disk = {"total": du.total, "used": du.used, "free": du.free,
                "percent": round(du.used / du.total * 100, 1),
                "limit_percent": ctx.cfg.storage.max_usage_percent,
                "retention_days": ctx.cfg.storage.retention_days}
    except OSError:
        pass
    services = {k.split("/")[0]: v for k, v in ctx.status.items() if k.endswith("/available")}
    return web.json_response({"cameras": cams, "disk": disk, "services": services,
                              "armed": await asyncio.to_thread(ctx.alerts.armed), "ts": now})


# -- armed ------------------------------------------------------------------------------------

async def get_armed(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response({"armed": await asyncio.to_thread(ctx.alerts.armed)})


async def set_armed(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await request.json()
    on = bool(body.get("armed"))
    await asyncio.to_thread(ctx.alerts.set_armed, on)
    if ctx.mqtt:  # the alerter (and Home Assistant later) follow this topic
        ctx.mqtt.publish("alerts/armed/set", "on" if on else "off")
    log.info("alerts %s by %s", "armed" if on else "disarmed", request[SESSION].user.username)
    return web.json_response({"armed": on})


# -- events -------------------------------------------------------------------------------------

def _public_event(e: dict) -> dict:
    e = dict(e)
    e.pop("snapshot", None)       # file paths stay server-side
    e.pop("alert_image", None)
    return e


async def events(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    q = request.query
    try:
        limit = int(q.get("limit", "50"))
    except ValueError:
        limit = 50
    rows = await asyncio.to_thread(
        ctx.queries.events, cameras=_list(q, "camera"), labels=_list(q, "label"),
        since=_float(q, "since"), until=_float(q, "until"), before=_float(q, "before"),
        alert=q.get("alert") or None, zone=q.get("zone") or None, limit=limit)
    return web.json_response([_public_event(e) for e in rows])


async def event(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    e = await asyncio.to_thread(ctx.queries.event, request.match_info["id"])
    if e is None:
        raise web.HTTPNotFound()
    return web.json_response(_public_event(e))


@lru_cache(maxsize=256)
def _render_snapshot(path: str, mtime: float, box: tuple, label: str, width: int) -> bytes | None:
    import cv2

    from ..detector.viewer import GREEN, draw_box
    img = cv2.imread(path)
    if img is None:
        return None
    if box:
        draw_box(img, box, GREEN, label)
    if width and img.shape[1] > width:
        h = round(img.shape[0] * width / img.shape[1])
        img = cv2.resize(img, (width, h), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes() if ok else None


def _data_file(ctx: WebContext, rel: str | None) -> Path | None:
    """A file under the data dir, by a path read from the database (never from the request)."""
    if not rel:
        return None
    p = (ctx.data_dir / rel).resolve()
    if not p.is_relative_to(ctx.data_dir.resolve()) or not p.is_file():
        return None
    return p


async def snapshot(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    e = await asyncio.to_thread(ctx.queries.event, request.match_info["id"])
    p = _data_file(ctx, e and e["snapshot"])
    if p is None:
        raise web.HTTPNotFound()
    try:
        width = max(0, min(int(request.query.get("w", "0")), 4000))
    except ValueError:
        width = 0
    box = () if request.query.get("box") == "0" else tuple(e["box"])
    body = await asyncio.to_thread(_render_snapshot, str(p), p.stat().st_mtime, box,
                                   f"{e['label']} {e['top_score']:.2f}", width)
    if body is None:
        raise web.HTTPNotFound()
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=60"})


async def alert_image(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    e = await asyncio.to_thread(ctx.queries.event, request.match_info["id"])
    p = _data_file(ctx, e and e["alert_image"])
    if p is None:
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=3600"})


async def clip(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    try:
        got = await asyncio.to_thread(ctx.playback.event_clip, request.match_info["id"])
    except RuntimeError as exc:
        log.warning("clip for %s failed: %s", request.match_info["id"], exc)
        return web.json_response({"error": "Couldn't build the clip."}, status=500)
    if got is None:
        return web.json_response({"error": "No recording covers this event yet. Recordings are saved in "
                                           "1-minute pieces, so try again in a minute."}, status=404)
    path, meta = got
    return web.FileResponse(path, headers={
        "Content-Type": "video/mp4", "Cache-Control": "no-store",
        "X-Clip-Partial": "1" if meta["partial"] else "0",
        "Content-Disposition": f'inline; filename="{path.name}"'})


# -- recordings (timeline) ---------------------------------------------------------------------------

def _camera(ctx: WebContext, cam_id: str):
    cam = ctx.cfg.cameras.get(cam_id)
    if cam is None:
        raise web.HTTPNotFound(text="unknown camera")
    return cam


async def timeline(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    q = request.query
    cam = _camera(ctx, q.get("camera", ""))
    now = time.time()
    start, end = _float(q, "start") or now - 86400, _float(q, "end") or now
    return web.json_response(await asyncio.to_thread(ctx.playback.timeline, cam.id, start, end))


async def segments(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    q = request.query
    cam = _camera(ctx, q.get("camera", ""))
    now = time.time()
    start, end = _float(q, "start") or now - 3600, _float(q, "end") or now
    return web.json_response(await asyncio.to_thread(ctx.playback.segments, cam.id, start, end))


async def recording(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    seg = await asyncio.to_thread(ctx.playback.index.by_id, int(request.match_info["id"]))
    if seg is None:
        raise web.HTTPNotFound()
    p = (ctx.recordings_root / seg.path).resolve()
    if not p.is_relative_to(ctx.recordings_root.resolve()) or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"})


async def download(request: web.Request) -> web.StreamResponse:
    """A stretch of recording as one MP4 (max 30 minutes), for saving or sharing."""
    ctx = request.app[CTX]
    q = request.query
    cam = _camera(ctx, q.get("camera", ""))
    start, end = _float(q, "start"), _float(q, "end")
    if start is None or end is None or not 0 < end - start <= 1800:
        return web.json_response({"error": "Pick a range of up to 30 minutes."}, status=400)
    from ..playback import build_clip
    out = ctx.data_dir / "clips" / "_exports" / f"{cam.id}-{int(start)}-{int(end)}.mp4"
    try:
        meta = await asyncio.to_thread(build_clip, ctx.playback.index, ctx.recordings_root, cam.id, start, end, out)
    except RuntimeError as exc:
        log.warning("export failed: %s", exc)
        return web.json_response({"error": "Couldn't build the video."}, status=500)
    if meta is None:
        return web.json_response({"error": "Nothing was recorded in that range."}, status=404)
    stamp = time.strftime("%Y-%m-%d_%H%M", time.localtime(start))
    return web.FileResponse(out, headers={"Content-Type": "video/mp4", "Cache-Control": "no-store",
                                          "Content-Disposition": f'attachment; filename="{cam.id}_{stamp}.mp4"'})


# -- a still frame from a camera (posters, zone editor) ------------------------------------------------

async def frame(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    cam = _camera(ctx, request.match_info["cam"])
    stream = cam.sub_stream if request.query.get("q") == "low" and cam.sub_stream else cam.main_stream
    try:
        async with ctx.session.get(f"{ctx.cfg.go2rtc_api}/api/frame.jpeg", params={"src": stream},
                                   timeout=aiohttp.ClientTimeout(total=12)) as r:
            if r.status != 200:
                raise web.HTTPServiceUnavailable(text="camera unavailable")
            body = await r.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPServiceUnavailable(text="camera unavailable") from exc
    return web.Response(body=body, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


def register(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/cameras", cameras)
    r.add_get("/api/status", status)
    r.add_get("/api/armed", get_armed)
    r.add_post("/api/armed", set_armed)
    r.add_get("/api/events", events)
    r.add_get("/api/events/{id}", event)
    r.add_get("/media/snapshot/{id}.jpg", snapshot)
    r.add_get("/media/alert/{id}.jpg", alert_image)
    r.add_get("/media/clip/{id}.mp4", clip)
    r.add_get("/media/frame/{cam}.jpg", frame)
    r.add_get("/api/timeline", timeline)
    r.add_get("/api/segments", segments)
    r.add_get("/media/recording/{id:\\d+}.mp4", recording)
    r.add_get("/media/export.mp4", download)
