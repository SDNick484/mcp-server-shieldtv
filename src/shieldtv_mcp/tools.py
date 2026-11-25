"""Standard MCP ToolAnnotations presets for LAN hardware/IoT servers, and `trim_tools`.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Every tool carries explicit annotations because the MCP spec treats an
unannotated tool as destructive, non-idempotent and open-world by default.
"""

from __future__ import annotations

import re
from typing import Any

from mcp.types import ToolAnnotations

# Closed-world LAN reads (get_status, list_*).
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

# Non-idempotent, non-destructive device actions (pressing VolumeUp twice is +2).
ACT = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)

# Idempotent state setters (starting an already-running activity, setting volume to 40, pausing).
ACT_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

# Actions that discard user state or progress (quitting a running game, loading a save state, resetting).
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

# LAN broadcast/multicast discovery (SSDP, eISCP broadcast, mDNS).
OPEN_WORLD_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)


# --- Trimming what tools/list sends -------------------------------------------------------------
#
# Every tool's name, description and input schema is loaded into the model's context, so their size
# is paid on every conversation. `trim_tools(mcp)`, called once after the last `@mcp.tool()`, removes
# the parts that carry no meaning for a model. It changes only what is advertised: arguments are
# still validated against the function's own signature.

_NULL = {"type": "null"}
# Keys whose value maps names to schemas: a name there ("title", "default") is not a schema keyword.
_NAME_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SECTION_TAG = re.compile(r"</?(?:use_case|important_notes)>")


def trim_schema(schema: Any) -> Any:
    """A copy of a JSON schema without generated `title`s, and with `anyOf: [X, null], default: null`
    (how an optional argument is generated) flattened to `X`: not being required already says it."""
    if isinstance(schema, list):
        return [trim_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in _NAME_MAPS and isinstance(value, dict):
            out[key] = {name: trim_schema(sub) for name, sub in value.items()}
        else:
            out[key] = trim_schema(value)
    any_of = out.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2 and _NULL in any_of and out.get("default", 0) is None:
        other = next((branch for branch in any_of if branch != _NULL), None)
        if isinstance(other, dict):
            del out["anyOf"], out["default"]
            out = {**other, **out}
    return out


def trim_description(text: str) -> str:
    """A description without the `<use_case>`/`<important_notes>` wrappers, with hard-wrapped lines
    joined. A bullet, a line that opens with another tag, and a new paragraph each stay on their own line."""
    blocks: list[str] = []
    fresh = True  # the next line starts a block rather than continuing the last one
    for raw in text.splitlines():
        line = _SECTION_TAG.sub("", raw).strip()
        if not line:
            fresh = True
        elif fresh or line.startswith(("- ", "<")):
            blocks.append(line)
            fresh = False
        else:
            blocks[-1] += " " + line
    return "\n".join(blocks)


def trim_tools(mcp: Any) -> None:
    """Trim the description and input schema of every tool registered on an `MCPServer` so far.

    `_tool_manager` is private, but the SDK offers no public way to change a registered tool."""
    for tool in mcp._tool_manager.list_tools():
        tool.parameters = trim_schema(tool.parameters)
        if tool.description:
            tool.description = trim_description(tool.description)
