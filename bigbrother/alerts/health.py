"""System health alerts: tell the owner when the NVR itself stops working.

Checked every 30 s:
  - each recording camera has a fresh segment in the recordings index (ground truth:
    what's actually on disk, not what a service says about itself)
  - each detecting camera's detector worker is running
  - recorder and detector are online (MQTT last will), go2rtc answers
  - the recordings disk isn't fuller than retention should allow

A problem must last `after` seconds before it's reported; then it's reported once
(with a reminder every 12 h) and an all-clear follows when it's fixed. Health
alerts go out even when intrusion alerts are switched off.
"""

from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

CHECK_EVERY = 30.0
REMIND_EVERY = 12 * 3600


@dataclass
class _Problem:
    message: str
    since: float
    notified_at: float | None = None


@dataclass
class HealthMonitor:
    """Pure decision logic: feed it the current problems, get back what to announce."""
    after: float = 300.0
    clock: Callable[[], float] = time.time
    problems: dict[str, _Problem] = field(default_factory=dict)

    def update(self, current: dict[str, str]) -> tuple[list[str], list[str]]:
        """current: key -> human message. Returns (new or reminder messages, recovered messages)."""
        now = self.clock()
        announce, recovered = [], []
        for key, msg in current.items():
            p = self.problems.get(key)
            if p is None:
                p = self.problems[key] = _Problem(msg, now)
            p.message = msg
            due = p.notified_at is None and now - p.since >= self.after
            remind = p.notified_at is not None and now - p.notified_at >= REMIND_EVERY
            if due or remind:
                p.notified_at = now
                announce.append(msg)
        for key in [k for k in self.problems if k not in current]:
            p = self.problems.pop(key)
            if p.notified_at is not None:
                recovered.append(p.message)
        return announce, recovered

    def snapshot(self) -> list[dict]:
        return [{"key": k, "message": p.message, "since": p.since, "notified": p.notified_at is not None}
                for k, p in sorted(self.problems.items(), key=lambda kv: kv[1].since)]


def _http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310 - internal service URL
        return json.loads(r.read())


def _ok(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:  # noqa: S310
            return r.status < 500
    except Exception:  # noqa: BLE001
        return False


def latest_segments(db_path: Path) -> dict[str, float]:
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            return dict(db.execute("SELECT camera, MAX(end_ts) FROM recordings GROUP BY camera").fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        return {}


def find_problems(cfg, *, now: float, started: float, recorder_state: dict[str, dict],
                  available: dict[str, str], latest: dict[str, float], detector: dict[str, dict] | None,
                  go2rtc_ok: bool, disk: tuple[int, int] | None) -> dict[str, str]:
    """All current problems as key -> message. Pure: every input is passed in."""
    out: dict[str, str] = {}
    if available.get("recorder") == "offline":
        out["svc:recorder"] = "The recorder service is offline: nothing is being recorded."
    if available.get("detector") == "offline" or detector is None:
        out["svc:detector"] = "The detector service is offline: nothing is being detected."
    if not go2rtc_ok:
        out["svc:go2rtc"] = "The stream hub (go2rtc) isn't answering: live view, recording and detection depend on it."
    for cam in cfg.cameras.values():
        if cam.record.enabled and "svc:recorder" not in out:
            last = latest.get(cam.id)
            stale_after = cam.record.segment_seconds * 2 + 120
            ref = last if last is not None else started
            if now - ref > stale_after:
                detail = (recorder_state.get(cam.id) or {}).get("detail") or ""
                ago = int((now - ref) // 60)
                why = f" Last error: {detail}" if detail else ""
                out[f"rec:{cam.id}"] = (f"{cam.name} isn't recording (nothing saved for {ago} min).{why}"
                                        if last is not None else f"{cam.name} hasn't recorded anything yet.{why}")
        if cam.detect.enabled and detector is not None:
            st = detector.get(cam.id)
            if st is not None and st.get("state") not in ("running", "disabled"):
                err = st.get("error") or st.get("state")
                out[f"det:{cam.id}"] = f"Detection stopped on {cam.name}: {err}."
    if disk is not None:
        used, total = disk
        pct = used / total * 100 if total else 0
        free_gb = (total - used) / 1e9
        if pct > cfg.storage.max_usage_percent + 5 or free_gb < 10:
            out["disk"] = (f"The recordings disk is {pct:.0f}% full ({free_gb:.0f} GB free). Retention should keep it "
                           f"under {cfg.storage.max_usage_percent:g}%; check the recorder.")
    return out


class HealthWatcher:
    """Gathers the inputs, runs the monitor, sends notifications, publishes state."""

    def __init__(self, cfg, db_path: Path, recordings_root: Path, notifier, publish, detector_url: str):
        self.cfg, self.db_path, self.recordings_root = cfg, db_path, recordings_root
        self.notifier, self.publish, self.detector_url = notifier, publish, detector_url.rstrip("/")
        self.monitor = HealthMonitor(after=cfg.alerts.health_after_minutes * 60)
        self.started = time.time()
        self.recorder_state: dict[str, dict] = {}
        self.available: dict[str, str] = {}
        self._next = 0.0

    # MQTT callbacks
    def on_recorder_state(self, topic: str, payload: bytes) -> None:
        cam = topic.split("/")[-2]
        try:
            self.recorder_state[cam] = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    def on_available(self, topic: str, payload: bytes) -> None:
        self.available[topic.split("/")[-2]] = payload.decode(errors="ignore").strip()

    def tick(self) -> None:
        now = time.time()
        if now < self._next:
            return
        self._next = now + CHECK_EVERY
        try:
            stats = _http_json(f"{self.detector_url}/api/stats")
            detector = {s["camera"]: s for s in stats}
        except Exception:  # noqa: BLE001
            detector = None
        try:
            du = shutil.disk_usage(self.recordings_root)
            disk = (du.used, du.total)
        except OSError:
            disk = None
        current = find_problems(
            self.cfg, now=now, started=self.started, recorder_state=self.recorder_state,
            available=self.available, latest=latest_segments(self.db_path), detector=detector,
            go2rtc_ok=_ok(f"{self.cfg.go2rtc_api}/api"), disk=disk)
        announce, recovered = self.monitor.update(current)
        self.publish("health", {"problems": self.monitor.snapshot(), "ts": now}, True)
        if not self.cfg.alerts.health_enabled:
            return
        from .notify import Message
        if announce:
            title = "BigBrother needs attention" if len(announce) > 1 else announce[0].split(":")[0].split(" (")[0]
            self._send(Message(title=title[:100], body="\n".join(announce)))
        if recovered:
            self._send(Message(title="BigBrother is back to normal",
                               body="Fixed: " + "\n".join(m.split(":")[0].split(" (")[0] for m in recovered)))

    def _send(self, msg) -> None:
        try:
            res = (self.notifier.send(msg, health=True) if hasattr(self.notifier, "targets")
                   else self.notifier.send(msg))
            log.warning("health alert %s: %s | %s", res.status, msg.title, msg.body.replace("\n", " / "))
        except Exception:  # noqa: BLE001
            log.exception("couldn't send health alert")
