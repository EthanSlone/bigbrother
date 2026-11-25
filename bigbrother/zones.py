"""Zones and masks, in normalized coordinates (0..1 of frame width/height).

- Zone: a named area. Objects "enter" a zone when the point where they touch
  the ground (bottom-centre of the box) is inside it for 2 checks in a row.
  Alert rules can require a zone ("only alert for people on the porch").
- Motion mask: areas where movement is ignored (trees, the camera's timestamp).
- Object mask: detections whose ground point falls inside are dropped
  (a statue that looks like a person, a parked car you never care about).

Normalized coordinates work for any stream resolution, so the same config
applies to the substream (detection) and the main stream (verification).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Point = tuple[float, float]
Polygon = tuple[Point, ...]

ZONE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class ZoneConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Zone:
    name: str
    points: Polygon
    objects: tuple[str, ...] | None = None  # None = any object

    def applies_to(self, label: str) -> bool:
        return self.objects is None or label in self.objects


def parse_polygon(raw, where: str) -> Polygon:
    if not isinstance(raw, (list, tuple)) or len(raw) < 3:
        raise ZoneConfigError(f"{where} must be a list of at least 3 [x, y] points")
    pts = []
    for p in raw:
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)):
            raise ZoneConfigError(f"{where}: each point must be [x, y]")
        x, y = float(p[0]), float(p[1])
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ZoneConfigError(f"{where}: coordinates are fractions of the frame (0 to 1), got {p}")
        pts.append((x, y))
    return tuple(pts)


def point_in_polygon(pt: Point, poly: Polygon) -> bool:
    """Ray casting; points exactly on an edge may go either way, which is fine here."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < x_cross:
                inside = not inside
    return inside


def ground_point(box, frame_w: int, frame_h: int) -> Point:
    """Bottom-centre of a pixel box, normalized: where a person stands or a car sits."""
    x1, _, x2, y2 = box
    return ((x1 + x2) / 2 / frame_w, y2 / frame_h)


def in_any(pt: Point, polys: tuple[Polygon, ...]) -> bool:
    return any(point_in_polygon(pt, p) for p in polys)


ZONE_INERTIA = 2  # consecutive checks inside before an object counts as having entered


def update_zones(track, zones: dict[str, Zone], frame_w: int, frame_h: int) -> bool:
    """Update track.zones (currently in) and track.zones_visited. Returns True if either changed."""
    pt = ground_point(track.box, frame_w, frame_h)
    before = (set(track.zones), len(track.zones_visited))
    for name, zone in zones.items():
        if zone.applies_to(track.label) and point_in_polygon(pt, zone.points):
            track.zone_streak[name] = track.zone_streak.get(name, 0) + 1
            if track.zone_streak[name] >= ZONE_INERTIA:
                track.zones.add(name)
                if name not in track.zones_visited:
                    track.zones_visited.append(name)
        else:
            track.zone_streak[name] = 0
            track.zones.discard(name)
    return (track.zones, len(track.zones_visited)) != before
