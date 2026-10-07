"""Optional, read-only ADB access: what is playing right now.

The Android TV Remote protocol reports the foreground app but not what it is
playing. Android's media sessions do (title, artist or channel, play state,
position), and ``dumpsys media_session`` prints them. Reading that needs ADB.

ADB is a shell on the Shield, so this module is the only place it is touched,
and it only ever runs NOW_PLAYING_COMMAND, a constant. No tool argument reaches
it: get_now_playing takes none. Widening this means adding another constant,
deliberately, never passing a string through.

Parsing is kept in plain functions over the dumpsys text, so tests can feed
them output captured from a real Shield.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
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


async def run_now_playing_command(
    settings: Settings,
    auth_timeout_s: float = 5.0,
    device_factory: DeviceFactory = AdbDeviceTcpAsync,
) -> str:
    """Connect, run NOW_PLAYING_COMMAND, disconnect. One connection per call:
    calls are rare, and nothing is left open for a shell to hang off.

    auth_timeout_s is how long the Shield may take to accept our key; only
    adb-setup, where someone is answering the prompt on the TV, needs long.
    """
    if not settings.adb:
        raise ShieldError(
            "ADB isn't set up, so what's playing can't be read. Enable Network debugging on the "
            "Shield, then run `mcp-server-shieldtv adb-setup`."
        )
    if not settings.host or settings.adb_key_path is None or not settings.adb_key_path.exists():
        raise ShieldError("ADB key or Shield address missing. Re-run `mcp-server-shieldtv adb-setup`.")
    signer = PythonRSASigner.FromRSAKeyPath(str(settings.adb_key_path))
    device = device_factory(settings.host, ADB_PORT, default_transport_timeout_s=5.0)
    try:
        await device.connect(rsa_keys=[signer], auth_timeout_s=auth_timeout_s)
        output = await device.shell(NOW_PLAYING_COMMAND, read_timeout_s=10.0)
    except DeviceAuthError as exc:
        raise ShieldError(
            "The Shield didn't accept our ADB key. Re-run `mcp-server-shieldtv adb-setup` and allow "
            "the prompt on the TV."
        ) from exc
    except _ADB_ERRORS as exc:
        raise ShieldError(
            f"Couldn't read from the Shield over ADB ({type(exc).__name__}: {exc}). Network debugging "
            "may be off, or the Shield asleep or unreachable."
        ) from exc
    finally:
        await device.close()
    return str(output)


async def read_now_playing(settings: Settings, device_factory: DeviceFactory = AdbDeviceTcpAsync) -> NowPlaying:
    text = await run_now_playing_command(settings, device_factory=device_factory)
    return now_playing(text, settings.app_name_for)
