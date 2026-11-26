"""Command line entry point.

    python -m bigbrother configure   validate cameras.yml, check .env, write go2rtc.yaml
    python -m bigbrother probe       report codec/resolution/fps/keyframes for every stream
    python -m bigbrother fakecam     turn clips in testdata/ into fake RTSP cameras
    python -m bigbrother recorder    run the recording service
    python -m bigbrother detector    run the detection service (+ debug viewer)
    python -m bigbrother alerter     run the alert service (rules, second opinion, Pushover)
    python -m bigbrother test-alert  send one test notification
    python -m bigbrother models      download/verify built-in detection models
    python -m bigbrother evaluate    run clips through detection offline and count events
    python -m bigbrother web         run the web app (login, live view, events, recordings)
    python -m bigbrother user ...    manage app accounts: list | add NAME | passwd NAME | delete NAME
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, ConfigError, load_config


def _configure(args) -> int:
    from .go2rtc import write_go2rtc_config

    cfg = load_config(args.config)
    missing = sorted(v for v in cfg.env_refs() if v not in os.environ)
    if missing:
        print(f"ERROR: cameras.yml references variables missing from .env: {', '.join(missing)}")
        return 1
    path = write_go2rtc_config(cfg, Path(args.out))
    print(f"OK: {len(cfg.cameras)} camera(s) valid")
    for cam in cfg.cameras.values():
        rec = "on" if cam.record.enabled else "off"
        warn = "" if cam.sub else "   (no substream: detection will need one)"
        print(f"  - {cam.id:<16} {cam.name:<20} substream: {'yes' if cam.sub else 'NO':<4} "
              f"recording: {rec}{warn}")
    print(f"Wrote {path}")
    return 0


def _probe(args) -> int:
    from .probe import NO_SUB, probe_all, render_markdown, reports_json

    cfg = load_config(args.config)
    print(f"Probing {len(cfg.cameras)} camera(s), ~{args.seconds}s per stream...\n", flush=True)
    reports = probe_all(cfg, seconds=args.seconds)
    md = render_markdown(reports)
    print(md)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "probe-report.md").write_text("# Camera probe report\n\n" + md, encoding="utf-8")
    (out / "probe-report.json").write_text(reports_json(reports), encoding="utf-8")
    print(f"Saved {out / 'probe-report.md'}")
    return 0 if all(r.ok for r in reports if r.error != NO_SUB) else 2


def _fakecam(args) -> int:
    from .fakecam import cameras_yaml, write_fakecam

    testdata, config_dir = Path(args.testdata), Path(args.config).parent
    videos, cfg_path, cams_path = write_fakecam(testdata, config_dir)
    if not videos:
        print(f"No video files found in {testdata}. Put .mp4/.mkv/.mov clips in testdata/ first.")
        return 1
    print(f"{len(videos)} fake camera(s):")
    for cid, p in videos.items():
        print(f"  - {cid:<20} <- {p.name}")
    print(f"Wrote {cfg_path}")
    if cams_path:
        print(f"Wrote {cams_path} pointing at the fake cameras.")
    else:
        print(f"\n{config_dir / 'cameras.yml'} already exists, so it was left alone. "
              "To use the fake cameras, replace its cameras: section with:\n")
        print(cameras_yaml(videos))
    print("Next: docker compose run --rm tools configure, then "
          "docker compose --profile fakecam up -d")
    return 0


def _detector(args) -> int:
    from .detector.service import run

    cfg = load_config(args.config)
    run(cfg, Path(args.data), Path(args.db), args.viewer_port, Path(args.recordings))
    return 0


def _evaluate(args) -> int:
    from .config import DetectorSettings, DetectSettings
    from .detector.evaluate import evaluate_clip
    from .detector.model_store import resolve_model
    from .detector.models import load_model, plan_threads

    path, family = resolve_model(args.model)
    _, threads = plan_threads(1, 0)
    model = load_model(family or "yolox", path, args.backend, "", threads, DetectorSettings.input_size)
    for clip in args.clips:
        for fps in args.fps:
            r = evaluate_clip(Path(clip), model, fps=fps, settings=DetectSettings(fps=fps))
            print(r.summary(), flush=True)
    return 0


def _alerter(args) -> int:
    from .alerts.service import run

    cfg = load_config(args.config)
    run(cfg, Path(args.data), Path(args.db))
    return 0


def _test_alert(args) -> int:
    from .alerts.service import send_test

    return send_test(load_config(args.config))


def _models(args) -> int:
    from .detector.model_store import BUILTIN, resolve_model

    names = args.names or ["yolox_s"]
    for name in names:
        if name not in BUILTIN:
            print(f"unknown model {name}; built in: {', '.join(BUILTIN)}")
            return 1
        path, _ = resolve_model(name)
        print(f"{name}: {path} ({BUILTIN[name].note})")
    return 0


def _recorder(args) -> int:
    from .recorder.service import run

    cfg = load_config(args.config)
    run(cfg, Path(args.recordings), Path(args.db))
    return 0


def _web(args) -> int:
    from .web.app import run

    cfg = load_config(args.config)
    run(cfg, Path(args.data), Path(args.db), Path(args.recordings), args.port, Path(args.config))
    return 0


def _read_password(prompt: str) -> str:
    import getpass

    if not sys.stdin.isatty():  # e.g. piped: first line is the password
        return sys.stdin.readline().rstrip("\n")
    first = getpass.getpass(prompt)
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("Passwords don't match.")
    return first


def _user(args) -> int:
    from .web.auth import AuthError, UserStore

    users = UserStore(Path(args.data) / "web.db")
    try:
        if args.action == "list":
            for u in users.list():
                print(u.username)
            return 0
        if not args.name:
            print("Give a username.")
            return 1
        if args.action == "add":
            users.add(args.name, _read_password(f"Password for {args.name}: "))
            print(f"Added {args.name}.")
        elif args.action == "passwd":
            users.set_password(args.name, _read_password(f"New password for {args.name}: "))
            print(f"Password changed; {args.name} is signed out everywhere.")
        elif args.action == "delete":
            print(f"Deleted {args.name}." if users.delete(args.name) else f"No user {args.name}.")
        return 0
    except AuthError as exc:
        print(f"ERROR: {exc}")
        return 1


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("BB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    p = argparse.ArgumentParser(prog="bigbrother")
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("configure", help="validate config and generate go2rtc.yaml")
    c.add_argument("--out", default="/config/generated")
    c.set_defaults(func=_configure)

    pr = sub.add_parser("probe", help="inspect every camera stream")
    pr.add_argument("--seconds", type=int, default=10)
    pr.add_argument("--out", default="/config/generated")
    pr.set_defaults(func=_probe)

    f = sub.add_parser("fakecam", help="turn video files in testdata/ into fake RTSP cameras")
    f.add_argument("--testdata", default="/testdata")
    f.set_defaults(func=_fakecam)

    d = sub.add_parser("detector", help="run the detection service")
    d.add_argument("--data", default="/data")
    d.add_argument("--db", default="/data/bigbrother.db")
    d.add_argument("--viewer-port", type=int, default=5050, help="debug viewer port (0 = off)")
    d.add_argument("--recordings", default="/recordings", help="recordings folder (read-only, for playback)")
    d.set_defaults(func=_detector)

    al = sub.add_parser("alerter", help="run the alert service")
    al.add_argument("--data", default="/data")
    al.add_argument("--db", default="/data/bigbrother.db")
    al.set_defaults(func=_alerter)

    ta = sub.add_parser("test-alert", help="send one test notification")
    ta.set_defaults(func=_test_alert)

    ev = sub.add_parser("evaluate", help="run clips through detection and count events")
    ev.add_argument("clips", nargs="+")
    ev.add_argument("--fps", type=float, nargs="+", default=[5.0])
    ev.add_argument("--model", default="yolox_s")
    ev.add_argument("--backend", default="openvino")
    ev.set_defaults(func=_evaluate)

    mo = sub.add_parser("models", help="download built-in detection models")
    mo.add_argument("names", nargs="*")
    mo.set_defaults(func=_models)

    r = sub.add_parser("recorder", help="run the recording service")
    r.add_argument("--recordings", default="/recordings")
    r.add_argument("--db", default="/data/bigbrother.db")
    r.set_defaults(func=_recorder)

    w = sub.add_parser("web", help="run the web app")
    w.add_argument("--data", default="/data")
    w.add_argument("--db", default="/data/bigbrother.db")
    w.add_argument("--recordings", default="/recordings")
    w.add_argument("--port", type=int, default=8090)
    w.set_defaults(func=_web)

    us = sub.add_parser("user", help="manage app accounts")
    us.add_argument("action", choices=["list", "add", "passwd", "delete"])
    us.add_argument("name", nargs="?")
    us.add_argument("--data", default="/data")
    us.set_defaults(func=_user)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"CONFIG ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
