"""Settings API: read and change cameras.yml from the app."""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
from aiohttp import web

from ..config import DEFAULT_LABELS, ConfigError
from .app import CTX, SESSION, WebContext
from .config_edit import ConfigEditor, env_refs

log = logging.getLogger(__name__)

# Objects the model can find that make sense for a home camera (COCO names).
OBJECTS = ["person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat", "bird", "horse", "bear",
           "backpack", "umbrella", "handbag", "suitcase"]


def _editor(ctx: WebContext) -> ConfigEditor:
    if ctx.editor is None:
        ctx.editor = ConfigEditor(ctx.config_path)
    return ctx.editor


async def get_config(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    raw = await asyncio.to_thread(_editor(ctx).read)
    return web.json_response({
        "raw": raw, "env_vars": env_refs(raw), "objects": OBJECTS, "default_labels": list(DEFAULT_LABELS),
        "defaults": {"alert_labels": ["person"], "cooldown_seconds": 60},
    })


async def _apply(request: web.Request, op, *args) -> web.Response:
    ctx = request.app[CTX]
    try:
        _old, new, changed = await asyncio.to_thread(op, *args)
    except ConfigError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    ctx.cfg = new
    if ctx.playback is not None:
        ctx.playback.segment_seconds = {c.id: c.record.segment_seconds for c in new.cameras.values()}
    if "go2rtc" in changed:
        try:
            async with ctx.session.post(f"{new.go2rtc_api}/api/restart",
                                        timeout=aiohttp.ClientTimeout(total=5)) as r:
                if r.status >= 400:
                    log.warning("go2rtc restart returned %s", r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("couldn't restart go2rtc: %s", exc)
    services = sorted(changed - {"go2rtc"})
    if services and ctx.mqtt:
        mtime = ctx.config_path.stat().st_mtime
        for svc in services:
            ctx.mqtt.publish(f"config/changed/{svc}", {"mtime": mtime, "ts": time.time()}, retain=True)
    log.info("settings saved by %s; restarting: %s", request[SESSION].user.username,
             ", ".join(sorted(changed)) or "nothing")
    from .api import camera_dict
    return web.json_response({"ok": True, "restarted": sorted(changed),
                              "cameras": [camera_dict(c) for c in new.cameras.values()]})


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except ValueError as exc:
        raise web.HTTPBadRequest(text="bad json") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    return body


async def add_camera(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await _body(request)
    cam_id = str(body.pop("id", "")).strip()
    return await _apply(request, _editor(ctx).save_camera, cam_id, body, True)


async def save_camera(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return await _apply(request, _editor(ctx).save_camera, request.match_info["id"], await _body(request), False)


async def delete_camera(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return await _apply(request, _editor(ctx).delete_camera, request.match_info["id"])


async def save_settings(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return await _apply(request, _editor(ctx).save_settings, await _body(request))


def register(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/config", get_config)
    r.add_post("/api/config/cameras", add_camera)
    r.add_put("/api/config/cameras/{id}", save_camera)
    r.add_delete("/api/config/cameras/{id}", delete_camera)
    r.add_put("/api/config/settings", save_settings)
