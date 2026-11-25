"""Decode a camera stream into raw frames at the detection frame rate.

ffmpeg does the decoding, frame-rate reduction and scaling; we read fixed-size
BGR frames from its stdout. A reader thread keeps only the newest frame so a
slow detector never builds up latency: it skips frames instead.
"""

from __future__ import annotations

import collections
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass

import numpy as np

from ..config import input_args

log = logging.getLogger(__name__)


@dataclass
class Frame:
    image: np.ndarray  # HxWx3 BGR
    ts: float          # wall-clock seconds when received
    seq: int


def probe_size(url: str, timeout: float = 20) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", *input_args(url), "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", url],
        capture_output=True, text=True, timeout=timeout,
    )
    if out.returncode != 0:
        msg = out.stderr.strip().splitlines()[-1] if out.stderr.strip() else "ffprobe failed"
        raise RuntimeError(msg)
    streams = json.loads(out.stdout or "{}").get("streams") or [{}]
    w, h = streams[0].get("width"), streams[0].get("height")
    if not w or not h:
        raise RuntimeError("stream has no video size")
    return int(w), int(h)


def output_size(src_w: int, src_h: int, max_w: int) -> tuple[int, int]:
    if src_w <= max_w:
        w, h = src_w, src_h
    else:
        w, h = max_w, round(src_h * max_w / src_w)
    return w - w % 2, h - h % 2  # even sizes keep ffmpeg's scaler happy


def build_decode_cmd(url: str, fps: float, size: tuple[int, int]) -> list[str]:
    w, h = size
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *input_args(url), "-i", url,
        "-an", "-vf", f"fps={fps},scale={w}:{h}",
        "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
    ]


class FrameSource:
    """Keeps an ffmpeg decoder running for one stream; `latest()` returns the newest frame."""

    def __init__(self, name: str, url: str, fps: float, max_width: int):
        self.name, self.url, self.fps, self.max_width = name, url, fps, max_width
        self.size: tuple[int, int] | None = None
        self.state = "starting"
        self.error = ""
        self.frames_in = 0
        self._recent: collections.deque[float] = collections.deque(maxlen=60)  # arrival times
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._frame: Frame | None = None
        self._proc: subprocess.Popen | None = None
        self._thread = threading.Thread(target=self._run, name=f"frames-{name}", daemon=True)
        self._stderr: collections.deque[str] = collections.deque(maxlen=5)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        with self._cond:
            self._cond.notify_all()

    def wait_frame(self, after_seq: int, timeout: float) -> Frame | None:
        """Block until a frame newer than after_seq arrives (or timeout)."""
        with self._cond:
            self._cond.wait_for(
                lambda: self._stop.is_set() or (self._frame is not None and self._frame.seq > after_seq),
                timeout=timeout,
            )
            f = self._frame
        return f if f is not None and f.seq > after_seq else None

    def measured_fps(self) -> float:
        """Decoded frames per second over the last few seconds (0 if stalled)."""
        r = list(self._recent)
        if len(r) < 2 or time.monotonic() - r[-1] > 2:
            return 0.0
        return round((len(r) - 1) / (r[-1] - r[0]), 2)

    def _run(self) -> None:
        backoff, seq = 2.0, 0
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                src = probe_size(self.url)
                self.size = output_size(*src, self.max_width)
            except Exception as exc:  # noqa: BLE001
                self.state, self.error = "error", f"stream unavailable: {exc}"
                log.warning("[%s] %s (retry in %.0fs)", self.name, self.error, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue

            w, h = self.size
            frame_bytes = w * h * 3
            self._proc = subprocess.Popen(
                build_decode_cmd(self.url, self.fps, self.size),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=frame_bytes,
            )
            threading.Thread(target=self._pump_stderr, args=(self._proc,), daemon=True).start()
            self.state, self.error = "running", ""
            log.info("[%s] decoding %dx%d at %g fps", self.name, w, h, self.fps)
            assert self._proc.stdout is not None
            while not self._stop.is_set():
                buf = self._proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break  # ffmpeg exited or stream ended
                seq += 1
                self.frames_in += 1
                self._recent.append(time.monotonic())
                img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
                with self._cond:
                    self._frame = Frame(img, time.time(), seq)
                    self._cond.notify_all()
            if self._proc.poll() is None:
                self._proc.kill()
            self._proc.wait()
            if self._stop.is_set():
                break
            self.state = "error"
            self.error = "decoder stopped: " + (" | ".join(self._stderr) or f"exit {self._proc.returncode}")
            log.warning("[%s] %s", self.name, self.error)
            backoff = 2.0 if time.monotonic() - started > 300 else min(backoff * 2, 60)
            self._stop.wait(backoff)
        self.state = "stopped"

    def _pump_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            text = line.decode(errors="replace").strip()
            if text:
                self._stderr.append(text)
