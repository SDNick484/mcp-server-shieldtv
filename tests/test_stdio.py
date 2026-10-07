"""End to end: launch the real entry point as a subprocess and talk MCP over stdio.

The in-process tests hand the MCPServer object straight to the client. This
one covers what they skip: the installed console script, `serve` being the
default subcommand, real settings loading, and the stdio transport across a
process boundary. The server runs unpaired, so no Shield or network is needed.

(It does not catch a stray print(): mcp 2.x's stdio_server keeps the real
stdout descriptor for JSON-RPC and points sys.stdout at stderr. Logging to
stderr is still the rule; the SDK is just a backstop.)
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest
from mcp import Client, StdioServerParameters

pytestmark = pytest.mark.anyio


async def test_serve_over_stdio(tmp_path):
    exe = shutil.which("mcp-server-shieldtv", path=os.path.dirname(sys.executable))
    if exe is None:
        pytest.skip("entry point not installed (pip install -e .)")
    env = {**os.environ, "SHIELDTV_CONFIG_DIR": str(tmp_path)}
    env.pop("SHIELDTV_HOST", None)
    params = StdioServerParameters(command=exe, args=[], env=env)
    async with Client(params) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        assert names == {
            "get_status",
            "get_now_playing",
            "get_remotes",
            "list_apps",
            "send_key",
            "launch_app",
            "set_power",
            "reboot_shield",
        }
        status = (await c.call_tool("get_status", {})).structured_content
        assert (status["paired"], status["reachable"]) == (False, False)
