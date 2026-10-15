"""MCP resources (context a client attaches) and prompts (workflows a user picks)."""

from __future__ import annotations

import json
import re

import pytest
from mcp import Client

from shieldtv_mcp import server
from shieldtv_mcp.client import ShieldClient
from shieldtv_mcp.config import ALLOWED_KEYS

from .conftest import wait_until

pytestmark = pytest.mark.anyio


@pytest.fixture
async def c(settings, fake, monkeypatch):
    monkeypatch.setattr(server, "ShieldClient", lambda s: ShieldClient(s, remote_factory=fake.build))
    async with Client(server.mcp) as client:
        await wait_until(lambda: server.client().available)
        yield client


async def read(c: Client, uri: str) -> dict:
    return json.loads((await c.read_resource(uri)).contents[0].text)


async def test_resources_are_listed(c):
    assert {str(r.uri) for r in (await c.list_resources()).resources} == {"shieldtv://apps", "shieldtv://keys"}


async def test_apps_resource_says_where_each_entry_comes_from(c, fake):
    apps = await read(c, "shieldtv://apps")
    assert apps["netflix"] == {
        "link": "https://www.netflix.com/title",
        "package": "com.netflix.ninja",
        "source": "default",
    }
    assert apps["mine"]["source"] == "config.json"  # the test config's own app
    assert fake.keys == [] and fake.launched == []  # reading sends nothing


async def test_keys_resource_matches_the_allow_list(c):
    keys = await read(c, "shieldtv://keys")
    assert set(keys["allowed"]) == ALLOWED_KEYS
    assert not set(keys["left_out"]) & ALLOWED_KEYS


TOOL_NAME = re.compile(r"\b(?:get|set|send|launch|list|reboot)_[a-z_]+\b")


@pytest.mark.parametrize(
    ("name", "args"),
    [("watch", {"app": "netflix"}), ("watch", {"app": "plex", "what": "The Bear"}), ("remotes_not_working", {})],
)
async def test_prompts_only_name_tools_that_exist(c, name, args):
    tools = {t.name for t in (await c.list_tools()).tools}
    text = (await c.get_prompt(name, args)).messages[0].content.text
    named = set(TOOL_NAME.findall(text))
    assert named and named <= tools, named - tools


async def test_watch_prompt_requires_an_app(c):
    prompts = {p.name: p for p in (await c.list_prompts()).prompts}
    assert {a.name for a in prompts["watch"].arguments or [] if a.required} == {"app"}
