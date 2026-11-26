"""Live video: go2rtc's WebSocket (MSE) and HLS endpoints, proxied behind the login.

The browser player (go2rtc's video-rtc.js) talks to /go2rtc/api/ws?src=<stream>
exactly as it would to go2rtc itself. Only streams of configured cameras are
allowed, and only the MSE/HLS paths: no WebRTC, no go2rtc admin API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

import aiohttp
from aiohttp import WSMsgType, web

from .app import CTX, SESSION

log = logging.getLogger(__name__)


def device(ua: str) -> str:
    """'iPhone iOS 18.1 Chrome 131' from a user agent, for log lines."""
    m = re.search(r"(iPhone|iPad)[^)]*OS (\d+)_(\d+)", ua)
    name = f"{m.group(1)} iOS {m.group(2)}.{m.group(3)}" if m else \
        ("Android " + re.search(r"Android ([\d.]+)", ua).group(1) if "Android" in ua else
         "Mac" if "Mac OS" in ua else "Windows" if "Windows" in ua else "Linux" if "Linux" in ua else "browser")
    for pat, label in ((r"CriOS/(\d+)", "Chrome"), (r"EdgiOS/(\d+)|Edg/(\d+)", "Edge"), (r"FxiOS/(\d+)|Firefox/(\d+)", "Firefox"),
                       (r"Chrome/(\d+)", "Chrome"), (r"Version/(\d+)[\d.]* .*Safari", "Safari")):
        b = re.search(pat, ua)
        if b:
            return f"{name} {label} {next(g for g in b.groups() if g)}"
    return name


async def ws_proxy(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    src = request.query.get("src", "")
    if src not in ctx.streams():
        raise web.HTTPNotFound(text="unknown stream")
    upstream_url = ctx.cfg.go2rtc_api.replace("http", "ws", 1) + "/api/ws"
    # No permessage-deflate: H.264 doesn't compress, and deflating every chunk on the app's
    # one thread only adds CPU and delay (aiohttp would otherwise enable it for every browser).
    client = web.WebSocketResponse(heartbeat=30, max_msg_size=0, compress=False)
    await client.prepare(request)
    t0, sent, ended = time.monotonic(), [0], "?"
    upstream = None
    try:
        async with ctx.session.ws_connect(upstream_url, params={"src": src}, max_msg_size=0,
                                          heartbeat=30) as upstream:
            async def down():  # go2rtc -> browser
                try:
                    async for msg in upstream:
                        if msg.type == WSMsgType.BINARY:
                            sent[0] += len(msg.data)
                            await client.send_bytes(msg.data)
                        elif msg.type == WSMsgType.TEXT:
                            if '"error"' in msg.data:
                                log.warning("live %s: go2rtc says %s", src, msg.data[:300])
                            await client.send_str(msg.data)
                        else:
                            break
                except ConnectionResetError:
                    pass   # the viewer closed the stream (switched page, locked the phone)

            async def up():  # browser -> go2rtc (codec negotiation messages)
                try:
                    async for msg in client:
                        if msg.type == WSMsgType.TEXT:
                            if '"webrtc' in msg.data:   # WebRTC isn't proxied; MSE/HLS only
                                continue
                            await upstream.send_str(msg.data)
                        elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                            break
                except ConnectionResetError:
                    pass

            tasks = [asyncio.create_task(down(), name="down"), asyncio.create_task(up(), name="up")]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            first = next(iter(done)).get_name()
            for t in pending:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if first == "down":
                exc = upstream.exception()
                ended = (f"go2rtc ended it (code {upstream.close_code}{', ' + type(exc).__name__ if exc else ''})"
                         if not client.closed else "the browser went away while sending")
            else:
                exc = client.exception()
                ended = ("no reply to the keep-alive ping: connection lost or stuck" if exc and "Timeout" in type(exc).__name__
                         else f"the browser closed it (code {client.close_code})")
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        ended = f"go2rtc unavailable ({exc})"
        log.warning("live %s: go2rtc unavailable (%s)", src, exc)
        if not client.closed:
            await client.send_str('{"type":"error","value":"live video service unavailable"}')
    except ConnectionResetError:
        ended = "connection reset"
    finally:
        if not client.closed:
            await client.close()
        sess = request.get(SESSION)
        secs = time.monotonic() - t0
        log.info("live %s for %s (%s): %dm%02ds, %.1f MB, %s", src, sess.user.username if sess else "?",
                 device(request.headers.get("User-Agent", "")), secs // 60, secs % 60, sent[0] / 1e6, ended)
    return client


async def player_report(request: web.Request) -> web.Response:
    """Problems seen by the live player in someone's browser, written to the log."""
    sess = request[SESSION]
    try:
        body = await request.json()
        events = list(body.get("events", []))[:60]
    except (ValueError, AttributeError, TypeError):
        raise web.HTTPBadRequest() from None
    who = f"{sess.user.username} ({device(request.headers.get('User-Agent', ''))})"
    for ev in events:
        if not isinstance(ev, dict):
            continue
        clean = {str(k)[:20]: (v if isinstance(v, (int, float, bool)) or v is None else str(v)[:200])
                 for k, v in list(ev.items())[:14]}
        log.info("player %s %s %s: %s", who, clean.pop("stream", "?"), clean.pop("kind", "?"),
                 json.dumps(clean, separators=(",", ":")))
    return web.json_response({"ok": True})


async def hls_proxy(request: web.Request) -> web.StreamResponse:
    """HLS fallback for older iPhones: playlist and segments, by go2rtc session id."""
    ctx = request.app[CTX]
    tail = request.match_info["tail"]
    if not tail.replace(".", "").replace("_", "").isalnum():
        raise web.HTTPNotFound()
    try:
        async with ctx.session.get(f"{ctx.cfg.go2rtc_api}/api/hls/{tail}", params=request.query,
                                   timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.read()
            return web.Response(body=body, status=r.status,
                                content_type=r.headers.get("Content-Type", "application/octet-stream").split(";")[0],
                                headers={"Cache-Control": "no-cache"})
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise web.HTTPServiceUnavailable() from exc


def register(app: web.Application) -> None:
    app.router.add_get("/go2rtc/api/ws", ws_proxy)
    app.router.add_post("/api/diag/live", player_report)
    app.router.add_get("/go2rtc/api/hls/{tail}", hls_proxy)
