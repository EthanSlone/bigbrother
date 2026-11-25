"""Offline evaluation: run a clip through motion -> model -> tracker -> events.

Used to compare settings (and later, to compare against Frigate on the same
footage). Frames are taken from the file at the given rate, so results don't
depend on how fast the machine is.

    python -m bigbrother evaluate testdata/parking_lot.mp4 --fps 2
"""

from __future__ import annotations

import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import cv2

from ..config import DetectSettings
from ..events_db import EventStore
from .events import EventManager
from .motion import MotionDetector
from .tracker import Tracker
from .frames import output_size


@dataclass
class EvalResult:
    clip: str
    fps: float
    frames: int
    model_runs: int
    events: Counter
    filtered: int
    avg_infer_ms: float

    def summary(self) -> str:
        ev = ", ".join(f"{k}: {v}" for k, v in sorted(self.events.items())) or "none"
        return (f"{Path(self.clip).name} @ {self.fps:g} fps: {sum(self.events.values())} events ({ev}); "
                f"filtered {self.filtered}; model ran on {self.model_runs}/{self.frames} frames "
                f"({self.avg_infer_ms:.0f} ms avg)")


def evaluate_clip(clip: Path, model, fps: float = 5.0, settings: DetectSettings | None = None,
                  loops: int = 1, use_motion: bool = True, tracker_args: dict | None = None) -> EvalResult:
    settings = settings or DetectSettings(fps=fps)
    cap = cv2.VideoCapture(str(clip))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    size = output_size(w, h, settings.width)
    step = src_fps / fps

    with tempfile.TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp) / "eval.db")
        em = EventManager("eval", settings, store, Path(tmp), lambda *a: None)
        tracker = Tracker(**(tracker_args or {}))
        motion = MotionDetector()
        frames = runs = 0
        infer = 0.0
        ts = 0.0
        last_check = -1e9
        last = None
        for _ in range(loops):
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            pos = 0.0
            idx = 0
            while True:
                ok, img = cap.read()
                if not ok:
                    break
                if idx < round(pos):
                    idx += 1
                    continue
                idx += 1
                pos += step
                img = last = cv2.resize(img, size, interpolation=cv2.INTER_AREA)
                frames += 1
                ts += 1.0 / fps
                m = motion.update(img)
                need = (not use_motion or m.detected or m.lighting_change or tracker.has_moving()
                        or ts - last_check >= 30)
                if not need:
                    continue
                t0 = time.perf_counter()
                dets = model.detect(img, settings.min_score, set(settings.labels))
                infer += time.perf_counter() - t0
                runs += 1
                last_check = ts
                ended = tracker.update(dets, ts)
                em.process(tracker.tracks, ended, img, ts)
        # flush: let remaining tracks end
        for _ in range(int((tracker.lost_seconds + 1) * fps) + tracker.max_misses):
            ts += 1.0 / fps
            ended = tracker.update([], ts)
            em.process(tracker.tracks, ended, last, ts)
        events = Counter(e.label for e in store.recent(10000))
        store.close()
    cap.release()
    return EvalResult(str(clip), fps, frames, runs, events, em.filtered,
                      infer / runs * 1000 if runs else 0.0)
