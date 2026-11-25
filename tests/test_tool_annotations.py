"""Standard MCP ToolAnnotations presets, and the trim that shortens what tools/list sends.

Shared by the sibling servers along with tools.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from shieldtv_mcp.tools import (
    ACT,
    ACT_IDEMPOTENT,
    DESTRUCTIVE,
    OPEN_WORLD_READ,
    READ_ONLY,
    trim_description,
    trim_schema,
    trim_tools,
)


def test_annotation_presets():
    assert READ_ONLY.read_only_hint is True and READ_ONLY.open_world_hint is False
    assert ACT.read_only_hint is False and ACT.destructive_hint is False and ACT.idempotent_hint is False
    assert ACT_IDEMPOTENT.idempotent_hint is True and ACT_IDEMPOTENT.destructive_hint is False
    assert DESTRUCTIVE.destructive_hint is True and DESTRUCTIVE.read_only_hint is False
    assert OPEN_WORLD_READ.read_only_hint is True and OPEN_WORLD_READ.open_world_hint is True


def test_trim_schema_drops_generated_titles():
    schema = {
        "properties": {"level": {"title": "Level", "type": "integer"}},
        "required": ["level"],
        "title": "set_volumeArguments",
        "type": "object",
    }
    assert trim_schema(schema) == {
        "properties": {"level": {"type": "integer"}},
        "required": ["level"],
        "type": "object",
    }


def test_trim_schema_keeps_a_parameter_named_title_or_default():
    schema = {
        "properties": {
            "title": {"title": "Title", "type": "string"},
            "default": {"title": "Default", "type": "boolean"},
        },
        "type": "object",
    }
    assert trim_schema(schema)["properties"] == {"title": {"type": "string"}, "default": {"type": "boolean"}}


def test_trim_schema_flattens_an_optional_that_defaults_to_null():
    prop = {
        "anyOf": [{"maximum": 255, "minimum": 1, "type": "integer"}, {"type": "null"}],
        "default": None,
        "description": "Effect speed 1-255.",
        "title": "Speed",
    }
    assert trim_schema({"properties": {"speed": prop}, "type": "object"})["properties"]["speed"] == {
        "maximum": 255,
        "minimum": 1,
        "type": "integer",
        "description": "Effect speed 1-255.",
    }


def test_trim_schema_keeps_other_unions_and_defaults():
    # A real default tells the model what omitting the argument does; a three-way union can't be flattened;
    # a nullable with a non-null default still accepts null on purpose.
    props = {
        "zone": {"default": "main", "enum": ["main", "zone2"], "type": "string"},
        "target": {"anyOf": [{"type": "string"}, {"type": "integer"}, {"type": "null"}], "default": None},
        "limit": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 20},
        "required_nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
    }
    assert trim_schema({"properties": props, "type": "object"})["properties"] == props


def test_trim_schema_reaches_nested_schemas_and_leaves_its_input_alone():
    schema = {
        "$defs": {"Color": {"title": "Color", "enum": ["red", "blue"], "type": "string"}},
        "properties": {
            "colors": {
                "anyOf": [{"items": {"$ref": "#/$defs/Color", "title": "C"}, "type": "array"}, {"type": "null"}],
                "default": None,
                "title": "Colors",
            }
        },
        "type": "object",
    }
    before = repr(schema)
    assert trim_schema(schema) == {
        "$defs": {"Color": {"enum": ["red", "blue"], "type": "string"}},
        "properties": {"colors": {"items": {"$ref": "#/$defs/Color"}, "type": "array"}},
        "type": "object",
    }
    assert repr(schema) == before


def test_trim_description_drops_the_section_tags_and_unwraps_lines():
    text = """
<use_case>
Search the library with the dashboard's filters: name or genre words,
genres and tags.
</use_case>
<important_notes>
- Filters combine with AND. Games with no data are excluded
  unless include_unknown.
- When nothing matches, it returns the closest games.
</important_notes>
<example>find_games(tags=["soulslike"])</example>
"""
    assert trim_description(text) == (
        "Search the library with the dashboard's filters: name or genre words, genres and tags.\n"
        "- Filters combine with AND. Games with no data are excluded unless include_unknown.\n"
        "- When nothing matches, it returns the closest games.\n"
        '<example>find_games(tags=["soulslike"])</example>'
    )


def test_trim_description_keeps_paragraphs_apart():
    assert trim_description('What changed, and when.\n\nAnswers "what was on\nat 9?".') == (
        'What changed, and when.\nAnswers "what was on at 9?".'
    )


@pytest.mark.anyio
async def test_trim_tools_changes_what_the_server_lists_but_not_what_it_accepts():
    mcp = MCPServer("trim-test")

    @mcp.tool()
    def set_speed(level: int, zone: str | None = None) -> str:
        """
        <use_case>
        Set the speed
        of a zone.
        </use_case>
        """
        return f"{level}:{zone}"

    trim_tools(mcp)

    (tool,) = await mcp.list_tools()
    assert tool.description == "Set the speed of a zone."
    assert tool.input_schema == {
        "properties": {"level": {"type": "integer"}, "zone": {"type": "string"}},
        "required": ["level"],
        "type": "object",
    }
    # The advertised schema is shorter; validation still runs on the function's own signature, so an
    # explicit null for an optional argument is accepted as before.
    registered = mcp._tool_manager.get_tool("set_speed")
    assert registered is not None
    assert registered.fn_metadata.arg_model.model_validate({"level": 3, "zone": None}).zone is None
