"""MCP tool definitions. Deliberately small and typed: no raw key codes, no shell.

How a decorated function becomes a tool the model can call:
  - ``@mcp.tool()`` registers it; it then appears in the client's ``tools/list``.
  - The function's *docstring* becomes the tool's ``description``. It is the
    model's only documentation, so it is written for the model, not for us.
  - The *signature* becomes the ``inputSchema`` (JSON Schema): ``Literal`` turns
    into an enum, ``Field(ge=..., le=...)`` into minimum/maximum, and defaults
    make arguments optional. The SDK validates every call against it.
  - The *return type* becomes the ``outputSchema`` when it is structured
    (a TypedDict, model or dict), and the value is sent as structured content.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server import MCPServer
from pydantic import Field
from typing_extensions import TypedDict

from .adb import (
    NowPlaying,
    RebootResult,
    RemotesReport,
    read_now_playing,
    read_remotes,
    reboot_and_check,
    reboot_dry_run,
)
from .client import ShieldClient, ShieldError, Status
from .config import ALLOWED_KEYS, DEFAULT_APPS, KeyName, load_settings
from .lookup import unknown_with_suggestions
from .ratelimit import TokenBucket, refusal
from .tools import ACT, ACT_IDEMPOTENT, DESTRUCTIVE, READ_ONLY

log = logging.getLogger(__name__)

# One ShieldClient for the whole server process. Tools are plain functions the
# SDK calls, so they reach the shared connection through client(), not through
# an argument. It is None outside the lifespan (e.g. when merely imported).
_client: ShieldClient | None = None
_REBOOT_BUCKET = TokenBucket(capacity=1.0, rate=1.0 / 120.0)


def client() -> ShieldClient:
    assert _client is not None, "server lifespan has not started"
    return _client


# The lifespan runs once per server run: the code before `yield` at startup,
# the code after it at shutdown. That is where the long-lived Shield connection
# is opened and closed, so tools never connect on their own.
@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global _client
    settings = load_settings()
    for problem in settings.problems:
        log.warning("Config: %s", problem)
    _client = ShieldClient(settings)
    await _client.start()
    try:
        yield
    finally:
        await _client.stop()
        _client = None


# `instructions` is sent to the client in the initialize handshake; clients
# typically add it to the model's context as guidance for using these tools.
mcp = MCPServer(
    "shieldtv",
    instructions=(
        "Physical role: Media Source. Connects into Onkyo AVR input 'strm-box' (which routes video downstream "
        "to the TV display and plays master audio). Controls an NVIDIA Shield TV over the Android TV Remote protocol. "
        "Call shield_get_status first to see whether it is on and what app is in the foreground, "
        "and shield_get_now_playing for the title and play state. "
        "The Shield must have been paired once with `mcp-server-shieldtv pair`."
    ),
    lifespan=lifespan,
)

# Tool annotations are behavioral hints sent in tools/list. Clients use them to
# decide, e.g., whether to ask the user before a call. A tool without them is
# assumed by the spec to be destructive, non-idempotent and open-world.


# --- what an action returns ----------------------------------------------------------------
# Every tool that changes something returns an ActionResult, the same shape as
# the sibling servers' (Onkyo, Harmony, Sofabaton): structured, so a client can
# read `outcome` and `sent` without parsing prose, with `detail` one sentence
# for the user. Failures aren't an outcome: they're isError results (ShieldError).
Outcome = Literal["done", "unchanged", "dry_run"]


class ActionResult(TypedDict):
    shield: str  # its name from pairing, or its address
    outcome: Outcome
    detail: str
    sent: list[str]  # what went to the Shield (or, in a dry run, would have)
    warnings: list[str]


def _result(c: ShieldClient, detail: str, sent: list[str], outcome: Outcome = "done") -> ActionResult:
    warnings: list[str] = []
    if outcome == "dry_run":
        detail = f"DRY RUN, nothing sent: would {detail}"
        if not c.available:
            warnings.append("The Shield isn't reachable right now, so the real call would fail.")
    return {
        "shield": c.settings.name or c.settings.host or "the Shield",
        "outcome": outcome,
        "detail": detail,
        "sent": sent,
        "warnings": warnings,
    }


@mcp.tool(title="Get Shield status", annotations=READ_ONLY)
def shield_get_status() -> Status:
    """Start here. Query Shield TV power state, reachability, foreground app, and volume.

    <use_case>
    Inspect Shield TV power, connectivity, active foreground package, and volume before issuing commands.
    </use_case>

    <important_notes>
    - When unreachable, stale is true and values represent last known state.
    - Video output routes to the Onkyo AVR input 'strm-box'.
    </important_notes>
    """
    return client().snapshot()


@mcp.tool(title="Get what's playing", annotations=READ_ONLY)
async def shield_get_now_playing() -> NowPlaying:
    """Report what is playing: app, title, subtitle (artist or channel), play state, position in seconds.

    <use_case>
    Retrieve playback metadata and playback state of media currently active on the Shield.
    </use_case>

    <important_notes>
    - Requires ADB connection established via `mcp-server-shieldtv adb-setup`.
    - position_s is null for live streaming TV.
    </important_notes>
    """
    return await read_now_playing(client().settings)


@mcp.tool(title="Check Bluetooth remotes", annotations=READ_ONLY)
async def shield_get_remotes() -> RemotesReport:
    """List the Shield's Bluetooth remotes (e.g. a Harmony hub) and whether each works.

    <use_case>
    Diagnose unresponsive physical or hub remotes paired to the Shield over Bluetooth.
    </use_case>

    <important_notes>
    - Requires ADB connection (`mcp-server-shieldtv adb-setup`).
    - Reports "stuck" state when a connected remote stops sending button events (common after sleep/reboot).
    </important_notes>
    """
    return await read_remotes(client().settings)


@mcp.tool(title="Reboot the Shield", annotations=DESTRUCTIVE)
async def shield_reboot() -> RebootResult:
    """Restart the Shield TV.

    <use_case>
    Reboot the device when software is unresponsive or remotes require a cold reset.
    </use_case>

    <important_notes>
    - Disruptive: stops active playback and disconnects network services.
    - Rate limited to 1 reboot per 120s to prevent rapid boot loops.
    - Requires ADB connection (`mcp-server-shieldtv adb-setup`).
    </important_notes>
    """
    wait = _REBOOT_BUCKET.take(1.0)
    if wait > 0:
        raise ShieldError(refusal("reboot the Shield", wait, "Shield TV"))
    settings = client().settings
    if settings.dry_run:
        return reboot_dry_run(settings)
    return await reboot_and_check(settings)


@mcp.tool(title="List launchable apps", annotations=READ_ONLY)
def shield_list_apps() -> dict[str, str]:
    """List the app names launch_app accepts (friendly name -> the link it opens).

    <use_case>
    Discover valid app names for use with `shield_launch_app`.
    </use_case>

    <important_notes>
    - Returns mapped friendly names from configuration.
    </important_notes>
    """
    return {name: app.target for name, app in client().settings.apps.items()}


# Constraints written into the type become JSON Schema the model sees
# ("minimum": 1, "maximum": 10), and the SDK validates calls against it
# before our function runs, so there is no hand-written range check below.
Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press the key (1-10).")]


@mcp.tool(title="Press a remote key", annotations=ACT)
def shield_send_key(key: KeyName, repeat: Repeat = 1) -> ActionResult:
    """Press a remote-control key on the Shield, e.g. DPAD_DOWN with repeat=3 to move down three rows.

    <use_case>
    Navigate Android TV interfaces, menus, and controls using directional pad, enter, back, and home.
    </use_case>

    <important_notes>
    - Only sends allowed key enum values; power toggles and raw text typing are not supported.
    </important_notes>
    """
    c = client()
    sent = [f"KEYCODE_{key}"] * repeat
    if c.settings.dry_run:
        return _result(c, f"press {key} x{repeat}", sent, "dry_run")
    for _ in range(repeat):
        c.send_key(key)
    return _result(c, f"Pressed {key} x{repeat}", sent)


@mcp.tool(title="Launch an app", annotations=ACT_IDEMPOTENT)
async def shield_launch_app(
    app: Annotated[str, Field(description="Friendly app name from shield_list_apps, e.g. 'netflix'.")],
) -> ActionResult:
    """Launch an application on the Shield TV.

    <use_case>
    Launch streaming media apps (e.g. Netflix, YouTube, Plex) by friendly name.
    </use_case>

    <important_notes>
    - Video output requires Onkyo AVR input set to 'strm-box' and TV input set to AVR HDMI port.
    - Confirms that the application is running in the foreground before returning.
    - Unknown app names suggest closest matches from configured apps.
    </important_notes>
    """
    c = client()
    resolved = c.settings.resolve_app(app)
    if resolved is None:
        candidates = list(c.settings.apps.keys())
        msg = unknown_with_suggestions(
            "Known apps",
            app,
            candidates,
            fallback=f"Known apps: {', '.join(sorted(candidates))}.",
        )
        raise ShieldError(msg)
    name = app.strip().lower()
    sent = [f"app_link {resolved.target}"]
    if c.settings.dry_run:
        return _result(c, f"launch {name} ({resolved.target})", sent, "dry_run")
    package = await c.launch(resolved)
    return _result(c, f"Launched {name} ({package} is in the foreground)", sent)


@mcp.tool(title="Wake or sleep the Shield", annotations=ACT_IDEMPOTENT)
async def shield_set_power(state: Literal["on", "off"]) -> ActionResult:
    """Wake the Shield ('on') or put it to sleep ('off').

    <use_case>
    Manage device power state.
    </use_case>

    <important_notes>
    - Uses discrete WAKEUP and SLEEP commands rather than ambiguous toggles.
    - Idempotent: returns 'unchanged' if already in requested state.
    </important_notes>
    """
    c = client()
    on = state == "on"
    word = "on" if on else "in standby"
    if c.available and c.is_on is on:
        return _result(c, f"The Shield is already {word}", [], "unchanged")
    sent = ["KEYCODE_WAKEUP" if on else "KEYCODE_SLEEP"]
    if c.settings.dry_run:
        return _result(c, "wake the Shield" if on else "put the Shield to sleep", sent, "dry_run")
    await c.set_power(on)
    return _result(c, f"The Shield is {word}", sent)


# ---------------------------------------------------------------------------
# Resources: context a *client* reads (e.g. @-mentioned in Claude Code), as
# opposed to tools the model calls. They carry reference data that is useful
# up front and costs nothing to read: no command goes to the Shield.
#
#   shieldtv://apps   the apps launch_app accepts: link, package, and where the entry comes from
#   shieldtv://keys   the keys send_key accepts, and the ones deliberately left out
# ---------------------------------------------------------------------------
@mcp.resource("shieldtv://apps", name="apps", title="Apps launch_app can open, and how", mime_type="application/json")
def apps_resource() -> str:
    settings = client().settings
    return json.dumps(
        {
            name: {
                "link": app.target,
                "package": app.package,
                # Defaults were checked on the owner's Shield (ASSUMPTION S-APP-LINKS);
                # config.json entries are yours, unchecked by anyone
                "source": "default" if DEFAULT_APPS.get(name) == app else "config.json",
            }
            for name, app in sorted(settings.apps.items())
        },
        indent=1,
    )


@mcp.resource("shieldtv://keys", name="keys", title="Keys send_key can press", mime_type="application/json")
def keys_resource() -> str:
    return json.dumps(
        {
            "allowed": sorted(ALLOWED_KEYS),
            "left_out": {
                "POWER": "a toggle with ambiguous state; set_power uses WAKEUP/SLEEP",
                "SEARCH": "starts a voice session",
                "SETTINGS": "opens system settings",
                "MUTE": "Android's microphone mute (VOLUME_MUTE mutes the sound)",
                "text:": "typing arbitrary text",
            },
        },
        indent=1,
    )


# ---------------------------------------------------------------------------
# Prompts: workflows the *user* picks (e.g. /mcp__shieldtv__watch in Claude
# Code). Text with the arguments filled in; the model carries it out with the
# tools. They only use this server's tools: a cross-device "movie night" is a
# README example, so no server depends on another being connected.
# ---------------------------------------------------------------------------
@mcp.prompt(title="Watch something on the Shield")
def shield_watch(app: str, what: str = "") -> str:
    """Wake the Shield, open an app, and get to what you want to watch."""
    goal = f" and find {what!r}" if what else ""
    return (
        f"Open {app} on the Shield{goal}. Please:\n"
        "1. Call shield_get_status. If reachable is false, tell me the error and stop.\n"
        '2. If power is "standby", call shield_set_power with state="on".\n'
        f"3. Call shield_launch_app with app={app!r}. If it says the app is unknown, "
        "call shield_list_apps and ask me which one I meant.\n"
        + (
            f"4. To find {what!r}, use shield_send_key with DPAD_* keys and DPAD_CENTER, a few presses at a time, and "
            "ask me what's on screen when you can't tell (you can't see the TV). Don't type text: it isn't "
            "available.\n"
            if what
            else "4. Stop there and tell me it's open.\n"
        )
        + "5. If ADB is set up, shield_get_now_playing tells you what is playing; otherwise ask me."
    )


@mcp.prompt(title="My remote stopped working")
def shield_remotes_not_working() -> str:
    """Check the Shield's Bluetooth remotes and say how to fix one that's connected but dead."""
    return (
        "My remote isn't controlling the Shield. Please:\n"
        "1. Call shield_get_status to check the Shield is reachable and on.\n"
        "2. Call shield_get_remotes (needs ADB; if it says ADB isn't set up, tell me to run "
        "`mcp-server-shieldtv adb-setup` and stop).\n"
        '3. For any remote whose state is "stuck", repeat the advice shield_get_remotes gives. Do not reboot the '
        "Shield unless I ask: a reboot is what usually causes this."
    )
