"""ADB now-playing: parsing real `dumpsys media_session` output, and the guard
that only the one fixed command ever reaches the Shield."""

from __future__ import annotations

import dataclasses

import pytest
from adb_shell.exceptions import DeviceAuthError, TcpTimeoutException

from shieldtv_mcp import adb
from shieldtv_mcp.adb import NOW_PLAYING_COMMAND, now_playing, parse_sessions, read_now_playing
from shieldtv_mcp.client import ShieldError

pytestmark = pytest.mark.anyio

# Trimmed from a real Shield (2026-10). Session headers are indented 4 spaces,
# their fields 6; the stack ends at the first line indented less.
IDLE_SLING = """\
    PlayerManager com.sling/PlayerManager (userId=0)
      package=com.sling
      active=false
      state=null
      metadata: null
      queueTitle=null, size=0"""

YOUTUBE_PLAYING = """\
    starboard com.google.android.youtube.tv/starboard (userId=0)
      package=com.google.android.youtube.tv
      active=true
      controllers: 7
      state=PlaybackState {state=3, position=1382, buffered position=0, speed=1.0, updated=1576053, actions=379, \
custom actions=[], active item id=-1, error=null}
      volumeType=1, controlType=2, max=0, current=0
      metadata: size=5, description=Kids (Official HD Video), MGMT, null
      queueTitle=null, size=0"""

YOUTUBE_TV_LIVE = """\
    starboard com.google.android.youtube.tvunplugged/starboard (userId=0)
      package=com.google.android.youtube.tvunplugged
      active=true
      state=PlaybackState {state=3, position=46790522, buffered position=0, speed=1.0, updated=1392341, \
actions=379, custom actions=[], active item id=-1, error=null}
      metadata: size=5, description=KTVU Mornings on 2, FOX 2, null"""


def dump(*sessions: str, uptime: str = "1598.10 3000.00") -> str:
    return (
        "MEDIA SESSION SERVICE (dumpsys media_session)\n"
        "  Media button session is com.google.android.youtube.tv/starboard (userId=0)\n"
        f"  Sessions Stack - have {len(sessions)} sessions:\n"
        + "\n".join(sessions)
        + "\nAudio playback (lastly played comes first)\n"
        "  uid=10097 packages=com.google.android.youtube.tv\n"
        f"__UPTIME__\n{uptime}\n"
    )


def names(package: str | None) -> str | None:
    return {"com.google.android.youtube.tv": "youtube"}.get(package or "")


# --- parsing ------------------------------------------------------------------
def test_parse_sessions_in_stack_order():
    sessions = parse_sessions(dump(YOUTUBE_PLAYING, IDLE_SLING))
    assert [s.package for s in sessions] == ["com.google.android.youtube.tv", "com.sling"]
    yt, sling = sessions
    assert (yt.active, yt.state, yt.position_ms, yt.speed, yt.updated_ms) == (True, 3, 1382, 1.0, 1576053)
    assert (yt.title, yt.subtitle) == ("Kids (Official HD Video)", "MGMT")
    assert (sling.active, sling.state, sling.title) == (False, None, None)


def test_playing_position_is_advanced_to_now():
    # Snapshot 1.382s taken at uptime 1576.053s; uptime is now 1598.100s.
    assert now_playing(dump(YOUTUBE_PLAYING, IDLE_SLING), names) == {
        "state": "playing",
        "app_package": "com.google.android.youtube.tv",
        "app": "youtube",
        "title": "Kids (Official HD Video)",
        "subtitle": "MGMT",
        "position_s": 23.4,
    }


def test_paused_position_stays_put():
    paused = YOUTUBE_PLAYING.replace("state=3,", "state=2,")
    result = now_playing(dump(paused), names)
    assert (result["state"], result["position_s"]) == ("paused", 1.4)


def test_playing_beats_paused_lower_in_the_stack():
    paused = YOUTUBE_PLAYING.replace("state=3,", "state=2,")
    assert now_playing(dump(paused, YOUTUBE_TV_LIVE), names)["app_package"] == "com.google.android.youtube.tvunplugged"


def test_live_tv_has_no_position():
    result = now_playing(dump(YOUTUBE_TV_LIVE), names)
    assert (result["title"], result["subtitle"], result["position_s"]) == ("KTVU Mornings on 2", "FOX 2", None)


def test_nothing_playing_is_idle():
    assert now_playing(dump(IDLE_SLING), names) == {
        "state": "idle",
        "app_package": None,
        "app": None,
        "title": None,
        "subtitle": None,
        "position_s": None,
    }


def test_title_with_a_comma_keeps_the_artist():
    # The description's separator is ambiguous; the parser splits from the right.
    session = YOUTUBE_PLAYING.replace("Kids (Official HD Video), MGMT", "Hello, Goodbye, The Beatles")
    result = now_playing(dump(session), names)
    assert (result["title"], result["subtitle"]) == ("Hello, Goodbye", "The Beatles")


def test_missing_uptime_reports_the_snapshot():
    assert now_playing(dump(YOUTUBE_PLAYING, uptime=""), names)["position_s"] == 1.4


# --- talking to the Shield -------------------------------------------------------
class FakeDevice:
    """Stands in for adb_shell's AdbDeviceTcpAsync; records every shell command."""

    def __init__(self, output: str = "", connect_error: Exception | None = None) -> None:
        self.output = output
        self.connect_error = connect_error
        self.commands: list[str] = []
        self.built_with: tuple[object, ...] = ()
        self.closed = False

    def build(self, host: str, port: int, **kwargs: object) -> FakeDevice:
        self.built_with = (host, port)
        return self

    async def connect(self, rsa_keys: list[object], auth_timeout_s: float) -> bool:
        if self.connect_error:
            raise self.connect_error
        return True

    async def shell(self, command: str, read_timeout_s: float) -> str:
        self.commands.append(command)
        return self.output

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def adb_settings(settings, config_dir):
    s = dataclasses.replace(settings, adb=True)
    adb.ensure_adb_key(s)
    return s


async def test_reads_with_the_one_fixed_command(adb_settings):
    device = FakeDevice(dump(YOUTUBE_PLAYING))
    result = await read_now_playing(adb_settings, device_factory=device.build)
    assert result["title"] == "Kids (Official HD Video)"
    assert device.built_with == ("192.0.2.10", 5555)
    assert device.commands == [NOW_PLAYING_COMMAND]
    assert device.closed


def test_adb_key_is_private(adb_settings):
    key = adb_settings.adb_key_path
    for path in (key, key.with_name(key.name + ".pub")):
        assert path.stat().st_mode & 0o777 == 0o600


async def test_adb_off_explains_how_to_enable(settings):
    with pytest.raises(ShieldError, match="adb-setup"):
        await read_now_playing(settings, device_factory=FakeDevice().build)


@pytest.mark.parametrize(
    ("error", "message"),
    [(DeviceAuthError("no"), "didn't accept our ADB key"), (TcpTimeoutException("slow"), "Network debugging")],
)
async def test_adb_failures_are_tool_errors(adb_settings, error, message):
    device = FakeDevice(connect_error=error)
    with pytest.raises(ShieldError, match=message):
        await read_now_playing(adb_settings, device_factory=device.build)
    assert device.commands == [] and device.closed
