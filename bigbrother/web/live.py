"""Live video: go2rtc's WebSocket (MSE) and HLS endpoints, proxied behind the login.

The browser player (go2rtc's video-rtc.js) talks to /go2rtc/api/ws?src=<stream>
exactly as it would to go2rtc itself. Only streams of configured cameras are
allowed, and only the MSE/HLS paths: no WebRTC, no go2rtc admin API.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
from aiohttp import WSMsgType, web

from .app import CTX

log = logging.getLogger(__name__)


async def ws_proxy(request: web.Request) -> web.StreamResponse:
    ctx = request.app[CTX]
    src = request.query.get("src", "")
    if src not in ctx.streams():
        raise web.HTTPNotFound(text="unknown stream")
    upstream_url = ctx.cfg.go2rtc_api.replace("http", "ws", 1) + "/api/ws"
    client = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await client.prepare(request)
    try:
        async with ctx.session.ws_connect(upstream_url, params={"src": src}, max_msg_size=0,
                                          heartbeat=30) as upstream:
            async def down():  # go2rtc -> browser
                async for msg in upstream:
                    if msg.type == WSMsgType.BINARY:
                        await client.send_bytes(msg.data)
                    elif msg.type == WSMsgType.TEXT:
                        await client.send_str(msg.data)
                    else:
                        break

            async def up():  # browser -> go2rtc (codec negotiation messages)
                async for msg in client:
                    if msg.type == WSMsgType.TEXT:
                        if '"webrtc' in msg.data:   # WebRTC isn't proxied; MSE/HLS only
                            continue
                        await upstream.send_str(msg.data)
                    elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                        break

            tasks = [asyncio.create_task(down()), asyncio.create_task(up())]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("live %s: go2rtc unavailable (%s)", src, exc)
        if not client.closed:
            await client.send_str('{"type":"error","value":"live video service unavailable"}')
    except ConnectionResetError:
        pass
    finally:
        if not client.closed:
            await client.close()
    return client


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
    app.router.add_get("/go2rtc/api/hls/{tail}", hls_proxy)
