"""Score detection settings against an exported test set (your own cameras' events).

    python -m bigbrother evaluate-testset bigbrother-testset-YYYYMMDD-HHMM.zip
    python -m bigbrother evaluate-testset testset.zip --variant "strict:threshold=0.7" --variant "nozoom:zoom=false"

Every event clip (and every reported miss) is replayed through the production
pipeline with each variant's settings, and the result is compared with your
verdict:
  - false alarm  -> good if no event of that kind is produced any more
  - correct      -> good if it's still detected
  - wrong object -> good if it now comes out as the object you said it was
  - missed       -> good if it's now detected
Unreviewed events are reported as "still detected" for information only.
The second-opinion check before alerting isn't part of this; it filters further.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from ..config import DetectSettings
from .evaluate import evaluate_clip
from .tracker import group_of

BEFORE = {"width": 640, "zoom": False, "motion_gate": False, "label_vote": False, "night_boost": 0.0}
SLACK = 3.0   # seconds around the original event that a produced event may start in


def parse_variant(spec: str) -> tuple[str, dict]:
    """"name:key=value,key=value" over DetectSettings fields."""
    name, _, body = spec.partition(":")
    fields = {f.name: f for f in dataclasses.fields(DetectSettings)}
    out: dict = {}
    for part in filter(None, body.split(",")):
        k, _, v = part.partition("=")
        k = k.strip()
        if k not in fields:
            raise SystemExit(f"unknown setting {k!r} in variant {name!r}")
        default = getattr(DetectSettings(), k)
        if isinstance(default, bool):
            out[k] = v.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(default, int):
            out[k] = int(v)
        elif isinstance(default, float):
            out[k] = float(v)
        else:
            out[k] = v.strip()
    return name.strip(), out


@dataclass
class Score:
    name: str
    false_alarms: list = field(default_factory=lambda: [0, 0])   # [removed, total]
    correct: list = field(default_factory=lambda: [0, 0])        # [kept, total]
    wrong: list = field(default_factory=lambda: [0, 0])          # [fixed, total]
    missed: list = field(default_factory=lambda: [0, 0])         # [found, total]
    unreviewed: list = field(default_factory=lambda: [0, 0])     # [still detected, total]
    model_runs: int = 0
    zoom_runs: int = 0
    outcomes: dict = field(default_factory=dict)                 # id -> short text

    def line(self) -> str:
        f = lambda a: f"{a[0]}/{a[1]}" if a[1] else "-"  # noqa: E731
        return (f"{self.name:14s} false alarms removed {f(self.false_alarms):>7s} | correct kept {f(self.correct):>7s} | "
                f"wrong objects fixed {f(self.wrong):>7s} | misses found {f(self.missed):>7s} | "
                f"unreviewed still detected {f(self.unreviewed):>7s} | model runs {self.model_runs} (+{self.zoom_runs} zoom)")


def _settings(cam: dict, overrides: dict) -> DetectSettings:
    d = cam.get("detect", {})
    base = DetectSettings(fps=5.0, labels=tuple(d.get("labels") or DetectSettings().labels),
                          min_score=d.get("min_score", 0.2))
    return dataclasses.replace(base, **overrides)


def _produced(res, start: float, end: float) -> list:
    return [e for e in (res.event_list or []) if e.start_ts <= end + SLACK and (e.end_ts or e.start_ts) >= start - SLACK]


def evaluate_testset(zip_path: Path, model, variants: dict[str, dict]) -> list[Score]:
    scores = [Score(n) for n in variants]
    with tempfile.TemporaryDirectory() as tmp, zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
        root = Path(tmp)
        m = json.loads((root / "manifest.json").read_text())
        cams = m.get("cameras", {})
        for ev in m.get("events", []):
            if not ev.get("clip"):
                continue
            cam = cams.get(ev["camera"], {})
            verdict = (ev.get("feedback") or {}).get("verdict")
            right = (ev.get("feedback") or {}).get("label")
            end = ev["end_ts"] or ev["start_ts"] + 10
            for sc, (name, ov) in zip(scores, variants.items()):
                res = evaluate_clip(root / ev["clip"], model, fps=5.0, settings=_settings(cam, ov),
                                    start_offset=ev.get("clip_start_ts", ev["start_ts"] - 5), keep_events=True,
                                    motion_masks=[tuple(map(tuple, p)) for p in cam.get("motion_masks", [])],
                                    object_masks=[tuple(map(tuple, p)) for p in cam.get("object_masks", [])])
                sc.model_runs += res.model_runs
                sc.zoom_runs += res.zoom_runs
                got = _produced(res, ev["start_ts"], end)
                kinds = {e.label for e in got}
                same = any(group_of(lbl) == group_of(ev["label"]) for lbl in kinds)
                if verdict == "false_alarm":
                    sc.false_alarms[1] += 1
                    sc.false_alarms[0] += not same
                elif verdict == "correct":
                    sc.correct[1] += 1
                    sc.correct[0] += same
                elif verdict == "wrong_label":
                    sc.wrong[1] += 1
                    fixed = (right in kinds) if right and right != "other" else not any(
                        e.label == ev["label"] for e in got)
                    sc.wrong[0] += fixed
                else:
                    sc.unreviewed[1] += 1
                    sc.unreviewed[0] += same
                sc.outcomes[ev["id"]] = ",".join(sorted(kinds)) or "nothing"
        for miss in m.get("missed", []):
            if not miss.get("clip"):
                continue
            cam = cams.get(miss["camera"], {})
            for sc, (name, ov) in zip(scores, variants.items()):
                res = evaluate_clip(root / miss["clip"], model, fps=5.0, settings=_settings(cam, ov),
                                    start_offset=miss.get("clip_start_ts", miss["ts"] - 10), keep_events=True)
                sc.model_runs += res.model_runs
                sc.zoom_runs += res.zoom_runs
                kinds = {e.label for e in res.event_list or []}
                found = miss["label"] == "other" and bool(kinds) or any(
                    group_of(k) == group_of(miss["label"]) for k in kinds)
                sc.missed[1] += 1
                sc.missed[0] += found
                sc.outcomes[miss["id"]] = ",".join(sorted(kinds)) or "nothing"
    return scores
