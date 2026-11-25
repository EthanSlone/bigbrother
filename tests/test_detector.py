from pathlib import Path

import numpy as np
import pytest

from bigbrother.config import ConfigError, DetectSettings, parse_config
from bigbrother.detector.events import EventManager
from bigbrother.detector.frames import build_decode_cmd, output_size
from bigbrother.detector.model_store import BUILTIN, resolve_model
from bigbrother.detector.models import Detection, YoloxModel, nms
from bigbrother.detector.motion import MotionDetector
from bigbrother.detector.tracker import Tracker, iou
from bigbrother.events_db import EventStore


def det(label, score, x, y, w=40, h=80):
    return Detection(label, score, (x, y, x + w, y + h))


# -- tracker ---------------------------------------------------------------

def test_moving_object_keeps_its_identity():
    tr = Tracker()
    for i in range(10):
        tr.update([det("person", 0.8, 100 + i * 10, 100)], ts=i * 0.2)
    assert len(tr.tracks) == 1
    t = tr.tracks[0]
    assert t.hits == 10 and t.label == "person" and not t.stationary


def test_two_objects_get_separate_tracks():
    tr = Tracker()
    for i in range(5):
        tr.update([det("person", 0.8, 50 + i * 5, 100), det("person", 0.8, 400 - i * 5, 100)], ts=i * 0.2)
    assert len(tr.tracks) == 2
    assert {t.hits for t in tr.tracks} == {5}


def test_weak_detection_extends_but_never_starts_a_track():
    tr = Tracker(high_score=0.5)
    tr.update([det("person", 0.3, 100, 100)], ts=0)
    assert tr.tracks == []  # weak alone: ignored
    tr.update([det("person", 0.8, 100, 100)], ts=0.2)
    tr.update([det("person", 0.3, 104, 100)], ts=0.4)  # partially hidden: still the same person
    assert len(tr.tracks) == 1 and tr.tracks[0].hits == 2


def test_track_ends_after_misses_and_time_unseen():
    tr = Tracker(max_misses=2, lost_seconds=2.0)
    tr.update([det("car", 0.9, 10, 10)], ts=0)
    assert tr.update([], ts=0.5) == [] and tr.update([], ts=1.0) == []  # missed twice, but only 1 s
    ended = tr.update([], ts=2.2)
    assert len(ended) == 1 and tr.tracks == []
    # Not looking (no detection run) never ends a track, however long it's been.
    tr.update([det("car", 0.9, 10, 10)], ts=10)
    assert tr.update([], ts=60) == []  # first miss only


def test_identity_kept_when_model_runs_only_once_per_second():
    """At 1 check/s a walking person moves further than their own width: no overlap."""
    tr = Tracker()
    for i in range(6):
        tr.update([det("person", 0.8, 50 + i * 60, 100)], ts=float(i))  # 60 px/s, box 40 wide
    assert len(tr.tracks) == 1 and tr.tracks[0].hits == 6


def test_distance_match_refuses_jump_against_direction_of_travel():
    tr = Tracker()
    for i in range(5):  # car driving right
        tr.update([det("car", 0.9, 100 + i * 20, 100, 80, 40)], ts=i * 0.2)
    car = tr.tracks[0]
    # Car disappears; a different car shows up behind it, to the left.
    tr.update([], ts=1.0)
    tr.update([det("car", 0.9, 60, 100, 80, 40)], ts=1.4)
    assert car.hits == 5 and len(tr.tracks) == 2


def test_car_truck_confusion_is_one_object_with_majority_label():
    tr = Tracker()
    labels = ["car", "truck", "car", "car", "truck", "car"]
    for i, lab in enumerate(labels):
        tr.update([det(lab, 0.8, 100, 100, 120, 60)], ts=i * 0.2)
    assert len(tr.tracks) == 1 and tr.tracks[0].label == "car"


def test_parked_car_becomes_stationary_then_moving_again():
    tr = Tracker(stationary_seconds=4)
    ts = 0.0
    for _ in range(30):
        tr.update([det("car", 0.9, 200, 200, 120, 60)], ts=ts)
        ts += 0.2
    assert tr.tracks[0].stationary
    assert not tr.has_moving()
    x = 200
    for _ in range(10):
        x += 15
        tr.update([det("car", 0.9, x, 200, 120, 60)], ts=ts)
        ts += 0.2
    assert len(tr.tracks) == 1 and not tr.tracks[0].stationary


# -- events ----------------------------------------------------------------

class Recorder:
    def __init__(self):
        self.msgs = []

    def __call__(self, topic, payload, retain):
        self.msgs.append((topic, payload, retain))

    def types(self):
        return [p["type"] for t, p, _ in self.msgs if t == "events"]


def run_track(tmp_path, scores, settings=DetectSettings(min_frames=3, threshold=0.6)):
    store = EventStore(tmp_path / "db.sqlite")
    pub = Recorder()
    em = EventManager("front", settings, store, tmp_path, pub)
    tr = Tracker()
    frame = np.zeros((360, 640, 3), np.uint8)
    ts = 0.0
    for i, s in enumerate(scores):
        ended = tr.update([det("person", s, 100 + i * 8, 100)], ts)
        em.process(tr.tracks, ended, frame, ts)
        ts += 0.2
    for _ in range(15):  # > lost_seconds of empty frames
        ended = tr.update([], ts)
        em.process(tr.tracks, ended, frame, ts)
        ts += 0.2
    return em, store, pub


def test_confident_object_becomes_one_event_with_snapshot(tmp_path):
    em, store, pub = run_track(tmp_path, [0.8] * 8)
    assert pub.types()[0] == "start" and pub.types()[-1] == "end"
    events = store.recent()
    assert len(events) == 1
    e = events[0]
    assert e.label == "person" and e.end_ts is not None and e.median_score == pytest.approx(0.8)
    assert (tmp_path / e.snapshot).exists()
    assert em.started == 1 and em.filtered == 0
    # per-label count went up then back down for Home Assistant
    counts = [p["count"] for t, p, r in pub.msgs if t == "front/person"]
    assert counts[0] == 1 and counts[-1] == 0


def test_brief_or_low_scoring_objects_are_filtered(tmp_path):
    em, store, pub = run_track(tmp_path, [0.9, 0.9])  # too few frames
    assert store.recent() == [] and em.filtered == 1
    em2, store2, _ = run_track(tmp_path / "b", [0.55, 0.5, 0.55, 0.52, 0.5])  # never confident enough
    assert store2.recent() == [] and em2.filtered == 1


def test_slow_detection_still_confirms_by_time_seen(tmp_path):
    """2 sightings 0.5 s apart confirm at 5 fps settings (they span the time 3 frames would)."""
    em, store, _ = run_track(tmp_path, [0.8, 0.8],
                             settings=DetectSettings(fps=5, min_frames=3, threshold=0.6))
    assert em.filtered == 1  # 0.2 s apart: too brief
    store2 = EventStore(tmp_path / "b.sqlite")
    em2 = EventManager("front", DetectSettings(fps=5, min_frames=3, threshold=0.6), store2, tmp_path,
                       lambda *a: None)
    tr = Tracker()
    frame = np.zeros((360, 640, 3), np.uint8)
    for ts in (0.0, 0.5):
        em2.process(tr.tracks, tr.update([det("person", 0.8, 100 + ts * 20, 100)], ts), frame, ts)
    assert em2.started == 1


def test_one_very_confident_sighting_confirms_a_consistent_track(tmp_path):
    em, store, _ = run_track(tmp_path, [0.56, 0.45, 0.38, 0.87])  # median 0.5, top 0.87
    assert em.started == 1


def test_open_events_closed_after_restart(tmp_path):
    em, store, pub = run_track(tmp_path, [0.8] * 5)
    e = store.recent()[0]
    e.end_ts = None
    store.save(e)
    assert store.close_open(now=0) == 1
    assert store.recent()[0].end_ts == e.updated_ts


# -- motion ----------------------------------------------------------------

def test_motion_detects_moving_square_and_ignores_static_scene():
    md = MotionDetector()
    bg = np.full((360, 640, 3), 80, np.uint8)
    md.update(bg)
    assert not md.update(bg).detected
    frame = bg.copy()
    frame[100:160, 200:240] = 230
    r = md.update(frame)
    assert r.detected
    x1, y1, x2, y2 = r.boxes[0]
    assert x1 <= 200 <= x2 and y1 <= 100 <= y2


def test_lighting_change_is_not_motion():
    md = MotionDetector()
    md.update(np.full((360, 640, 3), 60, np.uint8))
    r = md.update(np.full((360, 640, 3), 200, np.uint8))  # IR / exposure flip
    assert r.lighting_change and not r.detected


# -- models ----------------------------------------------------------------

class FakeBackend:
    input_shape = (64, 64)

    def __init__(self, out):
        self.out = out

    def infer(self, tensor):
        assert tensor.shape == (1, 3, 64, 64)
        return self.out


def test_yolox_decode_maps_grid_offsets_to_image_boxes():
    model = YoloxModel(FakeBackend(None), labels=["person", "car"])
    n = sum((64 // s) ** 2 for s in YoloxModel.STRIDES)
    out = np.full((1, n, 7), -20.0, np.float32)
    out[..., 4:] = 0.0
    idx = 2 * 8 + 3  # stride-8 grid cell x=3, y=2 -> centre (3+0.5)*8, (2+0.5)*8
    out[0, idx, :4] = [0.5, 0.5, np.log(2.0), np.log(4.0)]  # w=16, h=32
    out[0, idx, 4] = 0.9      # objectness
    out[0, idx, 5:] = [0.95, 0.1]
    dets = model.decode(out, ratio=0.5, min_score=0.5)
    assert len(dets) == 1 and dets[0].label == "person"
    assert dets[0].score == pytest.approx(0.855, abs=1e-3)
    # centre (28,20), size 16x32 in model pixels -> /0.5 back to the source image
    assert dets[0].box == pytest.approx((40, 8, 72, 72))


def test_nms_suppresses_overlaps_and_label_filter_applies():
    boxes = np.array([[0, 0, 10, 10], [1, 1, 11, 11], [50, 50, 60, 60]], np.float32)
    keep = nms(boxes, np.array([0.9, 0.8, 0.7]), iou=0.5)
    assert list(keep) == [0, 2]
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == pytest.approx(1.0)


def test_thread_planning_splits_cpus_between_workers():
    from bigbrother.detector.models import plan_threads
    assert plan_threads(0, 0, cpus=8) == (1, 8)      # laptop: one inference at a time, all cores
    assert plan_threads(0, 0, cpus=40) == (5, 8)     # server VM
    assert plan_threads(2, 0, cpus=8) == (2, 4)
    assert plan_threads(0, 3, cpus=4) == (1, 3)


def test_builtin_models_have_checksums():
    for info in BUILTIN.values():
        assert len(info.sha256) == 64 and info.url.startswith("https://")


def test_unknown_model_path_is_a_clear_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="not built in"):
        resolve_model("my_model.onnx", models_dir=tmp_path)


# -- frames & config ---------------------------------------------------------

def test_decode_size_and_command():
    assert output_size(2560, 1440, 640) == (640, 360)
    assert output_size(640, 480, 640) == (640, 480)
    assert output_size(705, 481, 2000) == (704, 480)  # even sizes
    cmd = build_decode_cmd("rtsp://go2rtc:8554/front_sub", 5, (640, 360))
    assert "fps=5,scale=640:360" in cmd and cmd[-1] == "pipe:1" and "-rtsp_transport" in cmd


def test_detect_config_defaults_overrides_and_validation():
    cfg = parse_config({
        "defaults": {"detect": {"fps": 4, "labels": ["person", "car"]}},
        "detector": {"model": "yolox_m", "backend": "onnxruntime", "device": "cuda"},
        "cameras": {
            "a": {"streams": {"main": "rtsp://h/1", "sub": "rtsp://h/2"}},
            "b": {"streams": {"main": "rtsp://h/3"}, "detect": {"threshold": 0.7}},
        },
    })
    assert cfg.cameras["a"].detect.fps == 4 and cfg.cameras["a"].detect.labels == ("person", "car")
    assert cfg.cameras["b"].detect.threshold == 0.7 and cfg.cameras["b"].detect.fps == 4
    assert cfg.cameras["a"].detect_stream == "a_sub" and cfg.cameras["b"].detect_stream == "b"
    assert cfg.detector.backend == "onnxruntime" and cfg.detector.device == "cuda"
    with pytest.raises(ConfigError):
        parse_config({"cameras": {"a": {"streams": {"main": "rtsp://h/1"},
                                        "detect": {"min_score": 0.8, "threshold": 0.5}}}})
    with pytest.raises(ConfigError):
        parse_config({"detector": {"backend": "magic"}, "cameras": {"a": {"streams": {"main": "rtsp://h/1"}}}})


@pytest.mark.skipif(not Path("/tmp/models/yolox_s.onnx").exists(), reason="model not downloaded")
def test_real_model_finds_a_person_shaped_blob_or_nothing_spurious():
    """Smoke test with the real model: a blank frame must produce no detections."""
    from bigbrother.detector.models import load_model
    model = load_model("yolox", Path("/tmp/models/yolox_s.onnx"), "openvino", threads=1)
    assert model.detect(np.full((360, 640, 3), 120, np.uint8), 0.35) == []
