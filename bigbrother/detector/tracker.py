"""Multi-object tracker: turns per-frame detections into objects with identities.

ByteTrack-style two-stage matching against a constant-velocity prediction.
Confident detections are matched first; weak ones may only extend existing
tracks (so a person half-hidden behind a car stays the same object) and never
start new ones.

Matching uses box overlap (IoU) first, then falls back to centre distance
measured in object sizes. The allowed jump grows with the time since the
track was last seen. That keeps identities stable when the model can only run
a couple of times per second and people move further than their own width
between checks.

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
                "motorcycle": "cycle", "bicycle": "cycle",
                "dog": "animal", "cat": "animal", "bird": "animal", "horse": "animal", "sheep": "animal",
                "cow": "animal", "bear": "animal"}
# People and animals get mixed up at night and at a distance (a dog called a person for a few
# frames). With label voting, a strongly overlapping detection of the other kind updates the same
# object and adds a vote, instead of starting a second, wrongly-labelled object.
LOOKALIKE_GROUPS = {"person", "animal"}
CROSS_MATCH_IOU = 0.5

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
    zones: set = field(default_factory=set)             # zones it is in right now
    zones_visited: list = field(default_factory=list)   # every zone it has entered, in order
    zone_streak: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return max(self.label_votes, key=self.label_votes.get)

    @property
    def label_share(self) -> float:
        """How clearly the winning label won (1.0 = every vote agreed)."""
        total = sum(self.label_votes.values())
        return self.label_votes[self.label] / total if total else 0.0

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
    def __init__(self, high_score: float = 0.5, match_iou: float = 0.2, max_misses: int = 2,
                 lost_seconds: float = 2.0, jump_base: float = 0.5, jump_per_second: float = 1.5,
                 stationary_seconds: float = 10.0, stationary_move: float = 0.15,
                 start_ids: int = 1, label_vote: bool = True):
        self.high_score = high_score
        self.match_iou = match_iou
        # A track ends once it has missed at least `max_misses` detection runs AND has
        # been unseen for `lost_seconds`, so behaviour doesn't depend on the model's speed.
        self.max_misses = max_misses
        self.lost_seconds = lost_seconds
        self.jump_base = jump_base              # distance match allowance, in object sizes...
        self.jump_per_second = jump_per_second  # ...plus this much per second since last seen
        self.stationary_seconds = stationary_seconds
        self.stationary_move = stationary_move  # movement (fraction of object size) that counts as moving
        self.label_vote = label_vote
        self.tracks: list[Track] = []
        self._ids = itertools.count(start_ids)

    def has_moving(self) -> bool:
        return any(not t.stationary for t in self.tracks)

    def update(self, dets: list[Detection], ts: float, can_start=None) -> list[Track]:
        """Feed one frame's detections. Returns tracks that ended on this frame.

        `can_start(det)` decides whether an unmatched confident detection may start a new
        object (e.g. only where something moved)."""
        unmatched_tracks = list(self.tracks)
        high = [d for d in dets if d.score >= self.high_score]
        low = [d for d in dets if d.score < self.high_score]

        leftover_high = self._match(unmatched_tracks, high, ts)
        self._match(unmatched_tracks, low, ts)

        ended = []
        for t in unmatched_tracks:
            t.misses += 1
            if t.misses >= self.max_misses and ts - t.last_ts >= self.lost_seconds:
                ended.append(t)
        if ended:
            self.tracks = [t for t in self.tracks if t not in ended]

        for d in leftover_high:
            if can_start is not None and not can_start(d):
                continue
            t = Track(id=next(self._ids), group=group_of(d.label), box=d.box, first_ts=ts, last_ts=ts)
            t.scores.append(d.score)
            t.label_votes[d.label] = d.score
            for lbl, sc in d.votes:
                t.label_votes[lbl] = t.label_votes.get(lbl, 0.0) + sc
            t.history.append((ts, center(d.box)))
            t.last_det_score = d.score
            self.tracks.append(t)
        return ended

    def _match(self, tracks: list[Track], dets: list[Detection], ts: float) -> list[Detection]:
        """Greedy matching; matched tracks are removed from `tracks`. Returns unmatched dets.

        Every overlap (IoU) match ranks above every distance-only match.
        """
        if not tracks or not dets:
            return list(dets)
        pairs = []
        for ti, t in enumerate(tracks):
            pred = t.predicted(ts)
            pc = center(pred)
            size = t.size()
            allowed = self.jump_base + self.jump_per_second * min(max(ts - t.last_ts, 0.0), 3.0)
            for di, d in enumerate(dets):
                if group_of(d.label) != t.group:
                    if (self.label_vote and t.group in LOOKALIKE_GROUPS and group_of(d.label) in LOOKALIKE_GROUPS
                            and (overlap := iou(pred, d.box)) >= CROSS_MATCH_IOU):
                        pairs.append((0.75 + overlap * 0.5, ti, di))   # below same-kind overlap matches
                    continue
                overlap = iou(pred, d.box)
                if overlap >= self.match_iou:
                    pairs.append((1.0 + overlap, ti, di))
                    continue
                dc = center(d.box)
                dist = ((dc[0] - pc[0]) ** 2 + (dc[1] - pc[1]) ** 2) ** 0.5 / size
                d_size = max(d.box[2] - d.box[0], d.box[3] - d.box[1], 1.0)
                if dist > allowed or not 1 / 3 <= d_size / size <= 3:
                    continue
                if not self._direction_ok(t, dc):
                    continue  # a moving object doesn't jump against its own direction of travel
                pairs.append((1.0 - dist / allowed, ti, di))  # always < any IoU match
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

    @staticmethod
    def _direction_ok(t: Track, new_center: tuple[float, float], min_cos: float = 0.5) -> bool:
        vx = (t.velocity[0] + t.velocity[2]) / 2
        vy = (t.velocity[1] + t.velocity[3]) / 2
        speed = (vx * vx + vy * vy) ** 0.5
        if speed < 0.3 * t.size():  # barely moving: any direction is plausible
            return True
        cx, cy = center(t.box)
        dx, dy = new_center[0] - cx, new_center[1] - cy
        dist = (dx * dx + dy * dy) ** 0.5
        if dist < 0.25 * t.size():
            return True
        return (dx * vx + dy * vy) / (dist * speed) >= min_cos

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
        for lbl, sc in d.votes:
            t.label_votes[lbl] = t.label_votes.get(lbl, 0.0) + sc
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
