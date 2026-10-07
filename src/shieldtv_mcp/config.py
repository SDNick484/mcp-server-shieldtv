"""Settings, file locations, and the allow-lists that bound what the model can do.

Everything the server persists lives in one directory (default
``~/.config/mcp-server-shieldtv``): the pairing certificate and key, plus a
small ``config.json`` holding the Shield's host and any extra apps.

The cert/key pair *is* the credential: anyone holding it can control the
Shield, so the directory is created 0700 and the files 0600.
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


@dataclass(frozen=True)
class Settings:
    host: str | None
    cert_path: Path
    key_path: Path
    apps: dict[str, App]

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


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_settings(host_override: str | None = None) -> Settings:
    d = config_dir()
    data = _read_json(d / "config.json")
    user_apps = data.get("apps", {})
    apps = dict(DEFAULT_APPS)
    if isinstance(user_apps, dict):
        for name, value in user_apps.items():
            app = _parse_app(value)
            if app is None:
                log.warning("Ignoring app %r in config.json: expected a string or {link, package}", name)
            else:
                apps[name.lower()] = app
    # Most specific wins: `pair --host`, then the environment, then config.json.
    host = host_override or os.environ.get("SHIELDTV_HOST") or data.get("host")
    return Settings(host=host, cert_path=d / "cert.pem", key_path=d / "key.pem", apps=apps)


def ensure_private_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def lock_down_credentials(settings: Settings) -> None:
    for p in (settings.cert_path, settings.key_path):
        if p.exists():
            p.chmod(0o600)


def save_host(host: str) -> None:
    d = ensure_private_dir()
    path = d / "config.json"
    data = _read_json(path)
    data["host"] = host
    path.write_text(json.dumps(data, indent=2) + "\n")
    path.chmod(0o600)
