"""Phone alerts: each person's Pushover key and choices, and the admin's app token."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time

from aiohttp import web

from ..alerts.notify import PUSHOVER_URL, Message, PushoverNotifier, validate_user
from ..alerts.recipients import Directory, set_setting
from .app import CTX, SESSION, _json

log = logging.getLogger(__name__)

KEY = re.compile(r"^[A-Za-z0-9]{30}$")      # Pushover user, group and app keys
_last_test: dict[str, float] = {}


def _url() -> str:
    return os.environ.get("BB_PUSHOVER_URL", PUSHOVER_URL)


def _web_db(ctx):
    return ctx.data_dir / "web.db"


def _token(ctx) -> str:
    return Directory(_web_db(ctx)).load()[0]


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"error": msg}, status=status)


def _mine(ctx, username: str) -> dict:
    p = ctx.users.prefs(username)
    d = Directory(_web_db(ctx))
    return {"pushover_user": p.get("pushover_user", ""), "cameras": p.get("alert_cameras"),
            "health": p.get("alert_health", True) is not False, "ready": bool(d.token_source())}


async def get_mine(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response(await asyncio.to_thread(_mine, ctx, request[SESSION].user.username))


async def save_mine(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    user = request[SESSION].user.username
    body = await _json(request)
    changes: dict = {}
    note = ""
    if "pushover_user" in body:
        key = str(body["pushover_user"] or "").strip()
        if key and not KEY.match(key):
            return _err("A Pushover user key is 30 letters and numbers. Copy it from the top of pushover.net after signing in.")
        if key:
            token = await asyncio.to_thread(_token, ctx)
            if token:
                ok, note = await asyncio.to_thread(validate_user, token, key, _url())
                if not ok:
                    return _err(note)
        changes["pushover_user"] = key or None
    if "cameras" in body:
        cams = body["cameras"]
        if cams is not None:
            if not isinstance(cams, list) or any(c not in ctx.cfg.cameras for c in cams):
                return _err("Unknown camera.")
        changes["alert_cameras"] = cams
    if "health" in body:
        changes["alert_health"] = None if body["health"] else False
    await asyncio.to_thread(ctx.users.update_prefs, user, changes)
    out = await asyncio.to_thread(_mine, ctx, user)
    out["note"] = note
    return web.json_response(out)


async def test_mine(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    user = request[SESSION].user.username
    now = time.monotonic()
    if now - _last_test.get(user, 0) < 10:
        return _err("Give it a few seconds between tests.", 429)
    _last_test[user] = now
    prefs = await asyncio.to_thread(ctx.users.prefs, user)
    key = prefs.get("pushover_user")
    token = await asyncio.to_thread(_token, ctx)
    if not key:
        return _err("Add your Pushover user key first.")
    if not token:
        return _err("An admin needs to add the Pushover app token under Settings, Alerts.")

    def send():
        from ..alerts.service import render_alert_image
        from ..alerts.verifier import fetch_frame
        image = None
        cams = [c for c in ctx.cfg.cameras.values() if prefs.get("alert_cameras") in (None, []) or
                c.id in prefs.get("alert_cameras")]
        if cams:
            frame = fetch_frame(ctx.cfg.go2rtc_api, cams[0].main_stream)
            if frame is not None:
                h, w = frame.shape[:2]
                image = render_alert_image(frame, (0, 0, w - 1, h - 1), "test")
        url = ctx.cfg.alerts.public_url
        n = PushoverNotifier(key, token, ctx.cfg.alerts.priority, ctx.cfg.alerts.sound, url=_url(), attempts=1)
        return n.send(Message(title="BigBrother test",
                              body=f"Phone alerts work for {user}." + (f" Picture from {cams[0].name}." if image else ""),
                              image_jpeg=image, url=url, url_title="Open BigBrother" if url else "",
                              timestamp=int(time.time())))

    res = await asyncio.to_thread(send)
    if not res.ok:
        return _err(f"Pushover didn't take it: {res.detail}", 502)
    return web.json_response({"ok": True})


# -- admin -------------------------------------------------------------------------------

async def get_settings(request: web.Request) -> web.Response:
    ctx = request.app[CTX]

    def read():
        d = Directory(_web_db(ctx))
        _, people = d.load()
        return {"token_source": d.token_source(), "people": [{"name": r.name, "cameras": sorted(r.cameras)
                if r.cameras is not None else None, "health": r.health} for r in people]}
    return web.json_response(await asyncio.to_thread(read))


async def save_settings(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await _json(request)
    token = str(body.get("token") or "").strip()
    if token and not KEY.match(token):
        return _err("A Pushover app token is 30 letters and numbers, shown on your application's page at pushover.net.")
    if token:
        ok, note = await asyncio.to_thread(validate_user, token, "u" * 30, _url())
        if not ok and "token" in note:
            return _err("Pushover rejected that app token. Check it on your application's page at pushover.net.")
    await asyncio.to_thread(set_setting, _web_db(ctx), "pushover_token", token or None)
    log.info("Pushover app token %s by %s", "set" if token else "cleared", request[SESSION].user.username)
    return await get_settings(request)


def register(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/me/notify", get_mine)
    r.add_put("/api/me/notify", save_mine)
    r.add_post("/api/me/notify/test", test_mine)
    r.add_get("/api/notify/settings", get_settings)
    r.add_put("/api/notify/settings", save_settings)
