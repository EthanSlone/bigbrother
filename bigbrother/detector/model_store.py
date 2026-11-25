"""Built-in models: downloaded once into /models and verified by checksum."""

from __future__ import annotations

import hashlib
import logging
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MODELS_DIR = Path(os.environ.get("BB_MODELS", "/models"))
_YOLOX = "https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0"


@dataclass(frozen=True)
class ModelInfo:
    family: str
    url: str
    sha256: str
    note: str


# YOLOX: Apache-2.0, trained on COCO. Benchmarked 2026-11-25 on the test clips:
# tiny misses overhead cars; s catches them; m is ~2x slower for a small gain.
BUILTIN = {
    "yolox_tiny": ModelInfo("yolox", f"{_YOLOX}/yolox_tiny.onnx",
                            "427cc366d34e27ff7a03e2899b5e3671425c262ea2291f88bb942bc1cc70b0f7",
                            "416px, fastest, weakest"),
    "yolox_s": ModelInfo("yolox", f"{_YOLOX}/yolox_s.onnx",
                         "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063",
                         "640px, default"),
    "yolox_m": ModelInfo("yolox", f"{_YOLOX}/yolox_m.onnx",
                         "21ff6cfdeb53b013bac2249599e55f00bff3cfdfdab37ed7a4620818c1d15b3f",
                         "640px, ~2x slower, most accurate"),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_model(name_or_path: str, models_dir: Path = MODELS_DIR) -> tuple[Path, str | None]:
    """Return (local path, family-if-built-in), downloading a built-in model if needed."""
    if name_or_path not in BUILTIN:
        path = Path(name_or_path)
        if not path.is_absolute():
            path = models_dir / path
        if not path.exists():
            raise FileNotFoundError(
                f"model '{name_or_path}' is not built in ({', '.join(BUILTIN)}) and {path} does not exist"
            )
        return path, None

    info = BUILTIN[name_or_path]
    path = models_dir / f"{name_or_path}.onnx"
    if path.exists() and _sha256(path) == info.sha256:
        return path, info.family

    models_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    log.info("downloading %s from %s", name_or_path, info.url)
    with urllib.request.urlopen(info.url, timeout=60) as resp, tmp.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
    digest = _sha256(tmp)
    if digest != info.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{name_or_path}: checksum mismatch (got {digest[:12]}..., refusing to use it)")
    os.replace(tmp, path)
    log.info("saved %s (%.1f MB)", path, path.stat().st_size / 1e6)
    return path, info.family
