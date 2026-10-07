"""Tests run against a fake remote: no Shield (or network) needed."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from shieldtv_mcp.client import ShieldClient, ShieldError
from shieldtv_mcp.config import ALLOWED_KEYS, DEFAULT_APPS, KeyName, load_settings
from typing import get_args


class FakeRemote:
    def __init__(self) -> None:
        self.keys: list[str] = []
        self.launched: list[str] = []
        self.device_info = {"manufacturer": "NVIDIA", "model": "SHIELD Android TV", "sw_version": "9"}

    def send_key_command(self, key, direction=3):
        self.keys.append(key)

    def send_launch_app_command(self, target):
        self.launched.append(target)

    def disconnect(self):
        pass


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIELDTV_CONFIG_DIR", str(tmp_path))
    (tmp_path / "cert.pem").write_text("x")
    (tmp_path / "key.pem").write_text("x")
    (tmp_path / "config.json").write_text(json.dumps({"host": "192.0.2.10", "apps": {"Mine": "com.example.mine"}}))
    return load_settings()


@pytest.fixture
def connected(settings):
    c = ShieldClient(settings)
    c._remote = FakeRemote()
    c.available = True
    return c


def test_allow_list_matches_schema_enum():
    assert ALLOWED_KEYS == set(get_args(KeyName))


@pytest.mark.parametrize("dangerous", ["POWER", "SEARCH", "SETTINGS", "KEYCODE_POWER", "26"])
def test_dangerous_keys_not_allowed(connected, dangerous):
    assert dangerous not in ALLOWED_KEYS
    with pytest.raises(ShieldError):
        connected.send_key(dangerous)
    assert connected._remote.keys == []


def test_send_key_and_power(connected):
    connected.send_key("HOME")
    connected.set_power(True)
    connected.set_power(False)
    assert connected._remote.keys == ["HOME", "WAKEUP", "SLEEP"]


def test_user_apps_merge_and_reverse_lookup(settings):
    assert settings.resolve_app(" Netflix ") == DEFAULT_APPS["netflix"]
    assert settings.resolve_app("mine") == "com.example.mine"
    assert settings.resolve_app("nope") is None
    assert settings.app_name_for("com.example.mine") == "mine"


def test_unpaired_and_unreachable_errors(settings, tmp_path):
    c = ShieldClient(settings)  # paired on disk, but not connected yet
    with pytest.raises(ShieldError, match="Can't reach"):
        c.send_key("HOME")
    (tmp_path / "key.pem").unlink()
    with pytest.raises(ShieldError, match="Not paired"):
        ShieldClient(load_settings()).send_key("HOME")


def test_errors_are_tool_errors_so_the_model_sees_the_message():
    from mcp.server.mcpserver.exceptions import ToolError

    assert issubclass(ShieldError, ToolError)


def test_snapshot(connected):
    connected.is_on = True
    connected.current_app = "com.netflix.ninja"
    connected.volume = SimpleNamespace(level=10, max=100, muted=False)
    snap = connected.snapshot()
    assert snap["power"] == "on"
    assert snap["current_app"] == "netflix"
    assert snap["volume"] == {"level": 10, "max": 100, "muted": False}
    assert snap["device"]["manufacturer"] == "NVIDIA"
