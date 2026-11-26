"""Removing an event everywhere: its row, snapshot, kept clip, alert picture and review."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def remove_files(db_path: Path, data_dir: Path, events: list) -> None:
    """After the event rows are gone: delete their files and the rows that point at them."""
    from .alerts_db import AlertStore
    from .playback import archived_clip_path

    if not events:
        return
    for e in events:
        if e.snapshot:
            (data_dir / e.snapshot).unlink(missing_ok=True)
        archived_clip_path(data_dir, e.camera, e.id).unlink(missing_ok=True)
    alerts = AlertStore(db_path)
    try:
        for img in alerts.delete_for_events([e.id for e in events]):
            (data_dir / img).unlink(missing_ok=True)
    finally:
        alerts.close()
    db = sqlite3.connect(db_path, timeout=10)
    try:
        db.execute("DELETE FROM event_feedback WHERE event_id NOT IN (SELECT id FROM events)")
        db.commit()
    except sqlite3.OperationalError:
        pass   # no feedback table yet
    finally:
        db.close()
