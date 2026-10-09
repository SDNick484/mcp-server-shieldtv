"""Entry point: ``mcp-server-shieldtv [serve] | pair | discover | adb-setup``.

``serve`` is the default, so ``mcp-server-shieldtv`` alone runs the MCP server
over stdio, and ``mcp-server-shieldtv --http`` works as well as
``mcp-server-shieldtv serve --http`` (the spelling the sibling servers accept).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)

from . import __version__, remote
from .client import check_credentials
from .config import (
    CLIENT_NAME,
    config_dir,
    ensure_private_dir,
    load_settings,
    lock_down_credentials,
    save_config,
)
from .discovery import discover
from .logsafe import setup_logging


def _setup_logging(args: argparse.Namespace) -> None:
    """stderr only (stdout belongs to the MCP stdio transport), redacted: LAN
    addresses and MACs are masked unless --no-redact / SHIELDTV_LOG_UNREDACTED=1;
    keys, certificates and JWTs always are."""
    unredacted = getattr(args, "no_redact", False) or _flag(os.environ.get("SHIELDTV_LOG_UNREDACTED"))
    debug = getattr(args, "debug", False) or _flag(os.environ.get("SHIELDTV_DEBUG"))
    setup_logging(logging.DEBUG if debug else logging.INFO, redacted=not unredacted)
    if not debug:
        # The library logs every message it sends and receives at DEBUG; keep INFO readable.
        logging.getLogger("androidtvremote2").setLevel(logging.INFO)


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


async def _cmd_discover(timeout: float) -> int:
    print(f"Searching for Android TV devices ({timeout:.0f}s)...")
    found = await discover(timeout)
    if not found:
        print(
            "Nothing answered. If you're on WSL2, mDNS needs mirrored networking "
            "(networkingMode=mirrored in .wslconfig), or pass --host to `pair`."
        )
        return 1
    for name, ip in found.items():
        print(f"  {ip:<15}  {name}")
    return 0


async def _pick_host(host: str | None) -> str | None:
    if host:
        return host
    found = await discover(5.0)
    if not found:
        print("No device found via mDNS. Re-run with --host <ip>.", file=sys.stderr)
        return None
    if len(found) == 1:
        name, ip = next(iter(found.items()))
        print(f"Found {name} at {ip}")
        return ip
    items = list(found.items())
    for i, (name, ip) in enumerate(items, 1):
        print(f"  {i}) {ip:<15}  {name}")
    choice = await asyncio.to_thread(input, "Which one? ")
    try:
        return items[int(choice) - 1][1]
    except (ValueError, IndexError):
        print("Invalid choice.", file=sys.stderr)
        return None


CodeReader = Callable[[str], Awaitable[str]]


async def _read_code(prompt: str) -> str:
    # input() blocks; running it in a thread keeps the event loop (and the
    # open pairing connection) alive while the user types.
    return await asyncio.to_thread(input, prompt)


async def _cmd_pair(
    host_arg: str | None, read_code: CodeReader = _read_code, remote_factory: Callable[..., Any] = AndroidTVRemote
) -> int:
    """Pair once, so later connections are trusted without a code.

    1. Make sure there is a usable client certificate and key: generate them
       if missing, and set unreadable ones aside (they'd fail every attempt).
    2. Read the Shield's name and MAC from its certificate (no code shown yet).
    3. Start pairing: the Shield shows a code on the TV. Send back the code
       the user typed; the Shield now trusts our certificate. Up to 3 tries.
    4. Connect with it once to prove it works, then save host, name and MAC.
       The MAC is how a running server recognizes the Shield if its address
       changes (ShieldClient.rediscover); a running server also notices the
       new credentials and reconnects with them, no restart needed.

    read_code and remote_factory are parameters so tests can pair with the
    simulated Shield without a terminal.
    """
    host = await _pick_host(host_arg)
    if not host:
        return 1

    ensure_private_dir()
    settings = load_settings(host_override=host)
    problem = check_credentials(settings)
    if problem:
        for path in (settings.cert_path, settings.key_path):
            path.replace(path.with_name(path.name + ".broken"))
        print(f"Set aside the old credentials ({problem}); making new ones.")
    remote = remote_factory(
        CLIENT_NAME,
        str(settings.cert_path),
        str(settings.key_path),
        host,
        api_port=settings.port,
        pair_port=settings.pairing_port,
    )
    # The library writes the key with the process umask (often 0644). A 077
    # umask makes it private from the moment it exists, not just after chmod.
    old_umask = os.umask(0o077)
    try:
        generated = await remote.async_generate_cert_if_missing()
    finally:
        os.umask(old_umask)
    if generated:
        print(f"Generated a new client certificate in {config_dir()}")
    lock_down_credentials(settings)

    try:
        name, mac = await asyncio.wait_for(remote.async_get_name_and_mac(), 10.0)
    except (TimeoutError, CannotConnect, OSError) as exc:
        print(f"Can't reach {host}: {exc}\nIs the Shield awake and on the same network?", file=sys.stderr)
        return 1
    print(f"Pairing with {name} ({mac}). A code will appear on the TV screen.")

    for attempt in range(1, 4):
        try:
            await remote.async_start_pairing()
            code = (await read_code("Enter the code shown on the TV: ")).strip().upper()
            await remote.async_finish_pairing(code)
            break
        except EOFError:
            # stdin isn't a terminal (e.g. launched from a tool or with < /dev/null).
            print("\nNo input to read the code from; run `pair` in an interactive terminal.", file=sys.stderr)
            remote.disconnect()
            return 1
        except CannotConnect as exc:
            # The pairing port stopped answering (the Shield went to sleep, or
            # left the network) between attempts. Used to escape as a traceback.
            print(f"Lost the Shield while pairing ({exc or 'no answer on the pairing port'}).", file=sys.stderr)
            remote.disconnect()
            return 1
        except (InvalidAuth, ConnectionClosed) as exc:
            # InvalidAuth: the code doesn't match (the library checks its first
            # byte before sending). ConnectionClosed: the TV rejected it, or
            # Cancel was pressed, or it timed out. Either way a new code is shown.
            print(f"That didn't work ({_pairing_failure(exc)}).")
            if attempt == 3:
                print("Giving up after 3 attempts.", file=sys.stderr)
                remote.disconnect()
                return 1
            print("Starting over; a new code will appear.")

    lock_down_credentials(settings)
    # Prove the credential works before saving anything.
    try:
        await asyncio.wait_for(remote.async_connect(), 15.0)
        info = remote.device_info
        model = f"{info['manufacturer']} {info['model']}" if info else "unknown device"
        print(f"Paired and connected: {model}")
    except (TimeoutError, CannotConnect, InvalidAuth, ConnectionClosed, OSError) as exc:
        print(f"Paired, but the verification connect failed: {exc or type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        remote.disconnect()
    save_config(host=host, name=name, mac=mac)
    print(f"Saved {name} at {host} (MAC {mac}). Credentials are in {config_dir()} (keep them private).")
    return 0


def _pairing_failure(exc: BaseException) -> str:
    if isinstance(exc, InvalidAuth):
        return "that code doesn't match the one on the TV"
    return "the TV rejected the code, the pairing was cancelled on the TV, or it timed out"


async def _cmd_adb_setup() -> int:
    """Enable the optional ADB tools (get_now_playing, get_remotes, reboot_shield).

    1. Generate an ADB key pair (if missing), private to this config dir.
    2. Connect with it. An untrusted key makes the Shield ask "Allow USB
       debugging?" on the TV, so wait long enough for someone to answer.
    3. Run the one fixed command once to prove it works, then save "adb": true.
    """
    # Imported here so `pair` and `discover` don't need adb-shell loaded.
    from .adb import NOW_PLAYING_COMMAND, ensure_adb_key, now_playing, run_command
    from .client import ShieldError

    settings = load_settings()
    if not settings.host:
        print("No Shield address yet. Run `mcp-server-shieldtv pair` first.", file=sys.stderr)
        return 1
    ensure_private_dir()
    if ensure_adb_key(settings):
        print(f"Generated an ADB key in {config_dir()}")
    lock_down_credentials(settings)
    print(
        f'Connecting to {settings.host}:5555. If the TV asks "Allow USB debugging?", tick '
        '"Always allow from this computer" and choose Allow (waiting up to 60s).'
    )
    try:
        text = await run_command(dataclasses.replace(settings, adb=True), NOW_PLAYING_COMMAND, auth_timeout_s=60.0)
    except ShieldError as exc:
        print(
            f"{exc}\nNetwork debugging must be on: Settings > Device Preferences > About, select Build "
            "seven times, then Developer options > Network debugging.",
            file=sys.stderr,
        )
        return 1
    save_config(adb=True)
    playing = now_playing(text, settings.app_name_for)
    print(f'ADB works. Now playing: {playing["title"] or "nothing"} ({playing["state"]}). Saved "adb": true.')
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mcp-server-shieldtv", description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--debug", action="store_true", help="log protocol traffic to stderr (SHIELDTV_DEBUG=1)")
    common.add_argument(
        "--no-redact",
        action="store_true",
        help="show LAN addresses and MACs in logs (SHIELDTV_LOG_UNREDACTED=1); keys and tokens stay hidden",
    )
    sub = parser.add_subparsers(dest="cmd")
    p_serve = sub.add_parser("serve", parents=[common], help="run the MCP server (stdio by default, or --http)")
    remote.add_http_arguments(p_serve, default_port=8712, default_path="/shieldtv/mcp")
    p_pair = sub.add_parser("pair", parents=[common], help="one-time pairing with a Shield")
    p_pair.add_argument("--host", help="Shield IP address (skips mDNS discovery)")
    p_disc = sub.add_parser("discover", parents=[common], help="list Android TV devices on the LAN")
    p_disc.add_argument("--timeout", type=float, default=5.0)
    sub.add_parser("adb-setup", parents=[common], help="enable the optional ADB tools (now playing, remotes, reboot)")
    return parser


def normalize(argv: list[str]) -> list[str]:
    """No subcommand means serve: `mcp-server-shieldtv --http` is `serve --http`."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["serve", *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(normalize(sys.argv[1:] if argv is None else argv))
    _setup_logging(args)

    if args.cmd == "pair":
        sys.exit(asyncio.run(_cmd_pair(args.host)))
    if args.cmd == "discover":
        sys.exit(asyncio.run(_cmd_discover(args.timeout)))
    if args.cmd == "adb-setup":
        sys.exit(asyncio.run(_cmd_adb_setup()))

    from .server import mcp  # imported late: `pair` and `discover` don't need the server code

    # stdio by default (the client launches us); --http runs a long-lived
    # service for an LXC behind Cloudflare Access (see remote.py).
    if args.http:
        try:
            remote.serve_http(mcp, remote.http_config(args))
        except remote.ConfigError as exc:
            parser.exit(2, f"mcp-server-shieldtv: {exc}\n")
    else:
        mcp.run()
