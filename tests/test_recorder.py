import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from bigbrother.config import parse_config
from bigbrother.recorder.ffmpeg import build_record_cmd
from bigbrother.recorder.index import RecordingIndex, Segment
from bigbrother.recorder.retention import Retention
from bigbrother.recorder.segments import (
    Organizer, completed_segments, dest_relpath, parse_segment_start,
)

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def make_cfg(**storage):
    return parse_config({
        "storage": {"retention_days": 7, **storage},
        "cameras": {
            "front": {"streams": {"main": "rtsp://h/1"}},
            "back": {"streams": {"main": "rtsp://h/2"}, "record": {"retention_days": 1}},
        },
    })


def test_segment_name_round_trip():
    ts = parse_segment_start("20261125T181502Z.mp4")
    assert ts == 1795630502.0
    assert dest_relpath("front", ts) == "front/2026-11-25/18/15.02.mp4"
    assert parse_segment_start("notes.txt") is None


def test_record_cmd_hevc_and_audio():
    cmd = build_record_cmd("rtsp://go2rtc:8554/front", Path("/c"), 60, "hevc", True, True)
    assert cmd[cmd.index("-tag:v") + 1] == "hvc1"
    assert "aac" in cmd and "0:a:0" in cmd
    assert cmd[-1] == "/c/%Y%m%dT%H%M%SZ.mp4"
    plain = build_record_cmd("rtsp://x", Path("/c"), 60, "h264", True, False)
    assert "-tag:v" not in plain and "aac" not in plain  # audio requested but camera has none


def test_completed_segments_holds_back_active_file(tmp_path):
    for name in ["20261125T180000Z.mp4", "20261125T180100Z.mp4", "20261125T180200Z.mp4"]:
        (tmp_path / name).write_bytes(b"x")
    now = time.time()
    done = [p.name for p in completed_segments(tmp_path, now)]
    assert done == ["20261125T180000Z.mp4", "20261125T180100Z.mp4"]
    # Once the newest goes stale (writer gone) it is released too.
    assert len(completed_segments(tmp_path, now + 120)) == 3


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_end_to_end_segmenting_and_organizing(tmp_path):
    """Real ffmpeg writes segments with the production command; organizer indexes them."""
    root = tmp_path / "rec"
    cache = root / ".cache" / "front"
    cache.mkdir(parents=True)
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=10",
         "-t", "25", "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    cmd = build_record_cmd(str(src), cache, 10, "h264", False, False)
    # Swap live-stream input flags for a real-time file input in the test. Wall-clock
    # timestamps can't be combined with -re on a file (verified live against go2rtc instead).
    i = cmd.index("-rtsp_transport")
    del cmd[i:i + 4]
    i = cmd.index("-use_wallclock_as_timestamps")
    del cmd[i:i + 2]
    cmd[cmd.index("-i"):cmd.index("-i")] = ["-re"]
    subprocess.run(cmd, check=True, env={**os.environ, "TZ": "UTC"}, timeout=60)

    index = RecordingIndex(tmp_path / "db.sqlite")
    org = Organizer(root, index)
    moved = org.process_camera("front", now=time.time() + 3600)
    assert moved >= 2
    assert index.count("front") == moved
    assert not list(cache.glob("*.mp4"))
    files = sorted(root.glob("front/*/*/*.mp4"))
    assert len(files) == moved
    segs = index.oldest(10)
    # First and last segments are partial (clock-aligned cuts); every full one is ~10 s.
    for seg in segs[1:-1]:
        assert 8 < seg.end_ts - seg.start_ts < 13
    assert all(0 < s.end_ts - s.start_ts < 13 for s in segs)


def _seed(root: Path, index: RecordingIndex, camera: str, start: float, size=1000):
    rel = dest_relpath(camera, start)
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * size)
    index.add(Segment(camera, start, start + 60, rel, size))
    return p


def test_retention_by_age_respects_per_camera_override(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    now = 1_800_000_000.0
    day = 86400
    old_front = _seed(tmp_path, index, "front", now - 8 * day)
    new_front = _seed(tmp_path, index, "front", now - 2 * day)
    old_back = _seed(tmp_path, index, "back", now - 2 * day)   # back keeps 1 day
    new_back = _seed(tmp_path, index, "back", now - 3600)

    ret = Retention(make_cfg(), tmp_path, index, disk_usage=lambda _: 10.0)
    stats = ret.run_once(now)
    assert stats["age"] == 2
    assert not old_front.exists() and not old_back.exists()
    assert new_front.exists() and new_back.exists()
    assert not old_front.parent.exists()  # empty hour/day folders pruned
    assert index.count() == 2


def test_retention_by_disk_deletes_oldest_first(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    now = 1_800_000_000.0
    paths = [_seed(tmp_path, index, "front", now - i * 60) for i in range(250)]

    def fake_usage(_):
        # Each call after a deletion batch drops usage a bit.
        remaining = index.count()
        return 80.0 + remaining / 250 * 15  # 95% when full, 80% when empty

    ret = Retention(make_cfg(max_usage_percent=90), tmp_path, index, disk_usage=fake_usage)
    removed = ret.enforce_disk()
    assert removed == 100  # one batch got it under 90%
    assert paths[0].exists()          # newest kept
    assert not paths[-1].exists()     # oldest gone
