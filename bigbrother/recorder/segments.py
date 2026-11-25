"""Move finished segments from the cache into the dated tree and index them.

Layout (all times UTC):
    <root>/<camera>/<YYYY-MM-DD>/<HH>/<MM>.<SS>.mp4
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .index import RecordingIndex, Segment

log = logging.getLogger(__name__)

STALE_SECONDS = 30  # newest file untouched this long => its writer is gone
FAILED_DIR = "_failed"


def parse_segment_start(name: str) -> float | None:
    """'20261125T181500Z.mp4' -> unix timestamp, or None if not a segment name."""
    try:
        dt = datetime.strptime(name, "%Y%m%dT%H%M%SZ.mp4").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


def dest_relpath(camera: str, start_ts: float) -> str:
    dt = datetime.fromtimestamp(start_ts, tz=timezone.utc)
    return f"{camera}/{dt:%Y-%m-%d}/{dt:%H}/{dt:%M.%S}.mp4"


def probe_duration(path: Path, timeout: float = 15) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        dur = float(json.loads(out.stdout)["format"]["duration"])
    except (KeyError, ValueError, json.JSONDecodeError):
        return None
    return dur if dur > 0 else None


def completed_segments(cache_dir: Path, now: float | None = None) -> list[Path]:
    """Segments ffmpeg has finished writing.

    Every segment except the newest is complete (ffmpeg has moved on). The
    newest counts as complete only once it has gone stale, i.e. its writer
    exited or was restarted.
    """
    now = time.time() if now is None else now
    try:
        files = sorted(
            (p for p in cache_dir.iterdir() if p.is_file() and parse_segment_start(p.name) is not None),
            key=lambda p: p.name,
        )
    except FileNotFoundError:
        return []
    if not files:
        return []
    done = files[:-1]
    newest = files[-1]
    try:
        if now - newest.stat().st_mtime > STALE_SECONDS:
            done.append(newest)
    except FileNotFoundError:
        pass
    return done


class Organizer:
    def __init__(self, root: Path, index: RecordingIndex):
        self.root = root
        self.index = index

    def cache_dir(self, camera: str) -> Path:
        return self.root / ".cache" / camera

    def process_camera(self, camera: str, now: float | None = None) -> int:
        moved = 0
        for src in completed_segments(self.cache_dir(camera), now):
            if self._process_one(camera, src):
                moved += 1
        return moved

    def _process_one(self, camera: str, src: Path) -> bool:
        start = parse_segment_start(src.name)
        assert start is not None
        duration = probe_duration(src)
        if duration is None:
            size = src.stat().st_size if src.exists() else 0
            if size < 1024:
                log.warning("[%s] dropping empty segment %s", camera, src.name)
                src.unlink(missing_ok=True)
            else:
                failed = self.root / ".cache" / FAILED_DIR / camera
                failed.mkdir(parents=True, exist_ok=True)
                log.warning("[%s] unreadable segment %s (%d bytes) moved to %s",
                            camera, src.name, size, failed)
                os.replace(src, failed / src.name)
            return False

        rel = dest_relpath(camera, start)
        dest = self.root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dest)  # same volume -> atomic rename
        self.index.add(Segment(
            camera=camera, start_ts=start, end_ts=start + duration,
            path=rel, size_bytes=dest.stat().st_size,
        ))
        return True
