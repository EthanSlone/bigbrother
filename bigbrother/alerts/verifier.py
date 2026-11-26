"""Second opinion before alerting.

The detector works on the small substream. Before a notification goes out,
we grab a full-resolution frame from go2rtc, cut out the area around the
object, and run a larger model (YOLOX-m) on it. Only if that model also sees
the same kind of object does the alert go out. This is the main defence
against shadows, bugs and headlights that fooled the small model.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

import cv2
import numpy as np

from ..detector.tracker import group_of

log = logging.getLogger(__name__)

MIN_CROP = 640      # px: never look at less than this (the model's input size)
EXPAND = 2.5        # crop side = this x the object's longest side (it may have moved)


@dataclass
class VerifyResult:
    ok: bool
    score: float = 0.0
    box: tuple[float, float, float, float] | None = None  # in full-frame pixels
    reason: str = ""


def fetch_frame(go2rtc_api: str, stream: str, timeout: float = 8.0) -> np.ndarray | None:
    url = f"{go2rtc_api}/api/frame.jpeg?src={urllib.parse.quote(stream)}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = resp.read()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not fetch a full-res frame for %s: %s", stream, exc)
        return None
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    return img


def crop_region(box_norm, frame_w: int, frame_h: int) -> tuple[int, int, int, int]:
    """Square-ish region around a normalized box, at least MIN_CROP px, kept inside the frame."""
    x1, y1, x2, y2 = box_norm
    cx, cy = (x1 + x2) / 2 * frame_w, (y1 + y2) / 2 * frame_h
    side = max((x2 - x1) * frame_w, (y2 - y1) * frame_h) * EXPAND
    side = min(max(side, MIN_CROP), frame_w, frame_h)
    left = int(min(max(cx - side / 2, 0), frame_w - side))
    top = int(min(max(cy - side / 2, 0), frame_h - side))
    return left, top, left + int(side), top + int(side)


NIGHT_BOOST = 0.1   # infrared footage: the second opinion must be this much surer


class Verifier:
    def __init__(self, model, threshold: float):
        self.model = model
        self.threshold = threshold

    def verify(self, event: dict, frame: np.ndarray) -> VerifyResult:
        """Look for the event's object near where the detector saw it."""
        fh, fw = frame.shape[:2]
        ew, eh = event["frame_w"], event["frame_h"]
        bx = event["box"]
        box_norm = (bx[0] / ew, bx[1] / eh, bx[2] / ew, bx[3] / eh)
        l, t, r, b = crop_region(box_norm, fw, fh)
        crop = frame[t:b, l:r]
        group = group_of(event["label"])
        dets = self.model.detect(crop, min_score=0.25)
        same = [d for d in dets if group_of(d.label) == group]
        if not same:
            other = max((d for d in dets if d.score >= 0.5), key=lambda d: d.score, default=None)
            why = (f"second opinion saw a {other.label}, not a {event['label']}" if other
                   else f"second opinion found no {event['label']}")
            return VerifyResult(False, 0.0, None, why)
        best = max(same, key=lambda d: d.score)
        full_box = (best.box[0] + l, best.box[1] + t, best.box[2] + l, best.box[3] + t)
        threshold = min(0.95, self.threshold + (NIGHT_BOOST if event.get("night") else 0.0))
        if best.score < threshold:
            night = " at night" if event.get("night") else ""
            return VerifyResult(False, best.score, full_box,
                                f"second opinion unsure{night} ({best.label} {best.score:.2f} < {threshold:.2f})")
        return VerifyResult(True, best.score, full_box)
