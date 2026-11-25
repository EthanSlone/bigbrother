"""Detector service: one worker per camera sharing one loaded model."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..config import Camera, Config
from ..events_db import EventStore
from ..mqtt import Publisher
from .events import EventManager
from .frames import FrameSource
from .model_store import resolve_model
from .models import InferencePool, load_model, plan_threads
from .motion import MotionDetector, MotionResult
from .tracker import Track, Tracker

log = logging.getLogger(__name__)

RECHECK_SECONDS = 30.0  # re-run the model this often even without motion, to confirm parked objects


@dataclass
class ViewState:
    frame: np.ndarray | None = None
    ts: float = 0.0
    tracks: list[dict] = field(default_factory=list)
    motion: list[tuple[int, int, int, int]] = field(default_factory=list)


class CameraWorker(threading.Thread):
    def __init__(self, cam: Camera, url: str, model, store: EventStore, data_dir: Path, publish):
        super().__init__(name=f"detect-{cam.id}", daemon=True)
        self.cam = cam
        s = cam.detect
        self.source = FrameSource(cam.id, url, s.fps, s.width)
        self.motion = MotionDetector()
        self.tracker = Tracker()
        self.events = EventManager(cam.id, s, store, data_dir, publish)
        self.model = model
        self.view = ViewState()
        self._stop = threading.Event()
        self._last_check = 0.0
        self._infer_ms: deque[float] = deque(maxlen=50)
        self._detect_ts: deque[float] = deque(maxlen=200)
        self._frame_ts: deque[float] = deque(maxlen=200)    # frames processed
        self._in_samples: deque[tuple[float, int]] = deque(maxlen=60)  # (time, decoder frame count)
        self.skipped = 0

    def stop(self) -> None:
        self._stop.set()
        self.source.stop()

    def run(self) -> None:
        self.source.start()
        last_seq = 0
        while not self._stop.is_set():
            frame = self.source.wait_frame(last_seq, timeout=5)
            if frame is None:
                continue
            if last_seq and frame.seq > last_seq + 1:
                self.skipped += frame.seq - last_seq - 1  # detector fell behind; skipped stale frames
            last_seq = frame.seq
            self._frame_ts.append(frame.ts)
            try:
                self._process(frame.image, frame.ts)
            except Exception:  # noqa: BLE001 - one bad frame must not kill the camera
                log.exception("[%s] processing failed", self.cam.id)
        self.events.end_all(time.time())

    def _process(self, img: np.ndarray, ts: float) -> None:
        m: MotionResult = self.motion.update(img)
        need = (
            m.detected
            or m.lighting_change
            or self.tracker.has_moving()
            or ts - self._last_check >= RECHECK_SECONDS
        )
        if need:
            s = self.cam.detect
            t0 = time.perf_counter()
            dets = self.model.detect(img, s.min_score, set(s.labels))
            self._infer_ms.append((time.perf_counter() - t0) * 1000)
            self._detect_ts.append(ts)
            self._last_check = ts
            ended = self.tracker.update(dets, ts)
            self.events.process(self.tracker.tracks, ended, img, ts)
        self.view = ViewState(frame=img, ts=ts, tracks=[self._track_view(t) for t in self.tracker.tracks],
                              motion=m.boxes)

    def _track_view(self, t: Track) -> dict:
        st = self.events._states.get(t.id)
        return {"id": t.id, "label": t.label, "score": round(t.last_det_score, 2),
                "box": [round(v) for v in t.box], "stationary": t.stationary,
                "confirmed": bool(st and st.event), "missed": t.misses > 0}

    def stats(self) -> dict:
        def rate(q: deque) -> float:
            if len(q) < 2:
                return 0.0
            span = q[-1] - q[0]
            return round((len(q) - 1) / span, 2) if span > 0 else 0.0

        now = time.monotonic()
        self._in_samples.append((now, self.source.frames_in))
        while len(self._in_samples) > 2 and now - self._in_samples[0][0] > 30:
            self._in_samples.popleft()
        (t0, n0), (t1, n1) = self._in_samples[0], self._in_samples[-1]
        fps_in = round((n1 - n0) / (t1 - t0), 2) if t1 > t0 else 0.0
        return {
            "camera": self.cam.id, "name": self.cam.name, "state": self.source.state,
            "error": self.source.error, "size": self.source.size,
            "fps_in": fps_in, "fps_processed": rate(self._frame_ts), "detect_fps": rate(self._detect_ts),
            "infer_ms": round(sum(self._infer_ms) / len(self._infer_ms), 1) if self._infer_ms else None,
            "skipped_frames": self.skipped, "tracks": len(self.tracker.tracks),
            "events_started": self.events.started, "filtered": self.events.filtered,
        }


def run(cfg: Config, data_dir: Path, db_path: Path, viewer_port: int) -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    d = cfg.detector
    model_path, builtin_family = resolve_model(d.model)
    workers, threads = plan_threads(d.workers, d.threads)
    log.info("inference: %d worker(s) x %d thread(s)", workers, threads)
    model = InferencePool(load_model(builtin_family or d.family, model_path, d.backend, d.device, threads),
                          workers)

    store = EventStore(db_path)
    closed = store.close_open(time.time())
    if closed:
        log.info("closed %d event(s) left open by the last shutdown", closed)

    mqtt = None
    if os.environ.get("BB_MQTT", "1") != "0":
        mqtt = Publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix, "detector")
        mqtt.start()

    def publish(topic: str, payload: dict, retain: bool) -> None:
        if mqtt:
            mqtt.publish(topic, payload, retain=retain)

    workers = []
    for cam in cfg.cameras.values():
        if not cam.detect.enabled:
            log.info("[%s] detection disabled in config", cam.id)
            continue
        if not cam.sub:
            log.warning("[%s] no substream: detecting on the main stream (more CPU)", cam.id)
        w = CameraWorker(cam, cfg.restream_url(cam.detect_stream), model, store, data_dir, publish)
        w.start()
        workers.append(w)

    from .viewer import start_viewer

    server = start_viewer(workers, store, data_dir, viewer_port) if viewer_port else None

    try:
        while not stop.is_set():
            stop.wait(60)
            for w in workers:
                s = w.stats()
                log.info("[%s] %s in=%.1ffps detect=%.1ffps infer=%sms events=%d filtered=%d skipped=%d",
                         s["camera"], s["state"], s["fps_in"], s["detect_fps"], s["infer_ms"],
                         s["events_started"], s["filtered"], s["skipped_frames"])
    finally:
        log.info("shutting down %d camera worker(s)", len(workers))
        if server:
            server.shutdown()
        for w in workers:
            w.stop()
        for w in workers:
            w.join(timeout=10)
        model.shutdown()
        if mqtt:
            mqtt.stop()
        store.close()
