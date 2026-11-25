"""Recorder service: one ffmpeg per camera, plus organizer and retention loops."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from pathlib import Path

from ..config import Config
from ..mqtt import Publisher
from .ffmpeg import CameraRecorder
from .index import RecordingIndex
from .retention import Retention
from .segments import Organizer

log = logging.getLogger(__name__)

ORGANIZE_EVERY = 5      # seconds
RETENTION_EVERY = 600   # seconds


def run(cfg: Config, recordings_root: Path, db_path: Path) -> None:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    index = RecordingIndex(db_path)
    organizer = Organizer(recordings_root, index,
                          {c.id: c.record.segment_seconds for c in cfg.cameras.values()})
    retention = Retention(cfg, recordings_root, index)

    mqtt = None
    if os.environ.get("BB_MQTT", "1") != "0":
        mqtt = Publisher(cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix, "recorder")
        mqtt.start()

    def on_state(cam_id: str, state: str, detail: str) -> None:
        if mqtt:
            mqtt.publish(f"recorder/{cam_id}/state",
                         {"state": state, "detail": detail, "ts": time.time()}, retain=True)

    recorders: list[CameraRecorder] = []
    for cam in cfg.cameras.values():
        if not cam.record.enabled:
            log.info("[%s] recording disabled in config", cam.id)
            continue
        rec = CameraRecorder(cam, cfg.restream_url(cam.main_stream), organizer.cache_dir(cam.id), on_state)
        rec.start()
        recorders.append(rec)

    if not recorders:
        log.warning("no cameras have recording enabled; idling")

    last_retention = 0.0
    try:
        while not stop.is_set():
            for rec in recorders:
                try:
                    organizer.process_camera(rec.camera.id)
                except Exception:  # noqa: BLE001 - keep the loop alive
                    log.exception("[%s] organizer failed", rec.camera.id)
            if time.monotonic() - last_retention > RETENTION_EVERY:
                try:
                    retention.run_once()
                except Exception:  # noqa: BLE001
                    log.exception("retention failed")
                last_retention = time.monotonic()
            stop.wait(ORGANIZE_EVERY)
    finally:
        log.info("shutting down: stopping %d recorder(s)", len(recorders))
        for rec in recorders:
            rec.stop()
        for rec in recorders:
            rec.join(timeout=15)
        # Index whatever ffmpeg finalized on the way out.
        for rec in recorders:
            organizer.process_camera(rec.camera.id, now=time.time() + 3600)
        if mqtt:
            mqtt.stop()
        index.close()
