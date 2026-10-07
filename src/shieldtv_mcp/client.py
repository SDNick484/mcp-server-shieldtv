"""A long-lived connection to the Shield plus a cache of its pushed state.

Unlike an eISCP receiver, where each command can open its own socket, the
Android TV Remote protocol keeps one TLS connection open and *pushes* state
(power, foreground app, volume) over it. So this class owns that connection
for the life of the server and keeps the latest values for ``get_status``.

Connecting may fail simply because the Shield is asleep or rebooting, so the
first connect runs in a background task with backoff instead of blocking
server startup. Once connected, the library's own ``keep_reconnecting`` takes
over for later drops.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)

from mcp.server.mcpserver.exceptions import ToolError

from .config import ALLOWED_KEYS, CLIENT_NAME, Settings

log = logging.getLogger(__name__)


class ShieldError(ToolError):
    """A problem the model (and user) can act on.

    Subclassing the SDK's ToolError matters: only ToolError messages reach the
    model. Any other exception is treated as a crash and reported as just
    "Error executing tool <name>", which would hide advice like "run pair first".
    """


class ShieldClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._remote: AndroidTVRemote | None = None
        self._task: asyncio.Task[None] | None = None
        self.available = False
        self.auth_failed = False
        self.is_on: bool | None = None
        self.current_app: str | None = None
        self.volume: Any = None

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if not self.settings.paired:
            log.warning("Not paired; run `mcp-server-shieldtv pair` first.")
            return
        self._task = asyncio.create_task(self._connect_forever(), name="shieldtv-connect")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._remote:
            self._remote.disconnect()
        self.available = False

    async def _connect_forever(self) -> None:
        s = self.settings
        remote = AndroidTVRemote(CLIENT_NAME, str(s.cert_path), str(s.key_path), s.host)
        remote.add_is_on_updated_callback(self._set_is_on)
        remote.add_current_app_updated_callback(self._set_current_app)
        remote.add_volume_info_updated_callback(self._set_volume)
        remote.add_is_available_updated_callback(self._set_available)

        delay = 1.0
        while True:
            try:
                await remote.async_connect()
                break
            except InvalidAuth:
                self.auth_failed = True
                log.error("Shield rejected our certificate; re-run `mcp-server-shieldtv pair`.")
                return
            except (CannotConnect, ConnectionClosed, OSError, asyncio.TimeoutError) as exc:
                log.info("Shield unreachable (%s); retrying in %.0fs", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

        self._remote = remote
        self._set_is_on(remote.is_on)
        self._set_current_app(remote.current_app)
        self._set_volume(remote.volume_info)
        self._set_available(True)
        remote.keep_reconnecting(invalid_auth_callback=self._on_invalid_auth)
        log.info("Connected to Shield at %s", s.host)

    # --- state callbacks (called by the library) ----------------------------
    def _set_is_on(self, value: bool) -> None:
        self.is_on = value

    def _set_current_app(self, value: str) -> None:
        self.current_app = value

    def _set_volume(self, value: Any) -> None:
        self.volume = value

    def _set_available(self, value: bool) -> None:
        self.available = value

    def _on_invalid_auth(self) -> None:
        self.auth_failed = True
        self.available = False
        log.error("Shield rejected our certificate; re-run `mcp-server-shieldtv pair`.")

    # --- commands ----------------------------------------------------------
    def _require(self) -> AndroidTVRemote:
        if not self.settings.paired:
            raise ShieldError("Not paired with a Shield yet. Run `mcp-server-shieldtv pair` first.")
        if self.auth_failed:
            raise ShieldError("The Shield rejected our pairing. Re-run `mcp-server-shieldtv pair`.")
        if self._remote is None or not self.available:
            raise ShieldError(
                f"Can't reach the Shield at {self.settings.host}. It may be asleep, "
                "rebooting, or offline; the server keeps retrying in the background."
            )
        return self._remote

    def send_key(self, key: str) -> None:
        # Re-check here even though the tool schema already restricts the enum:
        # this is the last line of defense before a key hits the TV.
        if key not in ALLOWED_KEYS:
            raise ShieldError(f"Key {key!r} is not allowed.")
        self._run(lambda r: r.send_key_command(key))

    def set_power(self, on: bool) -> None:
        # WAKEUP/SLEEP are explicit; the POWER key is a toggle and is not allowed.
        self._run(lambda r: r.send_key_command("WAKEUP" if on else "SLEEP"))

    def launch(self, target: str) -> None:
        self._run(lambda r: r.send_launch_app_command(target))

    def _run(self, fn) -> None:
        remote = self._require()
        try:
            fn(remote)
        except ConnectionClosed as exc:
            raise ShieldError("The connection to the Shield dropped; try again in a moment.") from exc

    # --- reporting ---------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        vol = self.volume
        info = getattr(self._remote, "device_info", None) if self._remote else None
        return {
            "host": self.settings.host,
            "paired": self.settings.paired,
            "reachable": self.available,
            "power": None if self.is_on is None else ("on" if self.is_on else "standby"),
            "current_app_package": self.current_app or None,
            "current_app": self.settings.app_name_for(self.current_app),
            "volume": (
                {"level": vol.level, "max": vol.max, "muted": vol.muted} if vol is not None else None
            ),
            "device": (
                {"manufacturer": info["manufacturer"], "model": info["model"], "sw_version": info["sw_version"]}
                if info
                else None
            ),
        }
