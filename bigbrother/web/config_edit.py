"""Edit config/cameras.yml from the app.

Changes are validated with the same parser the services use, saved with comments
kept (ruamel round-trip), and a timestamped backup of the previous file is kept.
Then only the services the change affects are told to reload:
    go2rtc     streams changed            -> regenerate go2rtc.yaml, restart via its API
    recorder   streams/recording/storage  -> restarts (brief gap in recordings)
    detector   streams/detection/zones/masks
    alerter    alert rules/zones/global alert settings
"""

from __future__ import annotations

import copy
import io
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import DoubleQuotedScalarString
import yaml as pyyaml

from ..config import CAMERA_ID, Config, ConfigError, parse_config

BACKUPS_KEPT = 20

# Keys the app may set; anything else in the file is left as it is.
CAMERA_KEYS = {"name", "streams", "record", "detect", "zones", "masks", "alert"}
SETTINGS_KEYS = {"alerts": {"enabled", "notifier", "timezone", "verify", "verify_threshold", "public_url", "group_seconds", "arm_schedule"},
                 "storage": {"retention_days", "max_usage_percent", "event_retention_days"}}


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(obj: Any) -> Any:
    """ruamel containers -> plain JSON-able Python."""
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def _flow(seq) -> CommentedSeq:
    s = CommentedSeq(seq)
    s.fa.set_flow_style()
    return s


def _polys(polys: list) -> CommentedSeq:
    """Polygons as one compact line each: - [[x, y], [x, y], ...]"""
    out = CommentedSeq()
    for poly in polys:
        out.append(_flow([_flow([round(float(x), 4), round(float(y), 4)]) for x, y in poly]))
    return out


def _scalar(value: Any) -> Any:
    """Quote strings that the services' YAML loader (PyYAML, YAML 1.1) would read as something
    else: "22:00" is a number there, "on"/"no" are booleans."""
    if isinstance(value, str):
        try:
            if pyyaml.safe_load(value) != value:
                return DoubleQuotedScalarString(value)
        except pyyaml.YAMLError:
            return DoubleQuotedScalarString(value)
    return value


def _to_yaml_value(key: str, value: Any) -> Any:
    """Shape incoming JSON so the YAML stays readable (points on one line, short lists inline)."""
    if key == "zones" and isinstance(value, dict):
        out = CommentedMap()
        for name, z in value.items():
            zm = CommentedMap()
            zm["points"] = _flow([_flow([round(float(x), 4), round(float(y), 4)]) for x, y in z.get("points", [])])
            if z.get("objects"):
                zm["objects"] = _flow(list(z["objects"]))
            out[name] = zm
        return out
    if key == "masks" and isinstance(value, dict):
        out = CommentedMap()
        for kind in ("motion", "objects"):
            if value.get(kind):
                out[kind] = _polys(value[kind])
        return out
    if isinstance(value, dict):
        out = CommentedMap()
        for k, v in value.items():
            out[k] = _to_yaml_value(k, v)
        return out
    if isinstance(value, list) and all(not isinstance(v, (dict, list)) for v in value):
        return _flow([_scalar(v) for v in value])
    if isinstance(value, list):
        out = CommentedSeq()
        for v in value:
            out.append(_to_yaml_value("", v))
        return out
    return _scalar(value)


def _merge(target: CommentedMap, key: str, value: Any) -> None:
    """Set target[key] = value. Nested settings are merged key by key (so settings the app
    doesn't show, like detect.fps, survive); null removes a key. Zones and masks are
    replaced as a whole."""
    if value is None or (value in ({}, []) and key in ("zones", "masks", "schedule", "arm_schedule")):
        target.pop(key, None)
        return
    if isinstance(value, dict) and isinstance(target.get(key), CommentedMap) and key not in ("zones", "masks"):
        sub = target[key]
        for k, v in value.items():
            _merge(sub, k, v)
        if not sub:
            target.pop(key, None)
        return
    target[key] = _to_yaml_value(key, value)


def services_affected(old: Config, new: Config) -> set[str]:
    out: set[str] = set()
    if old.storage != new.storage:
        out.add("recorder")
    if old.alerts != new.alerts:
        out.add("alerter")
    if old.detector != new.detector:
        out.add("detector")
    for cid in set(old.cameras) | set(new.cameras):
        a, b = old.cameras.get(cid), new.cameras.get(cid)
        if a is None or b is None or (a.main, a.sub) != (b.main, b.sub):
            out |= {"go2rtc", "recorder", "detector", "alerter"}
            continue
        if a.record != b.record or a.name != b.name:
            out.add("recorder")
        if (a.detect, a.zones, a.motion_masks, a.object_masks, a.name) != \
                (b.detect, b.zones, b.motion_masks, b.object_masks, b.name):
            out.add("detector")
        if a.alert != b.alert or a.zones != b.zones or a.name != b.name:
            out.add("alerter")
    return out


class ConfigEditor:
    def __init__(self, path: Path, generated_dir: Path | None = None):
        self.path = path
        self.generated_dir = generated_dir or path.parent / "generated"
        self.backups = path.parent / "backups"
        self._lock = threading.Lock()

    def _load(self) -> CommentedMap:
        data = _yaml().load(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        return data if isinstance(data, CommentedMap) else CommentedMap()

    def read(self) -> dict:
        with self._lock:
            return _plain(self._load())

    def update(self, mutate: Callable[[CommentedMap], None]) -> tuple[Config, Config, set[str]]:
        """Apply `mutate` to the YAML, validate, back up and save. Returns (old, new, services)."""
        with self._lock:
            data = self._load()
            old = parse_config(_plain(data))
            mutate(data)
            parse_config(_plain(data))           # raises ConfigError -> nothing is written
            buf = io.StringIO()
            _yaml().dump(data, buf)
            # Validate exactly what the services will read (PyYAML), not ruamel's view of it.
            try:
                new = parse_config(pyyaml.safe_load(buf.getvalue()))
            except pyyaml.YAMLError as exc:
                raise ConfigError(f"the saved file wouldn't load: {exc}") from exc
            self._backup()
            tmp = self.path.with_suffix(".yml.tmp")
            tmp.write_text(buf.getvalue(), encoding="utf-8")
            os.replace(tmp, self.path)
            changed = services_affected(old, new)
            if "go2rtc" in changed:
                from ..go2rtc import write_go2rtc_config
                write_go2rtc_config(new, self.generated_dir)
            return old, new, changed

    def _backup(self) -> None:
        if not self.path.exists():
            return
        self.backups.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (self.backups / f"cameras-{stamp}.yml").write_bytes(self.path.read_bytes())
        old = sorted(self.backups.glob("cameras-*.yml"))
        for p in old[:-BACKUPS_KEPT]:
            p.unlink(missing_ok=True)

    # -- operations used by the API -------------------------------------------------
    def save_camera(self, cam_id: str, body: dict, create: bool = False):
        if not isinstance(cam_id, str) or not CAMERA_ID.match(cam_id) or cam_id.endswith("_sub"):
            raise ConfigError("Camera id: lowercase letters, digits and _, starting with a letter "
                              "(not ending in _sub).")
        unknown = set(body) - CAMERA_KEYS
        if unknown:
            raise ConfigError(f"Unknown camera setting(s): {', '.join(sorted(unknown))}")

        def mutate(data: CommentedMap) -> None:
            cams = data.setdefault("cameras", CommentedMap())
            if create and cam_id in cams:
                raise ConfigError(f"A camera with id {cam_id} already exists.")
            if not create and cam_id not in cams:
                raise ConfigError(f"No camera {cam_id}.")
            cam = cams.setdefault(cam_id, CommentedMap())
            for k, v in copy.deepcopy(body).items():
                _merge(cam, k, v)
        return self.update(mutate)

    def delete_camera(self, cam_id: str):
        def mutate(data: CommentedMap) -> None:
            cams = data.get("cameras") or {}
            if cam_id not in cams:
                raise ConfigError(f"No camera {cam_id}.")
            if len(cams) == 1:
                raise ConfigError("At least one camera is required.")
            del cams[cam_id]
        return self.update(mutate)

    def save_settings(self, body: dict):
        for section, keys in SETTINGS_KEYS.items():
            if section in body:
                if not isinstance(body[section], dict):
                    raise ConfigError(f"{section} must be an object")
                bad = set(body[section]) - keys
                if bad:
                    raise ConfigError(f"Unknown {section} setting(s): {', '.join(sorted(bad))}")
        if set(body) - set(SETTINGS_KEYS):
            raise ConfigError("Only alerts and storage settings can be changed here.")

        def mutate(data: CommentedMap) -> None:
            for section in SETTINGS_KEYS:
                if section in body:
                    sec = data.setdefault(section, CommentedMap())
                    for k, v in body[section].items():
                        _merge(sec, k, v)
        return self.update(mutate)


ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def env_refs(raw: dict) -> list[str]:
    refs: set[str] = set()
    for cam in (raw.get("cameras") or {}).values():
        for url in ((cam or {}).get("streams") or {}).values():
            if isinstance(url, str):
                refs.update(ENV_REF.findall(url))
    return sorted(refs)
