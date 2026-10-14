"""Settings, file locations, and the allow-lists that bound what the model can do.

Everything the server persists lives in one directory (default
``~/.config/mcp-server-shieldtv``): the pairing certificate and key, the
optional ADB key (``adbkey``, ``adbkey.pub``), plus a small ``config.json``
holding the Shield's host, any extra apps, and whether ADB is enabled.

The cert/key pair *is* the credential: anyone holding it can control the
Shield, and the ADB key grants a shell on it. So the directory is created
0700 and the files 0600.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args
from urllib.parse import urlparse

CLIENT_NAME = "mcp-server-shieldtv"

log = logging.getLogger(__name__)

# --- Allow-lists -----------------------------------------------------------
# The model can only press keys named here. Each name is a RemoteKeyCode from
# the protocol with the "KEYCODE_" prefix dropped (tests check they all exist).
# Deliberately absent: POWER (a toggle with ambiguous state), SEARCH (starts a
# voice session), SETTINGS, MUTE (Android's *microphone* mute; speaker mute is
# VOLUME_MUTE), plus the library's raw numeric codes and "text:" typing.
KeyName = Literal[
    "HOME",
    "BACK",
    "MENU",
    "DPAD_UP",
    "DPAD_DOWN",
    "DPAD_LEFT",
    "DPAD_RIGHT",
    "DPAD_CENTER",
    "MEDIA_PLAY_PAUSE",
    "MEDIA_PLAY",
    "MEDIA_PAUSE",
    "MEDIA_STOP",
    "MEDIA_NEXT",
    "MEDIA_PREVIOUS",
    "MEDIA_REWIND",
    "MEDIA_FAST_FORWARD",
    "VOLUME_UP",
    "VOLUME_DOWN",
    "VOLUME_MUTE",
]
ALLOWED_KEYS: frozenset[str] = frozenset(get_args(KeyName))


@dataclass(frozen=True)
class App:
    """What launch_app sends, and how to recognize the app once it is open.

    target  - a deep link (https, or an app's own scheme such as ``spotify:``),
              or a bare package name. The library turns a bare package into
              ``market://launch?id=<package>``, which the Shield (remote
              service 7.x) rejects, so links are the default.
    package - the Android package that should reach the foreground. Used to
              confirm a launch worked and to name the app in get_status.
              None means "unknown": any change of foreground app counts.
    """

    target: str
    package: str | None = None


# Friendly name -> app. Extend per-user through the "apps" object in
# config.json; user entries win over these defaults. Each of these was
# checked on a real Shield (2026-10): the link opened that package. Plex and
# Spotify use their own schemes: only the app handles those, while the https
# links also match the browser stub (open.spotify.com matches only the stub).
# ASSUMPTION S-APP-LINKS.
DEFAULT_APPS: dict[str, App] = {
    "youtube": App("https://www.youtube.com", "com.google.android.youtube.tv"),
    "youtube-tv": App("https://tv.youtube.com", "com.google.android.youtube.tvunplugged"),
    "netflix": App("https://www.netflix.com/title", "com.netflix.ninja"),
    "prime-video": App("https://app.primevideo.com", "com.amazon.amazonvideo.livingroom"),
    "disney+": App("https://www.disneyplus.com", "com.disney.disneyplus"),
    "hulu": App("https://www.hulu.com/welcome", "com.hulu.livingroomplus"),
    "plex": App("plex://", "com.plexapp.android"),
    "spotify": App("spotify:", "com.spotify.tv.android"),
}


def _parse_app(value: Any) -> App | None:
    """A config.json entry: a link or package string, or {"link", "package"}."""
    if isinstance(value, str) and value.strip():
        target = value.strip()
        # A bare package name is also the package that will be in front.
        return App(target, None if urlparse(target).scheme else target)
    if isinstance(value, dict) and isinstance(value.get("link"), str):
        package = value.get("package")
        return App(value["link"], package if isinstance(package, str) else None)
    return None


# --- Locations -------------------------------------------------------------
def config_dir() -> Path:
    override = os.environ.get("SHIELDTV_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "mcp-server-shieldtv"


REMOTE_PORT = 6466  # the remote session (every tool call)
PAIRING_PORT = 6467  # pairing, and reading the Shield's name and MAC


@dataclass(frozen=True)
class Settings:
    host: str | None
    cert_path: Path
    key_path: Path
    apps: dict[str, App]
    # Opt-in: set by `adb-setup` once the Shield has accepted adb_key_path.
    adb: bool = False
    adb_key_path: Path | None = None
    # Saved by `pair` from the Shield's certificate: how it is recognized if
    # its address changes (see ShieldClient.rediscover).
    name: str | None = None
    mac: str | None = None
    # Real Shields use 6466/6467; other values are for the simulator.
    port: int = REMOTE_PORT
    pairing_port: int = PAIRING_PORT
    dry_run: bool = False
    # Anything wrong with config.json, in words (logged at startup, shown by doctor).
    problems: tuple[str, ...] = ()

    @property
    def paired(self) -> bool:
        """Host and credential files exist. Says nothing about whether the
        Shield still accepts them; ShieldClient.auth_failed tracks that."""
        return bool(self.host) and self.cert_path.is_file() and self.key_path.is_file()

    def resolve_app(self, name: str) -> App | None:
        return self.apps.get(name.strip().lower())

    def app_name_for(self, package: str | None) -> str | None:
        """Reverse lookup so get_status can say 'netflix' instead of a package."""
        if not package:
            return None
        for friendly, app in self.apps.items():
            if app.package == package:
                return friendly
        return None


def _read_json(path: Path, problems: list[str] | None = None) -> dict[str, Any]:
    """config.json as a dict. A missing file is normal; a broken one is a
    problem worth saying out loud (silently ignoring it looks like "not
    paired", which sends people to re-pair for nothing)."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        if problems is not None:
            problems.append(f"{path} is not valid JSON (line {exc.lineno}: {exc.msg}); ignoring it")
        return {}
    except OSError as exc:
        if problems is not None:
            problems.append(f"can't read {path} ({exc})")
        return {}
    if not isinstance(data, dict):
        if problems is not None:
            problems.append(f"{path} should hold a JSON object; ignoring it")
        return {}
    return data


def _port(value: Any, what: str, default: int, problems: list[str]) -> int:
    if value is None:
        return default
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
        return value
    problems.append(f"ignoring {what}={value!r}: must be a port number")
    return default


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def load_settings(host_override: str | None = None) -> Settings:
    """Never raises: anything wrong becomes a sentence in Settings.problems."""
    d = config_dir()
    problems: list[str] = []
    data = _read_json(d / "config.json", problems)
    user_apps = data.get("apps", {})
    apps = dict(DEFAULT_APPS)
    if isinstance(user_apps, dict):
        for name, value in user_apps.items():
            app = _parse_app(value)
            if app is None:
                problems.append(f"ignoring app {name!r} in config.json: expected a string or {{link, package}}")
            else:
                apps[name.lower()] = app
    else:
        problems.append('"apps" in config.json should be an object; ignoring it')
    # Most specific wins: `pair --host`, then the environment, then config.json.
    host = host_override or os.environ.get("SHIELDTV_HOST") or data.get("host")
    if host is not None and not isinstance(host, str):
        problems.append(f"ignoring host {host!r} in config.json: expected a string")
        host = None
    port = _port(data.get("port"), "port", REMOTE_PORT, problems)
    pairing_port = _port(data.get("pairing_port"), "pairing_port", PAIRING_PORT, problems)
    return Settings(
        host=host,
        cert_path=d / "cert.pem",
        key_path=d / "key.pem",
        apps=apps,
        adb=data.get("adb") is True,
        adb_key_path=d / "adbkey",
        name=data.get("name") if isinstance(data.get("name"), str) else None,
        mac=data.get("mac") if isinstance(data.get("mac"), str) else None,
        port=port,
        pairing_port=pairing_port,
        dry_run=_flag(os.environ.get("SHIELDTV_DRY_RUN")),
        problems=tuple(problems),
    )


def ensure_private_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def lock_down_credentials(settings: Settings) -> None:
    paths = [settings.cert_path, settings.key_path]
    if settings.adb_key_path:
        paths += [settings.adb_key_path, settings.adb_key_path.with_name(settings.adb_key_path.name + ".pub")]
    for p in paths:
        if p.exists():
            p.chmod(0o600)


def save_config(**values: Any) -> None:
    """Merge values into config.json, keeping whatever else is there."""
    d = ensure_private_dir()
    path = d / "config.json"
    data = _read_json(path)
    data.update(values)
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)


def save_host(host: str) -> None:
    save_config(host=host)
