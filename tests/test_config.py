"""Allow-lists and settings: what the model may do, and where state lives."""

from __future__ import annotations

import json
import stat
from typing import get_args

import pytest
from androidtvremote2.remotemessage_pb2 import RemoteKeyCode

from shieldtv_mcp.config import (
    ALLOWED_KEYS,
    DEFAULT_APPS,
    App,
    KeyName,
    ensure_private_dir,
    load_settings,
    save_host,
)


def test_allow_list_matches_schema_enum():
    assert set(get_args(KeyName)) == ALLOWED_KEYS


@pytest.mark.parametrize("key", sorted(ALLOWED_KEYS))
def test_every_allowed_key_exists_in_the_protocol(key):
    # A typo here would only surface on real hardware, as a ValueError from
    # the library. Check against the protocol's own enum instead.
    assert f"KEYCODE_{key}" in RemoteKeyCode.keys()  # noqa: SIM118 (protobuf enum, not a dict)


@pytest.mark.parametrize("key", ["POWER", "SEARCH", "SETTINGS", "MUTE", "ASSIST", "VOICE_ASSIST"])
def test_risky_keys_are_not_allowed(key):
    assert key not in ALLOWED_KEYS


def test_speaker_mute_is_volume_mute():
    # KEYCODE_MUTE (91) mutes the *microphone*; KEYCODE_VOLUME_MUTE (164) the sound.
    assert "VOLUME_MUTE" in ALLOWED_KEYS


def test_default_apps_launch_by_link():
    # A bare package becomes market://launch?id=..., which the Shield rejects.
    for app in DEFAULT_APPS.values():
        assert app.target.startswith("https://") and app.package


def test_user_apps_merge_and_reverse_lookup(settings):
    assert settings.resolve_app(" Netflix ") == DEFAULT_APPS["netflix"]
    assert settings.resolve_app("mine") == App("com.example.mine", "com.example.mine")  # "Mine" lowercased
    assert settings.resolve_app("nope") is None
    assert settings.app_name_for("com.netflix.ninja") == "netflix"
    assert settings.app_name_for("com.example.mine") == "mine"
    assert settings.app_name_for("com.unknown") is None
    assert settings.app_name_for(None) is None


def test_user_app_overrides_default(config_dir):
    (config_dir / "config.json").write_text(json.dumps({"apps": {"netflix": "com.example.other"}}))
    assert load_settings().resolve_app("netflix") == App("com.example.other", "com.example.other")


def test_user_app_forms(config_dir):
    apps = {
        "link": "https://example.com/watch",
        "both": {"link": "https://example.com/tv", "package": "com.example.tv"},
        "bad": 42,
        "no-link": {"package": "com.example.x"},
    }
    (config_dir / "config.json").write_text(json.dumps({"apps": apps}))
    s = load_settings()
    assert s.resolve_app("link") == App("https://example.com/watch", None)  # package unknown
    assert s.resolve_app("both") == App("https://example.com/tv", "com.example.tv")
    assert s.resolve_app("bad") is None and s.resolve_app("no-link") is None


def test_host_precedence(config_dir, monkeypatch):
    assert load_settings().host == "192.0.2.10"  # config.json
    monkeypatch.setenv("SHIELDTV_HOST", "192.0.2.20")
    assert load_settings().host == "192.0.2.20"  # env beats config.json
    assert load_settings(host_override="192.0.2.30").host == "192.0.2.30"  # --host beats env


def test_broken_config_json_falls_back_to_defaults(config_dir):
    (config_dir / "config.json").write_text("{not json")
    s = load_settings()
    assert s.host is None and not s.paired
    assert s.apps == DEFAULT_APPS


def test_paired_needs_host_cert_and_key(config_dir):
    assert load_settings().paired
    (config_dir / "key.pem").unlink()
    assert not load_settings().paired


def test_save_host_keeps_files_private(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("SHIELDTV_CONFIG_DIR", str(d))
    monkeypatch.delenv("SHIELDTV_HOST", raising=False)
    save_host("192.0.2.40")
    assert load_settings().host == "192.0.2.40"
    assert stat.S_IMODE(ensure_private_dir().stat().st_mode) == 0o700
    assert stat.S_IMODE((d / "config.json").stat().st_mode) == 0o600
