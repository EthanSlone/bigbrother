"""Cheap motion check that decides when the (expensive) object model needs to run.

Background subtraction on a small grayscale copy of each frame. Sudden
whole-frame changes (IR switching on at dusk, auto-exposure jumps) are treated
as a lighting change, not motion: the background is reset instead.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import cv2
import numpy as np

WORK_WIDTH = 320


@dataclass
class MotionResult:
    boxes: list[tuple[int, int, int, int]] = field(default_factory=list)  # in frame pixels
    fraction: float = 0.0       # share of the frame that changed
    lighting_change: bool = False

    @property
    def detected(self) -> bool:
        return bool(self.boxes)


class MotionDetector:
    def __init__(self, threshold: int = 25, min_area: float = 0.0008, learn_rate: float = 0.05,
                 lighting_fraction: float = 0.5, masks=()):
        self.threshold = threshold          # pixel difference (0-255) that counts as change
        self.min_area = min_area            # smallest moving blob, as a fraction of the frame
        self.learn_rate = learn_rate        # how fast the background adapts
        self.lighting_fraction = lighting_fraction
        self._bg: np.ndarray | None = None
        self.masks = tuple(masks)            # normalized polygons where motion is ignored
        self._keep: np.ndarray | None = None  # 1 = count motion here, 0 = masked
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def update(self, frame: np.ndarray) -> MotionResult:
        h, w = frame.shape[:2]
        scale = WORK_WIDTH / w
        small = cv2.resize(frame, (WORK_WIDTH, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (7, 7), 0).astype(np.float32)

        if self._bg is None or self._bg.shape != gray.shape:
            self._bg = gray.copy()
            return MotionResult()

        diff = cv2.absdiff(gray, self._bg)
        mask = (diff > self.threshold).astype(np.uint8) * 255
        if self.masks:
            if self._keep is None or self._keep.shape != mask.shape:
                self._keep = np.ones_like(mask)
                mh, mw = mask.shape
                for poly in self.masks:
                    pts = np.array([[x * mw, y * mh] for x, y in poly], np.int32)
                    cv2.fillPoly(self._keep, [pts], 0)
            mask *= self._keep
        mask = cv2.dilate(mask, self._kernel, iterations=2)
        fraction = float(np.count_nonzero(mask)) / mask.size

        if fraction > self.lighting_fraction:
            self._bg = gray.copy()
            return MotionResult(fraction=fraction, lighting_change=True)

        cv2.accumulateWeighted(gray, self._bg, self.learn_rate)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        min_px = self.min_area * mask.size
        boxes = []
        for c in contours:
            if cv2.contourArea(c) < min_px:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            boxes.append((int(x / scale), int(y / scale), int((x + bw) / scale), int((y + bh) / scale)))
        return MotionResult(boxes=boxes, fraction=fraction)
