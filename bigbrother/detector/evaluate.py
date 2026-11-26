"""Offline evaluation: run a clip through motion -> model -> tracker -> events.

Used to compare settings (and later, to compare against Frigate on the same
footage). Frames are taken from the file at the given rate, so results don't
depend on how fast the machine is.

    python -m bigbrother evaluate testdata/parking_lot.mp4 --fps 2
"""

from __future__ import annotations

import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import cv2

from ..config import DetectSettings
from ..events_db import EventStore
from .events import EventManager
from .frames import output_size
from .pipeline import Pipeline


@dataclass
class EvalResult:
    clip: str
    fps: float
    frames: int
    model_runs: int
    events: Counter
    filtered: int
    avg_infer_ms: float
    zoom_runs: int = 0
    event_list: list | None = None

    def summary(self) -> str:
        ev = ", ".join(f"{k}: {v}" for k, v in sorted(self.events.items())) or "none"
        return (f"{Path(self.clip).name} @ {self.fps:g} fps: {sum(self.events.values())} events ({ev}); "
                f"filtered {self.filtered}; model ran on {self.model_runs}/{self.frames} frames "
                f"({self.avg_infer_ms:.0f} ms avg)")


def evaluate_clip(clip: Path, model, fps: float = 5.0, settings: DetectSettings | None = None,
                  loops: int = 1, use_motion: bool = True, tracker_args: dict | None = None,
                  start_offset: float = 0.0, keep_events: bool = False, motion_masks=(),
                  object_masks=()) -> EvalResult:
    """Run a clip through the production pipeline. `ts` counts seconds from the clip's start
    (plus `start_offset`), so produced events can be compared with real timestamps."""
    settings = settings or DetectSettings(fps=fps)
    cap = cv2.VideoCapture(str(clip))
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    size = output_size(w, h, settings.width)
    step = max(1.0, src_fps / fps)

    with tempfile.TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp) / "eval.db")
        em = EventManager("eval", settings, store, Path(tmp), lambda *a: None)
        pipe = Pipeline("eval", settings, model, em, motion_masks=motion_masks, object_masks=object_masks)
        if tracker_args:
            from .tracker import Tracker
            pipe.tracker = Tracker(label_vote=settings.label_vote, **tracker_args)
        frames = 0
        ts = start_offset
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
                img = last = cv2.resize(img, size, interpolation=cv2.INTER_AREA) if (img.shape[1], img.shape[0]) != size else img
                frames += 1
                ts += 1.0 / fps
                if not use_motion:
                    pipe._last_check = -1e18    # force a model run on every frame
                pipe.process(img, ts)
        if last is not None:
            pipe.flush(last, ts, fps)
        evs = store.recent(10000)
        events = Counter(e.label for e in evs)
        store.close()
    cap.release()
    runs = pipe.model_runs
    res = EvalResult(str(clip), fps, frames, runs, events, em.filtered,
                     sum(pipe.infer_ms) / len(pipe.infer_ms) if pipe.infer_ms else 0.0)
    res.zoom_runs = pipe.zoom_runs
    if keep_events:
        res.event_list = evs
    return res
