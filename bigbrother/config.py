"""Loading and validating config/cameras.yml.

Secrets never live in cameras.yml. Stream URLs reference environment
variables (``${CAM_PASSWORD}``) that come from the .env file. The raw,
unexpanded URLs are kept so that generated files (like go2rtc.yaml) never
contain secrets; only code that talks to a camera directly calls
``expand_env``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import datetime as _dt

import yaml

from .zones import ZONE_NAME, Polygon, Zone, ZoneConfigError, parse_polygon

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# rtsp:// for real cameras; ffmpeg:http(s):// lets go2rtc pull HLS feeds (e.g. public
# traffic cameras) and re-serve them as RTSP, so the rest of the system is unaffected.
SOURCE_PREFIXES = ("rtsp://", "rtsps://", "ffmpeg:http://", "ffmpeg:https://")
CAMERA_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

DEFAULT_CONFIG_PATH = Path(os.environ.get("BB_CONFIG", "/config/cameras.yml"))


class ConfigError(ValueError):
    """Raised when cameras.yml is missing, malformed or inconsistent."""


@dataclass(frozen=True)
class RecordSettings:
    enabled: bool = True
    audio: bool = False
    segment_seconds: int = 60
    retention_days: int | None = None  # None -> use storage default


DEFAULT_LABELS = ("person", "car", "truck", "bus", "motorcycle", "bicycle", "dog", "cat")


@dataclass(frozen=True)
class DetectSettings:
    enabled: bool = True
    fps: float = 5.0           # frames per second analysed (from the substream)
    live_fps: float = 0.0      # debug-viewer preview rate; 0 = same as fps (live video comes from go2rtc)
    width: int = 640           # frames wider than this are scaled down before detection
    labels: tuple[str, ...] = DEFAULT_LABELS
    min_score: float = 0.2     # weakest detection kept; below 0.5 it can only extend an existing track
    threshold: float = 0.6     # an object's median score must reach this to become an event
    min_frames: int = 3        # ...over at least this many frames


@dataclass(frozen=True)
class DetectorSettings:
    """Which model runs, and on what hardware. Changing hardware = changing backend/device."""
    model: str = "yolox_s"     # built-in name (yolox_tiny/yolox_s/yolox_m) or a path to an .onnx
    family: str = "yolox"      # output format of the model
    backend: str = "openvino"  # openvino (Intel CPU/GPU) | onnxruntime (cpu/cuda/tensorrt)
    device: str = ""           # "" = backend default (CPU)
    threads: int = 0           # threads per inference; 0 = CPUs / workers
    workers: int = 0           # inferences run in parallel; 0 = automatic (1 per 8 CPUs)
    # Model input (width, height). Cameras are 16:9, so 640x384 skips the grey padding a
    # square 640x640 input wastes (~40% less work, same detections). None = model's own size.
    input_size: tuple[int, int] | None = (640, 384)


DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Schedule:
    """A window when alerts are allowed. end < start means it runs past midnight."""
    days: frozenset[int]   # 0 = Monday
    start: _dt.time
    end: _dt.time

    def active(self, now: _dt.datetime) -> bool:
        t, day = now.time(), now.weekday()
        if self.start <= self.end:
            return day in self.days and self.start <= t < self.end
        # Overnight window: the part after midnight belongs to the previous day's window.
        return (day in self.days and t >= self.start) or ((day - 1) % 7 in self.days and t < self.end)


@dataclass(frozen=True)
class CameraAlertSettings:
    enabled: bool = True
    labels: tuple[str, ...] = ("person",)
    zones: tuple[str, ...] = ()           # if set, the object must enter one of these
    cooldown_seconds: int = 60            # no repeat alert for the same camera + object type
    schedule: tuple[Schedule, ...] = ()   # empty = any time


@dataclass(frozen=True)
class AlertSettings:
    enabled: bool = True
    notifier: str = "pushover"            # pushover | log
    timezone: str = "UTC"                 # for schedules and times in notifications
    verify: bool = True                   # second opinion on the full-resolution frame
    verify_model: str = "yolox_m"
    verify_threshold: float = 0.5
    public_url: str = ""                  # e.g. https://nvr.example.com (links in notifications, Phase 4)
    priority: int = 0                     # Pushover priority -2..1
    sound: str = ""                       # Pushover sound name ("" = your default)
    threads: int = 0                      # inference threads for the verifier (0 = auto)


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    main: str  # raw URL, may contain ${VARS}
    sub: str | None
    record: RecordSettings
    detect: DetectSettings = field(default_factory=DetectSettings)
    zones: dict[str, Zone] = field(default_factory=dict)
    motion_masks: tuple[Polygon, ...] = ()
    object_masks: tuple[Polygon, ...] = ()
    alert: CameraAlertSettings = field(default_factory=CameraAlertSettings)

    @property
    def main_stream(self) -> str:
        """go2rtc stream name for the main (recording) stream."""
        return self.id

    @property
    def sub_stream(self) -> str | None:
        """go2rtc stream name for the substream (detection), if any."""
        return f"{self.id}_sub" if self.sub else None

    @property
    def detect_stream(self) -> str:
        """Detection uses the substream when there is one (much cheaper to decode)."""
        return self.sub_stream or self.main_stream


@dataclass(frozen=True)
class StorageSettings:
    retention_days: int = 30
    max_usage_percent: float = 90.0


@dataclass(frozen=True)
class Config:
    cameras: dict[str, Camera]
    storage: StorageSettings = field(default_factory=StorageSettings)
    detector: DetectorSettings = field(default_factory=DetectorSettings)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    go2rtc_url: str = "rtsp://go2rtc:8554"
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_prefix: str = "bigbrother"
    go2rtc_api: str = "http://go2rtc:1984"

    def env_refs(self) -> set[str]:
        """All environment variable names referenced by stream URLs."""
        refs: set[str] = set()
        for cam in self.cameras.values():
            for url in (cam.main, cam.sub):
                if url:
                    refs.update(ENV_REF.findall(url))
        return refs

    def restream_url(self, stream_name: str) -> str:
        return f"{self.go2rtc_url.rstrip('/')}/{stream_name}"


def expand_env(value: str, env: Mapping[str, str] | None = None) -> str:
    """Replace ${VAR} references, failing loudly if any are unset."""
    env = os.environ if env is None else env
    missing = [name for name in ENV_REF.findall(value) if name not in env]
    if missing:
        raise ConfigError(f"Environment variable(s) not set: {', '.join(sorted(set(missing)))}")
    return ENV_REF.sub(lambda m: env[m.group(1)], value)


def direct_url(source: str) -> str:
    """The URL ffprobe/ffmpeg can open directly: drops go2rtc's 'ffmpeg:' prefix and #options."""
    if source.startswith("ffmpeg:"):
        return source[len("ffmpeg:"):].split("#", 1)[0]
    return source


def input_args(url: str) -> list[str]:
    """Demuxer options for ffprobe/ffmpeg; RTSP-only flags break other inputs."""
    if url.startswith(("rtsp://", "rtsps://")):
        return ["-rtsp_transport", "tcp", "-timeout", "10000000"]  # 10 s socket timeout (µs)
    return ["-rw_timeout", "15000000"]


def mask_url(url: str) -> str:
    """Hide the password in rtsp://user:pass@host URLs for logs and reports."""
    return re.sub(r"(://[^:/@]+:)[^@]*@", r"\1****@", url)


def _as_int(value: Any, where: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{where} must be an integer >= {minimum}, got {value!r}")
    return value


def _record_settings(raw: Any, base: RecordSettings, where: str) -> RecordSettings:
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - {"enabled", "audio", "segment_seconds", "retention_days"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    seg = raw.get("segment_seconds", base.segment_seconds)
    _as_int(seg, f"{where}.segment_seconds", minimum=10)
    ret = raw.get("retention_days", base.retention_days)
    if ret is not None:
        _as_int(ret, f"{where}.retention_days", minimum=1)
    return RecordSettings(
        enabled=bool(raw.get("enabled", base.enabled)),
        audio=bool(raw.get("audio", base.audio)),
        segment_seconds=seg,
        retention_days=ret,
    )


def _number(value: Any, where: str, lo: float, hi: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
        raise ConfigError(f"{where} must be a number between {lo} and {hi}, got {value!r}")
    return float(value)


def _detect_settings(raw: Any, base: DetectSettings, where: str) -> DetectSettings:
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - {"enabled", "fps", "live_fps", "width", "labels", "min_score", "threshold",
                          "min_frames"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    labels = raw.get("labels", base.labels)
    if not isinstance(labels, (list, tuple)) or not labels or not all(isinstance(x, str) for x in labels):
        raise ConfigError(f"{where}.labels must be a non-empty list of names")
    settings = DetectSettings(
        enabled=bool(raw.get("enabled", base.enabled)),
        fps=_number(raw.get("fps", base.fps), f"{where}.fps", 0.5, 30),
        live_fps=_number(raw.get("live_fps", base.live_fps), f"{where}.live_fps", 0, 30),
        width=_as_int(raw.get("width", base.width), f"{where}.width", 160),
        labels=tuple(labels),
        min_score=_number(raw.get("min_score", base.min_score), f"{where}.min_score", 0.05, 0.99),
        threshold=_number(raw.get("threshold", base.threshold), f"{where}.threshold", 0.05, 0.99),
        min_frames=_as_int(raw.get("min_frames", base.min_frames), f"{where}.min_frames", 1),
    )
    if settings.threshold < settings.min_score:
        raise ConfigError(f"{where}: threshold must be >= min_score")
    return settings


def _detector_settings(raw: Any) -> DetectorSettings:
    if raw is None:
        return DetectorSettings()
    if not isinstance(raw, dict):
        raise ConfigError("detector must be a mapping")
    unknown = set(raw) - {"model", "family", "backend", "device", "threads", "workers", "input_size"}
    if unknown:
        raise ConfigError(f"detector: unknown key(s) {sorted(unknown)}")
    backend = str(raw.get("backend", "openvino"))
    if backend not in ("openvino", "onnxruntime"):
        raise ConfigError("detector.backend must be openvino or onnxruntime")
    return DetectorSettings(
        model=str(raw.get("model", "yolox_s")),
        family=str(raw.get("family", "yolox")),
        backend=backend,
        device=str(raw.get("device", "")),
        threads=_as_int(raw.get("threads", 0), "detector.threads", 0),
        workers=_as_int(raw.get("workers", 0), "detector.workers", 0),
        input_size=_input_size(raw.get("input_size", "default")),
    )


def _input_size(raw: Any) -> tuple[int, int] | None:
    """detector.input_size: [width, height] in multiples of 32, or "native" for the model's own."""
    if raw == "default":
        return DetectorSettings.input_size
    if raw is None or raw == "native":
        return None
    if (not isinstance(raw, (list, tuple)) or len(raw) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in raw)):
        raise ConfigError('detector.input_size must be [width, height], e.g. [640, 384], or "native"')
    w, h = raw
    if not (64 <= w <= 1920 and 64 <= h <= 1920) or w % 32 or h % 32:
        raise ConfigError("detector.input_size: width and height must be multiples of 32 between 64 and 1920")
    return (w, h)


def _zones(raw: Any, where: str) -> dict[str, Zone]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping of zone name -> settings")
    zones = {}
    for name, z in raw.items():
        zw = f"{where}.{name}"
        if not isinstance(name, str) or not ZONE_NAME.match(name):
            raise ConfigError(f"{zw}: zone names must be lowercase letters, digits and underscores")
        if not isinstance(z, dict) or "points" not in z:
            raise ConfigError(f"{zw} needs points: [[x, y], ...]")
        unknown = set(z) - {"points", "objects"}
        if unknown:
            raise ConfigError(f"{zw}: unknown key(s) {sorted(unknown)}")
        objects = z.get("objects")
        if objects is not None and (not isinstance(objects, list) or not all(isinstance(o, str) for o in objects)):
            raise ConfigError(f"{zw}.objects must be a list of object names")
        try:
            zones[name] = Zone(name, parse_polygon(z["points"], f"{zw}.points"),
                               tuple(objects) if objects else None)
        except ZoneConfigError as exc:
            raise ConfigError(str(exc)) from exc
    return zones


def _masks(raw: Any, where: str) -> tuple[tuple[Polygon, ...], tuple[Polygon, ...]]:
    if raw is None:
        return (), ()
    if not isinstance(raw, dict) or set(raw) - {"motion", "objects"}:
        raise ConfigError(f"{where} must have 'motion' and/or 'objects' lists of polygons")
    out = []
    for kind in ("motion", "objects"):
        polys = raw.get(kind) or []
        if not isinstance(polys, list):
            raise ConfigError(f"{where}.{kind} must be a list of polygons")
        try:
            out.append(tuple(parse_polygon(p, f"{where}.{kind}[{i}]") for i, p in enumerate(polys)))
        except ZoneConfigError as exc:
            raise ConfigError(str(exc)) from exc
    return out[0], out[1]


def _time(value: Any, where: str) -> _dt.time:
    try:
        return _dt.time.fromisoformat(str(value))
    except ValueError as exc:
        raise ConfigError(f"{where} must be a time like \"22:00\", got {value!r}") from exc


def _schedule(raw: Any, where: str) -> tuple[Schedule, ...]:
    if not raw:
        return ()
    if not isinstance(raw, list):
        raise ConfigError(f"{where} must be a list of {{days, start, end}} windows")
    out = []
    for i, w in enumerate(raw):
        ww = f"{where}[{i}]"
        if not isinstance(w, dict) or set(w) - {"days", "start", "end"} or "start" not in w or "end" not in w:
            raise ConfigError(f"{ww} needs start and end (and optionally days)")
        days = w.get("days", list(DAYS))
        if not isinstance(days, list) or not all(isinstance(d, str) and d.lower()[:3] in DAYS for d in days):
            raise ConfigError(f"{ww}.days must be a list like [mon, tue, sat]")
        out.append(Schedule(frozenset(DAYS.index(d.lower()[:3]) for d in days),
                            _time(w["start"], f"{ww}.start"), _time(w["end"], f"{ww}.end")))
    return tuple(out)


def _camera_alert(raw: Any, base: CameraAlertSettings, where: str) -> CameraAlertSettings:
    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be a mapping")
    unknown = set(raw) - {"enabled", "labels", "zones", "cooldown_seconds", "schedule"}
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {sorted(unknown)}")
    for key in ("labels", "zones"):
        v = raw.get(key)
        if v is not None and (not isinstance(v, list) or not all(isinstance(x, str) for x in v)):
            raise ConfigError(f"{where}.{key} must be a list of names")
    return CameraAlertSettings(
        enabled=bool(raw.get("enabled", base.enabled)),
        labels=tuple(raw["labels"]) if "labels" in raw else base.labels,
        zones=tuple(raw["zones"]) if "zones" in raw else base.zones,
        cooldown_seconds=_as_int(raw.get("cooldown_seconds", base.cooldown_seconds),
                                 f"{where}.cooldown_seconds", 0),
        schedule=_schedule(raw["schedule"], f"{where}.schedule") if "schedule" in raw else base.schedule,
    )


def _alert_settings(raw: Any) -> AlertSettings:
    if raw is None:
        return AlertSettings()
    if not isinstance(raw, dict):
        raise ConfigError("alerts must be a mapping")
    unknown = set(raw) - {"enabled", "notifier", "timezone", "verify", "verify_model", "verify_threshold",
                          "public_url", "pushover", "threads"}
    if unknown:
        raise ConfigError(f"alerts: unknown key(s) {sorted(unknown)}")
    notifier = str(raw.get("notifier", "pushover"))
    if notifier not in ("pushover", "log"):
        raise ConfigError("alerts.notifier must be pushover or log")
    tz = str(raw.get("timezone", "UTC"))
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"alerts.timezone: unknown time zone {tz!r} (use e.g. America/New_York)") from exc
    po = raw.get("pushover") or {}
    if not isinstance(po, dict) or set(po) - {"priority", "sound"}:
        raise ConfigError("alerts.pushover may contain priority and sound")
    priority = po.get("priority", 0)
    if priority not in (-2, -1, 0, 1):
        raise ConfigError("alerts.pushover.priority must be -2, -1, 0 or 1 (2/emergency is not supported)")
    return AlertSettings(
        enabled=bool(raw.get("enabled", True)),
        notifier=notifier,
        timezone=tz,
        verify=bool(raw.get("verify", True)),
        verify_model=str(raw.get("verify_model", "yolox_m")),
        verify_threshold=_number(raw.get("verify_threshold", 0.5), "alerts.verify_threshold", 0.05, 0.99),
        public_url=str(raw.get("public_url", "")).rstrip("/"),
        priority=priority,
        sound=str(po.get("sound", "")),
        threads=_as_int(raw.get("threads", 0), "alerts.threads", 0),
    )


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("cameras.yml must be a mapping at the top level")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping")
    base_record = _record_settings(defaults.get("record"), RecordSettings(), "defaults.record")
    base_detect = _detect_settings(defaults.get("detect"), DetectSettings(), "defaults.detect")
    base_alert = _camera_alert(defaults.get("alert"), CameraAlertSettings(), "defaults.alert")

    storage_raw = data.get("storage") or {}
    if not isinstance(storage_raw, dict):
        raise ConfigError("storage must be a mapping")
    storage = StorageSettings(
        retention_days=_as_int(storage_raw.get("retention_days", 30), "storage.retention_days", 1),
        max_usage_percent=float(storage_raw.get("max_usage_percent", 90)),
    )
    if not 10 <= storage.max_usage_percent <= 99:
        raise ConfigError("storage.max_usage_percent must be between 10 and 99")

    cams_raw = data.get("cameras")
    if not isinstance(cams_raw, dict) or not cams_raw:
        raise ConfigError("cameras: at least one camera is required")

    cameras: dict[str, Camera] = {}
    for cam_id, raw in cams_raw.items():
        where = f"cameras.{cam_id}"
        if not isinstance(cam_id, str) or not CAMERA_ID.match(cam_id):
            raise ConfigError(
                f"{where}: camera ids must be lowercase letters, digits and underscores, "
                "starting with a letter (max 32 chars)"
            )
        if cam_id.endswith("_sub"):
            raise ConfigError(f"{where}: ids ending in '_sub' are reserved for substreams")
        if not isinstance(raw, dict):
            raise ConfigError(f"{where} must be a mapping")
        streams = raw.get("streams")
        if not isinstance(streams, dict) or not streams.get("main"):
            raise ConfigError(f"{where}.streams.main is required")
        for key, url in streams.items():
            if key not in ("main", "sub"):
                raise ConfigError(f"{where}.streams: unknown stream '{key}' (use main/sub)")
            if not isinstance(url, str) or not url.startswith(SOURCE_PREFIXES):
                raise ConfigError(
                    f"{where}.streams.{key} must be an rtsp:// URL, or ffmpeg:https://... "
                    "for an HLS (.m3u8) feed"
                )
            if re.search(r"://[^:/@]+:(?!\$\{)[^@/]+@", url):
                raise ConfigError(
                    f"{where}.streams.{key} has a literal password; put it in .env and "
                    "reference it as ${VAR}"
                )
        masks = _masks(raw.get("masks"), f"{where}.masks")
        zones = _zones(raw.get("zones"), f"{where}.zones")
        alert = _camera_alert(raw.get("alert"), base_alert, f"{where}.alert")
        missing = [z for z in alert.zones if z not in zones]
        if missing:
            raise ConfigError(f"{where}.alert.zones: no zone named {', '.join(missing)} on this camera")
        cameras[cam_id] = Camera(
            id=cam_id,
            name=str(raw.get("name") or cam_id.replace("_", " ").title()),
            main=streams["main"],
            sub=streams.get("sub"),
            record=_record_settings(raw.get("record"), base_record, f"{where}.record"),
            detect=_detect_settings(raw.get("detect"), base_detect, f"{where}.detect"),
            zones=zones,
            motion_masks=masks[0],
            object_masks=masks[1],
            alert=alert,
        )

    mqtt = data.get("mqtt") or {}
    return Config(
        cameras=cameras,
        storage=storage,
        detector=_detector_settings(data.get("detector")),
        alerts=_alert_settings(data.get("alerts")),
        go2rtc_url=str(data.get("go2rtc_url", "rtsp://go2rtc:8554")),
        mqtt_host=str(mqtt.get("host", "mosquitto")),
        mqtt_port=_as_int(mqtt.get("port", 1883), "mqtt.port", 1),
        mqtt_prefix=str(mqtt.get("prefix", "bigbrother")),
        go2rtc_api=str(data.get("go2rtc_api", "http://go2rtc:1984")).rstrip("/"),
    )


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"{path} not found. Copy config/cameras.example.yml to config/cameras.yml and edit it."
        )
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    return parse_config(data)
