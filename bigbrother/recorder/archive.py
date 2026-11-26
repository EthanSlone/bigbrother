"""Keep a clip of every event, so events outlive the 24/7 recordings they came from.

Once an event has ended and the recording covering it is on disk, its clip
(5 s before to 5 s after, at most the first 5 minutes) is cut from the
segments with a stream copy (no re-encoding: cheap) and stored under
data/events/<camera>/<id>.mp4. Those follow storage.event_retention_days
(default 30) while the 24/7 footage follows storage.retention_days.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from ..playback import ARCHIVE_MAX_SECONDS, POST_ROLL, PRE_ROLL, archived_clip_path, build_clip
from .index import RecordingIndex

log = logging.getLogger(__name__)

LOOKBACK = 2 * 86400     # events that ended within this window are considered
GIVE_UP_AFTER = 900      # archive whatever exists if the recording never caught up


class EventArchiver:
    def __init__(self, index: RecordingIndex, recordings_root: Path, data_dir: Path, db_path: Path,
                 recording_cameras: set[str]):
        self.index, self.root, self.data_dir, self.db_path = index, recordings_root, data_dir, db_path
        self.cameras = recording_cameras
        self._skip: set[str] = set()   # events with nothing recorded: don't retry every pass

    def _ended(self, now: float) -> list[tuple]:
        try:
            db = sqlite3.connect(self.db_path, timeout=10)
            try:
                return db.execute(
                    "SELECT id, camera, start_ts, end_ts FROM events WHERE end_ts IS NOT NULL AND end_ts > ? "
                    "ORDER BY end_ts", (now - LOOKBACK,)).fetchall()
            finally:
                db.close()
        except sqlite3.OperationalError:   # no events table yet
            return []

    def run_once(self, now: float | None = None, budget_s: float = 20.0) -> int:
        """Archive ready events; stops after `budget_s` so recording housekeeping isn't held up."""
        now = now or time.time()
        t0, done = time.monotonic(), 0
        latest: dict[str, float] = {}
        for eid, cam, start, end in self._ended(now):
            if cam not in self.cameras or eid in self._skip:
                continue
            out = archived_clip_path(self.data_dir, cam, eid)
            if out.exists():
                continue
            clip_start = start - PRE_ROLL
            clip_end = min(end, start + ARCHIVE_MAX_SECONDS) + POST_ROLL
            if cam not in latest:
                latest[cam] = self.index.latest_end(cam) or 0.0
            if latest[cam] < clip_end and now - end < GIVE_UP_AFTER:
                continue   # its last minute isn't on disk yet
            try:
                meta = build_clip(self.index, self.root, cam, clip_start, clip_end, out)
            except RuntimeError as exc:
                log.warning("[%s] couldn't keep clip for event %s: %s", cam, eid, exc)
                self._skip.add(eid)
                continue
            if meta is None:
                self._skip.add(eid)   # nothing recorded at that time
                continue
            done += 1
            if time.monotonic() - t0 > budget_s:
                break
        return done
