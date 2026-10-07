"""Optional ADB access: what is playing, the Bluetooth remotes, and reboot.

The Android TV Remote protocol reports the foreground app but not what it is
playing. Android's media sessions do (title, artist or channel, play state,
position), and ``dumpsys media_session`` prints them. Reading that needs ADB,
as do checking the Bluetooth remotes and rebooting.

ADB is a shell on the Shield, so this module is the only place it is touched,
and _run() only accepts the constant commands in COMMANDS (reboot uses the
ADB protocol's own reboot service, not a shell). No tool argument reaches
either: the ADB tools take none. Widening this means adding another constant,
deliberately, never passing a string through.

Parsing is kept in plain functions over the dumpsys text, so tests can feed
them output captured from a real Shield.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from adb_shell.adb_device_async import AdbDeviceTcpAsync
from adb_shell.auth.keygen import keygen
from adb_shell.auth.sign_pythonrsa import PythonRSASigner
from adb_shell.exceptions import (
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    InvalidResponseError,
    TcpTimeoutException,
)
from typing_extensions import TypedDict

from .client import ShieldError
from .config import Settings

ADB_PORT = 5555

# The one shell command this server runs. /proc/uptime is read in the same
# call because a session's position is a snapshot taken at `updated` (ms of
# uptime), and the live position is snapshot + time since then.
NOW_PLAYING_COMMAND = "dumpsys media_session; echo __UPTIME__; cat /proc/uptime"
# Bluetooth's view (bonded devices, HID connection states), then the input
# system's (which devices exist). A remote needs both to work.
REMOTES_COMMAND = "dumpsys bluetooth_manager; echo __INPUT__; dumpsys input"
BOOT_COMMAND = "getprop sys.boot_completed"  # "1" once Android has finished booting
COMMANDS = frozenset({NOW_PLAYING_COMMAND, REMOTES_COMMAND, BOOT_COMMAND})

# android.media.session.PlaybackState constants.
PlayState = Literal["playing", "paused", "buffering", "stopped", "other", "idle"]
_STATES: dict[int, PlayState] = {
    1: "stopped",
    2: "paused",
    3: "playing",
    4: "playing",  # fast-forwarding
    5: "playing",  # rewinding
    6: "buffering",
    8: "buffering",  # connecting
}
_ADVANCING = {3, 4, 5}  # states where the position moves on its own

# Live TV reports a stream offset (13 hours on YouTube TV), not a position in
# a show, so no position is reported for these, or for anything longer than
# any movie.
LIVE_TV_PACKAGES = frozenset({"com.google.android.youtube.tvunplugged", "com.sling"})
MAX_POSITION_S = 6 * 3600


class NowPlaying(TypedDict):
    state: PlayState
    app_package: str | None
    app: str | None
    title: str | None
    subtitle: str | None
    position_s: float | None


@dataclass
class Session:
    package: str
    active: bool
    state: int | None = None
    position_ms: int = 0
    speed: float = 0.0
    updated_ms: int = 0
    title: str | None = None
    subtitle: str | None = None


_SESSION_START = re.compile(r"^    \S")  # session headers are indented 4 spaces
_STATE = re.compile(r"state=PlaybackState \{state=(\d+), position=(-?\d+),.*?speed=(-?[\d.]+), updated=(\d+)")


def _null(value: str) -> str | None:
    value = value.strip()
    return None if value in ("", "null") else value


def parse_sessions(text: str) -> list[Session]:
    """The "Sessions Stack" entries, in the order dumpsys lists them (priority)."""
    sessions: list[Session] = []
    in_stack = False
    for line in text.splitlines():
        if "Sessions Stack" in line:
            in_stack = True
            continue
        if not in_stack:
            continue
        if not line.startswith("    "):  # dedent: the stack is over
            in_stack = False
            continue
        if _SESSION_START.match(line):
            sessions.append(Session(package="", active=False))
            continue
        if not sessions:
            continue
        s = sessions[-1]
        field = line.strip()
        if field.startswith("package="):
            s.package = field.removeprefix("package=")
        elif field.startswith("active="):
            s.active = field == "active=true"
        elif m := _STATE.search(field):
            s.state, s.position_ms, s.speed, s.updated_ms = int(m[1]), int(m[2]), float(m[3]), int(m[4])
        elif field.startswith("metadata:") and "description=" in field:
            # MediaDescription prints as "title, subtitle, description". The
            # separator is ambiguous when a title contains ", ", so split from
            # the right: titles have commas more often than artists do.
            parts = field.split("description=", 1)[1].rsplit(", ", 2)
            if len(parts) == 3:
                s.title, s.subtitle = _null(parts[0]), _null(parts[1])
            else:
                s.title = _null(parts[0])
    return sessions


def parse_uptime_ms(text: str) -> float | None:
    """/proc/uptime's first number, after the __UPTIME__ marker, in ms."""
    _, _, tail = text.partition("__UPTIME__")
    try:
        return float(tail.split()[0]) * 1000
    except (IndexError, ValueError):
        return None


def _pick(sessions: list[Session]) -> Session | None:
    """The session the user would call "what's playing": playing beats paused."""
    for wanted in (_ADVANCING | {6, 8}, {2}):
        for s in sessions:
            if s.active and s.state in wanted:
                return s
    return None


def now_playing(text: str, app_name_for: Callable[[str | None], str | None]) -> NowPlaying:
    s = _pick(parse_sessions(text))
    if s is None:
        return {"state": "idle", "app_package": None, "app": None, "title": None, "subtitle": None, "position_s": None}
    position_ms: float = s.position_ms
    uptime = parse_uptime_ms(text)
    if s.state in _ADVANCING and uptime is not None:
        position_ms += (uptime - s.updated_ms) * s.speed
    position_s: float | None = round(position_ms / 1000, 1)
    if s.package in LIVE_TV_PACKAGES or position_s is None or not 0 <= position_s <= MAX_POSITION_S:
        position_s = None
    return {
        "state": _STATES.get(s.state or 0, "other"),
        "app_package": s.package or None,
        "app": app_name_for(s.package),
        "title": s.title,
        "subtitle": s.subtitle,
        "position_s": position_s,
    }


# --- Bluetooth remotes ----------------------------------------------------------
# Seen on a real Shield after a reboot: Bluetooth reports a remote (Harmony hub,
# Shield remote) as HID-connected, but Android never creates its input device,
# so its buttons do nothing. A working remote's input device has bus 0x0005
# (Bluetooth) and its address as UniqueId, so the two views can be matched.
RemoteState = Literal["working", "stuck", "connecting", "disconnected"]
_HID_STATES: dict[int, RemoteState] = {0: "disconnected", 1: "connecting", 2: "working", 3: "disconnected"}

STUCK_ADVICE = (
    "{names} {verb} connected over Bluetooth but {has} no input device, so {pronoun} buttons do nothing "
    "(a known glitch after the Shield reboots). Make each one reconnect: for a Logitech Harmony hub, "
    "press Off and start the activity again (verified). For other remotes, power-cycle the remote, "
    "e.g. remove its batteries for a few seconds (unverified). Then call get_remotes to check."
)


class Remote(TypedDict):
    name: str
    address: str
    state: RemoteState


class RemotesReport(TypedDict):
    remotes: list[Remote]
    advice: str | None


class RebootResult(TypedDict):
    back_after_s: float
    remotes: list[Remote]
    advice: str | None


_ADDRESS = r"([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})"
_BONDED = re.compile(rf"^\s+{_ADDRESS} \[[^\]]*\] (.+)$")
_HID = re.compile(rf"^\s+{_ADDRESS} : (\d+)$")
# A bonded device's stored profile policies; HID_HOST=100 means "allowed as an
# input device", -1 means not an input device (e.g. earbuds).
_HID_POLICY = re.compile(rf"^\s+{_ADDRESS} \{{profile connection policy\(.*\bHID_HOST=(-?\d+)")


def parse_remotes(text: str) -> list[Remote]:
    """Every paired input (HID) device, in pairing-list order, with its state.

    mInputDevices only lists devices that have connected since Bluetooth
    started, so right after a reboot a remote can be missing from it. Paired
    devices allowed as input devices (HID_HOST policy 100) are listed too, as
    disconnected, so a remote never silently drops out of the report.
    """
    bt, _, inputs = text.partition("__INPUT__")
    names: dict[str, str] = {}
    hid: dict[str, int] = {}
    hid_allowed: set[str] = set()
    section = ""
    for line in bt.splitlines():
        stripped = line.strip()
        if stripped in ("Bonded devices:", "mInputDevices:", "Metadata:"):
            section = stripped
            continue
        if section == "Bonded devices:" and (m := _BONDED.match(line)):
            names[m[1].upper()] = m[2].strip()
        elif section == "mInputDevices:" and (m := _HID.match(line)):
            hid[m[1].upper()] = int(m[2])
        elif section == "Metadata:" and (m := _HID_POLICY.match(line)):
            if int(m[2]) > 0:
                hid_allowed.add(m[1].upper())
        elif stripped and not line.startswith("    "):
            section = ""  # dedent: that list is over

    # Addresses of Bluetooth input devices that actually exist.
    present: set[str] = set()
    unique_id = ""
    for line in inputs.splitlines():
        field = line.strip()
        if field.startswith("UniqueId:"):
            unique_id = field.removeprefix("UniqueId:").strip().upper()
        elif field.startswith("Identifier:") and "bus=0x0005" in field and unique_id:
            present.add(unique_id)

    remotes: list[Remote] = []
    ordered = [a for a in names if a in hid or a in hid_allowed] + [a for a in hid if a not in names]
    for address in ordered:
        state = _HID_STATES.get(hid.get(address, 0), "disconnected")
        if state == "working" and address not in present:
            state = "stuck"
        remotes.append({"name": names.get(address, "unknown device"), "address": address, "state": state})
    return remotes


def remotes_report(remotes: list[Remote], missing: list[Remote] | None = None) -> RemotesReport:
    """remotes plus advice for the user. missing: remotes that worked before a
    reboot and haven't reconnected since."""
    stuck = [r["name"] for r in remotes if r["state"] == "stuck"]
    advice: list[str] = []
    if stuck:
        many = len(stuck) > 1
        advice.append(
            STUCK_ADVICE.format(
                names=" and ".join(stuck),
                verb="are" if many else "is",
                has="have" if many else "has",
                pronoun="their" if many else "its",
            )
        )
    if missing:
        many = len(missing) > 1
        advice.append(
            f"{' and '.join(r['name'] for r in missing)} worked before the reboot and "
            f"{'haven' if many else 'hasn'}'t reconnected yet. A Harmony hub reconnects when a button is next "
            "pressed (seen on a real Shield). If the remote then does nothing, call get_remotes: it spots a "
            "stuck remote and says how to fix it."
        )
    return {"remotes": remotes, "advice": " ".join(advice) or None}


# --- talking to the Shield ---------------------------------------------------
# Anything that builds a device from (host, port, default_transport_timeout_s).
DeviceFactory = Callable[..., Any]

_ADB_ERRORS = (
    OSError,
    TimeoutError,
    AdbConnectionError,
    AdbTimeoutError,
    DeviceAuthError,
    InvalidResponseError,
    TcpTimeoutException,
)


def ensure_adb_key(settings: Settings) -> bool:
    """Create the ADB key pair if missing (0600 from the start); True if created."""
    path = settings.adb_key_path
    assert path is not None
    if path.exists():
        return False
    old_umask = os.umask(0o077)
    try:
        keygen(str(path))
    finally:
        os.umask(old_umask)
    return True


def _signer_and_host(settings: Settings) -> tuple[Any, str]:
    if not settings.adb:
        raise ShieldError(
            "ADB isn't set up, so this can't be done. Enable Network debugging on the Shield, then run "
            "`mcp-server-shieldtv adb-setup`."
        )
    if not settings.host or settings.adb_key_path is None or not settings.adb_key_path.exists():
        raise ShieldError("ADB key or Shield address missing. Re-run `mcp-server-shieldtv adb-setup`.")
    return PythonRSASigner.FromRSAKeyPath(str(settings.adb_key_path)), settings.host


async def _adb(
    settings: Settings,
    action: Callable[[Any], Awaitable[Any]],
    auth_timeout_s: float,
    device_factory: DeviceFactory,
) -> Any:
    """Connect, run action(device), disconnect, mapping failures to ShieldError.

    One connection per call: calls are rare, and nothing is left open for a
    shell to hang off. auth_timeout_s is how long the Shield may take to accept
    our key; only adb-setup, where someone is answering the prompt on the TV,
    needs long.
    """
    signer, host = _signer_and_host(settings)
    device = device_factory(host, ADB_PORT, default_transport_timeout_s=5.0)
    try:
        await device.connect(rsa_keys=[signer], auth_timeout_s=auth_timeout_s)
        return await action(device)
    except DeviceAuthError as exc:
        raise ShieldError(
            "The Shield didn't accept our ADB key. Re-run `mcp-server-shieldtv adb-setup` and allow "
            "the prompt on the TV."
        ) from exc
    except _ADB_ERRORS as exc:
        raise ShieldError(
            f"Couldn't reach the Shield over ADB ({type(exc).__name__}: {exc}). Network debugging "
            "may be off, or the Shield asleep, rebooting or unreachable."
        ) from exc
    finally:
        await device.close()


async def run_command(
    settings: Settings,
    command: str,
    auth_timeout_s: float = 5.0,
    device_factory: DeviceFactory = AdbDeviceTcpAsync,
) -> str:
    """Run one of the constant COMMANDS and return its output."""
    # The last line of defense: only the constants above ever reach a shell.
    if command not in COMMANDS:
        raise ValueError(f"not an allowed ADB command: {command!r}")

    async def shell(device: Any) -> str:
        return str(await device.shell(command, read_timeout_s=10.0))

    result: str = await _adb(settings, shell, auth_timeout_s, device_factory)
    return result


async def read_now_playing(settings: Settings, device_factory: DeviceFactory = AdbDeviceTcpAsync) -> NowPlaying:
    text = await run_command(settings, NOW_PLAYING_COMMAND, device_factory=device_factory)
    return now_playing(text, settings.app_name_for)


async def read_remotes(settings: Settings, device_factory: DeviceFactory = AdbDeviceTcpAsync) -> RemotesReport:
    return remotes_report(parse_remotes(await run_command(settings, REMOTES_COMMAND, device_factory=device_factory)))


# Reboot timing, measured on a real Shield: ADB and the remote service were
# back ~38s after the reboot command, and the remotes were stuck within a
# minute. Module-level so tests can shorten them.
BOOT_TIMEOUT_S = 180.0
POLL_S = 3.0
# Remotes that reconnect on their own (a Shield remote) may connect, and get
# stuck, a little after boot. A Harmony hub doesn't: on a real Shield it stayed
# disconnected for minutes, until a button was pressed. So wait this long, then
# look once, rather than wait for every remote to come back.
REMOTES_SETTLE_S = 20.0


async def reboot_and_check(
    settings: Settings,
    device_factory: DeviceFactory = AdbDeviceTcpAsync,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> RebootResult:
    """Reboot, wait until Android has booted, then watch the Bluetooth remotes.

    Uses the ADB protocol's reboot service (``reboot:``), not a shell command.
    """

    async def reboot(device: Any) -> None:
        # The Shield may drop the connection as it goes down.
        with contextlib.suppress(*_ADB_ERRORS):
            await device.reboot()

    # Which remotes work now, so the result can name any that haven't returned.
    before = parse_remotes(await run_command(settings, REMOTES_COMMAND, device_factory=device_factory))
    expected = {r["address"] for r in before if r["state"] == "working"}

    await _adb(settings, reboot, 5.0, device_factory)
    start = clock()
    await sleep(POLL_S * 3)  # don't catch it before it has gone down

    while True:
        try:
            booted = (await run_command(settings, BOOT_COMMAND, device_factory=device_factory)).strip() == "1"
        except ShieldError:
            booted = False  # still rebooting
        if booted:
            break
        if clock() - start > BOOT_TIMEOUT_S:
            raise ShieldError(
                f"Sent the reboot, but the Shield wasn't back after {BOOT_TIMEOUT_S:.0f}s. Check the TV; "
                "it may still be updating or starting up."
            )
        await sleep(POLL_S)
    booted_at = clock()

    await sleep(REMOTES_SETTLE_S)
    remotes = parse_remotes(await run_command(settings, REMOTES_COMMAND, device_factory=device_factory))
    missing = [r for r in remotes if r["address"] in expected and r["state"] in ("disconnected", "connecting")]
    return {"back_after_s": round(booted_at - start, 1), **remotes_report(remotes, missing)}
