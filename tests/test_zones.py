import sqlite3

import numpy as np
import pytest

from bigbrother.config import ConfigError, parse_config
from bigbrother.detector.motion import MotionDetector
from bigbrother.detector.tracker import Track
from bigbrother.events_db import EventStore
from bigbrother.zones import Zone, ground_point, in_any, point_in_polygon, update_zones

SQUARE = ((0.2, 0.2), (0.6, 0.2), (0.6, 0.6), (0.2, 0.6))
TRIANGLE = ((0.0, 0.0), (1.0, 0.0), (0.0, 1.0))


def test_point_in_polygon():
    assert point_in_polygon((0.4, 0.4), SQUARE)
    assert not point_in_polygon((0.7, 0.4), SQUARE)
    assert point_in_polygon((0.2, 0.2), TRIANGLE) and not point_in_polygon((0.8, 0.8), TRIANGLE)


def test_ground_point_is_bottom_centre():
    assert ground_point((100, 50, 140, 150), 640, 360) == pytest.approx((120 / 640, 150 / 360))


def make_track(box, label="person"):
    t = Track(id=1, group=label, box=box, first_ts=0, last_ts=0)
    t.label_votes[label] = 1.0
    return t


def test_zone_entry_needs_two_checks_and_respects_object_filter():
    zones = {"porch": Zone("porch", SQUARE), "cars_only": Zone("cars_only", SQUARE, ("car",))}
    t = make_track((200, 100, 240, 200))  # feet at (220,200) -> (0.34, 0.56): inside
    update_zones(t, zones, 640, 360)
    assert t.zones == set()  # one sighting isn't enough
    assert update_zones(t, zones, 640, 360)
    assert t.zones == {"porch"} and t.zones_visited == ["porch"]  # not cars_only: it's a person
    t.box = (500, 100, 540, 200)  # walks out
    update_zones(t, zones, 640, 360)
    assert t.zones == set() and t.zones_visited == ["porch"]  # history is kept


def test_zone_and_mask_config_parsing():
    cfg = parse_config({"cameras": {"a": {
        "streams": {"main": "rtsp://h/1"},
        "zones": {"porch": {"points": [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5]], "objects": ["person"]}},
        "masks": {"motion": [[[0, 0], [1, 0], [1, 0.1]]], "objects": [[[0, 0], [0.1, 0], [0.1, 0.1]]]},
    }}})
    cam = cfg.cameras["a"]
    assert cam.zones["porch"].objects == ("person",) and len(cam.motion_masks) == 1 and len(cam.object_masks) == 1
    bad = [
        {"zones": {"Porch": {"points": [[0, 0], [1, 0], [1, 1]]}}},           # bad name
        {"zones": {"porch": {"points": [[0, 0], [1, 0]]}}},                   # too few points
        {"zones": {"porch": {"points": [[0, 0], [640, 0], [640, 360]]}}},     # pixels, not fractions
        {"masks": {"motion": [[[0, 0], [1, 0], [1, 1]]], "other": []}},       # unknown mask kind
    ]
    for extra in bad:
        with pytest.raises(ConfigError):
            parse_config({"cameras": {"a": {"streams": {"main": "rtsp://h/1"}, **extra}}})


def test_motion_mask_hides_movement_only_inside_it():
    top_strip = (((0.0, 0.0), (1.0, 0.0), (1.0, 0.3), (0.0, 0.3)),)
    md = MotionDetector(masks=top_strip)
    bg = np.full((360, 640, 3), 80, np.uint8)
    md.update(bg)
    f = bg.copy()
    f[20:60, 100:160] = 230   # inside the mask (top strip)
    assert not md.update(f).detected
    f2 = bg.copy()
    f2[250:300, 100:160] = 230  # below the mask
    assert md.update(f2).detected


def test_object_mask_check():
    statue = (((0.4, 0.4), (0.6, 0.4), (0.6, 0.8), (0.4, 0.8)),)
    assert in_any(ground_point((300, 100, 340, 250), 640, 360), statue)       # feet at (0.5, 0.69)
    assert not in_any(ground_point((50, 100, 90, 250), 640, 360), statue)


def test_old_database_gets_zones_column(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE events (id TEXT PRIMARY KEY, camera TEXT NOT NULL, label TEXT NOT NULL, "
                "start_ts REAL NOT NULL, end_ts REAL, top_score REAL NOT NULL, median_score REAL NOT NULL, "
                "box TEXT NOT NULL, frame_w INTEGER NOT NULL, frame_h INTEGER NOT NULL, "
                "stationary INTEGER NOT NULL DEFAULT 0, snapshot TEXT, updated_ts REAL NOT NULL)")
    con.execute("INSERT INTO events VALUES ('e1','a','person',1,2,0.9,0.8,'[0,0,1,1]',640,360,0,NULL,2)")
    con.commit()
    con.close()
    store = EventStore(db)
    assert store.recent()[0].zones == []
