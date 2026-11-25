"""Alert decisions (one row per event) and small shared settings (armed/disarmed).

Every event gets a recorded outcome, so "why didn't I get an alert?" always
has an answer: sent, logged (no notifier configured), failed, or suppressed
with the reason.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    event_id     TEXT PRIMARY KEY,
    camera       TEXT NOT NULL,
    label        TEXT NOT NULL,
    status       TEXT NOT NULL,   -- pending | sent | logged | failed | suppressed
    reason       TEXT NOT NULL DEFAULT '',
    verify_score REAL,
    sent_ts      REAL,
    image        TEXT,            -- path relative to the data dir: the picture that was sent
    updated_ts   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_camera_sent ON alerts(camera, sent_ts);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class AlertRecord:
    event_id: str
    camera: str
    label: str
    status: str
    reason: str = ""
    verify_score: float | None = None
    sent_ts: float | None = None
    image: str | None = None
    updated_ts: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


class AlertStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def save(self, r: AlertRecord) -> None:
        r.updated_ts = time.time()
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO alerts (event_id, camera, label, status, reason, verify_score, "
                "sent_ts, image, updated_ts) VALUES (?,?,?,?,?,?,?,?,?)",
                (r.event_id, r.camera, r.label, r.status, r.reason, r.verify_score, r.sent_ts, r.image,
                 r.updated_ts),
            )

    def get(self, event_id: str) -> AlertRecord | None:
        got = self.for_events([event_id])
        return got.get(event_id)

    def for_events(self, event_ids: list[str]) -> dict[str, AlertRecord]:
        if not event_ids:
            return {}
        marks = ",".join("?" * len(event_ids))
        with self._lock:
            cur = self._db.execute(f"SELECT * FROM alerts WHERE event_id IN ({marks})", event_ids)
            cols = [c[0] for c in cur.description]
            rows = cur.fetchall()
        return {r[0]: AlertRecord(**dict(zip(cols, r))) for r in rows}

    def last_sent(self, camera: str, label: str) -> float | None:
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(sent_ts) FROM alerts WHERE camera = ? AND label = ? AND status IN ('sent','logged')",
                (camera, label)).fetchone()
        return row[0] if row else None

    # -- armed state -------------------------------------------------------
    def armed(self) -> bool:
        with self._lock:
            row = self._db.execute("SELECT value FROM settings WHERE key = 'armed'").fetchone()
        return row is None or row[0] == "1"  # armed unless explicitly disarmed

    def set_armed(self, on: bool) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('armed', ?)",
                             ("1" if on else "0",))

    def close(self) -> None:
        with self._lock:
            self._db.close()
