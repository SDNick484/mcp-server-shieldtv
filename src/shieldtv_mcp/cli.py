"""Entry point: `mcp-server-shieldtv` (serve), `... pair`, `... discover`, `... adb-setup`."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import sys

from androidtvremote2 import (
    AndroidTVRemote,
    CannotConnect,
    ConnectionClosed,
    InvalidAuth,
)

from . import __version__
from .config import (
    CLIENT_NAME,
    config_dir,
    ensure_private_dir,
    load_settings,
    lock_down_credentials,
    save_config,
    save_host,
)
from .discovery import discover


def _setup_logging() -> None:
    # stdout belongs to the MCP stdio transport; logs must go to stderr.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")


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


async def _cmd_pair(host_arg: str | None) -> int:
    """Pair once, so later connections are trusted without a code.

    1. Generate a self-signed client certificate and key (if missing).
    2. Start pairing: the Shield shows a code on the TV.
    3. Send back the code the user typed. The Shield now trusts our certificate.
    4. Connect with it once to prove it works, and save the host.
    """
    host = await _pick_host(host_arg)
    if not host:
        return 1

    ensure_private_dir()
    settings = load_settings(host_override=host)
    remote = AndroidTVRemote(CLIENT_NAME, str(settings.cert_path), str(settings.key_path), host)
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
        name, mac = await remote.async_get_name_and_mac()
    except (TimeoutError, CannotConnect, OSError) as exc:
        print(f"Can't reach {host}: {exc}\nIs the Shield awake and on the same network?", file=sys.stderr)
        return 1
    print(f"Pairing with {name} ({mac}). A code will appear on the TV screen.")

    for attempt in range(3):
        try:
            await remote.async_start_pairing()
            # input() blocks; running it in a thread keeps the event loop (and
            # the open pairing connection) alive while the user types.
            code = (await asyncio.to_thread(input, "Enter the code shown on the TV: ")).strip()
            await remote.async_finish_pairing(code)
            break
        except EOFError:
            # stdin isn't a terminal (e.g. launched from a tool or with < /dev/null).
            print("\nNo input to read the code from; run `pair` in an interactive terminal.", file=sys.stderr)
            remote.disconnect()
            return 1
        except (InvalidAuth, ConnectionClosed):
            print("That didn't work (wrong code or the session timed out).")
            if attempt == 2:
                print("Giving up after 3 attempts.", file=sys.stderr)
                return 1
    save_host(host)
    lock_down_credentials(settings)

    # Prove the credential works before declaring victory.
    try:
        await remote.async_connect()
        info = remote.device_info
        model = f"{info['manufacturer']} {info['model']}" if info else "unknown device"
        print(f"Paired and connected: {model}")
    except (CannotConnect, InvalidAuth, ConnectionClosed, OSError) as exc:
        print(f"Paired, but the verification connect failed: {exc}", file=sys.stderr)
        return 1
    finally:
        remote.disconnect()
    print(f"Saved host {host}. Credentials are in {config_dir()} (keep them private).")
    return 0


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-server-shieldtv", description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="run the MCP server over stdio (default)")
    p_pair = sub.add_parser("pair", help="one-time pairing with a Shield")
    p_pair.add_argument("--host", help="Shield IP address (skips mDNS discovery)")
    p_disc = sub.add_parser("discover", help="list Android TV devices on the LAN")
    p_disc.add_argument("--timeout", type=float, default=5.0)
    sub.add_parser("adb-setup", help="enable the optional ADB tools (now playing, remotes, reboot)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args()

    if args.cmd == "pair":
        sys.exit(asyncio.run(_cmd_pair(args.host)))
    if args.cmd == "discover":
        sys.exit(asyncio.run(_cmd_discover(args.timeout)))
    if args.cmd == "adb-setup":
        sys.exit(asyncio.run(_cmd_adb_setup()))

    _setup_logging()
    from .server import mcp  # imported late: `pair` and `discover` don't need the server code

    mcp.run()
