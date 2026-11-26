"""One camera's detection pipeline: motion -> model (+ zoom) -> tracker -> zones -> events.

Shared by the live detector and the offline evaluators, so what's measured
offline is exactly what runs in production.

Quality features (each can be switched off per camera, for comparison):
  - zoom: the model sees the whole frame at 640 px. Small moving things (a person
    at the far end of the yard) are only a few pixels there, so each small moving
    area not already explained by a confident detection gets a second look: a crop
    at the substream's full resolution. One extra inference, only when needed.
  - motion_gate: a new object must appear where something moved recently. A coat
    on a chair or a bush that looks like a person never starts an event; objects
    already being tracked keep being followed when they stop.
  - label_vote: overlapping person/animal detections of the same thing become
    votes on one object, and an event waits until one label clearly wins.
  - night: in infrared (black-and-white) footage the model is less sure and makes
    more look-alike mistakes, so confirmation asks for more.
"""

from __future__ import annotations

import time
from collections import deque

import cv2
import numpy as np

from ..config import MOTION_SENSITIVITY, DetectSettings
from ..zones import ground_point, in_any, update_zones
from .events import EventManager
from .models import Detection
from .motion import MotionDetector, MotionResult
from .tracker import LOOKALIKE_GROUPS, Tracker, group_of, iou

RECHECK_SECONDS = 30.0     # run the model this often even without motion (stationary objects)
MOTION_MEMORY = 2.0        # seconds a motion area stays "recent" for the motion gate
START_WITHOUT_MOTION = 0.85  # a detection this confident may start an object without motion
ZOOM_MAX_FRACTION = 0.3    # motion areas smaller than this share of the frame get a closer look
ZOOM_SMALL_TRACK = 0.15    # tracked objects shorter than this share of the frame height stay zoomed on
ZOOM_COOLDOWN = 10.0       # after a zoom finds nothing, that part of the frame isn't zoomed again for this long
ZOOM_GRID = 8              # cooldown cells per side
LOOKALIKE_IOU = 0.6


class NightDetector:
    """Infrared footage is pure black-and-white (colour ~0), while even grey daytime scenes
    (asphalt from above, overcast) measure ~1.5. Hysteresis avoids flapping at dusk."""

    def __init__(self, chroma_limit: float = 0.8, switch_after: int = 10):
        self.chroma_limit, self.switch_after = chroma_limit, switch_after
        self.is_night = False
        self._streak = 0

    def update(self, img: np.ndarray) -> bool:
        small = cv2.resize(img, (64, 36), interpolation=cv2.INTER_AREA)
        ycc = cv2.cvtColor(small, cv2.COLOR_BGR2YCrCb).astype(np.float32)
        chroma = float(np.mean(np.abs(ycc[..., 1] - 128) + np.abs(ycc[..., 2] - 128)))
        looks_night = chroma < self.chroma_limit
        if looks_night != self.is_night:
            self._streak += 1
            if self._streak >= self.switch_after:
                self.is_night, self._streak = looks_night, 0
        else:
            self._streak = 0
        return self.is_night


def model_input_size(model) -> tuple[int, int]:
    """(width, height) the model runs at."""
    m = getattr(model, "model", model)          # InferencePool wraps the model
    h, w = m.backend.input_shape
    return w, h


def nms_merge(dets: list[Detection], iou_limit: float = 0.5) -> list[Detection]:
    """Merge detections from overlapping passes: same-kind duplicates keep the best score."""
    out: list[Detection] = []
    for d in sorted(dets, key=lambda x: -x.score):
        if any(group_of(o.label) == group_of(d.label) and iou(o.box, d.box) >= iou_limit for o in out):
            continue
        out.append(d)
    return out


def merge_lookalikes(dets: list[Detection]) -> list[Detection]:
    """The model often reports one object twice, e.g. as person 0.55 and dog 0.62 on the same box.
    Keep the stronger one and carry the other as a vote."""
    out: list[Detection] = []
    for d in sorted(dets, key=lambda x: -x.score):
        g = group_of(d.label)
        for i, o in enumerate(out):
            og = group_of(o.label)
            if g != og and g in LOOKALIKE_GROUPS and og in LOOKALIKE_GROUPS and iou(o.box, d.box) >= LOOKALIKE_IOU:
                out[i] = Detection(o.label, o.score, o.box, o.votes + ((d.label, d.score),) + d.votes)
                break
        else:
            out.append(d)
    return out


def _intersects(a, b, grow: float = 0.15) -> bool:
    gw, gh = (a[2] - a[0]) * grow, (a[3] - a[1]) * grow
    return not (a[2] + gw < b[0] or b[2] < a[0] - gw or a[3] + gh < b[1] or b[3] < a[1] - gh)


class Pipeline:
    def __init__(self, cam_id: str, settings: DetectSettings, model, events: EventManager, *,
                 zones: dict | None = None, motion_masks=(), object_masks=()):
        self.cam_id, self.s, self.model, self.events = cam_id, settings, model, events
        threshold, min_area = MOTION_SENSITIVITY[settings.motion_sensitivity]
        self.motion = MotionDetector(threshold=threshold, min_area=min_area, masks=motion_masks)
        self.tracker = Tracker(label_vote=settings.label_vote)
        self.night = NightDetector()
        self.zones = zones or {}
        self.object_masks = tuple(object_masks)
        self._recent_motion: deque[tuple[float, list]] = deque()
        self._last_check = -1e18
        self.infer_ms: deque[float] = deque(maxlen=50)
        self.wait_ms: deque[float] = deque(maxlen=50)
        self.detect_ts: deque[float] = deque(maxlen=200)
        self.model_runs = 0
        self.zoom_runs = 0
        self._zoom_cool: dict[tuple[int, int], float] = {}

    # -- model --------------------------------------------------------------------
    def _run_model(self, img: np.ndarray) -> list[Detection]:
        s = self.s
        if hasattr(self.model, "detect_timed"):
            dets, infer_ms, wait_ms = self.model.detect_timed(img, s.min_score, set(s.labels))
        else:
            t0 = time.perf_counter()
            dets = self.model.detect(img, s.min_score, set(s.labels))
            infer_ms, wait_ms = (time.perf_counter() - t0) * 1000, 0.0
        self.infer_ms.append(infer_ms)
        self.wait_ms.append(wait_ms)
        self.model_runs += 1
        return dets

    def _zoom_target(self, img: np.ndarray, motion_boxes: list, dets: list[Detection], ts: float):
        """Pick at most one area for a closer look, or None.

        1. A small object being tracked that the full-frame pass just lost (keeps far-away
           people tracked). 2. Otherwise the biggest small moving area that nothing detected or
           tracked explains, unless a zoom there recently found nothing (trees, bugs, rain)."""
        h, w = img.shape[:2]
        iw, ih = model_input_size(self.model)
        if w < iw * 1.5:
            return None    # the model already sees this frame at (nearly) full resolution
        seen = [d.box for d in dets if d.score >= 0.3]
        lost_small = [t for t in self.tracker.tracks
                      if not t.stationary and (t.box[3] - t.box[1]) < h * ZOOM_SMALL_TRACK
                      and not any(iou(t.predicted(ts), b) > 0.1 for b in seen)]
        if lost_small:
            t = max(lost_small, key=lambda t: t.median_score)
            return t.predicted(ts)
        tracked = [t.predicted(ts) for t in self.tracker.tracks]
        cands = []
        for b in motion_boxes:
            bw, bh = b[2] - b[0], b[3] - b[1]
            if bw > w * ZOOM_MAX_FRACTION or bh > h * ZOOM_MAX_FRACTION or bw * bh <= 0:
                continue
            if any(_intersects(b, o, 0.1) for o in seen + tracked):
                continue
            if self._zoom_cool.get(self._cell(b, w, h), -1e18) > ts:
                continue
            cands.append(b)
        return max(cands, key=lambda x: (x[2] - x[0]) * (x[3] - x[1])) if cands else None

    @staticmethod
    def _cell(b, w: int, h: int) -> tuple[int, int]:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return min(ZOOM_GRID - 1, int(cx / w * ZOOM_GRID)), min(ZOOM_GRID - 1, int(cy / h * ZOOM_GRID))

    def _zoom(self, img: np.ndarray, motion_boxes: list, dets: list[Detection], ts: float) -> list[Detection]:
        """A closer look, at the substream's full resolution, at one small area."""
        b = self._zoom_target(img, motion_boxes, dets, ts)
        if b is None:
            return []
        h, w = img.shape[:2]
        iw, ih = model_input_size(self.model)
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        x0 = int(min(max(cx - iw / 2, 0), w - iw))
        y0 = int(min(max(cy - ih / 2, 0), h - ih))
        crop = img[y0:y0 + ih, x0:x0 + iw]
        self.zoom_runs += 1
        out = []
        for d in self._run_model(np.ascontiguousarray(crop)):
            x1, y1, x2, y2 = d.box
            # Objects cut off by the crop edge are left to the full-frame pass.
            cut = ((x1 <= 2 and x0 > 0) or (y1 <= 2 and y0 > 0) or
                   (x2 >= iw - 2 and x0 + iw < w) or (y2 >= ih - 2 and y0 + ih < h))
            if not cut:
                out.append(Detection(d.label, d.score, (x1 + x0, y1 + y0, x2 + x0, y2 + y0), d.votes))
        if not any(d.score >= 0.5 for d in out):
            self._zoom_cool[self._cell(b, w, h)] = ts + ZOOM_COOLDOWN
        return out

    # -- per frame -----------------------------------------------------------------
    def process(self, img: np.ndarray, ts: float) -> tuple[MotionResult, bool]:
        """Analyse one frame. Returns (motion, whether the model ran)."""
        m = self.motion.update(img)
        night = self.night.update(img)
        if m.boxes or m.small_boxes:
            self._recent_motion.append((ts, m.boxes + m.small_boxes))
        while self._recent_motion and ts - self._recent_motion[0][0] > MOTION_MEMORY:
            self._recent_motion.popleft()
        need = (m.detected or m.lighting_change or self.tracker.has_moving()
                or ts - self._last_check >= RECHECK_SECONDS)
        if not need:
            return m, False
        s = self.s
        dets = self._run_model(img)
        if s.zoom:
            extra = self._zoom(img, m.boxes + m.small_boxes, dets, ts)
            if extra:
                dets = nms_merge(dets + extra)
        if s.label_vote:
            dets = merge_lookalikes(dets)
        h, w = img.shape[:2]
        if self.object_masks:
            dets = [d for d in dets if not in_any(ground_point(d.box, w, h), self.object_masks)]
        self.detect_ts.append(ts)
        self._last_check = ts
        can_start = self._can_start if s.motion_gate else None
        ended = self.tracker.update(dets, ts, can_start)
        if self.zones:
            for t in self.tracker.tracks:
                if t.misses == 0:
                    update_zones(t, self.zones, w, h)
        self.events.process(self.tracker.tracks, ended, img, ts, night=night)
        return m, True

    def _can_start(self, d: Detection) -> bool:
        if d.score >= START_WITHOUT_MOTION:
            return True
        # Small objects get a little more slack: their motion blobs are tiny and patchy.
        grow = 0.15 if (d.box[3] - d.box[1]) > 60 else 0.5
        return any(_intersects(d.box, b, grow) for _, boxes in self._recent_motion for b in boxes)

    def flush(self, img: np.ndarray, ts: float, fps: float) -> None:
        """End every track (end of a clip)."""
        for _ in range(int((self.tracker.lost_seconds + 1) * fps) + self.tracker.max_misses):
            ts += 1.0 / fps
            ended = self.tracker.update([], ts)
            self.events.process(self.tracker.tracks, ended, img, ts, night=self.night.is_night)
