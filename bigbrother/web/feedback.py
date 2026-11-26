"""Detection feedback from the owner, and the test-set export built from it.

- Per event: correct / false alarm / wrong object (with the right one).
- Missed detections: reported from the Recordings page at a moment in time;
  a short clip is saved right away so it outlives the 24/7 retention.
- Export: a zip with every marked event and reported miss (plus optionally a
  sample of unmarked events), small enough to attach to a chat, used to measure
  detection changes against the owner's own cameras.
"""

from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import subprocess
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

VERDICTS = ("correct", "false_alarm", "wrong_label")
MISS_SECONDS = 10            # a reported miss keeps 10 s before and after the moment
EXPORT_MAX_EVENTS = 300
EXPORT_CLIP_SECONDS = 20     # per event, from 3 s before the start
EXPORT_WIDTH, EXPORT_FPS = 960, 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_feedback (
    event_id    TEXT PRIMARY KEY,
    verdict     TEXT NOT NULL,
    label       TEXT,            -- the right object, for wrong_label
    note        TEXT NOT NULL DEFAULT '',
    user        TEXT NOT NULL DEFAULT '',
    ts          REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS missed (
    id          TEXT PRIMARY KEY,
    camera      TEXT NOT NULL,
    ts          REAL NOT NULL,   -- the moment the object was visible
    label       TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    clip        TEXT,            -- path under the data dir
    user        TEXT NOT NULL DEFAULT '',
    created_ts  REAL NOT NULL
);
"""


class FeedbackStore:
    def __init__(self, db_path: Path):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None, timeout=10)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)

    def set(self, event_id: str, verdict: str, label: str | None, note: str, user: str) -> None:
        if verdict not in VERDICTS:
            raise ValueError("verdict must be correct, false_alarm or wrong_label")
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO event_feedback VALUES (?,?,?,?,?,?)",
                             (event_id, verdict, label if verdict == "wrong_label" else None, note[:500], user,
                              time.time()))

    def clear(self, event_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM event_feedback WHERE event_id = ?", (event_id,))

    def for_events(self, ids: list[str]) -> dict[str, dict]:
        if not ids:
            return {}
        with self._lock:
            rows = self._db.execute(
                f"SELECT event_id, verdict, label, note FROM event_feedback WHERE event_id IN "
                f"({','.join('?' * len(ids))})", ids).fetchall()
        return {r[0]: {"verdict": r[1], "label": r[2], "note": r[3]} for r in rows}

    def all(self) -> dict[str, dict]:
        with self._lock:
            rows = self._db.execute("SELECT event_id, verdict, label, note, ts FROM event_feedback").fetchall()
        return {r[0]: {"verdict": r[1], "label": r[2], "note": r[3], "ts": r[4]} for r in rows}

    def add_miss(self, camera: str, ts: float, label: str, note: str, user: str) -> str:
        mid = f"miss-{int(ts)}-{camera}-{secrets.token_hex(2)}"
        with self._lock:
            self._db.execute("INSERT INTO missed (id, camera, ts, label, note, user, created_ts) VALUES (?,?,?,?,?,?,?)",
                             (mid, camera, ts, label, note[:500], user, time.time()))
        return mid

    def set_miss_clip(self, mid: str, rel: str) -> None:
        with self._lock:
            self._db.execute("UPDATE missed SET clip = ? WHERE id = ?", (rel, mid))

    def misses(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT id, camera, ts, label, note, clip FROM missed ORDER BY ts DESC").fetchall()
        return [{"id": r[0], "camera": r[1], "ts": r[2], "label": r[3], "note": r[4], "has_clip": bool(r[5]),
                 "clip": r[5]} for r in rows]

    def delete_miss(self, mid: str) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT clip FROM missed WHERE id = ?", (mid,)).fetchone()
            self._db.execute("DELETE FROM missed WHERE id = ?", (mid,))
        return row[0] if row else None

    def counts(self) -> dict:
        with self._lock:
            fb = dict(self._db.execute("SELECT verdict, COUNT(*) FROM event_feedback GROUP BY verdict").fetchall())
            misses = self._db.execute("SELECT COUNT(*) FROM missed").fetchone()[0]
        return {"correct": fb.get("correct", 0), "false_alarm": fb.get("false_alarm", 0),
                "wrong_label": fb.get("wrong_label", 0), "missed": misses}


def _shrink(src: Path, dst: Path, start: float = 0.0, seconds: float | None = None) -> bool:
    """Re-encode a clip small: 960 px wide, 5 fps, H.264 CRF 28, no audio."""
    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.2f}"]
    cmd += ["-i", str(src)]
    if seconds:
        cmd += ["-t", f"{seconds:.2f}"]
    cmd += ["-vf", f"fps={EXPORT_FPS},scale='min({EXPORT_WIDTH},iw)':-2", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-crf", "28", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return res.returncode == 0 and dst.exists()


@dataclass
class ExportJob:
    id: str
    state: str = "running"         # running | done | failed
    done: int = 0
    total: int = 0
    path: Path | None = None
    error: str = ""
    size: int = 0
    created: float = field(default_factory=time.time)


def build_testset(job: ExportJob, *, cfg, queries, feedback: FeedbackStore, playback, data_dir: Path,
                  include_unreviewed: int, out_dir: Path) -> None:
    """Write the zip. Runs in a thread; updates `job` as it goes."""
    try:
        fb = feedback.all()
        marked = [e for e in (queries.event(eid) for eid in fb) if e]
        marked_ids = {e["id"] for e in marked}
        extra = []
        if include_unreviewed:
            for e in queries.events(limit=min(include_unreviewed * 3, 1000)):
                if e["id"] not in marked_ids and e["end_ts"]:
                    extra.append(e)
                if len(extra) >= include_unreviewed:
                    break
        events = (sorted(marked, key=lambda e: -e["start_ts"]) + extra)[:EXPORT_MAX_EVENTS]
        misses = feedback.misses()
        job.total = len(events) + len(misses)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M")
        zpath = out_dir / f"bigbrother-testset-{stamp}.zip"
        manifest = {
            "version": 1, "created": time.time(),
            "detector": {"model": cfg.detector.model, "input_size": cfg.detector.input_size},
            "alerts": {"verify": cfg.alerts.verify, "verify_model": cfg.alerts.verify_model,
                       "verify_threshold": cfg.alerts.verify_threshold},
            "cameras": {c.id: {
                "name": c.name,
                "detect": {"enabled": c.detect.enabled, "fps": c.detect.fps, "min_score": c.detect.min_score,
                           "labels": list(c.detect.labels)},
                "zones": {n: {"points": [list(p) for p in z.points], "objects": list(z.objects or [])}
                          for n, z in c.zones.items()},
                "motion_masks": [[list(p) for p in m] for m in c.motion_masks],
                "object_masks": [[list(p) for p in m] for m in c.object_masks],
                "alert_labels": list(c.alert.labels)} for c in cfg.cameras.values()},
            "events": [], "missed": [],
        }
        with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
            tmp = Path(tmp)
            for e in events:
                rec = {k: e[k] for k in ("id", "camera", "label", "start_ts", "end_ts", "top_score", "median_score",
                                         "box", "frame_w", "frame_h", "stationary", "zones", "night")}
                rec["alert"] = e["alert"]
                rec["feedback"] = fb.get(e["id"])
                if e.get("snapshot") and (data_dir / e["snapshot"]).is_file():
                    z.write(data_dir / e["snapshot"], f"events/{e['id']}/snapshot.jpg")
                    rec["snapshot"] = f"events/{e['id']}/snapshot.jpg"
                if e.get("alert_image") and (data_dir / e["alert_image"]).is_file():
                    z.write(data_dir / e["alert_image"], f"events/{e['id']}/alert.jpg")
                    rec["alert_image"] = f"events/{e['id']}/alert.jpg"
                try:
                    got = playback.event_clip(e["id"])
                except RuntimeError:
                    got = None
                if got:
                    small = tmp / f"{e['id']}.mp4"
                    if _shrink(got[0], small, seconds=EXPORT_CLIP_SECONDS):
                        z.write(small, f"events/{e['id']}/clip.mp4")
                        rec["clip"] = f"events/{e['id']}/clip.mp4"
                        rec["clip_start_ts"] = e["start_ts"] - 5.0 if not got[1].get("start") else got[1]["start"]
                manifest["events"].append(rec)
                job.done += 1
            for m in misses:
                rec = {k: m[k] for k in ("id", "camera", "ts", "label", "note")}
                if m["clip"] and (data_dir / m["clip"]).is_file():
                    small = tmp / f"{m['id']}.mp4"
                    if _shrink(data_dir / m["clip"], small):
                        z.write(small, f"missed/{m['id']}.mp4")
                        rec["clip"] = f"missed/{m['id']}.mp4"
                        rec["clip_start_ts"] = m["ts"] - MISS_SECONDS
                manifest["missed"].append(rec)
                job.done += 1
            z.writestr("manifest.json", json.dumps(manifest, indent=1))
            z.writestr("README.txt", "BigBrother detection test set.\n"
                       "manifest.json lists every event (with your verdict, if you gave one) and every missed\n"
                       "detection you reported. Clips are shrunk to 960 px wide at 5 fps.\n"
                       "No passwords or stream addresses are included.\n")
        job.path, job.size, job.state = zpath, zpath.stat().st_size, "done"
        log.info("test set export: %d events, %d misses, %.1f MB", len(events), len(misses), job.size / 1e6)
    except Exception as exc:  # noqa: BLE001
        log.exception("test set export failed")
        job.state, job.error = "failed", str(exc)[:300]
