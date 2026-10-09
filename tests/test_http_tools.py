"""The real Shield tools over Streamable HTTP, behind the Cloudflare Access check.

test_remote.py (shared with the sibling servers) tests the transport with a
one-tool server. This runs *this* server end to end: a uvicorn server on a
free port, MCP over HTTP, Access assertions signed with a throwaway RSA key
(so nothing here needs Cloudflare), and the simulated Shield behind it all,
paired over the wire like a real one.
"""

from __future__ import annotations

import socket
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio
import httpx2
import jwt
import pytest
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from shieldtv_mcp import remote, server

pytestmark = pytest.mark.anyio

TEAM = "example.cloudflareaccess.com"
AUD = "shieldtv-aud"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
POLICY = remote.AccessPolicy(TEAM, (AUD,))


def token(**overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "nick@example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


async def public_key(_token: str) -> object:
    return KEY.public_key()


@asynccontextmanager
async def serving() -> AsyncIterator[str]:
    cfg = remote.HttpConfig(path="/shieldtv/mcp", access=POLICY)
    app = remote.build_app(server.mcp, cfg, remote.AccessVerifier(POLICY, public_key))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(srv.serve, [sock])
        while not srv.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}/shieldtv/mcp"
        srv.should_exit = True


def client(url: str, assertion: str | None) -> Client:
    headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


async def test_tools_work_over_http_with_a_valid_assertion(paired):
    async with serving() as url, client(url, token()) as c:
        for _ in range(100):  # the server connects to the Shield in the background
            status = (await c.call_tool("get_status", {})).structured_content
            if status["reachable"]:
                break
            await anyio.sleep(0.05)
        assert (status["name"], status["power"]) == ("SHIELD Android TV", "on")
        result = (await c.call_tool("launch_app", {"app": "netflix"})).structured_content
        assert result["outcome"] == "done"
    assert paired.launched == ["https://www.netflix.com/title"]


@pytest.mark.parametrize(
    "assertion",
    [
        None,  # no header: didn't come through Access
        "garbage",
        token(exp=int(time.time()) - 3600),  # expired
        token(aud=["someone-elses-app"]),  # wrong audience
        token(iss="https://evil.cloudflareaccess.com"),  # wrong issuer
    ],
    ids=["missing", "garbage", "expired", "wrong-audience", "wrong-issuer"],
)
async def test_bad_assertions_never_reach_the_shield(paired, assertion):
    async with serving() as url, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: assertion} if assertion else {}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "send_key"}}
        resp = await http.post(url, json=body, headers=headers)
        assert (resp.status_code, resp.json()) == (403, {"error": "forbidden"})
    assert paired.keys == [] and paired.launched == []
