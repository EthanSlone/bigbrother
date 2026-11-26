"""Who gets which alerts: each person's Pushover key and choices, kept with their account.

The web app writes these (Settings -> Your account -> Phone alerts; the app token
under Settings -> Alerts). The alerter only reads them, a few seconds stale at most.
Values in .env (PUSHOVER_TOKEN, PUSHOVER_USER) still work: the token is the fallback
when none is set in the app, and PUSHOVER_USER is one more recipient for everything.

Per-person preferences (users.prefs JSON in web.db):
    pushover_user   their Pushover user (or group) key
    alert_cameras   list of camera ids, or absent for all cameras
    alert_health    false to skip "BigBrother needs attention" messages
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
CACHE_SECONDS = 10.0


@dataclass(frozen=True)
class Recipient:
    name: str                       # username, or ".env" for PUSHOVER_USER
    key: str
    cameras: frozenset[str] | None  # None: every camera
    health: bool = True

    def wants(self, camera: str | None, health: bool) -> bool:
        if health:
            return self.health
        return self.cameras is None or camera is None or camera in self.cameras


def _connect(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    db.execute(SCHEMA)
    return db


def get_setting(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    db = _connect(path)
    try:
        row = db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    finally:
        db.close()


def set_setting(path: Path, key: str, value: str | None) -> None:
    db = _connect(path)
    try:
        if value:
            db.execute("INSERT OR REPLACE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
        else:
            db.execute("DELETE FROM app_settings WHERE key = ?", (key,))
    finally:
        db.close()


class Directory:
    """Pushover token and recipients, from web.db with .env as the fallback."""

    def __init__(self, web_db: Path | None, env: dict | None = None, clock=time.monotonic):
        self.web_db, self.env, self.clock = web_db, env if env is not None else os.environ, clock
        self._lock = threading.Lock()
        self._cached: tuple[float, str, list[Recipient]] | None = None

    def token_source(self) -> str | None:
        if self.web_db and get_setting(self.web_db, "pushover_token"):
            return "app"
        return "env" if self.env.get("PUSHOVER_TOKEN") else None

    def load(self) -> tuple[str, list[Recipient]]:
        with self._lock:
            now = self.clock()
            if self._cached and now - self._cached[0] < CACHE_SECONDS:
                return self._cached[1], self._cached[2]
            token, people = self._read()
            self._cached = (now, token, people)
            return token, people

    def forget(self) -> None:
        with self._lock:
            self._cached = None

    def _read(self) -> tuple[str, list[Recipient]]:
        token, people = "", []
        if self.web_db and self.web_db.exists():
            db = _connect(self.web_db)
            try:
                row = db.execute("SELECT value FROM app_settings WHERE key = 'pushover_token'").fetchone()
                token = row[0] if row else ""
                try:
                    rows = db.execute("SELECT username, prefs FROM users").fetchall()
                except sqlite3.OperationalError:  # accounts not set up yet
                    rows = []
            finally:
                db.close()
            for name, raw in rows:
                try:
                    p = json.loads(raw or "{}")
                except json.JSONDecodeError:
                    continue
                key = str(p.get("pushover_user") or "").strip()
                if not key:
                    continue
                cams = p.get("alert_cameras")
                people.append(Recipient(name, key, None if cams is None else frozenset(cams),
                                        p.get("alert_health", True) is not False))
        token = token or self.env.get("PUSHOVER_TOKEN", "")
        env_user = self.env.get("PUSHOVER_USER", "").strip()
        if env_user and all(r.key != env_user for r in people):
            people.append(Recipient(".env", env_user, None, True))
        return token, people
