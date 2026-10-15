"""Entry point: ``mcp-server-shieldtv [serve] | pair | discover | adb-setup | doctor | simulate | call``.

``serve`` is the default, so ``mcp-server-shieldtv`` alone runs the MCP server
over stdio, and ``mcp-server-shieldtv --http`` works as well as
``mcp-server-shieldtv serve --http`` (the spelling the sibling servers accept).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import json
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
    unredacted = _unredacted(args)
    debug = getattr(args, "debug", False) or _flag(os.environ.get("SHIELDTV_DEBUG"))
    setup_logging(logging.DEBUG if debug else logging.INFO, redacted=not unredacted)
    if not debug:
        # The library logs every message it sends and receives at DEBUG; keep INFO readable.
        logging.getLogger("androidtvremote2").setLevel(logging.INFO)


def _unredacted(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "no_redact", False)) or _flag(os.environ.get("SHIELDTV_LOG_UNREDACTED"))


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
       ASSUMPTION S-PAIRING.
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


async def _cmd_doctor(args: argparse.Namespace, redacted: bool) -> int:
    from .doctor import render, run_doctor, to_json

    report = await run_doctor(load_settings(), timeout=args.timeout, finder=None if args.no_mdns else _mdns_finder)
    print(to_json(report, redacted) if args.json else render(report, redacted))
    return 0 if report.ok else 1


async def _mdns_finder(timeout: float) -> dict[str, str]:
    return await discover(timeout)


async def _cmd_simulate(args: argparse.Namespace) -> int:
    """Run a simulated Shield and point a config directory at it.

    Without --paired, pair with it as with a real one, in another terminal:
    the code "on the TV" is printed here. The simulator trusts the client
    certificate from that directory (see sim/fake_shield.py for why).
    """
    from pathlib import Path

    from .sim.fake_shield import FakeShield

    home = Path(args.config_dir).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    home.chmod(0o700)

    def client_cert() -> bytes | None:
        path = home / "cert.pem"
        return path.read_bytes() if path.exists() else None

    def show(code: str) -> None:
        print(f"\n  The TV shows the pairing code: {code}\n", flush=True)

    shield = FakeShield(host=args.host, remote_port=args.port, client_cert=client_cert, on_code=show)
    try:
        await shield.start()
    except OSError as exc:
        print(f"Couldn't start the simulated Shield: {exc}", file=sys.stderr)
        return 1
    os.environ["SHIELDTV_CONFIG_DIR"] = str(home)
    save_config(port=shield.remote_port, pairing_port=shield.pairing_port)
    if args.paired:
        from androidtvremote2.certificate_generator import generate_selfsigned_cert

        settings = load_settings()
        if check_credentials(settings) is not None or not settings.cert_path.exists():
            cert, key = generate_selfsigned_cert(CLIENT_NAME)
            settings.cert_path.write_bytes(cert)
            settings.key_path.write_bytes(key)
            lock_down_credentials(settings)
        shield.pair_with(settings.cert_path.read_bytes())
        save_config(host=args.host, name=shield.name, mac=shield.mac)
    print(f"Simulated {shield.name} (MAC {shield.mac}) on {args.host}:")
    print(f"  remote port {shield.remote_port}, pairing port {shield.pairing_port} (saved in {home}/config.json)")
    print("It implements the protocol as androidtvremote2 speaks it; see assumptions.py for what is unconfirmed.\n")
    print("In another terminal:")
    print(f"  export SHIELDTV_CONFIG_DIR={home}")
    if not args.paired:
        print(f"  mcp-server-shieldtv pair --host {args.host}      # type the code shown here")
    print("  mcp-server-shieldtv doctor --no-mdns")
    print("  mcp-server-shieldtv call get_status")
    print("Ctrl+C to stop.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await shield.stop()
    return 0


async def _cmd_call(tool: str, raw_args: list[str], wait_s: float = 5.0) -> int:
    """One tool call through the real MCP layer, as the model would make it."""
    from mcp import Client

    from .server import client, mcp

    tool_args: dict[str, object] = {}
    for pair in raw_args:
        key, sep, raw = pair.partition("=")
        if not sep:
            print(f"arguments are key=value, got {pair!r}", file=sys.stderr)
            return 2
        try:
            tool_args[key] = json.loads(raw)  # repeat=3 -> 3
        except json.JSONDecodeError:
            tool_args[key] = raw  # app=netflix -> a string
    async with Client(mcp) as c:
        if tool == "tools":
            for t in (await c.list_tools()).tools:
                print(f"{t.name:<16} {(t.description or '').splitlines()[0]}")
            return 0
        # The server connects in the background; give it a moment, as a
        # long-running server would have had
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s
        sc = client()
        while sc.settings.paired and not sc.available and not sc.auth_failed and loop.time() < deadline:
            await asyncio.sleep(0.1)
        result = await c.call_tool(tool, tool_args)
    if result.is_error:
        print(" ".join(getattr(part, "text", "") for part in result.content) or "error", file=sys.stderr)
        return 1
    body = result.structured_content
    if isinstance(body, dict) and set(body) == {"result"}:
        body = body["result"]
    print(json.dumps(body, indent=2, ensure_ascii=False))
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
    p_serve.add_argument(
        "--dry-run",
        action="store_true",
        help="read the Shield, but send nothing that changes anything (SHIELDTV_DRY_RUN=1)",
    )
    remote.add_http_arguments(p_serve, default_port=8712, default_path="/shieldtv/mcp")
    p_pair = sub.add_parser("pair", parents=[common], help="one-time pairing with a Shield")
    p_pair.add_argument("--host", help="Shield IP address (skips mDNS discovery)")
    p_disc = sub.add_parser("discover", parents=[common], help="list Android TV devices on the LAN")
    p_disc.add_argument("--timeout", type=float, default=5.0)
    sub.add_parser("adb-setup", parents=[common], help="enable the optional ADB tools (now playing, remotes, reboot)")
    doc = sub.add_parser(
        "doctor", parents=[common], help="check config, network and pairing, layer by layer (read-only)"
    )
    doc.add_argument("--json", action="store_true", help="machine-readable output")
    doc.add_argument("--timeout", type=float, default=5.0, help="seconds per step (default 5)")
    doc.add_argument("--no-mdns", action="store_true", help="skip the mDNS search")
    sim = sub.add_parser("simulate", parents=[common], help="run a simulated Shield, for testing without one")
    sim.add_argument("--config-dir", required=True, metavar="DIR", help="config directory to point at the simulator")
    sim.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    sim.add_argument("--port", type=int, default=0, help="remote port (default: a free one; pairing uses another)")
    sim.add_argument("--paired", action="store_true", help="skip pairing: make credentials and trust them")
    cal = sub.add_parser("call", parents=[common], help="call one tool as the model would and print the result")
    cal.add_argument("tool", help="tool name, or 'tools' to list them")
    cal.add_argument("args", nargs="*", metavar="key=value", help="tool arguments (values are JSON if they parse)")
    cal.add_argument("--dry-run", action="store_true", help="send nothing that changes anything")
    return parser


def normalize(argv: list[str]) -> list[str]:
    """No subcommand means serve: `mcp-server-shieldtv --http` is `serve --http`."""
    if not argv or (argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version")):
        return ["serve", *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(normalize(sys.argv[1:] if argv is None else argv))
    if getattr(args, "dry_run", False):
        os.environ["SHIELDTV_DRY_RUN"] = "1"  # the server's lifespan reads settings from the environment
    _setup_logging(args)

    if args.cmd == "pair":
        sys.exit(asyncio.run(_cmd_pair(args.host)))
    if args.cmd == "discover":
        sys.exit(asyncio.run(_cmd_discover(args.timeout)))
    if args.cmd == "adb-setup":
        sys.exit(asyncio.run(_cmd_adb_setup()))
    if args.cmd == "doctor":
        sys.exit(asyncio.run(_cmd_doctor(args, redacted=not _unredacted(args))))
    if args.cmd == "simulate":
        with contextlib.suppress(KeyboardInterrupt):
            sys.exit(asyncio.run(_cmd_simulate(args)))
        return
    if args.cmd == "call":
        sys.exit(asyncio.run(_cmd_call(args.tool, args.args)))

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
