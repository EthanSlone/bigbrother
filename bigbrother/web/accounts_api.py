"""Accounts: the admin's Users page, and each person's own security settings."""

from __future__ import annotations

import asyncio
import io
import logging

from aiohttp import web

from .app import CTX, SESSION, _json
from .auth import ROLES, AuthError, totp_uri

log = logging.getLogger(__name__)


def _err(exc: Exception, status: int = 400) -> web.Response:
    return web.json_response({"error": str(exc)}, status=status)


# -- admin: users ------------------------------------------------------------------------

async def list_users(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    return web.json_response(await asyncio.to_thread(ctx.users.overview))


async def add_user(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    body = await _json(request)
    role = body.get("role", "member")
    try:
        u = await asyncio.to_thread(ctx.users.add, str(body.get("username", "")), str(body.get("password", "")),
                                    role if role in ROLES else "")
    except AuthError as exc:
        return _err(exc)
    log.info("account %s (%s) added by %s", u.username, u.role, request[SESSION].user.username)
    return web.json_response({"username": u.username, "role": u.role})


async def change_user(request: web.Request) -> web.Response:
    """Change someone's role, set a new password for them, or switch off their two-factor
    (e.g. a lost phone). Their devices are signed out for the last two."""
    ctx = request.app[CTX]
    name = request.match_info["name"]
    body = await _json(request)
    me = request[SESSION].user
    user = await asyncio.to_thread(ctx.users.get, name)
    if user is None:
        raise web.HTTPNotFound()
    try:
        if "role" in body:
            await asyncio.to_thread(ctx.users.set_role, name, str(body["role"]))
        if body.get("password"):
            await asyncio.to_thread(ctx.users.set_password, name, str(body["password"]))
        if body.get("reset_twofa"):
            await asyncio.to_thread(ctx.users.totp_disable, name)
            await asyncio.to_thread(ctx.users.end_all_sessions, user)
    except AuthError as exc:
        return _err(exc)
    log.info("account %s changed by %s: %s", name, me.username, ", ".join(k for k in body if body[k]))
    return web.json_response({"ok": True})


async def delete_user(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    name = request.match_info["name"]
    if name.lower() == request[SESSION].user.username.lower():
        return _err(AuthError("You can't remove your own account."))
    try:
        if not await asyncio.to_thread(ctx.users.delete, name):
            raise web.HTTPNotFound()
    except AuthError as exc:
        return _err(exc)
    log.info("account %s removed by %s", name, request[SESSION].user.username)
    return web.json_response({"ok": True})


# -- me: two-factor and devices ----------------------------------------------------------------

def _qr_svg(uri: str) -> str:
    import segno
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0f171e", light="#ffffff",
                                    xmldecl=False, svgns=True, nl=False)
    return buf.getvalue().decode()


async def twofa_begin(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    me = request[SESSION].user
    secret = await asyncio.to_thread(ctx.users.totp_begin, me.username)
    uri = totp_uri(secret, me.username)
    return web.json_response({"secret": secret, "uri": uri, "qr": await asyncio.to_thread(_qr_svg, uri)})


async def twofa_confirm(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    me = request[SESSION].user
    body = await _json(request)
    try:
        codes = await asyncio.to_thread(ctx.users.totp_confirm, me.username, str(body.get("code", "")))
    except AuthError as exc:
        return _err(exc)
    log.info("two-factor turned on by %s", me.username)
    return web.json_response({"recovery_codes": codes})


async def twofa_disable(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    me = request[SESSION].user
    body = await _json(request)
    ip = ctx.client_ip(request)
    if ctx.limiter.retry_after(ip, me.username) > 0:
        return _err(AuthError("Too many failed attempts. Try again later."), 429)
    if await asyncio.to_thread(ctx.users.authenticate, me.username, str(body.get("password", ""))) is None:
        ctx.limiter.failed(ip, me.username)
        return _err(AuthError("Password is wrong."), 403)
    await asyncio.to_thread(ctx.users.totp_disable, me.username)
    log.info("two-factor turned off by %s", me.username)
    return web.json_response({"ok": True})


async def my_devices(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    sess = request[SESSION]
    rows = await asyncio.to_thread(ctx.users.sessions_for, sess.user)
    for r in rows:
        r["current"] = r.pop("hash") == sess.token_hash
    return web.json_response(rows)


async def sign_out_others(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    sess = request[SESSION]
    n = await asyncio.to_thread(ctx.users.end_all_sessions, sess.user, sess.token_hash)
    return web.json_response({"signed_out": n})


def register(app: web.Application) -> None:
    r = app.router
    r.add_get("/api/users", list_users)
    r.add_post("/api/users", add_user)
    r.add_put("/api/users/{name}", change_user)
    r.add_delete("/api/users/{name}", delete_user)
    r.add_post("/api/me/2fa/begin", twofa_begin)
    r.add_post("/api/me/2fa/confirm", twofa_confirm)
    r.add_post("/api/me/2fa/disable", twofa_disable)
    r.add_get("/api/me/devices", my_devices)
    r.add_post("/api/me/devices/sign-out-others", sign_out_others)
