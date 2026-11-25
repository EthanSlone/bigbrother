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
from ..zones import ground_point, in_any, update_zones

log = logging.getLogger(__name__)

RECHECK_SECONDS = 30.0  # re-run the model this often even without motion, to confirm parked objects


@dataclass
class ViewState:
    """What the live preview overlays: boxes from the latest analysis (frames come straight
    from the decoder, so the preview never waits for the model)."""
    ts: float = 0.0
    tracks: list[dict] = field(default_factory=list)
    motion: list[tuple[int, int, int, int]] = field(default_factory=list)


class CameraWorker(threading.Thread):
    def __init__(self, cam: Camera, url: str, model, store: EventStore, data_dir: Path, publish):
        super().__init__(name=f"detect-{cam.id}", daemon=True)
        self.cam = cam
        s = cam.detect
        # Decode at the preview rate; analyse every `_every`-th frame (e.g. 15 fps -> 5 fps = every 3rd).
        decode_fps = max(s.fps, s.live_fps)
        self.source = FrameSource(cam.id, url, decode_fps, s.width)
        self._every = max(1, round(decode_fps / s.fps))
        self._next_seq = 0
        self.motion = MotionDetector(masks=cam.motion_masks)
        self.tracker = Tracker()
        self.events = EventManager(cam.id, s, store, data_dir, publish)
        self.model = model
        self.view = ViewState()
        self._stop = threading.Event()
        self._last_check = 0.0
        self._infer_ms: deque[float] = deque(maxlen=50)
        self._detect_ts: deque[float] = deque(maxlen=200)
        self._frame_ts: deque[float] = deque(maxlen=200)    # frames processed
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
            last_seq = frame.seq
            if frame.seq < self._next_seq:
                continue  # preview-only frame
            if self._next_seq:
                self.skipped += (frame.seq - self._next_seq) // self._every  # model was busy
            self._next_seq = frame.seq + self._every
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
            h, w = img.shape[:2]
            if self.cam.object_masks:
                dets = [d for d in dets if not in_any(ground_point(d.box, w, h), self.cam.object_masks)]
            self._detect_ts.append(ts)
            self._last_check = ts
            ended = self.tracker.update(dets, ts)
            if self.cam.zones:
                for t in self.tracker.tracks:
                    if t.misses == 0:
                        update_zones(t, self.cam.zones, w, h)
            self.events.process(self.tracker.tracks, ended, img, ts)
        self.view = ViewState(ts=ts, tracks=[self._track_view(t) for t in self.tracker.tracks],
                              motion=m.boxes)

    def _track_view(self, t: Track) -> dict:
        st = self.events._states.get(t.id)
        return {"id": t.id, "label": t.label, "score": round(t.last_det_score, 2),
                "box": list(t.box), "velocity": list(t.velocity), "seen_ts": t.last_ts,
                "stationary": t.stationary, "confirmed": bool(st and st.event), "missed": t.misses > 0,
                "zones": sorted(t.zones)}

    def stats(self) -> dict:
        def rate(q: deque) -> float:
            if len(q) < 2:
                return 0.0
            span = q[-1] - q[0]
            return round((len(q) - 1) / span, 2) if span > 0 else 0.0

        fps_in = self.source.measured_fps()
        return {
            "camera": self.cam.id, "name": self.cam.name, "state": self.source.state,
            "error": self.source.error, "size": self.source.size,
            "fps_in": fps_in, "fps_analysed": rate(self._frame_ts), "detect_fps": rate(self._detect_ts),
            "infer_ms": round(sum(self._infer_ms) / len(self._infer_ms), 1) if self._infer_ms else None,
            "skipped_frames": self.skipped, "tracks": len(self.tracker.tracks),
            "events_started": self.events.started, "filtered": self.events.filtered,
        }


def run(cfg: Config, data_dir: Path, db_path: Path, viewer_port: int,
        recordings_root: Path = Path("/recordings")) -> None:
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

    from ..alerts_db import AlertStore
    from ..playback import Playback
    from ..recorder.index import RecordingIndex
    from .viewer import ROUTES

    server = None
    if viewer_port:
        Playback(RecordingIndex(db_path), recordings_root, data_dir, store,
                 {c.id: c.record.segment_seconds for c in cfg.cameras.values()}).register(ROUTES)
        server = start_viewer(workers, store, data_dir, viewer_port, AlertStore(db_path), publish)

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
