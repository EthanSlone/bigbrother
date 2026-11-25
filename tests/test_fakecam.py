from pathlib import Path

import yaml

from bigbrother.config import direct_url, input_args, parse_config
from bigbrother.fakecam import build_fakecam_config, camera_id_for, cameras_yaml, write_fakecam


def test_camera_ids_are_valid_and_unique(tmp_path):
    for name in ["Front Door.mp4", "front-door.mkv", "2nd floor.mov", "garage_sub.mp4", "notes.txt"]:
        (tmp_path / name).write_bytes(b"x")
    videos, cfg_path, cams_path = write_fakecam(tmp_path, tmp_path / "config")
    assert list(videos) == ["cam_2nd_floor", "front_door", "front_door_2", "garage_cam"]
    # The generated cameras.yml must pass our own validation.
    cfg = parse_config(yaml.safe_load(cams_path.read_text()))
    assert set(cfg.cameras) == set(videos)
    assert cfg.cameras["front_door"].sub.endswith("@fakecam:8554/front_door_sub")


def test_existing_cameras_yml_is_not_overwritten(tmp_path):
    (tmp_path / "a.mp4").write_bytes(b"x")
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "cameras.yml").write_text("mine")
    _, _, cams_path = write_fakecam(tmp_path, cfg_dir)
    assert cams_path is None
    assert (cfg_dir / "cameras.yml").read_text() == "mine"


def test_fakecam_config_loops_and_requires_login():
    cfg = build_fakecam_config({"front_door": Path("Front Door.mp4")})
    main = cfg["streams"]["front_door_main"]
    assert main.startswith("exec:ffmpeg") and "-stream_loop -1" in main
    assert '"/testdata/Front Door.mp4"' in main and main.endswith("{output}")
    assert "scale=640:-2" in cfg["streams"]["front_door_sub"]
    assert cfg["rtsp"]["password"] == "${CAM_PASSWORD}"


def test_hls_sources_supported():
    parse_config({"cameras": {"traffic": {"streams": {"main": "ffmpeg:https://x/live.m3u8#video=copy"}}}})
    assert direct_url("ffmpeg:https://x/live.m3u8#video=copy") == "https://x/live.m3u8"
    assert "-rtsp_transport" not in input_args("https://x/live.m3u8")
    assert "-rtsp_transport" in input_args("rtsp://h/1")


def test_cameras_yaml_has_no_literal_secrets():
    text = cameras_yaml({"a": Path("a.mp4")})
    assert "${CAM_PASSWORD}" in text
