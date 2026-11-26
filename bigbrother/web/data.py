"""Read-side queries for the app: events joined with their alert outcome."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

ALERT_STATUSES = ("sent", "logged", "failed", "suppressed", "pending")


class EventQueries:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        # The detector and alerter create these tables; create them too so a fresh install's
        # app works before either has run.
        from ..alerts_db import SCHEMA as ALERTS_SCHEMA
        from ..events_db import SCHEMA as EVENTS_SCHEMA
        from .feedback import SCHEMA as FEEDBACK_SCHEMA
        self._db.executescript(EVENTS_SCHEMA)
        self._db.executescript(ALERTS_SCHEMA)
        self._db.executescript(FEEDBACK_SCHEMA)
        cols = {r[1] for r in self._db.execute("PRAGMA table_info(events)")}
        if "zones" not in cols:
            self._db.execute("ALTER TABLE events ADD COLUMN zones TEXT NOT NULL DEFAULT '[]'")
        if "night" not in cols:
            self._db.execute("ALTER TABLE events ADD COLUMN night INTEGER NOT NULL DEFAULT 0")

    def events(self, *, cameras: list[str] | None = None, labels: list[str] | None = None,
               since: float | None = None, until: float | None = None, before: float | None = None,
               alert: str | None = None, zone: str | None = None, review: str | None = None,
               limit: int = 50) -> list[dict]:
        """Newest first. `before` is a start_ts cursor for paging. `alert`: an alert status,
        or 'notified' for sent+logged."""
        where, args = [], []
        if cameras:
            where.append(f"e.camera IN ({','.join('?' * len(cameras))})")
            args += cameras
        if labels:
            where.append(f"e.label IN ({','.join('?' * len(labels))})")
            args += labels
        if since is not None:
            where.append("COALESCE(e.end_ts, 1e18) >= ?")
            args.append(since)
        if until is not None:
            where.append("e.start_ts < ?")
            args.append(until)
        if before is not None:
            where.append("e.start_ts < ?")
            args.append(before)
        if alert == "notified":
            where.append("a.status IN ('sent','logged')")
        elif alert in ALERT_STATUSES:
            where.append("a.status = ?")
            args.append(alert)
        if zone:
            where.append("EXISTS (SELECT 1 FROM json_each(e.zones) WHERE value = ?)")
            args.append(zone)
        if review == "unreviewed":
            where.append("f.verdict IS NULL")
        elif review == "reviewed":
            where.append("f.verdict IS NOT NULL")
        elif review in ("correct", "false_alarm", "wrong_label"):
            where.append("f.verdict = ?")
            args.append(review)
        sql = self._SELECT
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.start_ts DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        with self._lock:
            cur = self._db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return [self._out(r) for r in rows]

    _SELECT = ("SELECT e.*, a.status AS a_status, a.reason AS a_reason, a.verify_score AS a_verify, "
               "a.sent_ts AS a_sent, a.image AS a_image, f.verdict AS f_verdict, f.label AS f_label, "
               "f.note AS f_note FROM events e LEFT JOIN alerts a ON a.event_id = e.id "
               "LEFT JOIN event_feedback f ON f.event_id = e.id")

    def event(self, event_id: str) -> dict | None:
        with self._lock:
            cur = self._db.execute(self._SELECT + " WHERE e.id = ?", (event_id,))
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
        return self._out(dict(zip(cols, row))) if row else None

    def counts_by_camera(self, since: float) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT camera, COUNT(*) FROM events WHERE start_ts >= ? GROUP BY camera",
                                    (since,)).fetchall()
        return dict(rows)

    @staticmethod
    def _out(r: dict) -> dict:
        alert = None
        if r.get("a_status"):
            alert = {"status": r["a_status"], "reason": r["a_reason"] or "",
                     "verify_score": r["a_verify"], "sent_ts": r["a_sent"], "has_image": bool(r["a_image"])}
        return {
            "id": r["id"], "camera": r["camera"], "label": r["label"],
            "start_ts": r["start_ts"], "end_ts": r["end_ts"],
            "top_score": round(r["top_score"], 3), "median_score": round(r["median_score"], 3),
            "box": [round(v, 1) for v in json.loads(r["box"])], "frame_w": r["frame_w"], "frame_h": r["frame_h"],
            "stationary": bool(r["stationary"]), "has_snapshot": bool(r["snapshot"]),
            "night": bool(r.get("night") or 0),
            "snapshot": r["snapshot"], "alert_image": r["a_image"],
            "zones": json.loads(r.get("zones") or "[]"), "updated_ts": r["updated_ts"], "alert": alert,
            "feedback": {"verdict": r["f_verdict"], "label": r["f_label"], "note": r["f_note"] or ""}
                        if r.get("f_verdict") else None,
        }

    def close(self) -> None:
        with self._lock:
            self._db.close()
