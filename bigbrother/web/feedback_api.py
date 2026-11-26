"""Routes: event verdicts, missed-detection reports, and the test-set export."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from pathlib import Path

from aiohttp import web

from .app import CTX, SESSION, WebContext
from .feedback import MISS_SECONDS, VERDICTS, ExportJob, build_testset

log = logging.getLogger(__name__)

LABELS = {"person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat", "bird", "horse", "bear",
          "backpack", "umbrella", "handbag", "suitcase", "other"}


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text="bad json") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    return body


async def set_feedback(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    eid = request.match_info["id"]
    if await asyncio.to_thread(ctx.queries.event, eid) is None:
        raise web.HTTPNotFound()
    body = await _body(request)
    verdict, label = body.get("verdict"), body.get("label")
    if verdict not in VERDICTS:
        return web.json_response({"error": "Pick correct, false alarm or wrong object."}, status=400)
    if verdict == "wrong_label" and label not in LABELS:
        return web.json_response({"error": "Pick what the object really was."}, status=400)
    await asyncio.to_thread(ctx.feedback.set, eid, verdict, label, str(body.get("note", "")),
                            request[SESSION].user.username)
    return web.json_response({"ok": True})


async def clear_feedback(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    await asyncio.to_thread(ctx.feedback.clear, request.match_info["id"])
    return web.json_response({"ok": True})


async def _keep_miss_clip(ctx: WebContext, mid: str, camera: str, ts: float) -> None:
    """Save the moment's clip so it outlives the 24/7 retention. If it was reported at the
    live edge, wait for its minute to be recorded first."""
    from ..playback import build_clip
    out = ctx.data_dir / "feedback" / "missed" / f"{mid}.mp4"
    for attempt in range(6):
        try:
            meta = await asyncio.to_thread(build_clip, ctx.playback.index, ctx.recordings_root, camera,
                                           ts - MISS_SECONDS, ts + MISS_SECONDS, out)
        except RuntimeError as exc:
            log.warning("missed-detection clip %s failed: %s", mid, exc)
            meta = None
        if meta and not meta["partial"]:
            break
        await asyncio.sleep(40)
    if out.exists():
        await asyncio.to_thread(ctx.feedback.set_miss_clip, mid, str(out.relative_to(ctx.data_dir)))


async def add_miss(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await _body(request)
    cam = ctx.cfg.cameras.get(str(body.get("camera", "")))
    try:
        ts = float(body.get("ts"))
    except (TypeError, ValueError):
        ts = 0
    label = body.get("label")
    if cam is None or not 0 < ts <= time.time() + 5 or label not in LABELS:
        return web.json_response({"error": "Pick a camera, a moment and what was missed."}, status=400)
    mid = await asyncio.to_thread(ctx.feedback.add_miss, cam.id, ts, label, str(body.get("note", "")),
                                  request[SESSION].user.username)
    asyncio.get_running_loop().create_task(_keep_miss_clip(ctx, mid, cam.id, ts))
    return web.json_response({"id": mid})


async def list_misses(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    rows = await asyncio.to_thread(ctx.feedback.misses)
    for r in rows:
        r.pop("clip", None)
    return web.json_response(rows)


async def delete_miss(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    rel = await asyncio.to_thread(ctx.feedback.delete_miss, request.match_info["id"])
    if rel:
        (ctx.data_dir / rel).unlink(missing_ok=True)
    return web.json_response({"ok": True})


async def miss_clip(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    rows = await asyncio.to_thread(ctx.feedback.misses)
    m = next((r for r in rows if r["id"] == request.match_info["id"]), None)
    if not m or not m["clip"]:
        raise web.HTTPNotFound()
    p = (ctx.data_dir / m["clip"]).resolve()
    if not p.is_relative_to(ctx.data_dir.resolve()) or not p.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(p, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=3600"})


async def feedback_counts(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response(await asyncio.to_thread(ctx.feedback.counts))


# -- export --------------------------------------------------------------------------------

def _job_dict(job: ExportJob) -> dict:
    return {"id": job.id, "state": job.state, "done": job.done, "total": job.total, "size": job.size,
            "error": job.error, "file": job.path.name if job.path else None}


async def start_export(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await _body(request)
    running = [j for j in ctx.exports.values() if j.state == "running"]
    if running:
        return web.json_response(_job_dict(running[0]))
    try:
        extra = max(0, min(int(body.get("include_unreviewed", 0)), 200))
    except (TypeError, ValueError):
        extra = 0
    out_dir = ctx.data_dir / "exports"
    # Only the newest export is kept.
    for old in list(ctx.exports.values()):
        if old.path:
            old.path.unlink(missing_ok=True)
    ctx.exports.clear()
    if out_dir.exists():
        for p in out_dir.glob("*.zip"):
            p.unlink(missing_ok=True)
    job = ExportJob(secrets.token_hex(6))
    ctx.exports[job.id] = job
    threading.Thread(target=build_testset, name="export", daemon=True, kwargs=dict(
        job=job, cfg=ctx.cfg, queries=ctx.queries, feedback=ctx.feedback, playback=ctx.playback,
        data_dir=ctx.data_dir, include_unreviewed=extra, out_dir=out_dir)).start()
    log.info("test set export started by %s", request[SESSION].user.username)
    return web.json_response(_job_dict(job))


async def export_status(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    job = ctx.exports.get(request.match_info["id"])
    if job is None:
        raise web.HTTPNotFound()
    return web.json_response(_job_dict(job))


async def export_file(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    job = ctx.exports.get(request.match_info["id"])
    if job is None or job.state != "done" or not job.path or not Path(job.path).is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(job.path, headers={"Content-Type": "application/zip", "Cache-Control": "no-store",
                                               "Content-Disposition": f'attachment; filename="{job.path.name}"'})


def register(app: web.Application) -> None:
    r = app.router
    r.add_put("/api/events/{id}/feedback", set_feedback)
    r.add_delete("/api/events/{id}/feedback", clear_feedback)
    r.add_get("/api/feedback", feedback_counts)
    r.add_get("/api/misses", list_misses)
    r.add_post("/api/misses", add_miss)
    r.add_delete("/api/misses/{id}", delete_miss)
    r.add_get("/media/missed/{id}.mp4", miss_clip)
    r.add_post("/api/testset", start_export)
    r.add_get("/api/testset/{id}", export_status)
    r.add_get("/media/testset/{id}.zip", export_file)
