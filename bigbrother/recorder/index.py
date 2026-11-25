"""SQLite index of recorded segments, used for retention now and playback later."""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id          INTEGER PRIMARY KEY,
    camera      TEXT    NOT NULL,
    start_ts    REAL    NOT NULL,   -- unix seconds, UTC
    end_ts      REAL    NOT NULL,
    path        TEXT    NOT NULL UNIQUE,  -- relative to the recordings root
    size_bytes  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recordings_camera_start ON recordings(camera, start_ts);
CREATE INDEX IF NOT EXISTS idx_recordings_start ON recordings(start_ts);
"""


@dataclass(frozen=True)
class Segment:
    camera: str
    start_ts: float
    end_ts: float
    path: str
    size_bytes: int


class RecordingIndex:
    """Thread-safe wrapper; one connection shared behind a lock (low write volume)."""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def add(self, seg: Segment) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO recordings(camera, start_ts, end_ts, path, size_bytes) "
                "VALUES (?, ?, ?, ?, ?)",
                (seg.camera, seg.start_ts, seg.end_ts, seg.path, seg.size_bytes),
            )

    def older_than(self, camera: str, cutoff_ts: float, limit: int = 1000) -> list[Segment]:
        with self._lock:
            rows = self._db.execute(
                "SELECT camera, start_ts, end_ts, path, size_bytes FROM recordings "
                "WHERE camera = ? AND end_ts < ? ORDER BY start_ts LIMIT ?",
                (camera, cutoff_ts, limit),
            ).fetchall()
        return [Segment(*r) for r in rows]

    def oldest(self, limit: int) -> list[Segment]:
        with self._lock:
            rows = self._db.execute(
                "SELECT camera, start_ts, end_ts, path, size_bytes FROM recordings "
                "ORDER BY start_ts LIMIT ?",
                (limit,),
            ).fetchall()
        return [Segment(*r) for r in rows]

    def remove(self, paths: list[str]) -> None:
        if not paths:
            return
        with self._lock:
            self._db.executemany("DELETE FROM recordings WHERE path = ?", [(p,) for p in paths])

    def count(self, camera: str | None = None) -> int:
        with self._lock:
            if camera:
                return self._db.execute(
                    "SELECT COUNT(*) FROM recordings WHERE camera = ?", (camera,)
                ).fetchone()[0]
            return self._db.execute("SELECT COUNT(*) FROM recordings").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._db.close()
