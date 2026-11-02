"""Standard MCP ToolAnnotations presets for LAN hardware/IoT servers.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Every tool carries explicit annotations because the MCP spec treats an
unannotated tool as destructive, non-idempotent and open-world by default.
"""

from __future__ import annotations

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
