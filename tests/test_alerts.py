import datetime as dt
import email
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import cv2
import numpy as np
import pytest

from bigbrother.alerts.notify import LogNotifier, Message, PushoverNotifier, SendResult
from bigbrother.alerts.rules import evaluate
from bigbrother.alerts.service import Alerter
from bigbrother.alerts.verifier import Verifier, crop_region
from bigbrother.alerts_db import AlertStore
from bigbrother.config import ConfigError, parse_config
from bigbrother.detector.models import Detection

NY = dt.timezone(dt.timedelta(hours=-5))


def cfg_with(alert=None, alerts=None, zones=None):
    cam = {"streams": {"main": "rtsp://h/1"}}
    if alert is not None:
        cam["alert"] = alert
    if zones:
        cam["zones"] = zones
    data = {"cameras": {"front": cam}}
    if alerts is not None:
        data["alerts"] = alerts
    return parse_config(data)


def event(**kw):
    e = {"id": "e1", "camera": "front", "label": "person", "start_ts": 1000.0, "box": [100, 50, 140, 150],
         "frame_w": 640, "frame_h": 360, "stationary": False, "zones": [], "snapshot": None}
    e.update(kw)
    return e


MON_NOON = dt.datetime(2026, 11, 23, 12, 0, tzinfo=NY)  # a Monday


# -- rules --------------------------------------------------------------------

def test_rules_happy_path_and_each_reason():
    cfg = cfg_with()
    cam = cfg.cameras["front"]
    ok = evaluate(event(), cam, cfg.alerts, True, None, 1000, MON_NOON)
    assert ok.alert
    assert evaluate(event(label="car"), cam, cfg.alerts, True, None, 1000, MON_NOON).reason.startswith("car doesn't")
    assert evaluate(event(), cam, cfg.alerts, False, None, 1000, MON_NOON).reason == "disarmed"
    assert "stationary" in evaluate(event(stationary=True), cam, cfg.alerts, True, None, 1000, MON_NOON).reason
    cd = evaluate(event(), cam, cfg.alerts, True, 970, 1000, MON_NOON)
    assert not cd.alert and "30s ago" in cd.reason


def test_rules_zone_requirement():
    cfg = cfg_with(alert={"zones": ["porch"]},
                   zones={"porch": {"points": [[0, 0], [1, 0], [1, 1]]}})
    cam = cfg.cameras["front"]
    assert evaluate(event(), cam, cfg.alerts, True, None, 1000, MON_NOON).reason == "hasn't entered porch"
    assert evaluate(event(zones=["porch"]), cam, cfg.alerts, True, None, 1000, MON_NOON).alert


def test_alert_zone_must_exist():
    with pytest.raises(ConfigError, match="no zone named porch"):
        cfg_with(alert={"zones": ["porch"]})


def test_overnight_schedule():
    cfg = cfg_with(alert={"schedule": [{"days": ["mon"], "start": "22:00", "end": "06:00"}]})
    cam = cfg.cameras["front"]
    at = lambda d, h, m=0: dt.datetime(2026, 11, d, h, m, tzinfo=NY)  # 23rd = Monday
    assert evaluate(event(), cam, cfg.alerts, True, None, 0, at(23, 23)).alert          # Mon 23:00
    assert evaluate(event(), cam, cfg.alerts, True, None, 0, at(24, 5, 59)).alert       # Tue 05:59 (Mon night)
    assert not evaluate(event(), cam, cfg.alerts, True, None, 0, at(24, 23)).alert      # Tue 23:00: not Tue
    assert not evaluate(event(), cam, cfg.alerts, True, None, 0, at(23, 12)).alert      # Mon noon


def test_alert_config_validation():
    with pytest.raises(ConfigError, match="time zone"):
        cfg_with(alerts={"timezone": "Mars/Olympus"})
    with pytest.raises(ConfigError, match="priority"):
        cfg_with(alerts={"pushover": {"priority": 2}})
    with pytest.raises(ConfigError):
        cfg_with(alert={"schedule": [{"start": "25:00", "end": "06:00"}]})


# -- verifier -------------------------------------------------------------------

def test_crop_region_is_at_least_model_size_and_inside_frame():
    l, t, r, b = crop_region((0.95, 0.9, 0.99, 0.99), 2560, 1440)  # small object in a corner
    assert r - l >= 640 and b - t >= 640
    assert 0 <= l and r <= 2560 and 0 <= t and b <= 1440


class FakeModel:
    def __init__(self, dets):
        self.dets = dets
        self.seen = None

    def detect(self, img, min_score=0.25, labels=None):
        self.seen = img.shape
        return self.dets


def test_verifier_confirms_same_kind_and_maps_box_to_full_frame():
    frame = np.zeros((1440, 2560, 3), np.uint8)
    v = Verifier(FakeModel([Detection("person", 0.8, (10, 20, 50, 120))]), 0.5)
    res = v.verify(event(), frame)
    assert res.ok and res.score == 0.8
    l, t, _, _ = crop_region((100 / 640, 50 / 360, 140 / 640, 150 / 360), 2560, 1440)
    assert res.box == (10 + l, 20 + t, 50 + l, 120 + t)


def test_verifier_rejects_other_objects_and_low_scores():
    frame = np.zeros((1440, 2560, 3), np.uint8)
    assert not Verifier(FakeModel([Detection("dog", 0.9, (0, 0, 10, 10))]), 0.5).verify(event(), frame).ok
    low = Verifier(FakeModel([Detection("person", 0.3, (0, 0, 10, 10))]), 0.5).verify(event(), frame)
    assert not low.ok and "unsure" in low.reason
    # car/truck confusion still counts as the same kind of object
    assert Verifier(FakeModel([Detection("truck", 0.9, (0, 0, 10, 10))]), 0.5).verify(event(label="car"), frame).ok


# -- Pushover -------------------------------------------------------------------

class FakePushover:
    def __init__(self, codes):
        self.codes = list(codes)
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                msg = email.message_from_bytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields = {}
                for part in msg.get_payload():
                    name = part.get_param("name", header="content-disposition")
                    data = part.get_payload(decode=True)
                    fields[name] = (part.get_content_type(), data) if part.get_filename() else data.decode()
                outer.requests.append(fields)
                code = outer.codes.pop(0) if outer.codes else 200
                out = json.dumps({"status": 1, "request": "r1"} if code == 200 else
                                 {"status": 0, "errors": ["user identifier is invalid"]}).encode()
                self.send_response(code)
                self.send_header("X-Limit-App-Remaining", "9876")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/1/messages.json"


def test_pushover_sends_fields_and_image():
    fake = FakePushover([200])
    n = PushoverNotifier("U", "T", priority=1, sound="siren", url=fake.url)
    res = n.send(Message("Person · Front", "Person at 3:00 PM", b"\xff\xd8jpeg", url="https://x/e/1", timestamp=1000))
    assert res.ok and res.status == "sent" and n.remaining == 9876
    f = fake.requests[0]
    assert f["token"] == "T" and f["user"] == "U" and f["priority"] == "1" and f["sound"] == "siren"
    assert f["title"] == "Person · Front" and f["timestamp"] == "1000" and f["url"] == "https://x/e/1"
    assert f["attachment"] == ("image/jpeg", b"\xff\xd8jpeg")


def test_pushover_never_retries_4xx_but_retries_5xx():
    fake = FakePushover([400])
    res = PushoverNotifier("U", "T", url=fake.url, retry_wait=0).send(Message("t", "b"))
    assert not res.ok and "user identifier is invalid" in res.detail and len(fake.requests) == 1
    fake2 = FakePushover([500, 200])
    res2 = PushoverNotifier("U", "T", url=fake2.url, retry_wait=0).send(Message("t", "b"))
    assert res2.ok and len(fake2.requests) == 2


def test_oversized_image_is_not_attached():
    fake = FakePushover([200])
    PushoverNotifier("U", "T", url=fake.url).send(Message("t", "b", b"x" * 5_300_000))
    assert "attachment" not in fake.requests[0]


# -- alerter flow -----------------------------------------------------------------

class RecordingNotifier:
    name = "test"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return SendResult(True, "sent", "ok")


def make_alerter(tmp_path, cfg, model_dets, frame=True, clock=None):
    store = AlertStore(tmp_path / "db.sqlite")
    notifier = RecordingNotifier()
    img = np.zeros((1440, 2560, 3), np.uint8)
    a = Alerter(cfg, store, tmp_path, notifier, Verifier(FakeModel(model_dets), 0.5),
                fetch=(lambda api, s: img) if frame else (lambda api, s: None),
                clock=clock or (lambda: 1000.0))
    return a, store, notifier


def test_alerter_sends_once_per_event_and_records_outcome(tmp_path):
    a, store, n = make_alerter(tmp_path, cfg_with(), [Detection("person", 0.9, (0, 0, 40, 100))])
    a.handle({"type": "start", "event": event()})
    a.handle({"type": "update", "event": event()})
    a.handle({"type": "end", "event": event()})
    assert len(n.sent) == 1 and n.sent[0].title == "Person · Front"
    rec = store.get("e1")
    assert rec.status == "sent" and rec.verify_score == 0.9 and (tmp_path / rec.image).exists()


def test_alerter_records_why_nothing_was_sent(tmp_path):
    a, store, n = make_alerter(tmp_path, cfg_with(), [])  # second opinion sees nothing
    for _ in range(3):
        a.handle({"type": "update", "event": event()})
    a.handle({"type": "end", "event": event()})
    assert n.sent == []
    rec = store.get("e1")
    assert rec.status == "suppressed" and "second opinion found no person" in rec.reason


def test_alerter_waits_for_zone_then_alerts(tmp_path):
    cfg = cfg_with(alert={"zones": ["porch"]}, zones={"porch": {"points": [[0, 0], [1, 0], [1, 1]]}})
    a, store, n = make_alerter(tmp_path, cfg, [Detection("person", 0.9, (0, 0, 40, 100))])
    a.handle({"type": "start", "event": event()})
    assert n.sent == [] and store.get("e1").status == "pending"
    a.handle({"type": "update", "event": event(zones=["porch"])})
    assert len(n.sent) == 1 and "in porch" in n.sent[0].body


def test_alerter_cooldown_and_disarm(tmp_path):
    now = [1000.0]
    a, store, n = make_alerter(tmp_path, cfg_with(), [Detection("person", 0.9, (0, 0, 40, 100))],
                               clock=lambda: now[0])
    a.handle({"type": "start", "event": event(id="e1")})
    now[0] = 1030
    a.handle({"type": "start", "event": event(id="e2")})
    assert len(n.sent) == 1 and "cooldown" in store.get("e2").reason
    store.set_armed(False)
    now[0] = 2000
    a.handle({"type": "start", "event": event(id="e3")})
    assert len(n.sent) == 1 and store.get("e3").reason == "disarmed"


def test_snapshot_fallback_when_full_res_grab_lags(tmp_path):
    """Full-res frame shows nothing (object moved on); the detector's snapshot does."""
    snap = np.full((360, 640, 3), 255, np.uint8)
    cv2.imwrite(str(tmp_path / "snap.jpg"), snap)

    class ByImage:
        def detect(self, img, min_score=0.25, labels=None):
            return [Detection("person", 0.9, (0, 0, 40, 100))] if img.mean() > 200 else []

    store = AlertStore(tmp_path / "db.sqlite")
    n = RecordingNotifier()
    a = Alerter(cfg_with(), store, tmp_path, n, Verifier(ByImage(), 0.5),
                fetch=lambda api, s: np.zeros((1440, 2560, 3), np.uint8), clock=lambda: 1000.0)
    a.handle({"type": "start", "event": event(snapshot="snap.jpg")})
    assert len(n.sent) == 1 and store.get("e1").status == "sent"


def test_log_notifier_marks_alerts_logged():
    assert LogNotifier().send(Message("t", "b")).status == "logged"


# -- health alerts -----------------------------------------------------------------------

def test_health_monitor_reports_once_then_all_clear():
    from bigbrother.alerts.health import HealthMonitor
    now = [0.0]
    m = HealthMonitor(after=300, clock=lambda: now[0])
    assert m.update({"rec:a": "A isn't recording"}) == ([], [])          # too new to report
    now[0] = 299
    assert m.update({"rec:a": "A isn't recording"}) == ([], [])
    now[0] = 300
    assert m.update({"rec:a": "A isn't recording"}) == (["A isn't recording"], [])
    now[0] = 400
    assert m.update({"rec:a": "A isn't recording"}) == ([], [])          # no repeat
    now[0] = 500
    assert m.update({}) == ([], ["A isn't recording"])                   # all clear
    m.update({"rec:b": "blip"})
    now[0] = 520
    assert m.update({}) == ([], [])     # never reported, so no all-clear either


def test_find_problems():
    from bigbrother.alerts.health import find_problems
    from bigbrother.config import parse_config
    cfg = parse_config({"storage": {"max_usage_percent": 85}, "cameras": {
        "a": {"name": "Alpha", "streams": {"main": "rtsp://h/1"}},
        "b": {"name": "Bravo", "streams": {"main": "rtsp://h/2"}, "detect": {"enabled": False}}}})
    base = dict(now=10_000.0, started=0.0, recorder_state={}, available={"recorder": "online", "detector": "online"},
                latest={"a": 9_990.0, "b": 9_990.0}, detector={"a": {"state": "running"}}, go2rtc_ok=True,
                disk=(500e9, 1000e9))
    assert find_problems(cfg, **base) == {}
    p = find_problems(cfg, **{**base, "latest": {"a": 9_000.0, "b": 9_990.0},
                               "recorder_state": {"a": {"state": "error", "detail": "connection refused"}}})
    assert set(p) == {"rec:a"} and "connection refused" in p["rec:a"] and "Alpha" in p["rec:a"]
    p = find_problems(cfg, **{**base, "detector": {"a": {"state": "error", "error": "stream unavailable"}}})
    assert set(p) == {"det:a"}                         # b has detection off: never reported
    assert set(find_problems(cfg, **{**base, "disk": (950e9, 1000e9)})) == {"disk"}
    assert set(find_problems(cfg, **{**base, "available": {"recorder": "offline"}})) == {"svc:recorder"}
    assert "svc:go2rtc" in find_problems(cfg, **{**base, "go2rtc_ok": False})
    assert "svc:detector" in find_problems(cfg, **{**base, "detector": None})


# -- people and visits ---------------------------------------------------------------

def _two_cams(alerts=None):
    data = {"cameras": {"front": {"streams": {"main": "rtsp://h/1"}},
                        "drive": {"name": "Driveway", "streams": {"main": "rtsp://h/2"}}}}
    if alerts is not None:
        data["alerts"] = alerts
    return parse_config(data)


class RoutedRecorder:
    """Stands in for RoutedNotifier: fixed recipients per camera."""
    name = "pushover"

    def __init__(self, by_cam):
        self.by_cam, self.sent = by_cam, []

    def targets(self, camera=None, health=False):
        return list(self.by_cam.get(camera, []))

    def send(self, msg, camera=None, health=False, keys=None):
        self.sent.append((msg.title, sorted(keys)))
        return SendResult(True, "sent", "ok")


def test_one_alert_per_visit_across_cameras(tmp_path):
    now = [1000.0]
    a, store, n = make_alerter(tmp_path, _two_cams(), [Detection("person", 0.9, (0, 0, 40, 100))],
                               clock=lambda: now[0])
    a.handle({"type": "start", "event": event(id="e1", camera="drive")})
    now[0] = 1040
    a.handle({"type": "start", "event": event(id="e2", camera="front")})
    assert len(n.sent) == 1
    rec = store.get("e2")
    assert rec.status == "grouped" and "Driveway" in rec.reason and (tmp_path / rec.image).exists()
    now[0] = 1200   # a later visit alerts again
    a.handle({"type": "start", "event": event(id="e3", camera="drive")})
    assert len(n.sent) == 2


def test_visit_grouping_is_per_person_and_can_be_off(tmp_path):
    now = [1000.0]
    a, store, _ = make_alerter(tmp_path, _two_cams(), [Detection("person", 0.9, (0, 0, 40, 100))],
                               clock=lambda: now[0])
    a.notifier = RoutedRecorder({"drive": ["ANNA"], "front": ["ANNA", "BEN"]})
    a.handle({"type": "start", "event": event(id="e1", camera="drive")})
    now[0] = 1030
    a.handle({"type": "start", "event": event(id="e2", camera="front")})
    # Ben only follows the front door, so he still hears about it; Anna already did.
    assert a.notifier.sent == [("Person · Driveway", ["ANNA"]), ("Person · Front", ["BEN"])]

    a2, _, n2 = make_alerter(tmp_path / "b", _two_cams({"group_seconds": 0}),
                             [Detection("person", 0.9, (0, 0, 40, 100))], clock=lambda: now[0])
    a2.handle({"type": "start", "event": event(id="e1", camera="drive")})
    a2.handle({"type": "start", "event": event(id="e2", camera="front")})
    assert len(n2.sent) == 2


def test_recipients_come_from_accounts_with_env_fallback(tmp_path):
    from bigbrother.alerts.notify import RoutedNotifier
    from bigbrother.alerts.recipients import Directory, set_setting
    from bigbrother.web.auth import UserStore

    db = tmp_path / "web.db"
    users = UserStore(db)
    users.add("anna", "correct horse battery")
    users.add("ben", "correct horse battery", "member")
    users.update_prefs("anna", {"pushover_user": "A" * 30})
    users.update_prefs("ben", {"pushover_user": "B" * 30, "alert_cameras": ["front"], "alert_health": False})

    sent = []

    class Fake:
        def __init__(self, user, token, *a, **kw):
            self.user, self.token = user, token

        def send(self, msg):
            sent.append((self.user, self.token))
            return SendResult(True, "sent", "")

    env = {"PUSHOVER_USER": "E" * 30}
    n = RoutedNotifier(Directory(db, env), factory=Fake)
    assert n.name == "log"                                   # no token anywhere yet
    assert n.send(Message("t", "b"), camera="front").status == "logged"
    env["PUSHOVER_TOKEN"] = "T" * 30
    n.directory.forget()
    assert n.targets("front") == ["A" * 30, "B" * 30, "E" * 30]
    assert n.targets("drive") == ["A" * 30, "E" * 30]
    assert n.targets(health=True) == ["A" * 30, "E" * 30]
    set_setting(db, "pushover_token", "S" * 30)              # the app's token wins over .env
    n.directory.forget()
    n.send(Message("t", "b"), camera="drive")
    assert sent[-1] == ("A" * 30 + "," + "E" * 30, "S" * 30)


def test_arm_schedule_acts_only_at_changes(tmp_path):
    from bigbrother.alerts.arming import ArmSchedule
    cfg = cfg_with(alerts={"timezone": "America/New_York",
                           "arm_schedule": [{"start": "22:00", "end": "07:00"}]})
    store = AlertStore(tmp_path / "db.sqlite")
    noon = MON_NOON.timestamp()
    now = [noon]
    published = []
    sched = ArmSchedule(cfg.alerts.arm_schedule, store, "America/New_York",
                        lambda t, p, r: published.append(p), clock=lambda: now[0])
    assert sched.tick() is False and not store.armed()        # new schedule applies at once
    store.set_armed(True)                                      # turned on by hand at lunch...
    now[0] = noon + 3600
    assert sched.tick() is None and store.armed()              # ...and it stays on
    now[0] = noon + 10 * 3600 + 60                             # 22:01: window starts
    assert sched.tick() is True and store.armed()
    store.set_armed(False)
    now[0] += 3600
    assert sched.tick() is None and not store.armed()          # manual off sticks overnight
    now[0] = noon + 19 * 3600 + 60                             # 07:01 Tuesday: window ends
    assert sched.tick() is False
    assert published == ["off"]                                # nothing to announce when already right
    # editing the schedule re-applies right away
    cfg2 = cfg_with(alerts={"timezone": "America/New_York", "arm_schedule": [{"start": "06:00", "end": "09:00"}]})
    sched2 = ArmSchedule(cfg2.alerts.arm_schedule, store, "America/New_York", clock=lambda: now[0])
    assert sched2.tick() is True and store.armed()
