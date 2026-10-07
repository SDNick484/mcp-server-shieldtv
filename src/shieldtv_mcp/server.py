"""MCP tool definitions. Deliberately small and typed: no raw key codes, no shell."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .client import ShieldClient, ShieldError
from .config import KeyName, load_settings

log = logging.getLogger(__name__)

_client: ShieldClient | None = None


def client() -> ShieldClient:
    assert _client is not None, "server lifespan has not started"
    return _client


@asynccontextmanager
async def lifespan(_server: MCPServer):
    global _client
    _client = ShieldClient(load_settings())
    await _client.start()
    try:
        yield
    finally:
        await _client.stop()
        _client = None


mcp = MCPServer(
    "shieldtv",
    instructions=(
        "Controls an NVIDIA Shield TV over the Android TV Remote protocol. "
        "Call get_status first to see whether it is on and what app is in the foreground. "
        "The Shield must have been paired once with `mcp-server-shieldtv pair`."
    ),
    lifespan=lifespan,
)

_READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
_ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
_ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)


@mcp.tool(annotations=_READ)
def get_status() -> dict[str, Any]:
    """Report whether the Shield is reachable, its power state, the foreground app, and volume.

    Note: this protocol does not expose what is playing (title/artist/position).
    """
    return client().snapshot()


@mcp.tool(annotations=_READ)
def list_apps() -> dict[str, str]:
    """List the app names launch_app accepts (friendly name -> package or link)."""
    return dict(client().settings.apps)


@mcp.tool(annotations=_ACT)
def send_key(key: KeyName, repeat: int = 1) -> str:
    """Press a remote-control key on the Shield. `repeat` presses it 1-10 times (e.g. to scroll)."""
    if not 1 <= repeat <= 10:
        raise ShieldError("repeat must be between 1 and 10.")
    c = client()
    for _ in range(repeat):
        c.send_key(key)
    return f"Sent {key} x{repeat}"


@mcp.tool(annotations=_ACT_IDEMPOTENT)
def launch_app(app: str) -> str:
    """Launch an app by friendly name (see list_apps), e.g. 'netflix' or 'youtube'."""
    c = client()
    target = c.settings.resolve_app(app)
    if target is None:
        names = ", ".join(sorted(c.settings.apps))
        raise ShieldError(f"Unknown app {app!r}. Known apps: {names}")
    c.launch(target)
    return f"Launched {app.strip().lower()} ({target})"


@mcp.tool(annotations=_ACT_IDEMPOTENT)
def set_power(state: Literal["on", "off"]) -> str:
    """Wake the Shield ('on') or put it to sleep ('off'). Uses WAKEUP/SLEEP, not the POWER toggle."""
    client().set_power(state == "on")
    return f"Requested power {state}"
