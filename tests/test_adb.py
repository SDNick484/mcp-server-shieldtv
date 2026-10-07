"""ADB: parsing real `dumpsys` output (media sessions, Bluetooth remotes), the
reboot flow, and the guard that only fixed commands ever reach the Shield."""

from __future__ import annotations

import dataclasses

import pytest
from adb_shell.exceptions import DeviceAuthError, TcpTimeoutException

from shieldtv_mcp import adb
from shieldtv_mcp.adb import (
    BOOT_COMMAND,
    NOW_PLAYING_COMMAND,
    REMOTES_COMMAND,
    now_playing,
    parse_remotes,
    parse_sessions,
    read_now_playing,
    read_remotes,
    reboot_and_check,
    run_command,
)
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


# --- Bluetooth remotes ------------------------------------------------------------
# Built from a real Shield's dumps (2026-10). Four paired devices: an older
# Shield remote (allowed as input, not connected since boot), earbuds (not an
# input device), a Shield remote, and the Harmony hub ("Harmony Keyboard").
HARMONY, NVIDIA = "00:04:20:FB:44:6B", "48:B0:2D:6B:7D:C5"
BONDED = """\
AdapterProperties
  Bonded devices:
    00:04:4B:F2:89:3A [  LE  ] NVIDIA SHIELD Remote
    74:5C:4B:C4:FC:32 [BR/EDR] Jabra Elite Active 65t
    48:B0:2D:6B:7D:C5 [  LE  ] NVIDIA SHIELD Remote
    00:04:20:FB:44:6B [BR/EDR] Harmony Keyboard
mSnoopLogSettingAtEnable = empty
"""
METADATA = """\
BluetoothDatabase:
  Metadata Changes:

Metadata:
    00:04:20:FB:44:6B {profile connection policy(A2DP=-1|HEADSET=-1|HID_HOST=100|PAN=-1), optional codec(support=-1)}
    48:B0:2D:6B:7D:C5 {profile connection policy(A2DP=-1|HEADSET=-1|HID_HOST=100|PAN=-1), optional codec(support=-1)}
    74:5C:4B:C4:FC:32 {profile connection policy(A2DP=100|HEADSET=-1|HID_HOST=-1|PAN=-1), optional codec(support=-1)}
    00:04:4B:F2:89:3A {profile connection policy(A2DP=-1|HEADSET=-1|HID_HOST=100|PAN=-1), optional codec(support=-1)}
"""
INPUT_DEVICE = """\
    {n}: {name}
      Path: /dev/input/event{n}
      UniqueId: {address}
      Identifier: bus=0x0005, vendor=0xffff, product=0x0000, version=0x0000
"""


def remotes_dump(harmony: str = "working", nvidia: str = "stuck") -> str:
    """State per remote: "working", "stuck" (connected, no input device),
    "disconnected", or "absent" (not connected since Bluetooth started)."""
    hid = {"working": 2, "stuck": 2, "disconnected": 0}
    lines, inputs = [], []
    for n, (address, name, state) in enumerate(
        [(HARMONY, "Harmony Keyboard", harmony), (NVIDIA, "NVIDIA SHIELD Remote", nvidia)], start=14
    ):
        if state in hid:
            lines.append(f"    {address} : {hid[state]}")
        if state == "working":
            inputs.append(INPUT_DEVICE.format(n=n, name=name, address=address.lower()))
    return (
        BONDED
        + "Profile: HidHostService\n  mTargetDevice: null\n  mInputDevices:\n"
        + "\n".join(lines)
        + "\n\nProfile: AvrcpTargetService:\n"
        + METADATA
        + "__INPUT__\nEvent Hub State:\n  Devices:\n    -1: Virtual\n      UniqueId: <virtual>\n"
        + "      Identifier: bus=0x0000, vendor=0x0000, product=0x0000, version=0x0000\n"
        + "".join(inputs)
    )


def states(text: str) -> dict[str, str]:
    return {r["address"]: r["state"] for r in parse_remotes(text)}


def test_parse_remotes_lists_paired_input_devices_in_order():
    assert parse_remotes(remotes_dump()) == [
        {"name": "NVIDIA SHIELD Remote", "address": "00:04:4B:F2:89:3A", "state": "disconnected"},
        {"name": "NVIDIA SHIELD Remote", "address": NVIDIA, "state": "stuck"},
        {"name": "Harmony Keyboard", "address": HARMONY, "state": "working"},
    ]  # no earbuds: HID_HOST=-1


def test_connected_without_input_device_is_stuck():
    # The state seen after a real reboot: Bluetooth says connected, but Android
    # never created the input device, so the buttons do nothing.
    assert states(remotes_dump(harmony="stuck"))[HARMONY] == "stuck"


@pytest.mark.parametrize("harmony", ["disconnected", "absent"])
def test_remote_not_in_use_is_disconnected_not_stuck(harmony):
    # "absent": just after a reboot, before the hub has reconnected. It must
    # still be listed, not silently dropped.
    assert states(remotes_dump(harmony=harmony))[HARMONY] == "disconnected"


# --- talking to the Shield -------------------------------------------------------
class FakeDevice:
    """Stands in for adb_shell's AdbDeviceTcpAsync; records every shell command.

    outputs maps command -> output (a str, or a list consumed one per call).
    While `down` > 0, each connect fails as if the Shield were rebooting.
    """

    def __init__(self, output: str = "", connect_error: Exception | None = None) -> None:
        self.outputs: dict[str, str | list[str]] = {NOW_PLAYING_COMMAND: output}
        self.connect_error = connect_error
        self.commands: list[str] = []
        self.built_with: tuple[object, ...] = ()
        self.closed = False
        self.rebooted = False
        self.down = 0

    def build(self, host: str, port: int, **kwargs: object) -> FakeDevice:
        self.built_with = (host, port)
        return self

    async def connect(self, rsa_keys: list[object], auth_timeout_s: float) -> bool:
        if self.connect_error:
            raise self.connect_error
        if self.down:
            self.down -= 1
            raise ConnectionRefusedError("rebooting")
        return True

    async def shell(self, command: str, read_timeout_s: float) -> str:
        self.commands.append(command)
        out = self.outputs[command]
        if isinstance(out, list):  # the last one repeats
            return out.pop(0) if len(out) > 1 else out[0]
        return out

    async def reboot(self) -> None:
        self.rebooted = True
        raise ConnectionResetError("going down")  # as the real one may

    async def close(self) -> None:
        self.closed = True


class FakeClock:
    """time.monotonic and asyncio.sleep for the reboot loop, without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


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


async def test_only_fixed_commands_reach_a_shell(adb_settings):
    with pytest.raises(ValueError, match="not an allowed ADB command"):
        await run_command(adb_settings, "rm -rf /sdcard", device_factory=FakeDevice().build)


async def test_get_remotes_reports_stuck_remote_with_advice(adb_settings):
    device = FakeDevice()
    device.outputs[REMOTES_COMMAND] = remotes_dump()
    report = await read_remotes(adb_settings, device_factory=device.build)
    assert report["advice"].startswith("NVIDIA SHIELD Remote is connected over Bluetooth but has no input device")
    assert "Harmony hub, press Off and start the activity again" in report["advice"]
    assert device.commands == [REMOTES_COMMAND]


async def reboot(adb_settings, device: FakeDevice, booted_after_polls: int = 1):
    """Run reboot_and_check on a fake clock. The Shield refuses connections
    for two polls, then answers but is still booting for booted_after_polls."""
    device.outputs[BOOT_COMMAND] = [""] * booted_after_polls + ["1"]
    clock = FakeClock()

    async def sleep(seconds: float) -> None:
        await clock.sleep(seconds)
        if clock.now == adb.POLL_S * 3:  # the grace period after the reboot command
            device.down = 2

    result = await reboot_and_check(adb_settings, device_factory=device.build, sleep=sleep, clock=clock)
    assert device.rebooted
    return result, clock.now - adb.POLL_S * 6  # seconds spent watching remotes after boot


async def test_reboot_names_a_remote_that_hasnt_reconnected(adb_settings):
    # Seen on a real Shield: the Harmony hub stayed disconnected after the
    # reboot until a button was pressed, then worked.
    device = FakeDevice()
    device.outputs[REMOTES_COMMAND] = [remotes_dump(nvidia="working"), remotes_dump(harmony="absent", nvidia="working")]
    result, watched = await reboot(adb_settings, device)
    assert result["back_after_s"] == adb.POLL_S * 6  # grace, 2 refused, 1 still booting, booted
    assert states_of(result) == {"00:04:4B:F2:89:3A": "disconnected", NVIDIA: "working", HARMONY: "disconnected"}
    assert result["advice"].startswith("Harmony Keyboard worked before the reboot and hasn't reconnected yet.")
    assert watched == adb.REMOTES_SETTLE_S  # no waiting for it: it reconnects only when used


async def test_reboot_reports_a_remote_that_came_back_stuck(adb_settings):
    device = FakeDevice()
    device.outputs[REMOTES_COMMAND] = [remotes_dump(harmony="disconnected"), remotes_dump(nvidia="working")]
    result, _ = await reboot(adb_settings, device)
    # The Shield remote was stuck before; the reboot fixed it. The hub was off
    # before, so its absence isn't news.
    assert states_of(result)[NVIDIA] == "working"
    device.outputs[REMOTES_COMMAND] = [remotes_dump(nvidia="working"), remotes_dump(harmony="stuck", nvidia="working")]
    result, _ = await reboot(adb_settings, device)
    assert result["advice"].startswith("Harmony Keyboard is connected over Bluetooth but has no input device")


async def test_reboot_with_remotes_fine_has_no_advice(adb_settings):
    device = FakeDevice()
    device.outputs[REMOTES_COMMAND] = remotes_dump(nvidia="working")
    result, _ = await reboot(adb_settings, device)
    assert result["advice"] is None


async def test_reboot_that_never_comes_back_is_an_error(adb_settings):
    device = FakeDevice()
    device.outputs[REMOTES_COMMAND] = remotes_dump()
    device.outputs[BOOT_COMMAND] = "0"
    clock = FakeClock()
    with pytest.raises(ShieldError, match="wasn't back after 180s"):
        await reboot_and_check(adb_settings, device_factory=device.build, sleep=clock.sleep, clock=clock)


def states_of(result) -> dict[str, str]:
    return {r["address"]: r["state"] for r in result["remotes"]}
