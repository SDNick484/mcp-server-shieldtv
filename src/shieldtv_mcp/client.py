"""A long-lived connection to the Shield plus a cache of its pushed state.

Unlike an eISCP receiver, where each command can open its own socket, the
Android TV Remote protocol keeps one TLS connection open and *pushes* state
(power, foreground app, volume) over it. So this class owns that connection
for the life of the server and keeps the latest values for ``get_status``.

What can go wrong, and what this class does about it:

  - The Shield is asleep, rebooting or offline at startup: the first connect
    runs in a background task with backoff (1s doubling to 60s), so the
    server starts anyway. Each attempt is bounded by ``connect_timeout``: the
    library waits for the Shield's "remote started" message without a limit.
  - The connection drops later: the library's ``keep_reconnecting`` takes
    over (seen reconnecting within ~0.1s on a real Shield). While it is down,
    get_status still shows the last values, marked ``stale`` with ``as_of``.
  - The Shield moved to another address (DHCP): after ``rediscover_after``
    seconds unreachable, the watchdog looks for it by mDNS and recognizes it
    by the MAC address in its certificate, saved by ``pair``. Only an exact
    MAC match moves it; anything else stays an error (never guess a device).
    ASSUMPTION S-CERT-MAC.
  - The pairing files are unreadable (corrupt, wrong key): that is reported
    as such at startup, not retried forever as "unreachable".
  - The Shield rejects our certificate (unpaired on the TV): ``auth_failed``.
    When ``pair`` writes new credentials, the watchdog notices and reconnects
    with them, so the server needn't be restarted.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import ssl
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
    VolumeInfo,
)
from mcp.server.mcpserver.exceptions import ToolError
from typing_extensions import TypedDict

from .config import ALLOWED_KEYS, CLIENT_NAME, App, Settings, config_dir, load_settings, save_config

log = logging.getLogger(__name__)

# Home-screen packages (Android TV, Google TV). Landing on one never means a
# launch worked: on real hardware, the launcher's own "now in front" push
# after a HOME key can arrive just after a launch request is sent.
LAUNCHERS = frozenset({"com.google.android.tvlauncher", "com.google.android.apps.tv.launcherx"})


class ShieldError(ToolError):
    """A problem the model (and user) can act on.

    Subclassing the SDK's ToolError matters: only ToolError messages reach the
    model. Any other exception is treated as a crash and reported as just
    "Error executing tool <name>", which would hide advice like "run pair first".
    """


# --- get_status's shape -----------------------------------------------------
# Returning TypedDicts (rather than dict[str, Any]) makes the MCP SDK publish an
# outputSchema for get_status, so clients know the fields without guessing.
# They come from typing_extensions: on Python 3.11, Pydantic rejects
# typing.TypedDict, and the SDK then silently drops the schema.
class Volume(TypedDict):
    level: int
    max: int
    muted: bool


class Device(TypedDict):
    manufacturer: str
    model: str
    sw_version: str


class Status(TypedDict):
    host: str | None
    paired: bool
    reachable: bool
    # True when not reachable now: power, app and volume are the last values
    # the Shield pushed, at as_of, and may no longer be true.
    stale: bool
    as_of: str | None  # ISO 8601 time of the last pushed value (or connect)
    error: str | None  # why it isn't reachable, when it isn't
    power: Literal["on", "standby"] | None
    current_app_package: str | None
    current_app: str | None
    volume: Volume | None
    device: Device | None


# Anything that builds a remote from (client_name, certfile, keyfile, host,
# api_port=, pair_port=). Normally AndroidTVRemote itself; tests pass a fake.
RemoteFactory = Callable[..., Any]
# Anything that finds Android TV devices: timeout -> {mDNS name: IPv4 address}.
Finder = Callable[[float], Awaitable[dict[str, str]]]


async def _mdns(timeout: float) -> dict[str, str]:
    from .discovery import discover  # zeroconf is only loaded when needed

    return await discover(timeout)


def check_credentials(settings: Settings) -> str | None:
    """None if cert.pem and key.pem load as a TLS client identity, else what's
    wrong. Without this, a corrupt file made every connect fail with an
    SSLError, which looked like "can't reach the Shield" and was retried
    forever."""
    if not (settings.cert_path.is_file() and settings.key_path.is_file()):
        return None  # not paired: reported as such
    try:
        ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(str(settings.cert_path), str(settings.key_path))
    except (ssl.SSLError, ValueError, OSError) as exc:
        return f"the pairing files in {settings.cert_path.parent} can't be used ({exc})"
    return None


def credential_stamp(settings: Settings) -> tuple[int, ...] | None:
    """Changes whenever `pair` writes new credentials (or the host changes)."""
    try:
        a, b = settings.cert_path.stat(), settings.key_path.stat()
    except OSError:
        return None
    cfg = config_dir() / "config.json"
    c = cfg.stat().st_mtime_ns if cfg.exists() else 0
    return (a.st_mtime_ns, a.st_size, b.st_mtime_ns, b.st_size, c)


def _describe(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "it accepted the connection but didn't start a remote session in time"
    return str(exc) or type(exc).__name__


class ShieldClient:
    """Owns the connection to one Shield and the latest state it pushed.

    State fields (written by the library's callbacks, read by tools):
      available   - connected right now. Goes False while the library reconnects.
      auth_failed - the Shield rejected our certificate. Retrying won't help;
                    only re-pairing will, so it is tracked apart from available.
      cert_error  - our own pairing files can't be loaded. Same: re-pair.
      is_on, current_app, volume - last pushed values; None until first known.
      as_of       - when one of them last arrived (wall clock).
      error       - why the last connect failed, for messages.
      drops       - how many times the connection has been lost. A counter, not
                    a flag: the library reconnects within ~0.1s, so a drop can
                    come and go between two looks at ``available``.
                    ASSUMPTION S-RECONNECT.

    Everything runs on the server's single asyncio event loop, so the callbacks
    and tools never run at the same time and need no locks.
    """

    # How long launch() and set_power() wait to see the result, and how often
    # they look. Class attributes so tests can shorten them. A cold YouTube
    # start on a real Shield took over 5s; power changes were reported at once.
    launch_timeout = 10.0
    power_timeout = 5.0
    poll_interval = 0.25
    connect_timeout = 15.0  # per attempt
    rediscover_after = 60.0  # unreachable this long: look for it at another address
    watch_interval = 5.0  # how often the watchdog looks

    def __init__(
        self, settings: Settings, remote_factory: RemoteFactory = AndroidTVRemote, finder: Finder = _mdns
    ) -> None:
        self.settings = settings
        self._remote_factory = remote_factory
        self._finder = finder
        self._remote: AndroidTVRemote | None = None
        self._task: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self.available = False
        self.auth_failed = False
        self.cert_error: str | None = None
        self.error: str | None = None
        self.is_on: bool | None = None
        self.current_app: str | None = None
        self.volume: VolumeInfo | None = None
        self.as_of: float | None = None
        self.drops = 0
        self.moved_from: str | None = None  # the old address, after a rediscovery
        self._down_since: float | None = None  # monotonic, while unreachable after a connect
        self._last_search = 0.0
        self._stamp = credential_stamp(settings)

    # --- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if not self.settings.paired:
            log.warning("Not paired; run `mcp-server-shieldtv pair` first.")
            self._watch = asyncio.create_task(self._watchdog(), name="shieldtv-watch")
            return
        self.cert_error = check_credentials(self.settings)
        if self.cert_error:
            log.error("Can't use the pairing credentials: %s. Re-run `mcp-server-shieldtv pair`.", self.cert_error)
        else:
            self._task = asyncio.create_task(self._connect_forever(), name="shieldtv-connect")
        self._watch = asyncio.create_task(self._watchdog(), name="shieldtv-watch")

    async def stop(self) -> None:
        for task in (self._watch, self._task):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._watch = self._task = None
        await self._disconnect()

    async def _disconnect(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._remote:
            self._remote.disconnect()
            self._remote = None
        self.available = False

    async def restart(self, settings: Settings | None = None) -> None:
        """Drop the connection and start over, e.g. with new credentials or a
        new address. Pushed values stay (marked stale) until fresh ones arrive."""
        await self._disconnect()
        self.settings = settings or load_settings()
        self.auth_failed = False
        self.error = None
        self._stamp = credential_stamp(self.settings)
        self.cert_error = check_credentials(self.settings) if self.settings.paired else None
        if self.settings.paired and not self.cert_error:
            self._task = asyncio.create_task(self._connect_forever(), name="shieldtv-connect")

    async def _connect_forever(self) -> None:
        # Nothing awaits this task until shutdown, so an unexpected exception
        # would vanish silently. Log it instead.
        try:
            await self._connect_and_watch()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Shield connection task crashed")

    async def _connect_and_watch(self) -> None:
        s = self.settings
        assert s.host is not None  # start() only runs this when paired
        remote = self._remote_factory(
            CLIENT_NAME, str(s.cert_path), str(s.key_path), s.host, api_port=s.port, pair_port=s.pairing_port
        )
        remote.add_is_on_updated_callback(self._set_is_on)
        remote.add_current_app_updated_callback(self._set_current_app)
        remote.add_volume_info_updated_callback(self._set_volume)
        remote.add_is_available_updated_callback(self._set_available)
        self._remote = remote

        delay = 1.0
        if self._down_since is None:
            self._down_since = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(remote.async_connect(), self.connect_timeout)
                break
            except InvalidAuth:
                self._on_invalid_auth()
                return
            except (TimeoutError, CannotConnect, ConnectionClosed, OSError) as exc:
                self.error = _describe(exc)
                log.info("Shield unreachable (%s); retrying in %.0fs", self.error, delay)
                # A half-open attempt (TCP up, no remote_start) must not linger
                remote.disconnect()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

        self._set_is_on(remote.is_on)
        self._set_current_app(remote.current_app)
        self._set_volume(remote.volume_info)
        self._set_available(True)
        remote.keep_reconnecting(invalid_auth_callback=self._on_invalid_auth)
        log.info("Connected to Shield at %s", s.host)

    async def _watchdog(self) -> None:
        """Every watch_interval: pick up a new pairing, or look for a Shield
        that has been unreachable too long at another address."""
        while True:
            await asyncio.sleep(self.watch_interval)
            try:
                # Changed credentials matter only while they're the problem (not
                # connected). A half-written pairing (files missing) waits.
                stamp = credential_stamp(load_settings())
                if stamp is not None and stamp != self._stamp and not self.available:
                    log.info("New pairing credentials found; reconnecting with them.")
                    await self.restart()
                    continue
                down = self._down_since
                if (
                    down is not None
                    and not self.available
                    and not self.auth_failed
                    and self.settings.mac
                    and time.monotonic() - down >= self.rediscover_after
                    and time.monotonic() - self._last_search >= self.rediscover_after
                ):
                    await self.rediscover()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Shield watchdog failed; it will try again")

    async def rediscover(self) -> str | None:
        """Look for this Shield (by the MAC saved at pairing) at another address.

        Returns the new address after switching to it, else None. ASSUMPTION
        S-CERT-MAC: the MAC in the Shield's certificate stays the same when its
        address changes, and reading the certificate (a TLS connect to the
        pairing port, no pairing) shows nothing on the TV. ASSUMPTION
        S-MDNS: the Shield advertises _androidtvremote2._tcp."""
        self._last_search = time.monotonic()
        s = self.settings
        if not s.mac:
            return None
        try:
            found = await self._finder(5.0)
        except Exception as exc:  # zeroconf can fail in many OS-specific ways
            log.info("mDNS search failed (%s)", exc)
            return None
        matches: list[str] = []
        for ip in sorted(set(found.values())):
            if ip == s.host:
                continue
            probe = self._remote_factory(
                CLIENT_NAME, str(s.cert_path), str(s.key_path), ip, api_port=s.port, pair_port=s.pairing_port
            )
            try:
                _, mac = await asyncio.wait_for(probe.async_get_name_and_mac(), 5.0)
            except (TimeoutError, CannotConnect, OSError) as exc:
                log.debug("Not the Shield: %s (%s)", ip, exc)
                continue
            if mac.lower() == s.mac.lower():
                matches.append(ip)
        if len(matches) != 1:
            if len(matches) > 1:
                log.error("Several devices claim the Shield's MAC (%s); not switching.", ", ".join(matches))
            else:
                log.info("The Shield (MAC %s) isn't answering anywhere else on the network either.", s.mac)
            return None
        new = matches[0]
        log.warning("The Shield moved from %s to %s; switching and saving the new address.", s.host, new)
        self.moved_from = s.host
        save_config(host=new)
        await self.restart(dataclasses.replace(s, host=new))
        return new

    # --- state callbacks (called by the library) ----------------------------
    def _touch(self) -> None:
        self.as_of = time.time()

    def _set_is_on(self, value: bool | None) -> None:
        self.is_on = value
        self._touch()

    def _set_current_app(self, value: str | None) -> None:
        self.current_app = value
        self._touch()

    def _set_volume(self, value: VolumeInfo | None) -> None:
        self.volume = value
        self._touch()

    def _set_available(self, value: bool) -> None:
        if self.available and not value:
            self.drops += 1
            self._down_since = time.monotonic()
        if value:
            self._down_since = None
            self.error = None
            self._touch()
        self.available = value

    def _on_invalid_auth(self) -> None:
        self.auth_failed = True
        self.available = False
        self.error = "the Shield rejected our certificate (it was unpaired, or the Shield was reset)"
        log.error("Shield rejected our certificate; re-run `mcp-server-shieldtv pair`.")

    # --- commands ----------------------------------------------------------
    def _require(self) -> AndroidTVRemote:
        if not self.settings.paired:
            raise ShieldError("Not paired with a Shield yet. Run `mcp-server-shieldtv pair` first.")
        if self.cert_error:
            raise ShieldError(f"Can't connect: {self.cert_error}. Re-run `mcp-server-shieldtv pair`.")
        if self.auth_failed:
            raise ShieldError(
                "The Shield rejected our pairing. Re-run `mcp-server-shieldtv pair`; the server picks up the "
                "new pairing on its own."
            )
        if self._remote is None or not self.available:
            why = f" ({self.error})" if self.error else ""
            raise ShieldError(
                f"Can't reach the Shield at {self.settings.host}{why}. It may be asleep, rebooting, or offline; "
                "the server keeps retrying in the background, and looks for it at another address if it moved."
            )
        return self._remote

    def send_key(self, key: str) -> None:
        # Re-check here even though the tool schema already restricts the enum:
        # this is the last line of defense before a key hits the TV.
        if key not in ALLOWED_KEYS:
            raise ShieldError(f"Key {key!r} is not allowed.")
        self._run(lambda r: r.send_key_command(key))

    async def set_power(self, on: bool) -> None:
        """Wake or sleep the Shield and confirm it reported the new state.

        WAKEUP/SLEEP are explicit; the POWER key is a toggle and is not allowed.
        Like launch(), a sent key proves nothing, so wait for the pushed is_on.
        ASSUMPTION S-WAKE-ANY-KEY.
        """
        self._run(lambda r: r.send_key_command("WAKEUP" if on else "SLEEP"))
        if not await self._wait_for(lambda: self.is_on is on, self.power_timeout):
            state = {True: "on", False: "standby", None: "unknown"}[self.is_on]
            raise ShieldError(
                f"Sent {'WAKEUP' if on else 'SLEEP'}, but the Shield didn't report the change within "
                f"{self.power_timeout:.0f}s (it still reports {state}). Check get_status before retrying."
            )

    async def launch(self, app: App) -> str:
        """Launch app and confirm it reached the foreground; return its package.

        The library's launch is fire-and-forget, and a sent request proves
        nothing. On real hardware a launch fails in two quiet ways:
          - the Shield rejects the link: it replies with an error that the
            library only logs, then drops the connection (counted in drops);
          - the Shield accepts it, but nothing opens (app not installed, or no
            app handles the link), so the foreground app never changes.
        So instead of trusting the send, watch what the Shield pushes back.
        ASSUMPTION S-MARKET-REJECT, S-LINK-UNHANDLED.
        """
        before = self.current_app
        drops = self.drops
        self._run(lambda r: r.send_launch_app_command(app.target))

        def opened() -> bool:
            if self.drops != drops:
                raise ShieldError(
                    f"The Shield rejected the launch request for {app.target} and dropped the connection "
                    "(it reconnects on its own). Bare package names are rejected; use an https link."
                )
            now = self.current_app
            # With a known package, being in front is success, even if it
            # already was. Without one, a change to some app other than the
            # home screen counts.
            if app.package:
                return now == app.package
            return bool(now) and now != before and now not in LAUNCHERS

        if await self._wait_for(opened, self.launch_timeout):
            assert self.current_app is not None
            return self.current_app
        raise ShieldError(
            f"The Shield accepted {app.target}, but the foreground app didn't change within "
            f"{self.launch_timeout:.0f}s (still {self.current_app or 'unknown'}). The app may not be "
            "installed, or no installed app handles that link; the TV may be showing an error."
        )

    async def _wait_for(self, condition: Callable[[], bool], timeout: float) -> bool:
        """Poll condition() while the library's callbacks update state; False on timeout.

        Polling (rather than an Event per callback) keeps the state callbacks
        trivial, and a few checks a second is plenty for a TV.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(self.poll_interval)
            if condition():
                return True
        return False

    def _run(self, fn: Callable[[AndroidTVRemote], None]) -> None:
        remote = self._require()
        # _require() passing isn't a guarantee: the connection can drop before
        # the library reports it via the is_available callback, and the send
        # then raises ConnectionClosed. Turn that into advice the model sees.
        try:
            fn(remote)
        except ConnectionClosed as exc:
            raise ShieldError("The connection to the Shield dropped; try again in a moment.") from exc

    # --- reporting ---------------------------------------------------------
    def snapshot(self) -> Status:
        # The library hands volume and device info over as plain dicts
        # (TypedDicts), not objects: index them, don't use attributes.
        # Volume max 0 means the Shield isn't reporting volume, typically
        # because HDMI-CEC hands it to a TV or receiver. Report that as unknown,
        # not as "level 0 of 0". ASSUMPTION S-CEC-VOLUME.
        vol = self.volume if self.volume and self.volume["max"] > 0 else None
        info = self._remote.device_info if self._remote else None
        known = self.is_on is not None or self.current_app is not None or self.volume is not None
        error = self.cert_error or (self.error if not self.available else None)
        if not self.available and self.auth_failed:
            error = self.error
        return {
            "host": self.settings.host,
            "paired": self.settings.paired,
            "reachable": self.available,
            "stale": known and not self.available,
            "as_of": datetime.fromtimestamp(self.as_of, UTC).isoformat(timespec="seconds") if self.as_of else None,
            "error": error,
            "power": None if self.is_on is None else ("on" if self.is_on else "standby"),
            "current_app_package": self.current_app or None,
            "current_app": self.settings.app_name_for(self.current_app),
            "volume": ({"level": vol["level"], "max": vol["max"], "muted": vol["muted"]} if vol else None),
            "device": (
                {
                    "manufacturer": info.get("manufacturer", ""),
                    "model": info.get("model", ""),
                    "sw_version": info.get("sw_version", ""),
                }
                if info
                else None
            ),
        }
