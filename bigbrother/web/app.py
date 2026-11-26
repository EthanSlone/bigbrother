"""The BigBrother web app: login, live view, events, recordings and settings.

This is the one service meant to be reachable from outside (through the
reverse proxy). Everything except the login page and static files needs a
session. Live video is go2rtc's MSE/HLS stream, proxied through here so it
shares the login and needs no extra ports.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from ..alerts_db import AlertStore
from ..config import Config
from .auth import AuthError, RateLimiter, UserStore, new_setup_code, setup_code_matches
from .data import EventQueries

log = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
COOKIE = "bb_session"
CSRF_HEADER = "X-BigBrother"       # custom header: browsers won't send it cross-site without CORS
PUBLIC = {"/", "/index.html", "/manifest.webmanifest", "/sw.js", "/api/session", "/api/login", "/api/setup"}
VERSION = "1"


def _nets(spec: str) -> list[ipaddress._BaseNetwork]:
    out = []
    for part in (p.strip() for p in spec.split(",")):
        if part:
            try:
                out.append(ipaddress.ip_network(part, strict=False))
            except ValueError:
                log.warning("ignoring bad trusted proxy %r", part)
    return out


@dataclass
class WebContext:
    cfg: Config
    data_dir: Path
    recordings_root: Path
    users: UserStore
    queries: EventQueries
    alerts: AlertStore
    detector_url: str = "http://detector:5050"
    trusted_proxies: list = field(default_factory=list)
    limiter: RateLimiter = field(default_factory=RateLimiter)
    setup_code: str = ""
    playback: object = None          # Playback, created lazily (needs the recordings index)
    mqtt: object = None              # Publisher or None
    status: dict = field(default_factory=dict)   # retained MQTT state: topic -> payload
    session: aiohttp.ClientSession | None = None
    config_path: Path = Path("/config/cameras.yml")
    editor: object = None            # ConfigEditor, created on first use
    db_path: Path = Path("/data/bigbrother.db")
    _feedback: object = None
    exports: dict = field(default_factory=dict)   # test-set export jobs by id

    @property
    def feedback(self):
        if self._feedback is None:
            from .feedback import FeedbackStore
            self._feedback = FeedbackStore(self.db_path)
        return self._feedback

    # -- request helpers ------------------------------------------------------------
    def client_ip(self, request: web.Request) -> str:
        peer = request.remote or ""
        if not self._trusted(peer):
            return peer
        # Walk X-Forwarded-For from the right, skipping our own proxies.
        hops = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        for hop in reversed(hops):
            if not self._trusted(hop):
                return hop
        return hops[0] if hops else peer

    def is_https(self, request: web.Request) -> bool:
        if request.secure:
            return True
        return self._trusted(request.remote or "") and \
            request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip() == "https"

    def _trusted(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.trusted_proxies)

    def streams(self) -> set[str]:
        out = set()
        for cam in self.cfg.cameras.values():
            out.add(cam.main_stream)
            if cam.sub_stream:
                out.add(cam.sub_stream)
        return out


CTX = web.AppKey("ctx", WebContext)
# Per-request login session (RequestKey on aiohttp >= 3.14, a plain key before).
SESSION = web.RequestKey("session", object) if hasattr(web, "RequestKey") else "session"


# -- middlewares ----------------------------------------------------------------------

CSP = ("default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
       "connect-src 'self' ws: wss:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        resp = await handler(request)
    except web.HTTPException as exc:
        resp = exc
    if not isinstance(resp, web.WebSocketResponse) and not getattr(resp, "prepared", False):
        resp.headers.setdefault("Content-Security-Policy", CSP)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if isinstance(resp, web.HTTPException):
        raise resp
    return resp


@web.middleware
async def require_login(request: web.Request, handler):
    ctx = request.app[CTX]
    path = request.path
    if path in PUBLIC or path.startswith("/static/"):
        return await handler(request)
    sess = await asyncio.to_thread(ctx.users.session, request.cookies.get(COOKIE))
    if sess is None:
        if path.startswith("/api/") or path.startswith("/go2rtc/") or path.startswith("/media/"):
            return web.json_response({"error": "login required"}, status=401)
        raise web.HTTPFound("/")
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != "1":
        return web.json_response({"error": "missing request header"}, status=403)
    if request.headers.get("Upgrade", "").lower() == "websocket":
        origin, host = request.headers.get("Origin", ""), request.headers.get("Host", "")
        if not origin or origin.split("://", 1)[-1] != host:
            return web.json_response({"error": "cross-origin websocket refused"}, status=403)
    request[SESSION] = sess
    return await handler(request)


# -- auth routes ------------------------------------------------------------------------

def _set_cookie(ctx: WebContext, request: web.Request, resp: web.StreamResponse, token: str,
                persistent: bool) -> None:
    resp.set_cookie(COOKIE, token, httponly=True, samesite="Lax", secure=ctx.is_https(request), path="/",
                    max_age=30 * 86400 if persistent else None)


async def session_info(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    sess = await asyncio.to_thread(ctx.users.session, request.cookies.get(COOKIE))
    needs_setup = await asyncio.to_thread(ctx.users.count) == 0
    return web.json_response({
        "user": sess.user.username if sess else None, "setup": needs_setup, "version": VERSION,
    })


async def _json(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise web.HTTPBadRequest(text="bad json") from exc
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="bad json")
    return body


async def login(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    if request.headers.get(CSRF_HEADER) != "1":
        return web.json_response({"error": "missing request header"}, status=403)
    body = await _json(request)
    username, password = str(body.get("username", ""))[:64], str(body.get("password", ""))[:256]
    ip = ctx.client_ip(request)
    wait = ctx.limiter.retry_after(ip, username)
    if wait > 0:
        return web.json_response({"error": f"Too many failed attempts. Try again in {int(wait // 60) + 1} min."},
                                 status=429, headers={"Retry-After": str(int(wait) + 1)})
    user = await asyncio.to_thread(ctx.users.authenticate, username, password)
    if user is None:
        ctx.limiter.failed(ip, username)
        log.warning("failed login for %r from %s", username, ip)
        await asyncio.sleep(0.5)
        return web.json_response({"error": "Wrong username or password."}, status=401)
    persistent = bool(body.get("remember", True))
    token = await asyncio.to_thread(ctx.users.create_session, user, persistent, ip,
                                    request.headers.get("User-Agent", ""))
    log.info("login: %s from %s", user.username, ip)
    resp = web.json_response({"user": user.username})
    _set_cookie(ctx, request, resp, token, persistent)
    return resp


async def setup(request: web.Request) -> web.Response:
    """Create the first account. Needs the one-time code from the service log."""
    ctx = request.app[CTX]
    if request.headers.get(CSRF_HEADER) != "1":
        return web.json_response({"error": "missing request header"}, status=403)
    if await asyncio.to_thread(ctx.users.count) > 0:
        return web.json_response({"error": "Already set up. Log in instead."}, status=409)
    body = await _json(request)
    ip = ctx.client_ip(request)
    if ctx.limiter.retry_after(ip, "#setup") > 0:
        return web.json_response({"error": "Too many attempts. Wait a few minutes."}, status=429)
    if not setup_code_matches(str(body.get("code", "")), ctx.setup_code):
        ctx.limiter.failed(ip, "#setup")
        return web.json_response({"error": "That setup code is wrong. It's in the web service's log."},
                                 status=403)
    try:
        user = await asyncio.to_thread(ctx.users.add, str(body.get("username", "")), str(body.get("password", "")))
    except AuthError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    ctx.setup_code = ""
    log.info("first account created: %s from %s", user.username, ip)
    token = await asyncio.to_thread(ctx.users.create_session, user, True, ip, request.headers.get("User-Agent", ""))
    resp = web.json_response({"user": user.username})
    _set_cookie(ctx, request, resp, token, True)
    return resp


async def logout(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    await asyncio.to_thread(ctx.users.end_session, request.cookies.get(COOKIE))
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def change_password(request: web.Request) -> web.Response:
    ctx = request.app[CTX]
    sess = request[SESSION]
    body = await _json(request)
    ip = ctx.client_ip(request)
    if ctx.limiter.retry_after(ip, sess.user.username) > 0:
        return web.json_response({"error": "Too many failed attempts. Try again later."}, status=429)
    if await asyncio.to_thread(ctx.users.authenticate, sess.user.username, str(body.get("current", ""))) is None:
        ctx.limiter.failed(ip, sess.user.username)
        return web.json_response({"error": "Current password is wrong."}, status=403)
    try:
        await asyncio.to_thread(ctx.users.set_password, sess.user.username, str(body.get("new", "")))
    except AuthError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    # set_password signed out every session; give this device a fresh one.
    token = await asyncio.to_thread(ctx.users.create_session, sess.user, sess.persistent, ip,
                                    request.headers.get("User-Agent", ""))
    resp = web.json_response({"ok": True})
    _set_cookie(ctx, request, resp, token, sess.persistent)
    return resp


# -- static -----------------------------------------------------------------------------

async def index(request: web.Request) -> web.Response:
    html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{V}}", VERSION)
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-cache"})


async def manifest(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "manifest.webmanifest",
                            headers={"Content-Type": "application/manifest+json", "Cache-Control": "no-cache"})


async def service_worker(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "sw.js", headers={"Content-Type": "text/javascript", "Cache-Control": "no-cache"})


# -- app factory --------------------------------------------------------------------------

def create_app(ctx: WebContext) -> web.Application:
    from . import api, feedback_api, live, settings_api

    app = web.Application(middlewares=[security_headers, require_login], client_max_size=2 * 1024 * 1024)
    app[CTX] = ctx
    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_static("/static/", STATIC, append_version=False)
    app.router.add_get("/api/session", session_info)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/setup", setup)
    app.router.add_post("/api/logout", logout)
    app.router.add_post("/api/password", change_password)
    api.register(app)
    live.register(app)
    settings_api.register(app)
    feedback_api.register(app)

    async def on_startup(app: web.Application) -> None:
        ctx.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_connect=5))

    async def on_cleanup(app: web.Application) -> None:
        if ctx.session:
            await ctx.session.close()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def run(cfg: Config, data_dir: Path, db_path: Path, recordings_root: Path, port: int,
        config_path: Path = Path("/config/cameras.yml")) -> None:
    ctx = WebContext(
        cfg=cfg, data_dir=data_dir, recordings_root=recordings_root, config_path=config_path, db_path=db_path,
        users=UserStore(data_dir / "web.db"), queries=EventQueries(db_path), alerts=AlertStore(db_path),
        detector_url=os.environ.get("BB_DETECTOR_URL", "http://detector:5050").rstrip("/"),
        trusted_proxies=_nets(os.environ.get("BB_TRUSTED_PROXIES", "")),
    )
    from ..playback import Playback
    from ..events_db import EventStore
    from ..recorder.index import RecordingIndex
    ctx.playback = Playback(RecordingIndex(db_path), recordings_root, data_dir, EventStore(db_path),
                            {c.id: c.record.segment_seconds for c in cfg.cameras.values()})

    if ctx.users.count() == 0:
        ctx.setup_code = new_setup_code()
        log.warning("=" * 64)
        log.warning("No accounts yet. Open the app and create one with this setup code: %s", ctx.setup_code)
        log.warning("=" * 64)

    if os.environ.get("BB_MQTT", "1") != "0":
        from ..mqtt import Publisher
        ctx.mqtt = Publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix, "web")

        def on_status(topic: str, payload: bytes) -> None:
            key = topic[len(cfg.mqtt_prefix) + 1:]
            try:
                ctx.status[key] = json.loads(payload)
            except (json.JSONDecodeError, UnicodeDecodeError):
                ctx.status[key] = payload.decode(errors="replace")

        ctx.mqtt.subscribe("recorder/+/state", on_status)
        ctx.mqtt.subscribe("alerts/armed", on_status)
        ctx.mqtt.subscribe("+/available", on_status)
        ctx.mqtt.subscribe("health", on_status)
        ctx.mqtt.start()

    log.info("web app on port %d", port)
    web.run_app(create_app(ctx), port=port, access_log=None, print=None, handle_signals=True)
    if ctx.mqtt:
        ctx.mqtt.stop()
