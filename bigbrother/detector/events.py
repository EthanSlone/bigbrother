"""Turns tracks into events: confirm, snapshot, publish, end.

A track only becomes an event once it has been seen for long enough with a
high enough median score: `min_frames` detections, or at least two detections
spanning the time `min_frames` would take at the configured fps (so a slow
machine running the model less often still confirms real objects). One
confident-looking frame of a shadow is never an event. Tracks that end before confirmation are counted as "filtered" so we
can see how many false positives the confirmation step removes.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from ..config import DetectSettings
from ..events_db import Event, EventStore
from .tracker import Track

log = logging.getLogger(__name__)

Publish = Callable[[str, dict, bool], None]  # (topic, payload, retain)

SNAPSHOT_INTERVAL = 2.0  # seconds between snapshot rewrites while an event improves
SAVE_INTERVAL = 5.0      # seconds between database updates for an ongoing event


@dataclass
class _State:
    event: Event | None = None
    best_quality: float = -1.0
    last_snapshot: float = 0.0
    last_save: float = 0.0
    pending_snapshot: tuple[np.ndarray, tuple] | None = None


def snapshot_quality(track: Track, frame_w: int, frame_h: int) -> float:
    """Prefer confident, large, fully-visible views of the object."""
    x1, y1, x2, y2 = track.box
    area = max(0.0, x2 - x1) * max(0.0, y2 - y1) / (frame_w * frame_h)
    q = track.last_det_score * (area ** 0.5)
    margin = 2
    if x1 <= margin or y1 <= margin or x2 >= frame_w - margin or y2 >= frame_h - margin:
        q *= 0.5  # cut off by the frame edge
    return q


class EventManager:
    def __init__(self, camera: str, settings: DetectSettings, store: EventStore, data_dir: Path,
                 publish: Publish):
        self.camera = camera
        self.settings = settings
        self.store = store
        self.data_dir = data_dir
        self.publish = publish
        self.snap_dir = data_dir / "snapshots" / camera
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self._states: dict[int, _State] = {}
        self._counts: dict[str, int] = {}
        self.filtered = 0  # tracks that never became events
        self.started = 0

    # -- main entry ------------------------------------------------------
    def process(self, tracks: list[Track], ended: list[Track], frame: np.ndarray, ts: float) -> None:
        h, w = frame.shape[:2]
        for t in tracks:
            st = self._states.setdefault(t.id, _State())
            if st.event is None:
                if self._confirmed(t):
                    self._start(t, st, frame, w, h, ts)
                continue
            self._ongoing(t, st, frame, w, h, ts)

        for t in ended:
            st = self._states.pop(t.id, None)
            if st is None or st.event is None:
                self.filtered += 1
                continue
            self._end(t, st, ts)
        self._publish_counts(tracks)

    def end_all(self, ts: float) -> None:
        for st in self._states.values():
            if st.event is not None:
                st.event.end_ts = ts
                st.event.updated_ts = ts
                self.store.save(st.event)
                self.publish("events", {"type": "end", "event": st.event.to_dict()}, False)
        self._states.clear()

    def _confirmed(self, t: Track) -> bool:
        """Seen long enough, and either consistently confident or at least once very confident."""
        s = self.settings
        if t.hits < 2:
            return False
        seen_long_enough = t.hits >= s.min_frames or (t.last_ts - t.first_ts) >= (s.min_frames - 1) / s.fps
        if not seen_long_enough:
            return False
        return t.median_score >= s.threshold or (
            t.top_score >= min(0.95, s.threshold + 0.2) and t.hits >= s.min_frames)

    # -- lifecycle -------------------------------------------------------
    def _start(self, t: Track, st: _State, frame: np.ndarray, w: int, h: int, ts: float) -> None:
        eid = f"{int(ts)}-{self.camera}-{secrets.token_hex(2)}"
        st.event = Event(
            id=eid, camera=self.camera, label=t.label, start_ts=t.first_ts, end_ts=None,
            top_score=t.top_score, median_score=t.median_score, box=list(t.box),
            frame_w=w, frame_h=h, stationary=t.stationary, snapshot=None, updated_ts=ts,
        )
        st.best_quality = snapshot_quality(t, w, h)
        self._write_snapshot(st, frame, ts)
        self.store.save(st.event)
        st.last_save = ts
        self.started += 1
        log.info("[%s] event start %s: %s (score %.2f)%s", self.camera, eid, t.label,
                 t.median_score, " [stationary]" if t.stationary else "")
        self.publish("events", {"type": "start", "event": st.event.to_dict()}, False)

    def _ongoing(self, t: Track, st: _State, frame: np.ndarray, w: int, h: int, ts: float) -> None:
        e = st.event
        assert e is not None
        changed = t.label != e.label or t.stationary != e.stationary
        e.label, e.stationary = t.label, t.stationary
        e.top_score, e.median_score = t.top_score, t.median_score
        e.updated_ts = ts

        q = snapshot_quality(t, w, h)
        if q > st.best_quality * 1.1:
            st.best_quality = q
            e.box = list(t.box)
            if ts - st.last_snapshot >= SNAPSHOT_INTERVAL:
                self._write_snapshot(st, frame, ts)
                changed = True
            else:
                st.pending_snapshot = (frame, t.box)  # keep the better frame; write it later

        if st.pending_snapshot is not None and ts - st.last_snapshot >= SNAPSHOT_INTERVAL:
            self._write_snapshot(st, st.pending_snapshot[0], ts)
            changed = True

        if changed or ts - st.last_save >= SAVE_INTERVAL:
            self.store.save(e)
            st.last_save = ts
        if changed:
            self.publish("events", {"type": "update", "event": e.to_dict()}, False)

    def _end(self, t: Track, st: _State, ts: float) -> None:
        e = st.event
        assert e is not None
        if st.pending_snapshot is not None:
            self._write_snapshot(st, st.pending_snapshot[0], ts)
        e.end_ts = t.last_ts
        e.updated_ts = ts
        self.store.save(e)
        log.info("[%s] event end %s: %s after %.0fs", self.camera, e.id, e.label, e.end_ts - e.start_ts)
        self.publish("events", {"type": "end", "event": e.to_dict()}, False)

    # -- helpers ---------------------------------------------------------
    def _write_snapshot(self, st: _State, frame: np.ndarray, ts: float) -> None:
        e = st.event
        assert e is not None
        path = self.snap_dir / f"{e.id}.jpg"
        tmp = path.with_suffix(".tmp.jpg")
        cv2.imwrite(str(tmp), frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
        os.replace(tmp, path)
        e.snapshot = str(path.relative_to(self.data_dir))
        st.last_snapshot = ts
        st.pending_snapshot = None

    def _publish_counts(self, tracks: list[Track]) -> None:
        """Retained per-label counts of moving, confirmed objects (handy for Home Assistant)."""
        counts: dict[str, int] = {}
        for t in tracks:
            st = self._states.get(t.id)
            if st and st.event is not None and not t.stationary:
                counts[t.label] = counts.get(t.label, 0) + 1
        for label in set(counts) | set(self._counts):
            n = counts.get(label, 0)
            if self._counts.get(label) != n:
                self.publish(f"{self.camera}/{label}", {"count": n, "ts": time.time()}, True)
        self._counts = counts
