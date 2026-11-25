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

import yaml

ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
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


@dataclass(frozen=True)
class Camera:
    id: str
    name: str
    main: str  # raw URL, may contain ${VARS}
    sub: str | None
    record: RecordSettings

    @property
    def main_stream(self) -> str:
        """go2rtc stream name for the main (recording) stream."""
        return self.id

    @property
    def sub_stream(self) -> str | None:
        """go2rtc stream name for the substream (detection), if any."""
        return f"{self.id}_sub" if self.sub else None


@dataclass(frozen=True)
class StorageSettings:
    retention_days: int = 30
    max_usage_percent: float = 90.0


@dataclass(frozen=True)
class Config:
    cameras: dict[str, Camera]
    storage: StorageSettings = field(default_factory=StorageSettings)
    go2rtc_url: str = "rtsp://go2rtc:8554"
    mqtt_host: str = "mosquitto"
    mqtt_port: int = 1883
    mqtt_prefix: str = "bigbrother"

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


def parse_config(data: Any) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("cameras.yml must be a mapping at the top level")

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ConfigError("defaults must be a mapping")
    base_record = _record_settings(defaults.get("record"), RecordSettings(), "defaults.record")

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
            if not isinstance(url, str) or not url.startswith(("rtsp://", "rtsps://")):
                raise ConfigError(f"{where}.streams.{key} must be an rtsp:// URL")
            if re.search(r"://[^:/@]+:(?!\$\{)[^@/]+@", url):
                raise ConfigError(
                    f"{where}.streams.{key} has a literal password; put it in .env and "
                    "reference it as ${VAR}"
                )
        cameras[cam_id] = Camera(
            id=cam_id,
            name=str(raw.get("name") or cam_id.replace("_", " ").title()),
            main=streams["main"],
            sub=streams.get("sub"),
            record=_record_settings(raw.get("record"), base_record, f"{where}.record"),
        )

    mqtt = data.get("mqtt") or {}
    return Config(
        cameras=cameras,
        storage=storage,
        go2rtc_url=str(data.get("go2rtc_url", "rtsp://go2rtc:8554")),
        mqtt_host=str(mqtt.get("host", "mosquitto")),
        mqtt_port=_as_int(mqtt.get("port", 1883), "mqtt.port", 1),
        mqtt_prefix=str(mqtt.get("prefix", "bigbrother")),
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
