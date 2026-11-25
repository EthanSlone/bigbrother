"""Multi-object tracker: turns per-frame detections into objects with identities.

ByteTrack-style two-stage matching on IoU against a constant-velocity
prediction. Confident detections are matched first; weak ones may only extend
existing tracks (so a person half-hidden behind a car stays the same object)
and never start new ones.

Misses are counted only on frames where detection actually ran. When nothing
moves we skip the model entirely, and a parked car must not "disappear"
just because we stopped looking.
"""

from __future__ import annotations

import itertools
import statistics
from collections import deque
from dataclasses import dataclass, field

from .models import Detection

# Labels the model often confuses with each other; treated as one kind of object for matching.
LABEL_GROUPS = {"car": "vehicle", "truck": "vehicle", "bus": "vehicle",
                "motorcycle": "cycle", "bicycle": "cycle"}

Box = tuple[float, float, float, float]


def group_of(label: str) -> str:
    return LABEL_GROUPS.get(label, label)


def iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter + 1e-9)


def center(b: Box) -> tuple[float, float]:
    return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2


@dataclass
class Track:
    id: int
    group: str
    box: Box
    first_ts: float
    last_ts: float
    velocity: Box = (0.0, 0.0, 0.0, 0.0)  # box change per second
    hits: int = 1
    misses: int = 0
    scores: deque = field(default_factory=lambda: deque(maxlen=30))
    label_votes: dict[str, float] = field(default_factory=dict)
    history: deque = field(default_factory=lambda: deque(maxlen=200))  # (ts, center)
    stationary: bool = False
    anchor: tuple[float, float] | None = None  # where it settled when it became stationary
    last_det_score: float = 0.0

    @property
    def label(self) -> str:
        return max(self.label_votes, key=self.label_votes.get)

    @property
    def median_score(self) -> float:
        return statistics.median(self.scores) if self.scores else 0.0

    @property
    def top_score(self) -> float:
        return max(self.scores) if self.scores else 0.0

    def predicted(self, ts: float) -> Box:
        dt = min(max(ts - self.last_ts, 0.0), 2.0)  # don't extrapolate wildly after long gaps
        return tuple(b + v * dt for b, v in zip(self.box, self.velocity))  # type: ignore[return-value]

    def size(self) -> float:
        return max(self.box[2] - self.box[0], self.box[3] - self.box[1], 1.0)


class Tracker:
    def __init__(self, high_score: float = 0.5, match_iou: float = 0.2, max_misses: int = 8,
                 stationary_seconds: float = 10.0, stationary_move: float = 0.15,
                 start_ids: int = 1):
        self.high_score = high_score
        self.match_iou = match_iou
        self.max_misses = max_misses            # detection runs without a match before a track ends
        self.stationary_seconds = stationary_seconds
        self.stationary_move = stationary_move  # movement (fraction of object size) that counts as moving
        self.tracks: list[Track] = []
        self._ids = itertools.count(start_ids)

    def has_moving(self) -> bool:
        return any(not t.stationary for t in self.tracks)

    def update(self, dets: list[Detection], ts: float) -> list[Track]:
        """Feed one frame's detections. Returns tracks that ended on this frame."""
        unmatched_tracks = list(self.tracks)
        high = [d for d in dets if d.score >= self.high_score]
        low = [d for d in dets if d.score < self.high_score]

        leftover_high = self._match(unmatched_tracks, high, ts)
        self._match(unmatched_tracks, low, ts)

        ended = []
        for t in unmatched_tracks:
            t.misses += 1
            if t.misses > self.max_misses:
                ended.append(t)
        if ended:
            self.tracks = [t for t in self.tracks if t not in ended]

        for d in leftover_high:
            t = Track(id=next(self._ids), group=group_of(d.label), box=d.box, first_ts=ts, last_ts=ts)
            t.scores.append(d.score)
            t.label_votes[d.label] = d.score
            t.history.append((ts, center(d.box)))
            t.last_det_score = d.score
            self.tracks.append(t)
        return ended

    def _match(self, tracks: list[Track], dets: list[Detection], ts: float) -> list[Detection]:
        """Greedy IoU matching; matched tracks are removed from `tracks`. Returns unmatched dets."""
        if not tracks or not dets:
            return list(dets)
        pairs = []
        for ti, t in enumerate(tracks):
            pred = t.predicted(ts)
            for di, d in enumerate(dets):
                if group_of(d.label) != t.group:
                    continue
                score = iou(pred, d.box)
                if score >= self.match_iou:
                    pairs.append((score, ti, di))
        pairs.sort(reverse=True)
        used_t, used_d = set(), set()
        for _, ti, di in pairs:
            if ti in used_t or di in used_d:
                continue
            used_t.add(ti)
            used_d.add(di)
            self._apply(tracks[ti], dets[di], ts)
        for ti in sorted(used_t, reverse=True):
            tracks.pop(ti)
        return [d for i, d in enumerate(dets) if i not in used_d]

    def _apply(self, t: Track, d: Detection, ts: float) -> None:
        dt = ts - t.last_ts
        if dt > 0:
            inst = tuple((n - o) / dt for n, o in zip(d.box, t.box))
            t.velocity = tuple(0.6 * v + 0.4 * i for v, i in zip(t.velocity, inst))  # type: ignore[assignment]
        t.box = d.box
        t.last_ts = ts
        t.hits += 1
        t.misses = 0
        t.scores.append(d.score)
        t.last_det_score = d.score
        t.label_votes[d.label] = t.label_votes.get(d.label, 0.0) + d.score
        t.history.append((ts, center(d.box)))
        self._update_stationary(t, ts)

    def _update_stationary(self, t: Track, ts: float) -> None:
        cx, cy = center(t.box)
        limit = self.stationary_move * t.size()
        if t.stationary:
            ax, ay = t.anchor  # type: ignore[misc]
            if abs(cx - ax) > 2 * limit or abs(cy - ay) > 2 * limit:
                t.stationary, t.anchor = False, None
                t.velocity = (0.0, 0.0, 0.0, 0.0)
            return
        window = [(hts, c) for hts, c in t.history if ts - hts <= self.stationary_seconds]
        if not window or ts - window[0][0] < self.stationary_seconds * 0.8:
            return  # not enough history yet
        if all(abs(c[0] - cx) <= limit and abs(c[1] - cy) <= limit for _, c in window):
            t.stationary, t.anchor = True, (cx, cy)
            t.velocity = (0.0, 0.0, 0.0, 0.0)
