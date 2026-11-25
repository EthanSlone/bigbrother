from pathlib import Path

import pytest
import yaml

from bigbrother.config import ConfigError, expand_env, load_config, mask_url, parse_config
from bigbrother.go2rtc import build_go2rtc_config

EXAMPLE = Path(__file__).parent.parent / "config" / "cameras.example.yml"


def cam(main="rtsp://${U}:${P}@10.0.0.1/101", sub="rtsp://${U}:${P}@10.0.0.1/102", **extra):
    streams = {"main": main}
    if sub:
        streams["sub"] = sub
    return {"streams": streams, **extra}


def test_example_config_is_valid():
    cfg = load_config(EXAMPLE)
    assert set(cfg.cameras) == {"front_door", "driveway"}
    assert cfg.cameras["driveway"].record.retention_days == 60
    assert cfg.cameras["front_door"].record.retention_days is None
    assert cfg.env_refs() == {"CAM_USER", "CAM_PASSWORD"}


def test_defaults_and_overrides():
    cfg = parse_config({
        "defaults": {"record": {"segment_seconds": 30, "audio": True}},
        "cameras": {"a": cam(), "b": cam(record={"audio": False})},
    })
    assert cfg.cameras["a"].record.segment_seconds == 30
    assert cfg.cameras["a"].record.audio is True
    assert cfg.cameras["b"].record.audio is False
    assert cfg.cameras["b"].record.segment_seconds == 30
    assert cfg.cameras["a"].name == "A"


@pytest.mark.parametrize("bad", [
    {},
    {"cameras": {}},
    {"cameras": {"Front": cam()}},
    {"cameras": {"x_sub": cam()}},
    {"cameras": {"a": {"streams": {}}}},
    {"cameras": {"a": cam(main="http://x")}},
    {"cameras": {"a": cam(main="rtsp://admin:hunter2@10.0.0.1/101")}},
    {"cameras": {"a": cam(record={"segment_seconds": 2})}},
    {"cameras": {"a": cam(record={"bogus": 1})}},
    {"cameras": {"a": cam()}, "storage": {"max_usage_percent": 100}},
])
def test_invalid_configs_rejected(bad):
    with pytest.raises(ConfigError):
        parse_config(bad)


def test_host_port_not_mistaken_for_password():
    parse_config({"cameras": {"a": cam(main="rtsp://10.0.0.1:554/Streaming/Channels/101")}})


def test_expand_env_and_missing():
    assert expand_env("rtsp://${U}:${P}@h", {"U": "admin", "P": "x%40y"}) == "rtsp://admin:x%40y@h"
    with pytest.raises(ConfigError, match="P"):
        expand_env("rtsp://${U}:${P}@h", {"U": "admin"})


def test_mask_url():
    assert mask_url("rtsp://admin:secret@10.0.0.1:554/101") == "rtsp://admin:****@10.0.0.1:554/101"
    assert mask_url("rtsp://10.0.0.1:554/101") == "rtsp://10.0.0.1:554/101"


def test_go2rtc_config_keeps_env_refs_and_names_streams():
    cfg = parse_config({"cameras": {"front": cam(), "back": cam(sub=None)}})
    out = build_go2rtc_config(cfg)
    assert set(out["streams"]) == {"front", "front_sub", "back"}
    dumped = yaml.safe_dump(out)
    assert "${P}" in dumped  # secrets are expanded by go2rtc, not written to disk
    assert cfg.restream_url("front") == "rtsp://go2rtc:8554/front"
