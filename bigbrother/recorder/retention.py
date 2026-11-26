"""Delete old recordings: by age per camera, then oldest-first if the disk is too full."""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable

from ..config import Config
from .index import RecordingIndex, Segment
from .segments import FAILED_DIR

log = logging.getLogger(__name__)

DiskUsage = Callable[[Path], float]  # returns used percent 0-100


def disk_used_percent(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return usage.used / usage.total * 100 if usage.total else 0.0


CLIP_CACHE_SECONDS = 2 * 86400  # event clips are rebuilt on demand, so keep them briefly


class Retention:
    def __init__(self, cfg: Config, root: Path, index: RecordingIndex,
                 disk_usage: DiskUsage = disk_used_percent, data_dir: Path | None = None,
                 db_path: Path | None = None):
        self.cfg = cfg
        self.root = root
        self.index = index
        self.disk_usage = disk_usage
        self.data_dir = data_dir
        self.db_path = db_path

    def run_once(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        stats = {"age": self.enforce_age(now), "disk": self.enforce_disk(), "failed": self.clean_failed(now),
                 "events": self.prune_events(now), "clips": self.prune_clips(now)}
        if any(stats.values()):
            log.info("retention removed %s", stats)
        return stats

    def enforce_age(self, now: float) -> int:
        removed = 0
        for cam in self.cfg.cameras.values():
            days = cam.record.retention_days or self.cfg.storage.retention_days
            cutoff = now - days * 86400
            while batch := self.index.older_than(cam.id, cutoff, limit=500):
                removed += self._delete(batch)
        return removed

    def enforce_disk(self) -> int:
        limit = self.cfg.storage.max_usage_percent
        removed = 0
        while self.disk_usage(self.root) > limit:
            batch = self.index.oldest(limit=100)
            if not batch:
                log.error("disk is %.1f%% full but there are no recordings left to delete",
                          self.disk_usage(self.root))
                break
            removed += self._delete(batch)
        return removed

    def clean_failed(self, now: float, max_age_s: float = 86400) -> int:
        failed = self.root / ".cache" / FAILED_DIR
        removed = 0
        if failed.exists():
            for p in failed.rglob("*.mp4"):
                if now - p.stat().st_mtime > max_age_s:
                    p.unlink(missing_ok=True)
                    removed += 1
        return removed

    def prune_events(self, now: float) -> int:
        """Events, their snapshots, alert pictures and kept clips follow storage.event_retention_days."""
        if self.data_dir is None or self.db_path is None:
            return 0
        from ..alerts_db import AlertStore
        from ..events_db import EventStore

        events, alerts = EventStore(self.db_path), AlertStore(self.db_path)
        cutoff = now - self.cfg.storage.event_retention_days * 86400
        removed = 0
        try:
            while batch := events.delete_older_than(cutoff):
                from ..playback import archived_clip_path
                for e in batch:
                    if e.snapshot:
                        (self.data_dir / e.snapshot).unlink(missing_ok=True)
                    archived_clip_path(self.data_dir, e.camera, e.id).unlink(missing_ok=True)
                for img in alerts.delete_for_events([e.id for e in batch]):
                    (self.data_dir / img).unlink(missing_ok=True)
                removed += len(batch)
            if removed:
                import sqlite3
                db = sqlite3.connect(self.db_path, timeout=10)
                try:
                    db.execute("DELETE FROM event_feedback WHERE event_id NOT IN (SELECT id FROM events)")
                    db.commit()
                except sqlite3.OperationalError:
                    pass   # no feedback table yet
                finally:
                    db.close()
        finally:
            events.close()
            alerts.close()
        return removed

    def prune_clips(self, now: float) -> int:
        if self.data_dir is None or not (self.data_dir / "clips").exists():
            return 0
        removed = 0
        for p in (self.data_dir / "clips").rglob("*"):
            if p.is_file() and now - p.stat().st_mtime > CLIP_CACHE_SECONDS:
                p.unlink(missing_ok=True)
                removed += 1
        return removed

    def _delete(self, segments: list[Segment]) -> int:
        for seg in segments:
            path = self.root / seg.path
            path.unlink(missing_ok=True)
            self._prune_dirs(path.parent)
        self.index.remove([s.path for s in segments])
        return len(segments)

    def _prune_dirs(self, d: Path) -> None:
        """Remove empty hour/day folders up to (not including) the camera folder."""
        for _ in range(2):  # <HH> then <YYYY-MM-DD>
            try:
                d.rmdir()
            except OSError:
                return  # not empty (or already gone)
            d = d.parent
