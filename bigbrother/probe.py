"""Phase 0: inspect every camera stream and report what it actually sends.

For each main/sub stream we measure codec, resolution, real frame rate,
keyframe (I-frame) interval and bitrate by reading a few seconds of packets.
Those numbers drive later decisions: detection resolution, how fast clips
can start, and storage use per day.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, dataclass, field
from statistics import mean

from .config import Camera, Config, ConfigError, expand_env, mask_url

RTSP_IN = ["-rtsp_transport", "tcp", "-timeout", "10000000"]  # 10 s socket timeout (µs)


@dataclass
class StreamReport:
    camera: str
    stream: str  # "main" | "sub"
    url: str  # password masked
    ok: bool = False
    error: str | None = None
    video_codec: str | None = None
    profile: str | None = None
    width: int | None = None
    height: int | None = None
    declared_fps: float | None = None
    measured_fps: float | None = None
    keyframe_interval_s: float | None = None
    bitrate_kbps: float | None = None
    audio_codec: str | None = None
    notes: list[str] = field(default_factory=list)


def _rate(text: str | None) -> float | None:
    if not text or text in ("0/0", "0"):
        return None
    num, _, den = text.partition("/")
    try:
        return round(float(num) / float(den or 1), 2)
    except (ValueError, ZeroDivisionError):
        return None


def _classify_error(stderr: str) -> str:
    s = stderr.lower()
    if "401" in s or "unauthorized" in s:
        return "Authentication failed (check CAM_USER / CAM_PASSWORD in .env)"
    if "404" in s or "not found" in s:
        return "Stream path not found (check the URL path, e.g. /Streaming/Channels/101)"
    if "connection refused" in s:
        return "Connection refused (wrong IP/port, or RTSP disabled on the camera)"
    if "timed out" in s or "timeout" in s:
        return "Timed out (camera unreachable from this machine?)"
    last = stderr.strip().splitlines()[-1:] or ["unknown error"]
    return last[0][:200]


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def analyze_packets(packets: list[dict]) -> tuple[float | None, float | None, float | None]:
    """Return (fps, keyframe_interval_s, bitrate_kbps) from ffprobe packet entries."""
    times: list[float] = []
    key_index: list[int] = []  # by packet position: the first keyframe of an RTSP
    total_bytes = 0            # session often has no timestamp, so don't rely on them
    for i, p in enumerate(packets):
        total_bytes += int(p.get("size", 0) or 0)
        if "K" in (p.get("flags") or ""):
            key_index.append(i)
        t = p.get("pts_time") or p.get("dts_time")
        if t not in (None, "N/A"):
            times.append(float(t))
    if len(times) < 2:
        return None, None, None
    span = max(times) - min(times)
    if span <= 0:
        return None, None, None
    fps = round((len(times) - 1) / span, 2)
    gaps = [b - a for a, b in zip(key_index, key_index[1:])]
    gop = round(mean(gaps) / fps, 2) if gaps and fps else None
    kbps = round(total_bytes * 8 / span / 1000, 1)
    return fps, gop, kbps


def probe_stream(cam: Camera, which: str, url_raw: str, seconds: int = 10) -> StreamReport:
    report = StreamReport(camera=cam.id, stream=which, url=mask_url(url_raw))
    try:
        url = expand_env(url_raw)
    except ConfigError as exc:
        report.error = str(exc)
        return report

    try:
        meta = _run(
            ["ffprobe", "-v", "error", *RTSP_IN,
             "-show_entries",
             "stream=codec_type,codec_name,profile,width,height,avg_frame_rate,r_frame_rate",
             "-of", "json", url],
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        report.error = "Timed out (camera unreachable from this machine?)"
        return report
    if meta.returncode != 0:
        report.error = _classify_error(meta.stderr.replace(url, report.url))
        return report

    for s in json.loads(meta.stdout or "{}").get("streams", []):
        if s.get("codec_type") == "video" and report.video_codec is None:
            report.video_codec = s.get("codec_name")
            report.profile = s.get("profile")
            report.width, report.height = s.get("width"), s.get("height")
            report.declared_fps = _rate(s.get("avg_frame_rate")) or _rate(s.get("r_frame_rate"))
        elif s.get("codec_type") == "audio" and report.audio_codec is None:
            report.audio_codec = s.get("codec_name")

    try:
        pk = _run(
            ["ffprobe", "-v", "error", *RTSP_IN, "-select_streams", "v:0",
             "-read_intervals", f"%+{seconds}",
             "-show_entries", "packet=pts_time,dts_time,flags,size",
             "-of", "json", url],
            timeout=seconds + 30,
        )
        if pk.returncode == 0:
            fps, gop, kbps = analyze_packets(json.loads(pk.stdout or "{}").get("packets", []))
            report.measured_fps, report.keyframe_interval_s, report.bitrate_kbps = fps, gop, kbps
            if fps and gop is None:
                report.notes.append(
                    f"Fewer than two keyframes in a {seconds}s sample: the keyframe interval is "
                    "very long. Set the camera's I-frame interval to about 1-2x the frame rate."
                )
    except subprocess.TimeoutExpired:
        report.notes.append("Packet sampling timed out; fps/keyframe/bitrate unknown")

    report.ok = True
    report.notes.extend(recommendations(report))
    return report


def recommendations(r: StreamReport) -> list[str]:
    notes: list[str] = []
    if r.keyframe_interval_s and r.keyframe_interval_s > 2.5:
        notes.append(
            f"Keyframe every {r.keyframe_interval_s}s: set the camera's I-frame interval to "
            "about 1-2x the frame rate so clips start quickly and segments cut cleanly."
        )
    if r.stream == "sub" and r.width and r.width < 640:
        notes.append("Substream is under 640px wide: raise it if the camera allows (helps distant detections).")
    if r.stream == "main" and r.video_codec == "hevc":
        notes.append("Main stream is H.265: fine for storage; recordings get tagged hvc1 for iPhone playback.")
    if r.stream == "main" and r.width and r.width <= 1920 and r.height and r.height <= 1080:
        notes.append("Main stream is 1080p or lower: if the sensor is 2K, set the camera to its native resolution.")
    if r.audio_codec in ("pcm_alaw", "pcm_mulaw"):
        notes.append("Audio is G.711: it will be converted to AAC if audio recording is turned on.")
    if r.measured_fps and r.declared_fps and r.measured_fps < r.declared_fps * 0.8:
        notes.append(
            f"Measured {r.measured_fps} fps vs declared {r.declared_fps}: the camera may be "
            "dropping frames or using smart/variable frame rate (consider turning that off)."
        )
    return notes


def probe_all(cfg: Config, seconds: int = 10) -> list[StreamReport]:
    reports = []
    for cam in cfg.cameras.values():
        reports.append(probe_stream(cam, "main", cam.main, seconds))
        if cam.sub:
            reports.append(probe_stream(cam, "sub", cam.sub, seconds))
        else:
            r = StreamReport(camera=cam.id, stream="sub", url="-", error="No substream configured")
            reports.append(r)
    return reports


def _fmt(v) -> str:
    return "-" if v is None else str(v)


def render_markdown(reports: list[StreamReport]) -> str:
    lines = [
        "| Camera | Stream | Codec | Resolution | FPS (measured) | Keyframe every | Bitrate | Audio |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in reports:
        if not r.ok:
            lines.append(f"| {r.camera} | {r.stream} | **error** | {r.error} | | | | |")
            continue
        res = f"{r.width}x{r.height}" if r.width else "-"
        fps = f"{_fmt(r.measured_fps)} ({_fmt(r.declared_fps)} declared)"
        gop = f"{r.keyframe_interval_s}s" if r.keyframe_interval_s else "-"
        kbps = f"{r.bitrate_kbps} kbps" if r.bitrate_kbps else "-"
        lines.append(
            f"| {r.camera} | {r.stream} | {_fmt(r.video_codec)} | {res} | {fps} | {gop} | {kbps} | {_fmt(r.audio_codec)} |"
        )
    notes = [(r, n) for r in reports for n in r.notes]
    if notes:
        lines += ["", "## Recommendations", ""]
        lines += [f"- **{r.camera} {r.stream}:** {n}" for r, n in notes]
    total_kbps = sum(r.bitrate_kbps or 0 for r in reports if r.ok and r.stream == "main")
    if total_kbps:
        gb_day = total_kbps * 1000 / 8 * 86400 / 1e9
        lines += ["", f"Estimated 24/7 main-stream storage: **{gb_day:.0f} GB/day** "
                      f"(~{gb_day * 30 / 1000:.1f} TB per 30 days)."]
    return "\n".join(lines) + "\n"


def reports_json(reports: list[StreamReport]) -> str:
    return json.dumps([asdict(r) for r in reports], indent=2)
