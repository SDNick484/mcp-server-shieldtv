"""Settings, file locations, and the allow-lists that bound what the model can do.

Everything the server persists lives in one directory (default
``~/.config/mcp-server-shieldtv``): the pairing certificate and key, plus a
small ``config.json`` holding the Shield's host and any extra apps.

The cert/key pair *is* the credential: anyone holding it can control the
Shield, so the directory is created 0700 and the files 0600.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, get_args

CLIENT_NAME = "mcp-server-shieldtv"

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

# Friendly name -> package name (or deep link). Extend per-user through the
# "apps" object in config.json; user entries win over these defaults.
# Unverified: these are the usual Android TV package names, not yet checked
# on a real Shield.
DEFAULT_APPS: dict[str, str] = {
    "netflix": "com.netflix.ninja",
    "youtube": "com.google.android.youtube.tv",
    "plex": "com.plexapp.android",
    "disney+": "com.disney.disneyplus",
    "prime-video": "com.amazon.amazonvideo.livingroom",
    "hulu": "com.hulu.livingroomplus",
    "spotify": "com.spotify.tv.android",
    "kodi": "org.xbmc.kodi",
}


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
    apps: dict[str, str]

    @property
    def paired(self) -> bool:
        """Host and credential files exist. Says nothing about whether the
        Shield still accepts them; ShieldClient.auth_failed tracks that."""
        return bool(self.host) and self.cert_path.is_file() and self.key_path.is_file()

    def resolve_app(self, name: str) -> str | None:
        return self.apps.get(name.strip().lower())

    def app_name_for(self, package: str | None) -> str | None:
        """Reverse lookup so get_status can say 'netflix' instead of a package."""
        if not package:
            return None
        for friendly, target in self.apps.items():
            if target == package:
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
    apps = {**DEFAULT_APPS, **{k.lower(): v for k, v in data.get("apps", {}).items()}}
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
