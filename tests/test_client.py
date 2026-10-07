"""ShieldClient: connection lifecycle, pushed state, and error mapping."""

from __future__ import annotations

import asyncio

import pytest
from androidtvremote2 import CannotConnect, InvalidAuth
from mcp.server.mcpserver.exceptions import ToolError

from shieldtv_mcp import client as client_mod
from shieldtv_mcp.client import ShieldClient, ShieldError
from shieldtv_mcp.config import App, load_settings

from .conftest import wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def connected(settings, fake):
    c = ShieldClient(settings, remote_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    yield c
    await c.stop()


def test_errors_are_tool_errors_so_the_model_sees_the_message():
    assert issubclass(ShieldError, ToolError)


# --- lifecycle --------------------------------------------------------------
async def test_connect_builds_remote_from_settings(connected, fake, config_dir):
    assert fake.built_with == (
        "mcp-server-shieldtv",
        str(config_dir / "cert.pem"),
        str(config_dir / "key.pem"),
        "192.0.2.10",
    )
    assert fake.reconnecting  # the library takes over reconnects after the first
    assert connected.is_on is True
    assert connected.current_app == "com.netflix.ninja"


async def test_retries_with_backoff_until_reachable(settings, fake, monkeypatch):
    fake.connect_errors = [CannotConnect(), OSError("no route"), TimeoutError()]
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def fast_sleep(delay):
        delays.append(delay)
        await real_sleep(0)

    monkeypatch.setattr(client_mod.asyncio, "sleep", fast_sleep)
    c = ShieldClient(settings, remote_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    assert fake.connect_calls == 4
    assert [d for d in delays if d] == [1.0, 2.0, 4.0]
    await c.stop()


async def test_rejected_certificate_stops_retrying(settings, fake):
    fake.connect_errors = [InvalidAuth()]
    c = ShieldClient(settings, remote_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.auth_failed)
    assert fake.connect_calls == 1
    with pytest.raises(ShieldError, match="Re-run `mcp-server-shieldtv pair`"):
        c.send_key("HOME")
    await c.stop()


async def test_rejected_later_by_reconnect(connected, fake):
    fake.invalid_auth_callback()
    with pytest.raises(ShieldError, match="rejected our pairing"):
        connected.send_key("HOME")


async def test_unexpected_crash_is_logged(settings, fake, caplog):
    fake.connect_errors = [RuntimeError("boom")]
    c = ShieldClient(settings, remote_factory=fake.build)
    await c.start()
    await wait_until(lambda: c._task.done())
    assert "connection task crashed" in caplog.text
    await c.stop()


async def test_start_without_pairing_does_nothing(config_dir, fake):
    (config_dir / "key.pem").unlink()
    c = ShieldClient(load_settings(), remote_factory=fake.build)
    await c.start()
    assert fake.built_with is None
    with pytest.raises(ShieldError, match="Not paired"):
        c.send_key("HOME")
    await c.stop()


async def test_stop_disconnects(settings, fake):
    c = ShieldClient(settings, remote_factory=fake.build)
    await c.start()
    await wait_until(lambda: c.available)
    await c.stop()
    assert fake.disconnected and not c.available


# --- pushed state -----------------------------------------------------------
async def test_pushed_state_updates_snapshot(connected, fake):
    fake.push("is_on", False)
    fake.push("current_app", "com.example.mine")
    fake.push("volume_info", {"level": 3, "max": 15, "muted": True})
    snap = connected.snapshot()
    assert snap["power"] == "standby"
    assert (snap["current_app"], snap["current_app_package"]) == ("mine", "com.example.mine")
    assert snap["volume"] == {"level": 3, "max": 15, "muted": True}


async def test_volume_handed_to_cec_is_unknown_not_zero(connected, fake):
    # Seen on a real Shield whose volume goes to an AV receiver over HDMI-CEC:
    # the volume message arrives with no level or max at all.
    fake.push("volume_info", {"level": 0, "max": 0, "muted": False})
    assert connected.snapshot()["volume"] is None


async def test_unreachable_while_reconnecting(connected, fake):
    fake.push("is_available", False)
    assert connected.snapshot()["reachable"] is False
    with pytest.raises(ShieldError, match="Can't reach the Shield at 192.0.2.10"):
        connected.send_key("HOME")
    fake.push("is_available", True)
    connected.send_key("HOME")
    assert fake.keys == ["HOME"]


def test_snapshot_before_connecting(settings):
    snap = ShieldClient(settings).snapshot()
    assert snap == {
        "host": "192.0.2.10",
        "paired": True,
        "reachable": False,
        "power": None,
        "current_app_package": None,
        "current_app": None,
        "volume": None,
        "device": None,
    }


# --- commands ---------------------------------------------------------------
async def test_send_key_and_power(connected, fake):
    connected.send_key("HOME")
    await connected.set_power(True)
    await connected.set_power(False)
    assert fake.keys == ["HOME", "WAKEUP", "SLEEP"]
    assert connected.is_on is False


async def test_power_change_the_shield_never_reports_is_an_error(connected, fake):
    fake.ignore_power = True
    with pytest.raises(ShieldError, match="didn't report the change.*still reports on"):
        await connected.set_power(False)


@pytest.mark.parametrize("key", ["POWER", "SEARCH", "MUTE", "KEYCODE_POWER", "26", "text:hello", "home"])
async def test_keys_outside_allow_list_never_reach_the_shield(connected, fake, key):
    # "text:" would make the library type text; "home" is lowercase, and the
    # library is case-insensitive, but the allow-list is deliberately exact.
    with pytest.raises(ShieldError, match="not allowed"):
        connected.send_key(key)
    assert fake.keys == []


NETFLIX = App("https://www.netflix.com/title", "com.netflix.ninja")
YOUTUBE = App("https://www.youtube.com", "com.google.android.youtube.tv")


async def test_launch_confirms_the_app_is_in_front(connected, fake):
    fake.push("current_app", "com.google.android.tvlauncher")
    fake.launch_outcomes[YOUTUBE.target] = YOUTUBE.package
    assert await connected.launch(YOUTUBE) == YOUTUBE.package
    assert fake.launched == [YOUTUBE.target]


async def test_launch_of_the_app_already_in_front_succeeds(connected, fake):
    # The fake starts with Netflix in front; relaunching changes nothing.
    assert await connected.launch(NETFLIX) == "com.netflix.ninja"


async def test_launch_without_known_package_accepts_any_new_foreground_app(connected, fake):
    fake.launch_outcomes["https://example.com/x"] = "com.example.x"
    assert await connected.launch(App("https://example.com/x")) == "com.example.x"


async def test_returning_to_the_home_screen_is_not_a_launch(connected, fake):
    # Real Shield: HOME, then a link with no app to handle it. The launcher's
    # late "now in front" push must not be mistaken for the app opening.
    fake.launch_outcomes["https://example.com/x"] = "com.google.android.tvlauncher"
    with pytest.raises(ShieldError, match="foreground app didn't change"):
        await connected.launch(App("https://example.com/x"))


async def test_rejected_launch_is_an_error_not_a_success(connected, fake):
    # Real Shield: market://launch?id=... gets a remote_error, then the
    # connection drops and the library reconnects ~0.1s later.
    fake.launch_outcomes["com.netflix.ninja"] = "reject"
    with pytest.raises(ShieldError, match="rejected the launch request"):
        await connected.launch(App("com.netflix.ninja", "com.netflix.ninja"))
    assert connected.drops == 1 and connected.available


async def test_launch_that_opens_nothing_is_an_error(connected, fake):
    # Real Shield: an accepted link with no app to handle it shows a dialog,
    # and the foreground app never changes.
    with pytest.raises(ShieldError, match="foreground app didn't change.*still com.netflix.ninja"):
        await connected.launch(YOUTUBE)


async def test_dropped_connection_is_a_tool_error(connected, fake):
    fake.closed = True
    with pytest.raises(ShieldError, match="connection to the Shield dropped"):
        connected.send_key("HOME")
