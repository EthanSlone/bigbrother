import io
import json
import shutil
import subprocess
import time
from email.message import Message as Headers
from pathlib import Path

import pytest

from bigbrother.events_db import Event, EventStore
from bigbrother.playback import Playback, build_clip, send_file
from bigbrother.recorder.index import RecordingIndex, Segment
from bigbrother.recorder.segments import dest_relpath

HAVE_FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


class FakeHandler:
    """Just enough of BaseHTTPRequestHandler for send_file."""

    def __init__(self, range_header=None, command="GET"):
        self.headers = Headers()
        if range_header:
            self.headers["Range"] = range_header
        self.command = command
        self.status = None
        self.sent_headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, code):
        self.status = code

    def send_header(self, k, v):
        self.sent_headers[k] = v

    def end_headers(self):
        pass


def test_send_file_ranges(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(bytes(range(256)) * 4)  # 1024 bytes
    h = FakeHandler()
    send_file(h, f, "video/mp4")
    assert h.status == 200 and len(h.wfile.getvalue()) == 1024 and h.sent_headers["Accept-Ranges"] == "bytes"
    h = FakeHandler("bytes=100-199")
    send_file(h, f, "video/mp4")
    assert h.status == 206 and h.sent_headers["Content-Range"] == "bytes 100-199/1024"
    assert h.wfile.getvalue() == f.read_bytes()[100:200]
    h = FakeHandler("bytes=-24")
    send_file(h, f, "video/mp4")
    assert h.wfile.getvalue() == f.read_bytes()[-24:]
    h = FakeHandler("bytes=5000-")
    send_file(h, f, "video/mp4")
    assert h.status == 416
    h = FakeHandler(command="HEAD")
    send_file(h, f, "video/mp4")
    assert h.status == 200 and h.wfile.getvalue() == b"" and h.sent_headers["Content-Length"] == "1024"


def make_segments(root: Path, index: RecordingIndex, camera: str, start: float, n: int, seconds: int = 10,
                  codec: str = "libx264"):
    """Real 10 s MP4 segments with a visible clock, indexed like the recorder does."""
    for i in range(n):
        ts = start + i * seconds
        rel = dest_relpath(camera, ts)
        out = root / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=10:duration={seconds}",
                "-c:v", codec, "-g", "10", "-pix_fmt", "yuv420p"]
        if codec == "libx265":
            args += ["-tag:v", "hvc1", "-x265-params", "log-level=error"]
        subprocess.run(args + [str(out)], check=True)
        index.add(Segment(camera, ts, ts + seconds, rel, out.stat().st_size))


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                          str(path)], capture_output=True, text=True)
    return float(out.stdout.strip())


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_clip_spans_segments_and_trims(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    root = tmp_path / "rec"
    make_segments(root, index, "front", 1_000_000.0, 3)  # 30 s of recordings
    meta = build_clip(index, root, "front", 1_000_005.0, 1_000_022.0, tmp_path / "clip.mp4")
    assert meta["segments"] == 3 and not meta["partial"]
    assert 16 <= duration(tmp_path / "clip.mp4") <= 19  # 17 s requested, cut on keyframes
    # asking past the end of what's recorded gives a partial clip
    meta = build_clip(index, root, "front", 1_000_020.0, 1_000_045.0, tmp_path / "clip2.mp4")
    assert meta["partial"] and meta["end"] == 1_000_030.0
    assert build_clip(index, root, "front", 2_000_000.0, 2_000_010.0, tmp_path / "none.mp4") is None


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_hevc_clip_keeps_iphone_tag(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    root = tmp_path / "rec"
    make_segments(root, index, "front", 1_000_000.0, 2, codec="libx265")
    build_clip(index, root, "front", 1_000_002.0, 1_000_015.0, tmp_path / "clip.mp4")
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_tag_string", "-of", "csv=p=0", str(tmp_path / "clip.mp4")],
                         capture_output=True, text=True)
    assert out.stdout.strip() == "hvc1"


def ev(eid, start, end, label="person"):
    return Event(id=eid, camera="front", label=label, start_ts=start, end_ts=end, top_score=0.9,
                 median_score=0.8, box=[0, 0, 1, 1], frame_w=640, frame_h=360, stationary=False,
                 snapshot=None, updated_ts=end or start)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_event_clip_pre_post_roll_and_caching(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    events = EventStore(tmp_path / "db.sqlite")
    root = tmp_path / "rec"
    make_segments(root, index, "front", 1_000_000.0, 4)
    events.save(ev("e1", 1_000_010.0, 1_000_014.0))
    pb = Playback(index, root, tmp_path, events, {"front": 10})
    path, meta = pb.event_clip("e1")
    assert not meta["partial"] and meta["start"] == 1_000_005.0 and meta["end"] == 1_000_019.0
    mtime = path.stat().st_mtime_ns
    time.sleep(0.05)
    path2, _ = pb.event_clip("e1")
    assert path2.stat().st_mtime_ns == mtime  # complete clips are reused, not rebuilt
    events.save(ev("e2", 1_000_030.0, None))  # still in view: partial, rebuilt each time
    _, meta2 = pb.event_clip("e2")
    assert meta2["partial"]
    assert pb.event_clip("missing") is None


def test_timeline_merges_contiguous_segments_and_lists_events(tmp_path):
    index = RecordingIndex(tmp_path / "db.sqlite")
    events = EventStore(tmp_path / "db.sqlite")
    for start in (0, 60, 120, 400, 460):  # a gap between 180 and 400
        index.add(Segment("front", 1000 + start, 1000 + start + 60, f"front/{start}.mp4", 1))
    events.save(ev("e1", 1100, 1110))
    events.save(ev("old", 10, 20))
    t = Playback(index, tmp_path, tmp_path, events, {"front": 60}).timeline("front", 1000, 1600)
    assert t["recorded"] == [[1000, 1180], [1400, 1520]]
    assert [e["id"] for e in t["events"]] == ["e1"]


def test_retention_prunes_old_events_pictures_and_clips(tmp_path):
    from bigbrother.alerts_db import AlertRecord, AlertStore
    from bigbrother.config import parse_config
    from bigbrother.recorder.retention import Retention

    cfg = parse_config({"storage": {"retention_days": 7, "event_retention_days": 7},
                        "cameras": {"front": {"streams": {"main": "rtsp://h/1"}}}})
    db = tmp_path / "db.sqlite"
    events, alerts = EventStore(db), AlertStore(db)
    now = 1_800_000_000.0
    for eid, age_days in (("old", 10), ("new", 1)):
        snap = tmp_path / "snapshots" / f"{eid}.jpg"
        snap.parent.mkdir(exist_ok=True)
        snap.write_bytes(b"x")
        e = ev(eid, now - age_days * 86400, now - age_days * 86400 + 5)
        e.snapshot = f"snapshots/{eid}.jpg"
        events.save(e)
        img = tmp_path / "alerts" / f"{eid}.jpg"
        img.parent.mkdir(exist_ok=True)
        img.write_bytes(b"x")
        alerts.save(AlertRecord(eid, "front", "person", "sent", image=f"alerts/{eid}.jpg"))
    clip = tmp_path / "clips" / "front" / "old.mp4"
    clip.parent.mkdir(parents=True)
    clip.write_bytes(b"x")
    import os
    os.utime(clip, (now - 3 * 86400, now - 3 * 86400))

    r = Retention(cfg, tmp_path / "rec", RecordingIndex(db), lambda _: 10.0, data_dir=tmp_path, db_path=db)
    stats = r.run_once(now)
    assert stats["events"] == 1 and stats["clips"] == 1
    assert [e.id for e in events.recent()] == ["new"]
    assert not (tmp_path / "snapshots/old.jpg").exists() and (tmp_path / "snapshots/new.jpg").exists()
    assert not (tmp_path / "alerts/old.jpg").exists() and alerts.get("old") is None
    assert not clip.exists()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")
def test_event_clips_are_kept_after_recordings_go(tmp_path):
    from bigbrother.config import parse_config
    from bigbrother.playback import archived_clip_path
    from bigbrother.recorder.archive import EventArchiver
    from bigbrother.recorder.retention import Retention

    db = tmp_path / "db.sqlite"
    index, events, root = RecordingIndex(db), EventStore(db), tmp_path / "rec"
    t0 = 1_000_000.0
    make_segments(root, index, "front", t0, 4)                   # 40 s recorded
    events.save(ev("done", t0 + 10, t0 + 14))
    events.save(ev("late", t0 + 30, t0 + 38))                     # needs t0+43: not on disk yet
    events.save(ev("open", t0 + 20, None))                        # still in view
    arch = EventArchiver(index, root, tmp_path, db, {"front"})
    assert arch.run_once(now=t0 + 60) == 1
    kept = archived_clip_path(tmp_path, "front", "done")
    assert kept.exists() and 12 <= duration(kept) <= 16
    assert not archived_clip_path(tmp_path, "front", "late").exists()
    assert arch.run_once(now=t0 + 60 + 1000) == 1                 # gave up waiting: keeps what exists
    assert archived_clip_path(tmp_path, "front", "late").exists()

    # 24/7 footage expires after 1 day; events are kept 30.
    cfg = parse_config({"storage": {"retention_days": 1, "event_retention_days": 30},
                        "cameras": {"front": {"streams": {"main": "rtsp://h/1"}}}})
    Retention(cfg, root, index, data_dir=tmp_path, db_path=db).run_once(now=t0 + 3 * 86400)
    assert index.count("front") == 0
    pb = Playback(index, root, tmp_path, events, {"front": 10})
    path, meta = pb.event_clip("done")
    assert path == kept and not meta["partial"]                   # still playable
    Retention(cfg, root, index, data_dir=tmp_path, db_path=db).run_once(now=t0 + 31 * 86400)
    assert not kept.exists() and events.get("done") is None


def test_starred_events_outlive_retention(tmp_path):
    from bigbrother.config import parse_config
    from bigbrother.recorder.retention import Retention
    from bigbrother.web.data import EventQueries

    cfg = parse_config({"storage": {"event_retention_days": 7}, "cameras": {"front": {"streams": {"main": "rtsp://h/1"}}}})
    db = tmp_path / "db.sqlite"
    events, q = EventStore(db), EventQueries(db)
    now = 1_800_000_000.0
    for eid in ("a", "b"):
        events.save(ev(eid, now - 30 * 86400, now - 30 * 86400 + 5))
    assert q.keep("a", "ethan") and not q.keep("nope", "ethan")
    Retention(cfg, tmp_path / "rec", RecordingIndex(db), lambda _: 10.0, data_dir=tmp_path, db_path=db).run_once(now)
    assert [e.id for e in events.recent()] == ["a"]
    assert q.event("a")["kept"]["by"] == "ethan"
    assert [e["id"] for e in q.events(review="kept")] == ["a"]
    q.keep("a", None)
    Retention(cfg, tmp_path / "rec", RecordingIndex(db), lambda _: 10.0, data_dir=tmp_path, db_path=db).run_once(now)
    assert events.recent() == []
