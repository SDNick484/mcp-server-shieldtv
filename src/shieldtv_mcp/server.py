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

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import ShieldClient, ShieldError, Status
from .config import KeyName, load_settings

log = logging.getLogger(__name__)

# One ShieldClient for the whole server process. Tools are plain functions the
# SDK calls, so they reach the shared connection through client(), not through
# an argument. It is None outside the lifespan (e.g. when merely imported).
_client: ShieldClient | None = None


def client() -> ShieldClient:
    assert _client is not None, "server lifespan has not started"
    return _client


# The lifespan runs once per server run: the code before `yield` at startup,
# the code after it at shutdown. That is where the long-lived Shield connection
# is opened and closed, so tools never connect on their own.
@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    global _client
    _client = ShieldClient(load_settings())
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
        "Controls an NVIDIA Shield TV over the Android TV Remote protocol. "
        "Call get_status first to see whether it is on and what app is in the foreground. "
        "The Shield must have been paired once with `mcp-server-shieldtv pair`."
    ),
    lifespan=lifespan,
)

# Tool annotations are behavioral hints sent in tools/list. Clients use them to
# decide, e.g., whether to ask the user before a call. A tool without them is
# assumed by the spec to be destructive, non-idempotent and open-world, so
# every tool here states its hints explicitly:
#   read_only_hint   - changes nothing (status, app list)
#   destructive_hint - False: nothing here deletes data or is irreversible
#   idempotent_hint  - calling twice with the same args == calling once
#                      (true for launch/power, false for key presses: two
#                      DPAD_DOWNs move twice)
#   open_world_hint  - False: we only talk to one known device, not the internet
# They are hints, not enforcement: the allow-lists are what bound the model.
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@mcp.tool(title="Get Shield status", annotations=_READ)
def get_status() -> Status:
    """Report whether the Shield is reachable, its power state, the foreground app, and volume.

    Note: this protocol does not expose what is playing (title/artist/position).
    """
    return client().snapshot()


@mcp.tool(title="List launchable apps", annotations=_READ)
def list_apps() -> dict[str, str]:
    """List the app names launch_app accepts (friendly name -> package or link)."""
    return dict(client().settings.apps)


# Constraints written into the type become JSON Schema the model sees
# ("minimum": 1, "maximum": 10), and the SDK validates calls against it
# before our function runs, so there is no hand-written range check below.
Repeat = Annotated[int, Field(ge=1, le=10, description="How many times to press the key (1-10).")]


@mcp.tool(title="Press a remote key", annotations=_ACT)
def send_key(key: KeyName, repeat: Repeat = 1) -> str:
    """Press a remote-control key on the Shield, e.g. DPAD_DOWN with repeat=3 to move down three rows."""
    c = client()
    for _ in range(repeat):
        c.send_key(key)
    return f"Sent {key} x{repeat}"


@mcp.tool(title="Launch an app", annotations=_ACT_IDEMPOTENT)
def launch_app(
    app: Annotated[str, Field(description="Friendly app name from list_apps, e.g. 'netflix'.")],
) -> str:
    """Launch an app by friendly name (see list_apps), e.g. 'netflix' or 'youtube'."""
    c = client()
    target = c.settings.resolve_app(app)
    if target is None:
        names = ", ".join(sorted(c.settings.apps))
        raise ShieldError(f"Unknown app {app!r}. Known apps: {names}")
    c.launch(target)
    return f"Launched {app.strip().lower()} ({target})"


@mcp.tool(title="Wake or sleep the Shield", annotations=_ACT_IDEMPOTENT)
def set_power(state: Literal["on", "off"]) -> str:
    """Wake the Shield ('on') or put it to sleep ('off'). Uses WAKEUP/SLEEP, not the POWER toggle."""
    client().set_power(state == "on")
    return f"Requested power {state}"
