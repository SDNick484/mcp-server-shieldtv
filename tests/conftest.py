"""Shared fixtures. Tests run against a fake remote: no Shield (or network) needed."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import pytest
from androidtvremote2 import ConnectionClosed, DeviceInfo, VolumeInfo

from shieldtv_mcp.client import ShieldClient
from shieldtv_mcp.config import Settings, load_settings


# Async tests use anyio's pytest plugin (marked with pytest.mark.anyio) rather
# than pytest-asyncio: it runs an async fixture's setup and teardown in the
# same task, which the MCP SDK's in-process Client (built on anyio) requires.
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeRemote:
    """Stands in for androidtvremote2.AndroidTVRemote.

    It mirrors the parts of the real API the client uses, including the data
    shapes: volume and device info are dicts (TypedDicts) in the library, so
    they are dicts here too. A fake that is friendlier than the real thing
    hides bugs.
    """

    def __init__(self) -> None:
        self.built_with: tuple[str, str, str, str] | None = None
        self.connect_errors: list[Exception] = []  # raised by async_connect, in order
        self.connect_calls = 0
        self.closed = False  # True makes commands raise ConnectionClosed
        self.keys: list[str] = []
        self.launched: list[str] = []
        # What the "Shield" does with a launch target, like the real one does
        # after a moment: open a package, or "reject" (error, then the
        # connection drops and comes back). Unlisted targets open nothing.
        self.launch_outcomes: dict[str, str] = {}
        self.ignore_power = False  # True: WAKEUP/SLEEP change nothing
        self.reconnecting = False
        self.invalid_auth_callback: Callable[[], None] | None = None
        self.disconnected = False

        self.is_on: bool | None = True
        self.current_app: str | None = "com.netflix.ninja"
        self.volume_info: VolumeInfo | None = {"level": 10, "max": 100, "muted": False}
        self.device_info: DeviceInfo | None = {
            "manufacturer": "NVIDIA",
            "model": "SHIELD Android TV",
            "sw_version": "11",
        }
        self._callbacks: dict[str, list[Callable]] = {
            "is_on": [],
            "current_app": [],
            "volume_info": [],
            "is_available": [],
        }

    # Passed to ShieldClient as remote_factory; records how it was built.
    def build(self, client_name: str, certfile: str, keyfile: str, host: str, **ports: int) -> FakeRemote:
        self.built_with = (client_name, certfile, keyfile, host)
        self.ports = ports
        return self

    def add_is_on_updated_callback(self, cb: Callable) -> None:
        self._callbacks["is_on"].append(cb)

    def add_current_app_updated_callback(self, cb: Callable) -> None:
        self._callbacks["current_app"].append(cb)

    def add_volume_info_updated_callback(self, cb: Callable) -> None:
        self._callbacks["volume_info"].append(cb)

    def add_is_available_updated_callback(self, cb: Callable) -> None:
        self._callbacks["is_available"].append(cb)

    def push(self, kind: str, value: object) -> None:
        """Simulate the Shield pushing a state change."""
        for cb in self._callbacks[kind]:
            cb(value)

    async def async_connect(self) -> None:
        self.connect_calls += 1
        if self.connect_errors:
            raise self.connect_errors.pop(0)

    def keep_reconnecting(self, invalid_auth_callback: Callable[[], None] | None = None) -> None:
        self.reconnecting = True
        self.invalid_auth_callback = invalid_auth_callback

    def send_key_command(self, key_code: str, direction: str = "SHORT") -> None:
        if self.closed:
            raise ConnectionClosed("closed")
        self.keys.append(key_code)
        # The real Shield pushes its new power state right after these.
        if key_code in ("WAKEUP", "SLEEP") and not self.ignore_power:
            asyncio.get_running_loop().call_soon(self.push, "is_on", key_code == "WAKEUP")

    def send_launch_app_command(self, app_link_or_app_id: str) -> None:
        if self.closed:
            raise ConnectionClosed("closed")
        self.launched.append(app_link_or_app_id)
        outcome = self.launch_outcomes.get(app_link_or_app_id)
        loop = asyncio.get_running_loop()
        if outcome == "reject":
            loop.call_soon(self.push, "is_available", False)
            loop.call_soon(self.push, "is_available", True)
        elif outcome:
            loop.call_soon(self.push, "current_app", outcome)

    def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture(autouse=True)
def fast_launch(monkeypatch):
    """Keep launch and power confirmation waits short; the fake answers at once."""
    monkeypatch.setattr(ShieldClient, "launch_timeout", 0.2)
    monkeypatch.setattr(ShieldClient, "power_timeout", 0.2)
    monkeypatch.setattr(ShieldClient, "poll_interval", 0.01)


@pytest.fixture
def fake() -> FakeRemote:
    return FakeRemote()


@pytest.fixture(scope="session")
def client_identity() -> tuple[bytes, bytes]:
    """A real client certificate and key, made the way `pair` makes them (an
    RSA key takes a moment to generate, so once per test session). They must
    be real: the client now checks they load before connecting."""
    from androidtvremote2.certificate_generator import generate_selfsigned_cert

    return generate_selfsigned_cert("mcp-server-shieldtv")


@pytest.fixture
def config_dir(tmp_path, monkeypatch, client_identity):
    """An isolated, paired config directory (never the user's real one)."""
    monkeypatch.setenv("SHIELDTV_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SHIELDTV_HOST", raising=False)
    monkeypatch.delenv("SHIELDTV_DRY_RUN", raising=False)
    (tmp_path / "cert.pem").write_bytes(client_identity[0])
    (tmp_path / "key.pem").write_bytes(client_identity[1])
    (tmp_path / "config.json").write_text(json.dumps({"host": "192.0.2.10", "apps": {"Mine": "com.example.mine"}}))
    return tmp_path


@pytest.fixture
def settings(config_dir) -> Settings:
    return load_settings()


async def wait_until(condition: Callable[[], bool], tries: int = 100) -> None:
    """Let background tasks run until condition() holds (or fail)."""
    for _ in range(tries):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never became true")
