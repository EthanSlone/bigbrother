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


class Retention:
    def __init__(self, cfg: Config, root: Path, index: RecordingIndex,
                 disk_usage: DiskUsage = disk_used_percent):
        self.cfg = cfg
        self.root = root
        self.index = index
        self.disk_usage = disk_usage

    def run_once(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        stats = {"age": self.enforce_age(now), "disk": self.enforce_disk(), "failed": self.clean_failed(now)}
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
