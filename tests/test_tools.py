"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and tool
results, not just the Python functions underneath.
"""

from __future__ import annotations

import pytest
from mcp import Client

from shieldtv_mcp import server
from shieldtv_mcp.client import ShieldClient
from shieldtv_mcp.config import ALLOWED_KEYS

from .conftest import wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def mcp_client(settings, fake, monkeypatch):
    # The server's lifespan builds the ShieldClient; swap in one using the fake.
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp) as c:
        await wait_until(lambda: server.client().available)
        yield c


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


# --- tools/list --------------------------------------------------------------
async def test_tool_names(mcp_client):
    assert set(await tools(mcp_client)) == {"get_status", "list_apps", "send_key", "launch_app", "set_power"}


async def test_every_tool_has_title_description_and_annotations(mcp_client):
    # Without annotations, the spec says a client should assume the worst
    # (destructive, non-idempotent, open-world). State them explicitly.
    for t in (await tools(mcp_client)).values():
        assert t.title, t.name
        assert t.description, t.name
        a = t.annotations
        assert a is not None, t.name
        assert a.read_only_hint is not None and a.open_world_hint is False, t.name
        if not a.read_only_hint:
            assert a.destructive_hint is False and a.idempotent_hint is not None, t.name


async def test_annotations_match_behavior(mcp_client):
    t = await tools(mcp_client)
    assert t["get_status"].annotations.read_only_hint is True
    assert t["list_apps"].annotations.read_only_hint is True
    assert t["send_key"].annotations.idempotent_hint is False  # DPAD_DOWN twice moves twice
    assert t["launch_app"].annotations.idempotent_hint is True
    assert t["set_power"].annotations.idempotent_hint is True  # WAKEUP/SLEEP, not a toggle


async def test_send_key_schema(mcp_client):
    props = (await tools(mcp_client))["send_key"].input_schema["properties"]
    # Literal[...] becomes a JSON Schema enum: the model can only pick from it.
    assert set(props["key"]["enum"]) == ALLOWED_KEYS
    assert (props["repeat"]["minimum"], props["repeat"]["maximum"], props["repeat"]["default"]) == (1, 10, 1)


async def test_set_power_schema(mcp_client):
    props = (await tools(mcp_client))["set_power"].input_schema["properties"]
    assert set(props["state"]["enum"]) == {"on", "off"}


async def test_get_status_publishes_output_schema(mcp_client):
    schema = (await tools(mcp_client))["get_status"].output_schema
    assert set(schema["required"]) == {
        "host",
        "paired",
        "reachable",
        "power",
        "current_app_package",
        "current_app",
        "volume",
        "device",
    }


# --- tools/call --------------------------------------------------------------
async def test_get_status(mcp_client):
    result = await mcp_client.call_tool("get_status", {})
    assert not result.is_error
    assert result.structured_content == {
        "host": "192.0.2.10",
        "paired": True,
        "reachable": True,
        "power": "on",
        "current_app_package": "com.netflix.ninja",
        "current_app": "netflix",
        "volume": {"level": 10, "max": 100, "muted": False},
        "device": {"manufacturer": "NVIDIA", "model": "SHIELD Android TV", "sw_version": "11"},
    }


async def test_list_apps(mcp_client):
    apps = (await mcp_client.call_tool("list_apps", {})).structured_content
    assert apps["netflix"] == "com.netflix.ninja"
    assert apps["mine"] == "com.example.mine"


async def test_send_key_repeat(mcp_client, fake):
    result = await mcp_client.call_tool("send_key", {"key": "DPAD_DOWN", "repeat": 3})
    assert text(result) == "Sent DPAD_DOWN x3"
    assert fake.keys == ["DPAD_DOWN"] * 3


@pytest.mark.parametrize(
    "args", [{"key": "POWER"}, {"key": "text:hi"}, {"key": "HOME", "repeat": 11}, {"key": "HOME", "repeat": 0}]
)
async def test_send_key_rejects_bad_args_before_the_shield(mcp_client, fake, args):
    result = await mcp_client.call_tool("send_key", args)
    assert result.is_error
    assert fake.keys == []


async def test_launch_app(mcp_client, fake):
    result = await mcp_client.call_tool("launch_app", {"app": " YouTube "})
    assert text(result) == "Launched youtube (com.google.android.youtube.tv)"
    assert fake.launched == ["com.google.android.youtube.tv"]


async def test_launch_unknown_app_lists_known_ones(mcp_client, fake):
    result = await mcp_client.call_tool("launch_app", {"app": "com.evil.app"})
    assert result.is_error
    assert "Unknown app 'com.evil.app'. Known apps:" in text(result)
    assert "netflix" in text(result)
    assert fake.launched == []  # raw package names are not a back door


async def test_set_power(mcp_client, fake):
    assert text(await mcp_client.call_tool("set_power", {"state": "off"})) == "Requested power off"
    assert text(await mcp_client.call_tool("set_power", {"state": "on"})) == "Requested power on"
    assert fake.keys == ["SLEEP", "WAKEUP"]


async def test_unreachable_shield_explains_itself(mcp_client, fake):
    # The model must hear *why*, not just "Error executing tool send_key".
    fake.push("is_available", False)
    result = await mcp_client.call_tool("send_key", {"key": "HOME"})
    assert result.is_error
    assert "Can't reach the Shield at 192.0.2.10" in text(result)


async def test_unpaired_server_still_answers_status(config_dir, fake, monkeypatch):
    (config_dir / "key.pem").unlink()
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp) as c:
        status = (await c.call_tool("get_status", {})).structured_content
        assert (status["paired"], status["reachable"]) == (False, False)
        result = await c.call_tool("send_key", {"key": "HOME"})
        assert result.is_error and "mcp-server-shieldtv pair" in text(result)
