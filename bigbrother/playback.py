"""Recording playback: event clips, segment streaming and a per-camera timeline.

- Event clip: the event from PRE_ROLL seconds before to POST_ROLL after, stitched
  from the recorder's segments with ffmpeg's concat demuxer and stream copy (no
  re-encoding, so it's fast and lossless). Cuts land on keyframes.
- Segments are served with HTTP range support, which browsers (Safari in
  particular) need to seek in video.
- Timeline: recorded spans (gaps show missing footage) plus event markers.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

from .events_db import EventStore
from .recorder.index import RecordingIndex

log = logging.getLogger(__name__)

PRE_ROLL = 5.0
POST_ROLL = 5.0
ARCHIVE_MAX_SECONDS = 300   # archived clips cover at most the first 5 minutes of an event


def archived_clip_path(data_dir: Path, camera: str, event_id: str) -> Path:
    """Where an event's kept clip lives. It outlasts the 24/7 recordings it was cut from."""
    return data_dir / "events" / camera / f"{event_id}.mp4"
CHUNK = 256 * 1024
MAX_SPAN = 7 * 86400  # longest timeline range served at once


# -- HTTP helpers ----------------------------------------------------------------

def send_file(h, path: Path, ctype: str, extra_headers: dict | None = None) -> None:
    """Serve a file with single-range support (206 Partial Content)."""
    size = path.stat().st_size
    start, end, status = 0, size - 1, 200
    rng = h.headers.get("Range")
    if rng and rng.startswith("bytes="):
        first, _, last = rng[6:].split(",")[0].strip().partition("-")
        try:
            if first == "":                      # "bytes=-500": the last 500 bytes
                start = max(0, size - int(last))
            else:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
        except ValueError:
            start, end = 0, size - 1
        if start >= size or start > end:
            h.send_response(416)
            h.send_header("Content-Range", f"bytes */{size}")
            h.send_header("Content-Length", "0")
            h.end_headers()
            return
        status = 206
    h.send_response(status)
    h.send_header("Content-Type", ctype)
    h.send_header("Accept-Ranges", "bytes")
    h.send_header("Content-Length", str(end - start + 1))
    if status == 206:
        h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    for k, v in (extra_headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    if h.command == "HEAD":
        return
    with path.open("rb") as f:
        f.seek(start)
        left = end - start + 1
        while left > 0:
            chunk = f.read(min(CHUNK, left))
            if not chunk:
                break
            h.wfile.write(chunk)
            left -= len(chunk)


# -- clips ---------------------------------------------------------------------------

def _concat_line(path: Path) -> str:
    return "file '" + str(path).replace("'", "'\\''") + "'"


def _video_codec(path: Path) -> str | None:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                          "stream=codec_name", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, timeout=15)
    return out.stdout.strip() or None


def build_clip(index: RecordingIndex, root: Path, camera: str, start: float, end: float,
               out: Path) -> dict | None:
    """Write [start, end] of a camera's recordings to `out`. Returns info, or None if nothing recorded."""
    segs = [(i, s) for i, s in index.between(camera, start, end) if (root / s.path).exists()]
    if not segs:
        return None
    lines = []
    for n, (_, s) in enumerate(segs):
        lines.append(_concat_line(root / s.path))
        if n == 0 and start > s.start_ts:
            lines.append(f"inpoint {start - s.start_ts:.3f}")
        if n == len(segs) - 1 and end < s.end_ts:
            lines.append(f"outpoint {end - s.start_ts:.3f}")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as lf:
        lf.write("\n".join(lines) + "\n")
        list_file = lf.name
    tmp = out.with_suffix(".part.mp4")
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
           "-map", "0", "-c", "copy"]
    if _video_codec(root / segs[0][1].path) == "hevc":
        cmd += ["-tag:v", "hvc1"]  # keeps H.265 playable on iPhone/Safari
    cmd += ["-movflags", "+faststart", str(tmp)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        os.unlink(list_file)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {res.stderr.strip()[-300:]}")
    os.replace(tmp, out)
    recorded_until = segs[-1][1].end_ts
    return {"start": max(start, segs[0][1].start_ts), "end": min(end, recorded_until),
            "partial": recorded_until < end - 1.0, "segments": len(segs)}


class Playback:
    def __init__(self, index: RecordingIndex, recordings_root: Path, data_dir: Path, events: EventStore,
                 segment_seconds: dict[str, int]):
        self.index = index
        self.root = recordings_root
        self.clips = data_dir / "clips"
        self.data_dir = data_dir
        self.events = events
        self.segment_seconds = segment_seconds
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def event_clip(self, event_id: str) -> tuple[Path, dict] | None:
        e = self.events.get(event_id)
        if e is None:
            return None
        with self._lock:
            lock = self._locks.setdefault(event_id, threading.Lock())
        with lock:
            kept = archived_clip_path(self.data_dir, e.camera, e.id)
            if kept.exists():
                return kept, {"partial": False, "archived": True}
            out = self.clips / e.camera / f"{e.id}.mp4"
            meta_path = out.with_suffix(".json")
            if out.exists() and meta_path.exists():
                meta = json.loads(meta_path.read_text())
                if not meta.get("partial"):
                    return out, meta  # complete: never changes again
            end = (e.end_ts if e.end_ts else time.time()) + POST_ROLL
            meta = build_clip(self.index, self.root, e.camera, e.start_ts - PRE_ROLL, end, out)
            if meta is None:
                return None
            # A clip can only be complete once the event has ended and its last segment is on disk.
            meta["partial"] = meta["partial"] or e.end_ts is None
            meta_path.write_text(json.dumps(meta))
            return out, meta

    def timeline(self, camera: str, start: float, end: float) -> dict:
        end = min(end, start + MAX_SPAN)
        segs = self.index.between(camera, start, end)
        gap = self.segment_seconds.get(camera, 60) * 0.5 + 5  # tolerate small gaps between segments
        spans: list[list[float]] = []
        for _, s in segs:
            if spans and s.start_ts - spans[-1][1] <= gap:
                spans[-1][1] = max(spans[-1][1], s.end_ts)
            else:
                spans.append([s.start_ts, s.end_ts])
        events = [ev for ev in self.events.recent(2000, camera) if ev.start_ts < end and (ev.end_ts or end) > start]
        return {
            "camera": camera, "start": start, "end": end,
            "recorded": [[round(a, 2), round(b, 2)] for a, b in spans],
            "events": [{"id": ev.id, "label": ev.label, "start": ev.start_ts, "end": ev.end_ts,
                        "zones": ev.zones} for ev in events],
        }

    def segments(self, camera: str, start: float, end: float) -> list[dict]:
        return [{"id": i, "start": s.start_ts, "end": s.end_ts}
                for i, s in self.index.between(camera, start, min(end, start + 6 * 3600))]

    # -- routes for the viewer ------------------------------------------------------
    def register(self, routes: dict) -> None:
        routes["/clip/"] = self._route_clip
        routes["/recording/"] = self._route_segment
        routes["/api/timeline"] = self._route_timeline
        routes["/api/segments"] = self._route_segments

    def _route_clip(self, h, url) -> None:
        name = url.path[len("/clip/"):]
        if not name.endswith(".mp4"):
            return h.not_found()
        try:
            got = self.event_clip(name[:-4])  # id is looked up in the DB, never used as a path
        except RuntimeError as exc:
            log.warning("clip for %s failed: %s", name, exc)
            return h.send_json({"error": "could not build clip"}, 500)
        if got is None:
            return h.send_json({"error": "no recording covers this event yet"}, 404)
        path, meta = got
        send_file(h, path, "video/mp4", {"X-Clip-Partial": "1" if meta["partial"] else "0",
                                         "Cache-Control": "no-store"})

    def _route_segment(self, h, url) -> None:
        name = url.path[len("/recording/"):]
        if not name.endswith(".mp4") or not name[:-4].isdigit():
            return h.not_found()
        seg = self.index.by_id(int(name[:-4]))
        if seg is None or not (self.root / seg.path).exists():
            return h.not_found()
        send_file(h, self.root / seg.path, "video/mp4", {"Cache-Control": "private, max-age=3600"})

    @staticmethod
    def _range(url) -> tuple[str, float, float]:
        q = parse_qs(url.query)
        now = time.time()
        return (q.get("camera", [""])[0], float(q.get("start", [now - 86400])[0]),
                float(q.get("end", [now])[0]))

    def _route_timeline(self, h, url) -> None:
        cam, start, end = self._range(url)
        h.send_json(self.timeline(cam, start, end))

    def _route_segments(self, h, url) -> None:
        cam, start, end = self._range(url)
        h.send_json(self.segments(cam, start, end))
