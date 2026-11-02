"""Standard MCP ToolAnnotations presets.

Shared by the sibling servers along with tools.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.
"""

from __future__ import annotations

from shieldtv_mcp.tools import ACT, ACT_IDEMPOTENT, DESTRUCTIVE, OPEN_WORLD_READ, READ_ONLY


def test_annotation_presets():
    assert READ_ONLY.read_only_hint is True and READ_ONLY.open_world_hint is False
    assert ACT.read_only_hint is False and ACT.destructive_hint is False and ACT.idempotent_hint is False
    assert ACT_IDEMPOTENT.idempotent_hint is True and ACT_IDEMPOTENT.destructive_hint is False
    assert DESTRUCTIVE.destructive_hint is True and DESTRUCTIVE.read_only_hint is False
    assert OPEN_WORLD_READ.read_only_hint is True and OPEN_WORLD_READ.open_world_hint is True
