"""Per-camera ffmpeg process: copy the main stream to 1-minute MP4 segments.

ffmpeg reads the go2rtc restream (so each camera has only one connection),
copies video without re-encoding, and writes UTC-named segments into a
per-camera cache directory. The organizer then moves finished segments into
the dated folder tree and indexes them.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable

from ..config import Camera

log = logging.getLogger(__name__)

SEGMENT_PATTERN = "%Y%m%dT%H%M%SZ.mp4"  # strftime pattern; parsed back by segments.py
RTSP_IN = ["-rtsp_transport", "tcp", "-timeout", "10000000"]

LOG_BURST = 5  # ffmpeg stderr lines logged per camera per minute

StateCallback = Callable[[str, str, str], None]  # (camera_id, state, detail)


def probe_input(url: str, timeout: float = 20) -> dict:
    """Return {'video': codec_name|None, 'audio': codec_name|None}."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", *RTSP_IN, "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "ffprobe failed")
    info = {"video": None, "audio": None}
    for s in json.loads(out.stdout or "{}").get("streams", []):
        kind = s.get("codec_type")
        if kind in info and info[kind] is None:
            info[kind] = s.get("codec_name")
    if info["video"] is None:
        raise RuntimeError("stream has no video track")
    return info


def build_record_cmd(
    input_url: str,
    cache_dir: Path,
    segment_seconds: int,
    video_codec: str | None,
    record_audio: bool,
    has_audio: bool,
) -> list[str]:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-avoid_negative_ts", "make_zero", "-fflags", "+genpts+discardcorrupt",
        *RTSP_IN,
        # Stamp frames with the wall clock: cameras reboot, go2rtc reconnects and HLS
        # feeds wrap, and any of those can make source timestamps jump backwards.
        "-use_wallclock_as_timestamps", "1",
        "-i", input_url,
        "-map", "0:v:0", "-c:v", "copy",
    ]
    if video_codec == "hevc":
        cmd += ["-tag:v", "hvc1"]  # required for H.265 playback on iPhone/Safari
    if record_audio and has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "32k"]
    cmd += [
        "-f", "segment",
        "-segment_time", str(segment_seconds),
        "-segment_atclocktime", "1",
        "-segment_format", "mp4",
        "-segment_format_options", "movflags=+faststart",
        "-reset_timestamps", "1",
        "-strftime", "1",
        str(cache_dir / SEGMENT_PATTERN),
    ]
    return cmd


class CameraRecorder(threading.Thread):
    """Keeps one ffmpeg recording process alive for a camera."""

    def __init__(
        self,
        camera: Camera,
        input_url: str,
        cache_dir: Path,
        on_state: StateCallback | None = None,
        stall_seconds: float | None = None,
    ):
        super().__init__(name=f"rec-{camera.id}", daemon=True)
        self.camera = camera
        self.input_url = input_url
        self.cache_dir = cache_dir
        self.on_state = on_state or (lambda *_: None)
        seg = camera.record.segment_seconds
        self.stall_seconds = stall_seconds or max(60.0, seg * 1.5)
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=30)
        self.restarts = 0

    # -- lifecycle -------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()
        self._terminate()

    def run(self) -> None:
        backoff = 2.0
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                info = probe_input(self.input_url)
            except Exception as exc:  # noqa: BLE001 - any failure means retry later
                detail = str(exc)
                if "404" in detail:
                    detail = ("go2rtc could not open the camera (wrong address, password or path). "
                              "Run `docker compose run --rm tools probe` to see why.")
                self._set_state("error", f"stream unavailable: {detail} (retry in {backoff:.0f}s)")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            cmd = build_record_cmd(
                self.input_url, self.cache_dir, self.camera.record.segment_seconds,
                info["video"], self.camera.record.audio, info["audio"] is not None,
            )
            log.info("[%s] starting ffmpeg (%s%s)", self.camera.id, info["video"],
                     f" + {info['audio']}" if info["audio"] else "")
            env = {**os.environ, "TZ": "UTC"}  # segment names are UTC
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, text=True, env=env,
            )
            threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()
            self._set_state("recording", info["video"] or "")

            reason = self._watch(self._proc)
            if self._stop.is_set():
                break
            self.restarts += 1
            tail = " | ".join(list(self._stderr_tail)[-3:])
            self._set_state("error", f"{reason}{': ' + tail if tail else ''}")
            # Reset backoff if the process ran for a healthy while.
            backoff = 2.0 if time.monotonic() - started > 300 else min(backoff * 2, 60)
            self._stop.wait(backoff)

        self._set_state("stopped", "")

    # -- internals -------------------------------------------------------
    def _watch(self, proc: subprocess.Popen) -> str:
        """Block until ffmpeg exits or stalls; return a human-readable reason."""
        last_sig = None
        last_change = time.monotonic()
        while not self._stop.is_set():
            if proc.poll() is not None:
                return f"ffmpeg exited with code {proc.returncode}"
            sig = self._newest_signature()
            if sig != last_sig:
                last_sig, last_change = sig, time.monotonic()
            elif time.monotonic() - last_change > self.stall_seconds:
                log.warning("[%s] no new data for %.0fs, restarting", self.camera.id, self.stall_seconds)
                self._terminate()
                return "stalled (no data written)"
            self._stop.wait(2)
        return "stopping"

    def _newest_signature(self) -> tuple[str, int] | None:
        try:
            files = [p for p in self.cache_dir.iterdir() if p.suffix == ".mp4"]
        except FileNotFoundError:
            return None
        if not files:
            return None
        newest = max(files, key=lambda p: p.name)
        try:
            return newest.name, newest.stat().st_size
        except FileNotFoundError:
            return None

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        """Keep the last lines for error reports; log at most LOG_BURST lines per minute."""
        assert proc.stderr is not None
        window_start, logged, suppressed = time.monotonic(), 0, 0
        for line in proc.stderr:
            line = line.rstrip()
            if not line:
                continue
            self._stderr_tail.append(line)
            now = time.monotonic()
            if now - window_start > 60:
                if suppressed:
                    log.warning("[%s] ffmpeg: (%d similar messages suppressed)", self.camera.id, suppressed)
                window_start, logged, suppressed = now, 0, 0
            if logged < LOG_BURST:
                log.warning("[%s] ffmpeg: %s", self.camera.id, line)
                logged += 1
            else:
                suppressed += 1

    def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.send_signal(signal.SIGINT)  # lets ffmpeg finish the current segment
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        except ProcessLookupError:
            pass

    def _set_state(self, state: str, detail: str) -> None:
        log.log(logging.WARNING if state == "error" else logging.INFO,
                "[%s] %s %s", self.camera.id, state, detail)
        try:
            self.on_state(self.camera.id, state, detail)
        except Exception:  # noqa: BLE001 - status reporting must never kill recording
            log.exception("[%s] state callback failed", self.camera.id)
