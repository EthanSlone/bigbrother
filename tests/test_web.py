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
                     detector_url="http://127.0.0.1:9", setup_code="ABCD-EFGH", db_path=db)
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
            assert s == {"user": None, "role": None, "twofa": False, "setup": True, "version": s["version"]}
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


def test_feedback_misses_and_export(tmp_path):
    import json as _json
    import zipfile
    from bigbrother.web.app import create_app
    ctx = _ctx(tmp_path)
    ctx.users.add("ethan", PW)
    store = EventStore(tmp_path / "bb.db")
    for i, label in enumerate(["person", "person", "dog"]):
        store.save(Event(f"e{i}", "front", label, 1000.0 + i, 1005.0 + i, 0.9, 0.8, [1, 2, 30, 40], 160, 90,
                         False, None, 1006.0 + i))

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as c:
            await c.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            assert (await c.put("/api/events/e0/feedback", json={"verdict": "false_alarm"})).status == 403
            assert (await c.put("/api/events/e0/feedback", json={"verdict": "false_alarm"}, headers=H)).status == 200
            r = await c.put("/api/events/e2/feedback", json={"verdict": "wrong_label"}, headers=H)
            assert r.status == 400                                   # needs the right object
            await c.put("/api/events/e2/feedback", json={"verdict": "wrong_label", "label": "cat"}, headers=H)
            assert (await c.put("/api/events/nope/feedback", json={"verdict": "correct"}, headers=H)).status == 404
            fa = await (await c.get("/api/events?review=false_alarm")).json()
            assert [e["id"] for e in fa] == ["e0"] and fa[0]["feedback"]["verdict"] == "false_alarm"
            todo = await (await c.get("/api/events?review=unreviewed")).json()
            assert [e["id"] for e in todo] == ["e1"]
            r = await c.post("/api/misses", json={"camera": "front", "ts": 1500.0, "label": "person",
                                                  "note": "by the fence"}, headers=H)
            assert r.status == 200
            assert (await c.post("/api/misses", json={"camera": "nope", "ts": 1.0, "label": "person"},
                                 headers=H)).status == 400
            counts = await (await c.get("/api/feedback")).json()
            assert counts == {"correct": 0, "false_alarm": 1, "wrong_label": 1, "missed": 1}
            job = await (await c.post("/api/testset", json={"include_unreviewed": 50}, headers=H)).json()
            for _ in range(100):
                job = await (await c.get(f"/api/testset/{job['id']}")).json()
                if job["state"] != "running":
                    break
                await asyncio.sleep(0.05)
            assert job["state"] == "done", job
            r = await c.get(f"/media/testset/{job['id']}.zip")
            assert r.status == 200
            (tmp_path / "t.zip").write_bytes(await r.read())
    _run(go())
    with zipfile.ZipFile(tmp_path / "t.zip") as z:
        m = _json.loads(z.read("manifest.json"))
    assert {e["id"]: (e["feedback"] or {}).get("verdict") for e in m["events"]} == \
        {"e0": "false_alarm", "e2": "wrong_label", "e1": None}
    assert m["missed"][0]["note"] == "by the fence"
    assert "rtsp" not in _json.dumps(m)                               # no stream addresses



def test_totp_matches_rfc6238_vector():
    import base64
    from bigbrother.web.auth import totp_match
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_match(secret, "287082", now=59) is not None     # RFC 6238, T=59, last 6 digits
    assert totp_match(secret, "287083", now=59) is None
    assert totp_match(secret, "287082", now=59 + 300) is None   # long expired


def test_roles_and_two_factor_sign_in(tmp_path):
    import time as _t
    from bigbrother.web.app import create_app
    from bigbrother.web.auth import _hotp
    ctx = _ctx(tmp_path)
    ctx.users.add("ethan", PW, "admin")

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as admin, TestClient(TestServer(create_app(ctx))) as kid:
            await admin.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            r = await admin.post("/api/users", json={"username": "sam", "password": PW + "2", "role": "member"},
                                 headers=H)
            assert r.status == 200
            await kid.post("/api/login", json={"username": "sam", "password": PW + "2"}, headers=H)
            assert (await (await kid.get("/api/session")).json())["role"] == "member"
            # members watch and review, but can't change settings or accounts
            assert (await kid.get("/api/events")).status == 200
            assert (await kid.post("/api/armed", json={"armed": True}, headers=H)).status == 200
            for method, path in (("GET", "/api/users"), ("GET", "/api/config"),
                                 ("PUT", "/api/config/cameras/front"), ("POST", "/api/testset")):
                r = await kid.request(method, path, json={}, headers=H)
                assert r.status == 403, (method, path)
            # the last admin can't be demoted or removed
            r = await admin.put("/api/users/ethan", json={"role": "member"}, headers=H)
            assert r.status == 400
            assert (await admin.delete("/api/users/ethan", headers=H)).status == 400

            # sam turns on two-factor
            begin = await (await kid.post("/api/me/2fa/begin", headers=H)).json()
            assert begin["uri"].startswith("otpauth://totp/") and "<svg" in begin["qr"]
            step = int(_t.time() // 30)
            r = await kid.post("/api/me/2fa/confirm", json={"code": _hotp(begin["secret"], step)}, headers=H)
            codes = (await r.json())["recovery_codes"]
            assert r.status == 200 and len(codes) == 8
        async with TestClient(TestServer(create_app(ctx))) as c:
            r = await (await c.post("/api/login", json={"username": "sam", "password": PW + "2"}, headers=H)).json()
            assert r["need_code"] and "bb_session" not in c.session.cookie_jar.filter_cookies(c.make_url("/"))
            bad = await c.post("/api/login/code", json={"ticket": r["ticket"], "code": "000000"}, headers=H)
            assert bad.status == 401
            # the code used to turn it on can't be replayed; a recovery code works once
            ok = await c.post("/api/login/code", json={"ticket": r["ticket"], "code": codes[0]}, headers=H)
            assert ok.status == 200 and (await (await c.get("/api/session")).json())["twofa"]
            r = await (await c.post("/api/login", json={"username": "sam", "password": PW + "2"}, headers=H)).json()
            again = await c.post("/api/login/code", json={"ticket": r["ticket"], "code": codes[0]}, headers=H)
            assert again.status == 401
    _run(go())


def test_phone_alert_settings(tmp_path, monkeypatch):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from bigbrother.alerts.recipients import Directory
    from bigbrother.web.app import create_app

    hits = []

    class Pushover(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"])).decode(errors="ignore")
            hits.append((self.path, body))
            if self.path.endswith("validate.json"):
                good = "G" * 30 in body
                bad_token = "X" * 30 in body
                code = 200 if good and not bad_token else 400
                out = {"status": 1, "devices": ["iphone"]} if code == 200 else \
                    {"status": 0, "errors": ["application token is invalid" if bad_token else "user key is invalid"]}
            else:
                code, out = 200, {"status": 1, "request": "r1"}
            data = _json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Pushover)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("BB_PUSHOVER_URL", f"http://127.0.0.1:{srv.server_port}/1/messages.json")
    monkeypatch.delenv("PUSHOVER_TOKEN", raising=False)
    monkeypatch.delenv("PUSHOVER_USER", raising=False)
    ctx = _ctx(tmp_path)
    ctx.users.add("ethan", PW, "admin")
    ctx.users.add("sam", PW, "member")

    async def go():
        async with TestClient(TestServer(create_app(ctx))) as admin, TestClient(TestServer(create_app(ctx))) as kid:
            await admin.post("/api/login", json={"username": "ethan", "password": PW}, headers=H)
            await kid.post("/api/login", json={"username": "sam", "password": PW}, headers=H)
            mine = await (await kid.get("/api/me/notify")).json()
            assert mine == {"pushover_user": "", "cameras": None, "health": True, "ready": False}
            assert (await kid.put("/api/notify/settings", json={"token": "T" * 30}, headers=H)).status == 403
            r = await admin.put("/api/notify/settings", json={"token": "X" * 30}, headers=H)
            assert r.status == 400 and "rejected" in (await r.json())["error"]
            r = await admin.put("/api/notify/settings", json={"token": "T" * 30}, headers=H)
            assert (await r.json())["token_source"] == "app"
            # keys are checked with Pushover before they're saved
            r = await kid.put("/api/me/notify", json={"pushover_user": "B" * 30}, headers=H)
            assert r.status == 400 and "recognise" in (await r.json())["error"]
            r = await kid.put("/api/me/notify", json={"pushover_user": "G" * 30, "cameras": ["front"],
                                                      "health": False}, headers=H)
            got = await r.json()
            assert r.status == 200 and got["ready"] and got["cameras"] == ["front"] and "iphone" in got["note"]
            assert (await kid.put("/api/me/notify", json={"cameras": ["nope"]}, headers=H)).status == 400
            r = await kid.post("/api/me/notify/test", headers=H)
            assert r.status == 200
            assert (await kid.post("/api/me/notify/test", headers=H)).status == 429
            people = (await (await admin.get("/api/notify/settings")).json())["people"]
            assert people == [{"name": "sam", "cameras": ["front"], "health": False}]

    _run(go())
    srv.shutdown()
    msg = [b for p, b in hits if p.endswith("messages.json")]
    assert len(msg) == 1 and "G" * 30 in msg[0] and "T" * 30 in msg[0]
    token, people = Directory(tmp_path / "web.db", {}).load()
    assert token == "T" * 30 and people[0].cameras == frozenset({"front"}) and not people[0].health
