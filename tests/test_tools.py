"""MCP tools, called through a real MCP client connected in-process.

This tests the contract the model actually sees: tools/list (names, titles,
annotations, input and output schemas), argument validation, and tool
results, not just the Python functions underneath.
"""

from __future__ import annotations

import pytest
from mcp import Client
from mcp.types import ElicitResult

from shieldtv_mcp import server
from shieldtv_mcp.client import ShieldClient
from shieldtv_mcp.config import ALLOWED_KEYS

from .conftest import wait_until

pytestmark = pytest.mark.anyio


async def say_yes(context, params) -> ElicitResult:
    """The person's answer to a costly tool's prompt (shield_reboot asks before it runs)."""
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.fixture
async def mcp_client(settings, fake, monkeypatch):
    # The server's lifespan builds the ShieldClient; swap in one using the fake.
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp, elicitation_callback=say_yes) as c:
        await wait_until(lambda: server.client().available)
        yield c


async def tools(c: Client) -> dict:
    return {t.name: t for t in (await c.list_tools()).tools}


def text(result) -> str:
    return result.content[0].text


def detail(result) -> str:
    assert not result.is_error, text(result)
    return result.structured_content["detail"]


# --- tools/list --------------------------------------------------------------
async def test_tool_names(mcp_client):
    assert set(await tools(mcp_client)) == {
        "shield_get_status",
        "shield_get_now_playing",
        "shield_get_remotes",
        "shield_list_apps",
        "shield_send_key",
        "shield_launch_app",
        "shield_set_power",
        "shield_reboot",
    }


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
            # Only a reboot is disruptive enough for clients to confirm first.
            assert a.destructive_hint is (t.name == "shield_reboot") and a.idempotent_hint is not None, t.name


async def test_annotations_match_behavior(mcp_client):
    t = await tools(mcp_client)
    assert t["shield_get_status"].annotations.read_only_hint is True
    assert t["shield_list_apps"].annotations.read_only_hint is True
    assert t["shield_get_now_playing"].annotations.read_only_hint is True
    assert t["shield_get_remotes"].annotations.read_only_hint is True
    assert t["shield_reboot"].annotations.destructive_hint is True  # interrupts playback
    assert t["shield_send_key"].annotations.idempotent_hint is False  # DPAD_DOWN twice moves twice
    assert t["shield_launch_app"].annotations.idempotent_hint is True
    assert t["shield_set_power"].annotations.idempotent_hint is True  # WAKEUP/SLEEP, not a toggle


async def test_send_key_schema(mcp_client):
    props = (await tools(mcp_client))["shield_send_key"].input_schema["properties"]
    # Literal[...] becomes a JSON Schema enum: the model can only pick from it.
    assert set(props["key"]["enum"]) == ALLOWED_KEYS
    assert (props["repeat"]["minimum"], props["repeat"]["maximum"], props["repeat"]["default"]) == (1, 10, 1)


async def test_set_power_schema(mcp_client):
    props = (await tools(mcp_client))["shield_set_power"].input_schema["properties"]
    assert set(props["state"]["enum"]) == {"on", "off"}


@pytest.mark.parametrize("name", ["shield_get_now_playing", "shield_get_remotes", "shield_reboot"])
async def test_adb_tools_take_no_arguments(mcp_client, name):
    # They run commands over ADB, so nothing the model writes may reach them.
    assert not (await tools(mcp_client))[name].input_schema.get("properties")


async def test_get_now_playing_output_schema(mcp_client):
    t = (await tools(mcp_client))["shield_get_now_playing"]
    assert set(t.output_schema["required"]) == {"state", "app_package", "app", "title", "subtitle", "position_s"}


async def test_get_now_playing_without_adb_says_how_to_enable(mcp_client):
    result = await mcp_client.call_tool("shield_get_now_playing", {})
    assert result.is_error
    assert "mcp-server-shieldtv adb-setup" in text(result)


async def test_get_status_publishes_output_schema(mcp_client):
    schema = (await tools(mcp_client))["shield_get_status"].output_schema
    assert set(schema["required"]) == {
        "host",
        "name",
        "paired",
        "dry_run",
        "reachable",
        "stale",
        "as_of",
        "error",
        "power",
        "current_app_package",
        "current_app",
        "volume",
        "device",
    }


# --- tools/call --------------------------------------------------------------
async def test_get_status(mcp_client):
    result = await mcp_client.call_tool("shield_get_status", {})
    assert not result.is_error
    status = result.structured_content
    assert status.pop("as_of").endswith("+00:00")  # when the values arrived, UTC
    assert status == {
        "host": "192.0.2.10",
        "name": None,
        "paired": True,
        "dry_run": False,
        "reachable": True,
        "stale": False,
        "error": None,
        "power": "on",
        "current_app_package": "com.netflix.ninja",
        "current_app": "netflix",
        "volume": {"level": 10, "max": 100, "muted": False},
        "device": {"manufacturer": "NVIDIA", "model": "SHIELD Android TV", "sw_version": "11"},
    }


async def test_list_apps(mcp_client):
    apps = (await mcp_client.call_tool("shield_list_apps", {})).structured_content
    assert apps["netflix"] == "https://www.netflix.com/title"
    assert apps["mine"] == "com.example.mine"


async def test_send_key_repeat(mcp_client, fake):
    result = await mcp_client.call_tool("shield_send_key", {"key": "DPAD_DOWN", "repeat": 3})
    assert result.structured_content == {
        "shield": "192.0.2.10",
        "outcome": "done",
        "detail": "Pressed DPAD_DOWN x3",
        "sent": ["KEYCODE_DPAD_DOWN"] * 3,
        "warnings": [],
    }
    assert fake.keys == ["DPAD_DOWN"] * 3


# --- the volume-up cap -------------------------------------------------------------------------
# VOLUME_UP reaches the TV or receiver over HDMI-CEC, which can't report the level back, so every press is a
# step louder. WindowLimit itself is tested in test_ratelimit.py (shared); these test how send_key uses it.
def capped(limit: int = 20) -> list[float]:
    from shieldtv_mcp.ratelimit import WindowLimit

    now = [0.0]
    server.client().volume_up_window = WindowLimit(limit, 300, clock=lambda: now[0])
    return now


async def test_volume_up_is_capped_per_window(mcp_client, fake):
    now = capped()
    for _ in range(2):
        assert not (await mcp_client.call_tool("shield_send_key", {"key": "VOLUME_UP", "repeat": 10})).is_error
    now[0] = 299.0
    result = await mcp_client.call_tool("shield_send_key", {"key": "VOLUME_UP"})
    assert result.is_error
    for expected in [
        "at most 20 volume-up presses in any 5 minutes",
        "try again in 1s",
        "onkyo_set_volume",
        "webos_set_volume",
        "max_volume",
    ]:
        assert expected in text(result), expected
    assert fake.keys == ["VOLUME_UP"] * 20  # the refused press was not sent
    now[0] = 300.0
    assert not (await mcp_client.call_tool("shield_send_key", {"key": "VOLUME_UP", "repeat": 10})).is_error


async def test_volume_up_refuses_whole_calls(mcp_client, fake):
    capped(limit=5)
    await mcp_client.call_tool("shield_send_key", {"key": "VOLUME_UP", "repeat": 4})
    result = await mcp_client.call_tool("shield_send_key", {"key": "VOLUME_UP", "repeat": 2})
    assert result.is_error and "refusing to press VOLUME_UP x2" in text(result)
    assert fake.keys == ["VOLUME_UP"] * 4


async def test_volume_down_and_mute_are_never_capped(mcp_client, fake):
    capped(limit=1)
    for key, repeat in (("VOLUME_UP", 1), ("VOLUME_DOWN", 5), ("VOLUME_MUTE", 1)):
        assert not (await mcp_client.call_tool("shield_send_key", {"key": key, "repeat": repeat})).is_error
    assert len(fake.keys) == 7


@pytest.mark.parametrize(
    "args", [{"key": "POWER"}, {"key": "text:hi"}, {"key": "HOME", "repeat": 11}, {"key": "HOME", "repeat": 0}]
)
async def test_send_key_rejects_bad_args_before_the_shield(mcp_client, fake, args):
    result = await mcp_client.call_tool("shield_send_key", args)
    assert result.is_error
    assert fake.keys == []


async def test_launch_app(mcp_client, fake):
    fake.launch_outcomes["https://www.youtube.com"] = "com.google.android.youtube.tv"
    result = await mcp_client.call_tool("shield_launch_app", {"app": " YouTube "})
    assert detail(result) == "Launched youtube (com.google.android.youtube.tv is in the foreground)"
    assert result.structured_content["sent"] == ["app_link https://www.youtube.com"]
    assert fake.launched == ["https://www.youtube.com"]


async def test_failed_launch_tells_the_model_what_happened(mcp_client, fake):
    fake.launch_outcomes["https://www.youtube.com"] = "reject"
    result = await mcp_client.call_tool("shield_launch_app", {"app": "youtube"})
    assert result.is_error
    assert "rejected the launch request" in text(result)


async def test_launch_unknown_app_lists_known_ones(mcp_client, fake):
    result = await mcp_client.call_tool("shield_launch_app", {"app": "com.evil.app"})
    assert result.is_error
    assert "Known apps has no 'com.evil.app'" in text(result)
    assert "netflix" in text(result)
    assert fake.launched == []  # raw package names are not a back door


async def test_set_power(mcp_client, fake):
    assert detail(await mcp_client.call_tool("shield_set_power", {"state": "off"})) == "The Shield is in standby"
    assert detail(await mcp_client.call_tool("shield_set_power", {"state": "on"})) == "The Shield is on"
    assert fake.keys == ["SLEEP", "WAKEUP"]


async def test_set_power_to_the_current_state_sends_nothing(mcp_client, fake):
    result = (await mcp_client.call_tool("shield_set_power", {"state": "on"})).structured_content
    assert (result["outcome"], result["detail"], result["sent"]) == ("unchanged", "The Shield is already on", [])
    assert fake.keys == []


# --- dry run ------------------------------------------------------------------------------
@pytest.fixture
async def dry_client(config_dir, fake, monkeypatch):
    monkeypatch.setenv("SHIELDTV_DRY_RUN", "1")
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp, elicitation_callback=say_yes) as c:
        await wait_until(lambda: server.client().available)
        yield c


@pytest.mark.parametrize(
    ("tool", "args", "sent"),
    [
        ("shield_send_key", {"key": "HOME", "repeat": 2}, ["KEYCODE_HOME", "KEYCODE_HOME"]),
        ("shield_launch_app", {"app": "youtube"}, ["app_link https://www.youtube.com"]),
        ("shield_set_power", {"state": "off"}, ["KEYCODE_SLEEP"]),
    ],
)
async def test_dry_run_reports_and_sends_nothing(dry_client, fake, tool, args, sent):
    result = (await dry_client.call_tool(tool, args)).structured_content
    assert result["outcome"] == "dry_run" and result["sent"] == sent
    assert result["detail"].startswith("DRY RUN, nothing sent: would ")
    assert (fake.keys, fake.launched) == ([], [])
    assert (await dry_client.call_tool("shield_get_status", {})).structured_content["dry_run"] is True


async def test_dry_run_still_validates(dry_client, fake):
    result = await dry_client.call_tool("shield_launch_app", {"app": "com.evil.app"})
    assert result.is_error and "Known apps has no" in text(result)
    result = await dry_client.call_tool("shield_reboot", {})
    assert result.is_error and "ADB isn't set up" in text(result)  # as the real call would


async def test_dry_run_warns_when_the_real_call_would_fail(dry_client, fake):
    fake.push("is_available", False)
    result = (await dry_client.call_tool("shield_send_key", {"key": "HOME"})).structured_content
    assert result["warnings"] == ["The Shield isn't reachable right now, so the real call would fail."]


async def test_unreachable_shield_explains_itself(mcp_client, fake):
    # The model must hear *why*, not just "Error executing tool shield_send_key".
    fake.push("is_available", False)
    result = await mcp_client.call_tool("shield_send_key", {"key": "HOME"})
    assert result.is_error
    assert "Can't reach the Shield at 192.0.2.10" in text(result)


async def test_unpaired_server_still_answers_status(config_dir, fake, monkeypatch):
    (config_dir / "key.pem").unlink()
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp) as c:
        status = (await c.call_tool("shield_get_status", {})).structured_content
        assert (status["paired"], status["reachable"]) == (False, False)
        result = await c.call_tool("shield_send_key", {"key": "HOME"})
        assert result.is_error and "mcp-server-shieldtv pair" in text(result)


async def test_reboot_rate_limit_refusal(mcp_client):
    # First reboot call takes the single token
    await mcp_client.call_tool("shield_reboot", {})
    # May fail with ADB isn't set up, but token was consumed
    res2 = await mcp_client.call_tool("shield_reboot", {})
    assert res2.is_error
    assert "Rate limit on Shield TV" in text(res2)
    assert "try again in" in text(res2)


async def test_launch_app_fuzzy_suggestions(mcp_client, fake):
    result = await mcp_client.call_tool("shield_launch_app", {"app": "netfli"})
    assert result.is_error
    assert "Closest matches: 'netflix'" in text(result)


# --- authorization tiers ----------------------------------------------------------------------
async def test_reboot_asks_first_and_does_not_run_without_a_yes(settings, fake, monkeypatch):
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp) as c:  # no elicitation callback: this app can't show the prompt
        await wait_until(lambda: server.client().available)
        result = await c.call_tool("shield_reboot", {})
    assert result.is_error and "needs your confirmation" in text(result)


def test_set_power_is_power_off_only_for_off():
    from shieldtv_mcp.remote import tier_of

    spec = tier_of(server.shield_set_power)
    assert spec is not None
    assert spec.resolve({"state": "off"}) == "power-off"
    assert spec.resolve({"state": "on"}) == "everyday"
