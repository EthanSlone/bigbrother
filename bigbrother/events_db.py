"""Event storage shared by the detector (writes) and, later, the API/app (reads)."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY,
    camera        TEXT NOT NULL,
    label         TEXT NOT NULL,
    start_ts      REAL NOT NULL,
    end_ts        REAL,              -- NULL while the object is still in view
    top_score     REAL NOT NULL,
    median_score  REAL NOT NULL,
    box           TEXT NOT NULL,     -- JSON [x1,y1,x2,y2] of the best snapshot
    frame_w       INTEGER NOT NULL,
    frame_h       INTEGER NOT NULL,
    stationary    INTEGER NOT NULL DEFAULT 0,
    snapshot      TEXT,              -- path relative to the data dir
    updated_ts    REAL NOT NULL,
    zones         TEXT NOT NULL DEFAULT '[]', -- JSON list of zones entered, in order
    night         INTEGER NOT NULL DEFAULT 0  -- seen in infrared (black-and-white) footage
);
CREATE INDEX IF NOT EXISTS idx_events_camera_start ON events(camera, start_ts);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts);
"""


@dataclass
class Event:
    id: str
    camera: str
    label: str
    start_ts: float
    end_ts: float | None
    top_score: float
    median_score: float
    box: list[float]
    frame_w: int
    frame_h: int
    stationary: bool
    snapshot: str | None
    updated_ts: float
    zones: list[str] = field(default_factory=list)
    night: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["box"] = [round(v, 1) for v in self.box]
        d["top_score"] = round(self.top_score, 3)
        d["median_score"] = round(self.median_score, 3)
        return d


class EventStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
        if "zones" not in cols:  # databases created before zones existed
            self._db.execute("ALTER TABLE events ADD COLUMN zones TEXT NOT NULL DEFAULT '[]'")
        if "night" not in cols:
            self._db.execute("ALTER TABLE events ADD COLUMN night INTEGER NOT NULL DEFAULT 0")

    def save(self, e: Event) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO events (id, camera, label, start_ts, end_ts, top_score, "
                "median_score, box, frame_w, frame_h, stationary, snapshot, updated_ts, zones, night) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (e.id, e.camera, e.label, e.start_ts, e.end_ts, e.top_score, e.median_score,
                 json.dumps(e.box), e.frame_w, e.frame_h, int(e.stationary), e.snapshot, e.updated_ts,
                 json.dumps(e.zones), int(e.night)),
            )

    def close_open(self, now: float) -> int:
        """End events left open by a crash or restart."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE events SET end_ts = updated_ts WHERE end_ts IS NULL")
            return cur.rowcount

    def recent(self, limit: int = 50, camera: str | None = None) -> list[Event]:
        q = "SELECT * FROM events"
        args: list = []
        if camera:
            q += " WHERE camera = ?"
            args.append(camera)
        q += " ORDER BY start_ts DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            cur = self._db.execute(q, args)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        return [self._row(dict(zip(cols, r))) for r in rows]

    def get(self, event_id: str) -> Event | None:
        with self._lock:
            cur = self._db.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
        return self._row(dict(zip(cols, row))) if row else None

    @staticmethod
    def _row(r: dict) -> Event:
        r["box"] = json.loads(r["box"])
        r["zones"] = json.loads(r.get("zones") or "[]")
        r["stationary"] = bool(r["stationary"])
        r["night"] = bool(r.get("night", 0))
        return Event(**r)

    def delete_older_than(self, cutoff_ts: float, limit: int = 1000) -> list[Event]:
        """Remove ended events that started before cutoff; returns them (to delete their files)."""
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM events WHERE end_ts IS NOT NULL AND start_ts < ? ORDER BY start_ts LIMIT ?",
                (cutoff_ts, limit))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            self._db.executemany("DELETE FROM events WHERE id = ?", [(r["id"],) for r in rows])
        return [self._row(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._db.close()
