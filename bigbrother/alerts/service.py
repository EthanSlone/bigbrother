"""Alerter service: listens for detector events on MQTT and decides, verifies and notifies.

For every event: rules -> second opinion on a full-resolution frame -> notification,
with the outcome (and the reason when nothing was sent) recorded per event.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import queue
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import cv2
import numpy as np

from ..alerts_db import AlertRecord, AlertStore
from ..config import Config
from ..mqtt import Publisher, restart_on_config_change
from .notify import Message, make_notifier
from .rules import evaluate
from .verifier import Verifier, fetch_frame

log = logging.getLogger(__name__)

MAX_VERIFY_ATTEMPTS = 3
RETRY_SECONDS = 2.0
IMAGE_WIDTH = 1280


@dataclass
class _EventState:
    event: dict
    alerted: bool = False
    final: bool = False
    reason: str = ""
    attempts: int = 0
    last_try: float = 0.0
    verify_score: float | None = None


@dataclass
class Alerter:
    cfg: Config
    store: AlertStore
    data_dir: Path
    notifier: object
    verifier: Verifier | None = None
    publish: object = None                       # (topic, payload, retain) -> None
    fetch: object = fetch_frame                  # (go2rtc_api, stream) -> image | None
    clock: object = time.time
    _state: dict[str, _EventState] = field(default_factory=dict)

    def __post_init__(self):
        self.tz = ZoneInfo(self.cfg.alerts.timezone)
        (self.data_dir / "alerts").mkdir(parents=True, exist_ok=True)

    # -- inputs ---------------------------------------------------------------
    def handle(self, msg: dict) -> None:
        kind, event = msg.get("type"), msg.get("event") or {}
        cam = self.cfg.cameras.get(event.get("camera", ""))
        if cam is None or not event.get("id"):
            return
        eid = event["id"]
        if kind == "end":
            st = self._state.pop(eid, None)
            if st and not st.alerted:
                self._record(event, "suppressed", st.reason or "ended before the alert rules were met",
                             st.verify_score)
            return
        st = self._state.get(eid)
        if st is None:
            st = self._state[eid] = _EventState(event)
        st.event = event
        self._consider(st)

    def retry_pending(self) -> None:
        """Re-check events still in view (e.g. a second opinion that failed on a blurry frame)."""
        now = self.clock()
        for st in list(self._state.values()):
            if not st.alerted and not st.final and now - st.last_try >= RETRY_SECONDS:
                self._consider(st)

    # -- decision -------------------------------------------------------------
    def _consider(self, st: _EventState) -> None:
        if st.alerted or st.final:
            return
        ev = st.event
        cam = self.cfg.cameras[ev["camera"]]
        now = self.clock()
        st.last_try = now
        d = evaluate(ev, cam, self.cfg.alerts, self.store.armed(),
                     self.store.last_sent(cam.id, ev["label"]), now,
                     dt.datetime.fromtimestamp(now, self.tz))
        if not d.alert:
            if d.reason != st.reason:
                self._record(ev, "pending", d.reason)
            st.reason, st.final = d.reason, d.final
            return

        frame = self.fetch(self.cfg.go2rtc_api, cam.main_stream)
        snapshot = self._snapshot(ev)
        if frame is None:
            frame = snapshot  # fall back to the detector's snapshot rather than miss the alert
        if frame is None:
            st.reason = "no image available yet"
            return

        box = None
        if self.verifier is not None:
            res = self.verifier.verify(ev, frame)
            if not res.ok and snapshot is not None and frame is not snapshot:
                # The full-res grab comes a moment after detection, and a fast object may have
                # moved on. The detector's snapshot is from the exact moment: check it with the
                # bigger model instead (lower resolution, but no lag).
                res_snap = self.verifier.verify(ev, snapshot)
                if res_snap.ok:
                    res, frame = res_snap, snapshot
            st.verify_score = res.score
            if not res.ok:
                st.attempts += 1
                st.reason = res.reason
                st.final = st.attempts >= MAX_VERIFY_ATTEMPTS
                self._record(ev, "pending", res.reason, res.score)
                log.info("[%s] %s %s: %s (attempt %d)", ev["camera"], ev["label"], ev["id"], res.reason,
                         st.attempts)
                return
            box = res.box
        if box is None:  # no verifier: scale the detector's box to this frame
            fh, fw = frame.shape[:2]
            sx, sy = fw / ev["frame_w"], fh / ev["frame_h"]
            b = ev["box"]
            box = (b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy)

        self._send(st, cam, frame, box)

    def _send(self, st: _EventState, cam, frame: np.ndarray, box) -> None:
        ev = st.event
        image = render_alert_image(frame, box, ev["label"])
        when = dt.datetime.fromtimestamp(ev["start_ts"], self.tz)
        zones = ev.get("zones") or []
        where = f" in {', '.join(z.replace('_', ' ') for z in zones)}" if zones else ""
        clock = when.strftime("%I:%M:%S %p").lstrip("0")
        msg = Message(
            title=f"{ev['label'].capitalize()} · {cam.name}",
            body=f"{ev['label'].capitalize()}{where} at {clock}",
            image_jpeg=image,
            url=f"{self.cfg.alerts.public_url}/events/{ev['id']}" if self.cfg.alerts.public_url else "",
            url_title="View clip",
            timestamp=int(ev["start_ts"]),
        )
        result = self.notifier.send(msg)
        st.alerted = True
        rel = Path("alerts") / f"{ev['id']}.jpg"
        (self.data_dir / rel).write_bytes(image)
        self._record(ev, result.status, result.detail, st.verify_score, sent_ts=self.clock(), image=str(rel))
        log.info("[%s] ALERT %s: %s (%s)", ev["camera"], result.status, msg.title, result.detail)
        if self.publish:
            self.publish("alerts", {"event_id": ev["id"], "camera": ev["camera"], "label": ev["label"],
                                    "status": result.status, "ts": self.clock()}, False)

    # -- helpers --------------------------------------------------------------
    def _snapshot(self, ev: dict) -> np.ndarray | None:
        if ev.get("snapshot"):
            return cv2.imread(str(self.data_dir / ev["snapshot"]))
        return None

    def _record(self, ev: dict, status: str, reason: str, score: float | None = None,
                sent_ts: float | None = None, image: str | None = None) -> None:
        prev = self.store.get(ev["id"])
        if prev and prev.status in ("sent", "logged", "failed") and status in ("pending", "suppressed"):
            return  # never downgrade a delivered alert
        self.store.save(AlertRecord(ev["id"], ev["camera"], ev["label"], status, reason, score, sent_ts, image))


def render_alert_image(frame: np.ndarray, box, label: str) -> bytes:
    img = frame.copy()
    h, w = img.shape[:2]
    thickness = max(2, w // 400)
    x1, y1, x2, y2 = (int(v) for v in box)
    cv2.rectangle(img, (x1, y1), (x2, y2), (80, 200, 80), thickness)
    if w > IMAGE_WIDTH:
        img = cv2.resize(img, (IMAGE_WIDTH, int(h * IMAGE_WIDTH / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return buf.tobytes() if ok else b""


def build_verifier(cfg: Config) -> Verifier | None:
    if not cfg.alerts.verify:
        return None
    from ..detector.model_store import resolve_model
    from ..detector.models import load_model, plan_threads

    path, family = resolve_model(cfg.alerts.verify_model)
    _, threads = plan_threads(1, cfg.alerts.threads)
    model = load_model(family or "yolox", path, cfg.detector.backend, cfg.detector.device, threads)
    return Verifier(model, cfg.alerts.verify_threshold)


def run(cfg: Config, data_dir: Path, db_path: Path) -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    store = AlertStore(db_path)
    notifier = make_notifier(cfg.alerts.notifier, cfg.alerts.priority, cfg.alerts.sound)
    log.info("notifier: %s; second opinion: %s", notifier.name,
             cfg.alerts.verify_model if cfg.alerts.verify else "off")
    verifier = build_verifier(cfg)

    mqtt = Publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix, "alerter")
    inbox: queue.Queue = queue.Queue()

    def publish(topic, payload, retain):
        mqtt.publish(topic, payload, retain=retain)

    def on_event(_topic, payload):
        try:
            inbox.put(json.loads(payload))
        except json.JSONDecodeError:
            log.warning("ignoring malformed event message")

    def on_armed(_topic, payload):
        on = payload.decode(errors="ignore").strip().lower() in ("1", "on", "true", "armed")
        store.set_armed(on)
        publish("alerts/armed", "on" if on else "off", True)
        log.info("alerts %s via MQTT", "armed" if on else "disarmed")

    mqtt.subscribe("events", on_event)
    mqtt.subscribe("alerts/armed/set", on_armed)
    restart_on_config_change(mqtt, "alerter", stop, cfg.source_mtime)
    mqtt.start()
    publish("alerts/armed", "on" if store.armed() else "off", True)

    alerter = Alerter(cfg, store, data_dir, notifier, verifier, publish)
    try:
        while not stop.is_set():
            try:
                msg = inbox.get(timeout=1.0)
            except queue.Empty:
                alerter.retry_pending()
                continue
            try:
                alerter.handle(msg)
            except Exception:  # noqa: BLE001 - one bad event must not stop alerting
                log.exception("alert handling failed")
    finally:
        mqtt.stop()
        store.close()


def send_test(cfg: Config) -> int:
    """Send one test notification (uses a live frame from the first camera if available)."""
    notifier = make_notifier(cfg.alerts.notifier, cfg.alerts.priority, cfg.alerts.sound)
    cam = next(iter(cfg.cameras.values()))
    frame = fetch_frame(cfg.go2rtc_api, cam.main_stream)
    if frame is None:
        frame = np.full((720, 1280, 3), 40, np.uint8)
        cv2.putText(frame, "BigBrother test", (60, 380), cv2.FONT_HERSHEY_SIMPLEX, 3, (230, 230, 230), 5)
    h, w = frame.shape[:2]
    image = render_alert_image(frame, (w * 0.4, h * 0.3, w * 0.6, h * 0.9), "test")
    res = notifier.send(Message(title=f"Test alert · {cam.name}",
                                body="If you can see this with a picture, alerts are working.",
                                image_jpeg=image, timestamp=int(time.time())))
    print(f"{notifier.name}: {res.status} {res.detail}")
    if getattr(notifier, "remaining", None) is not None:
        print(f"Pushover messages left this month: {notifier.remaining}")
    return 0 if res.ok else 1
