"""Web app: accounts, sessions, rate limiting, and the API's access rules."""

import asyncio
import time
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from bigbrother.config import parse_config  # noqa: E402
from bigbrother.events_db import Event, EventStore  # noqa: E402
from bigbrother.web.auth import (AuthError, RateLimiter, UserStore, hash_password,  # noqa: E402
                                 setup_code_matches, verify_password)

PW = "correct horse battery"
H = {"X-BigBrother": "1"}


def test_password_hashing():
    h = hash_password(PW)
    assert h.startswith("scrypt$") and PW not in h
    assert verify_password(PW, h)
    assert not verify_password("wrong", h)
    assert not verify_password(PW, "garbage")
    assert hash_password(PW) != h  # salted


def test_users_and_sessions(tmp_path):
    users = UserStore(tmp_path / "web.db")
    with pytest.raises(AuthError):
        users.add("ethan", "short")
    with pytest.raises(AuthError):
        users.add("ethan", "ethan")
    u = users.add("ethan", PW)
    with pytest.raises(AuthError):
        users.add("ETHAN", PW)           # usernames are case-insensitive
    assert users.authenticate("ethan", PW) == u
    assert users.authenticate("ethan", "nope") is None
    assert users.authenticate("nobody", PW) is None

    token = users.create_session(u, persistent=True)
    assert users.session(token).user == u
    assert users.session("forged") is None
    other = users.create_session(u, persistent=False)
    users.set_password("ethan", PW + "!")   # changing the password signs out everywhere
    assert users.session(token) is None and users.session(other) is None
    t2 = users.create_session(u, persistent=False)
    users.end_session(t2)
    assert users.session(t2) is None


def test_expired_session_is_rejected(tmp_path):
    users = UserStore(tmp_path / "web.db")
    u = users.add("ethan", PW)
    token = users.create_session(u, persistent=False)
    users._db.execute("UPDATE sessions SET expires_ts = ?", (time.time() - 1,))
    assert users.session(token) is None


def test_rate_limiter():
    now = [1000.0]
    rl = RateLimiter(per_ip=3, per_user=2, window=60, clock=lambda: now[0])
    assert rl.retry_after("1.2.3.4", "ethan") == 0
    rl.failed("1.2.3.4", "ethan")
    rl.failed("5.6.7.8", "ethan")
    assert rl.retry_after("9.9.9.9", "ethan") > 0      # username limit
    assert rl.retry_after("1.2.3.4", "other") == 0
    now[0] += 61
    assert rl.retry_after("9.9.9.9", "ethan") == 0     # window passed


def test_setup_code_compare():
    assert setup_code_matches("abcd-efgh", "ABCD-EFGH")
    assert setup_code_matches(" ABCDEFGH ", "ABCD-EFGH")
    assert not setup_code_matches("ABCD-EFGX", "ABCD-EFGH")
    assert not setup_code_matches("", "")


# -- the app ------------------------------------------------------------------------

def _ctx(tmp_path):
    from bigbrother.alerts_db import AlertStore
    from bigbrother.playback import Playback
    from bigbrother.recorder.index import RecordingIndex
    from bigbrother.web.app import WebContext
    from bigbrother.web.data import EventQueries

    cfg = parse_config({"cameras": {"front": {"streams": {"main": "rtsp://h/1", "sub": "rtsp://h/2"}}}})
    db = tmp_path / "bb.db"
    (tmp_path / "rec").mkdir()
    ctx = WebContext(cfg=cfg, data_dir=tmp_path, recordings_root=tmp_path / "rec",
                     users=UserStore(tmp_path / "web.db"), queries=EventQueries(db), alerts=AlertStore(db),
                     detector_url="http://127.0.0.1:9", setup_code="ABCD-EFGH")
    ctx.playback = Playback(RecordingIndex(db), tmp_path / "rec", tmp_path, EventStore(db), {"front": 60})
    return ctx


def _run(coro):
    return asyncio.run(coro)


def test_api_requires_login_and_setup_code(tmp_path):
    from bigbrother.web.app import create_app
    ctx = _ctx(tmp_path)

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as c:
            assert (await c.get("/api/events")).status == 401
            assert (await c.get("/media/snapshot/x.jpg")).status == 401
            assert (await c.get("/")).status == 200                      # app shell is public
            s = await (await c.get("/api/session")).json()
            assert s == {"user": None, "setup": True, "version": s["version"]}
            body = {"code": "WRONG-CODE", "username": "ethan", "password": PW}
            assert (await c.post("/api/setup", json=body)).status == 403            # no header
            assert (await c.post("/api/setup", json=body, headers=H)).status == 403  # wrong code
            body["code"] = "abcd-efgh"
            r = await c.post("/api/setup", json=body, headers=H)
            assert r.status == 200 and "bb_session" in r.cookies
            assert r.cookies["bb_session"]["httponly"]
            assert (await c.get("/api/events")).status == 200
            # setup can't be repeated
            assert (await c.post("/api/setup", json=body, headers=H)).status == 409
            # state changes need the header
            assert (await c.post("/api/armed", json={"armed": False})).status == 403
            r = await c.post("/api/armed", json={"armed": False}, headers=H)
            assert (await r.json()) == {"armed": False} and not ctx.alerts.armed()
            await c.post("/api/logout", headers=H)
            assert (await c.get("/api/events")).status == 401
    _run(go())


def test_login_rate_limit_and_events(tmp_path):
    from bigbrother.web.app import create_app
    ctx = _ctx(tmp_path)
    ctx.users.add("ethan", PW)
    store = EventStore(tmp_path / "bb.db")
    (tmp_path / "snaps").mkdir()
    import cv2
    import numpy as np
    cv2.imwrite(str(tmp_path / "snaps" / "a.jpg"), np.zeros((90, 160, 3), np.uint8))
    for i, (cam, label, snap) in enumerate([("front", "person", "snaps/a.jpg"), ("front", "car", "../../etc/passwd")]):
        store.save(Event(f"e{i}", cam, label, 1000.0 + i, 1005.0 + i, 0.9, 0.8, [1, 2, 30, 40], 160, 90,
                         False, snap, 1006.0 + i))

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as c:
            for _ in range(10):
                r = await c.post("/api/login", json={"username": "ethan", "password": "nope"}, headers=H)
                assert r.status == 401
            r = await c.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            assert r.status == 429                                       # locked out for a while
            ctx.limiter = RateLimiter()
            r = await c.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            assert r.status == 200
            evs = await (await c.get("/api/events")).json()
            assert [e["id"] for e in evs] == ["e1", "e0"]
            assert "snapshot" not in evs[0]                              # no server paths leak
            assert [e["id"] for e in await (await c.get("/api/events?label=person")).json()] == ["e0"]
            assert await (await c.get("/api/events?before=1001")).json() == [evs[1]]
            r = await c.get("/media/snapshot/e0.jpg?w=80")
            assert r.status == 200 and r.content_type == "image/jpeg"
            assert (await c.get("/media/snapshot/e1.jpg")).status == 404  # path outside data dir
            assert (await c.get("/api/timeline?camera=nope")).status == 404
            assert (await c.get("/go2rtc/api/ws?src=not_a_camera")).status in (403, 404)
    _run(go())


# -- editing cameras.yml from the app ----------------------------------------------------

CAMS_YML = """\
# my cameras (this comment must survive)
alerts:
  timezone: America/New_York   # local time
cameras:
  front:
    name: Front
    streams:
      main: rtsp://${CAM_USER}:${CAM_PASSWORD}@10.0.0.2:554/Streaming/Channels/101
      sub: rtsp://${CAM_USER}:${CAM_PASSWORD}@10.0.0.2:554/Streaming/Channels/102
    detect:
      fps: 4               # custom, not shown in the app
  back:
    name: Back
    streams:
      main: rtsp://10.0.0.3/1
"""


def test_config_editor_keeps_comments_and_validates(tmp_path):
    from bigbrother.config import ConfigError, load_config
    from bigbrother.web.config_edit import ConfigEditor

    path = tmp_path / "cameras.yml"
    path.write_text(CAMS_YML)
    ed = ConfigEditor(path)
    _, new, changed = ed.save_camera("front", {
        "zones": {"porch": {"points": [[0.1, 0.5], [0.6, 0.5], [0.6, 0.9]], "objects": ["person"]}},
        "detect": {"enabled": True},
        "alert": {"zones": ["porch"], "schedule": [{"days": ["mon", "sun"], "start": "22:00", "end": "06:30"}]}})
    assert changed == {"detector", "alerter"}               # recording isn't interrupted
    text = path.read_text()
    assert "# my cameras (this comment must survive)" in text and "# custom, not shown in the app" in text
    assert 'start: "22:00"' in text                         # PyYAML would read 22:00 as a number
    cfg = load_config(path)                                 # what the services will load
    front = cfg.cameras["front"]
    assert front.detect.fps == 4 and "porch" in front.zones
    assert str(front.alert.schedule[0].start) == "22:00:00"
    assert len(list((tmp_path / "backups").glob("*.yml"))) == 1

    for bad in ({"alert": {"zones": ["nope"]}},
                {"streams": {"main": "rtsp://admin:hunter2@10.0.0.2/101"}},   # literal password
                {"bogus": 1}):
        with pytest.raises(ConfigError):
            ed.save_camera("front", bad)
    assert load_config(path).cameras["front"].alert.zones == ("porch",)  # nothing written

    _, new, changed = ed.save_camera("garage", {"name": "Garage", "streams": {"main": "rtsp://10.0.0.9/1"}},
                                     create=True)
    assert "go2rtc" in changed and (tmp_path / "generated" / "go2rtc.yaml").exists()
    with pytest.raises(ConfigError):
        ed.save_camera("garage", {"name": "Again"}, create=True)
    with pytest.raises(ConfigError):
        ed.save_camera("Bad Id", {"name": "x"}, create=True)
    ed.delete_camera("garage")
    assert "garage" not in load_config(path).cameras
    _, _, changed = ed.save_settings({"storage": {"retention_days": 12}})
    assert changed == {"recorder"} and load_config(path).storage.retention_days == 12
    with pytest.raises(ConfigError):
        ed.save_settings({"mqtt": {"host": "evil"}})


def test_settings_api(tmp_path):
    from bigbrother.web.app import create_app
    ctx = _ctx(tmp_path)
    ctx.config_path = tmp_path / "cameras.yml"
    ctx.config_path.write_text(CAMS_YML)
    ctx.users.add("ethan", PW)

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as c:
            await c.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            cfg = await (await c.get("/api/config")).json()
            assert set(cfg["env_vars"]) == {"CAM_USER", "CAM_PASSWORD"}
            r = await c.put("/api/config/cameras/front", json={"name": "Front door"})
            assert r.status == 403                                     # CSRF header required
            r = await c.put("/api/config/cameras/front", json={"name": "Front door"}, headers=H)
            body = await r.json()
            assert r.status == 200 and body["restarted"] == ["alerter", "detector", "recorder"]
            assert ctx.cfg.cameras["front"].name == "Front door"      # the app uses it right away
            r = await c.put("/api/config/cameras/front", json={"alert": {"zones": ["nope"]}}, headers=H)
            assert r.status == 400 and "nope" in (await r.json())["error"]
    _run(go())
