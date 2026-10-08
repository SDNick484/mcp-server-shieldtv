"""The HTTP transport and the Cloudflare Access check.

Shared by the sibling servers along with remote.py; only the import below
differs between repos. The end-to-end tests run a real uvicorn server on a
free port and talk MCP to it over HTTP, signing Access assertions with a
throwaway RSA key, so nothing here needs Cloudflare.
"""

from __future__ import annotations

import argparse
import socket
import time
from contextlib import asynccontextmanager

import anyio
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from shieldtv_mcp import remote  # the only line that differs between repos

TEAM = "example.cloudflareaccess.com"
AUD = "aud-tag-123"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(**overrides: object) -> str:
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "nick@example.com"}
    claims.update(overrides)
    return jwt.encode({k: v for k, v in claims.items() if v is not None}, KEY, algorithm="RS256")


async def public_key(_token: str) -> object:
    return KEY.public_key()


POLICY = remote.AccessPolicy(TEAM, (AUD,))


# --- configuration -------------------------------------------------------------
def parse(argv: list[str], env: dict[str, str] | None = None) -> remote.HttpConfig:
    p = argparse.ArgumentParser()
    remote.add_http_arguments(p, default_port=8101, default_path="/thing/mcp")
    return remote.http_config(p.parse_args(argv), env or {})


def test_defaults_are_loopback_and_per_server():
    cfg = parse(["--http"])
    assert (cfg.bind, cfg.port, cfg.path, cfg.access) == ("127.0.0.1", 8101, "/thing/mcp", None)
    cfg.check()  # loopback without auth is fine


def test_environment_then_flags():
    env = {
        "MCP_HTTP_BIND": "10.0.0.5",
        "MCP_HTTP_PORT": "9000",
        "MCP_PUBLIC_HOSTS": "mcp.example.com",
        "CF_ACCESS_TEAM_DOMAIN": f"https://{TEAM}/",
        "CF_ACCESS_AUD": f"{AUD}, other",
        "MCP_ALLOWED_EMAILS": "Nick@Example.com",
    }
    cfg = parse([], env)
    assert (cfg.bind, cfg.port, cfg.public_hosts) == ("10.0.0.5", 9000, ("mcp.example.com",))
    assert cfg.access == remote.AccessPolicy(TEAM, (AUD, "other"), frozenset({"nick@example.com"}))
    assert parse(["--port", "9100", "--bind", "127.0.0.1"], env).port == 9100


@pytest.mark.parametrize(
    "argv,env,message",
    [
        (["--bind", "0.0.0.0"], {}, "Refusing to serve on 0.0.0.0 without Cloudflare Access"),
        (["--path", "mcp"], {}, "must start with '/'"),
        (["--path", "/healthz"], {}, "can't be /healthz"),
        ([], {"CF_ACCESS_TEAM_DOMAIN": TEAM}, "CF_ACCESS_AUD is empty"),
    ],
)
def test_unsafe_or_incomplete_settings_are_refused(argv, env, message):
    with pytest.raises(remote.ConfigError, match=message):
        parse(argv, env).check()


def test_insecure_flag_is_an_explicit_opt_out():
    parse(["--bind", "0.0.0.0", "--insecure-no-auth"]).check()


# --- the verifier ---------------------------------------------------------------
@pytest.mark.anyio
async def test_valid_assertion():
    claims = await remote.AccessVerifier(POLICY, public_key).verify(token())
    assert claims["email"] == "nick@example.com"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"aud": ["someone-elses-app"]}, "(?i)audience"),
        ({"iss": "https://evil.cloudflareaccess.com"}, "(?i)issuer"),
        ({"exp": int(time.time()) - 3600}, "(?i)expired"),
        ({"exp": None}, "(?i)exp"),
    ],
)
async def test_bad_assertions_are_denied(overrides, reason):
    with pytest.raises(remote.AccessDenied, match=reason):
        await remote.AccessVerifier(POLICY, public_key).verify(token(**overrides))


@pytest.mark.anyio
async def test_a_token_signed_by_another_key_is_denied():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = jwt.encode({"iss": f"https://{TEAM}", "aud": AUD, "iat": 1, "exp": 2**31}, other, algorithm="RS256")
    with pytest.raises(remote.AccessDenied, match="(?i)signature"):
        await remote.AccessVerifier(POLICY, public_key).verify(forged)


@pytest.mark.anyio
async def test_unsigned_tokens_are_denied():
    unsigned = jwt.encode({"iss": f"https://{TEAM}", "aud": AUD, "iat": 1, "exp": 2**31}, None, algorithm="none")
    with pytest.raises(remote.AccessDenied):
        await remote.AccessVerifier(POLICY, public_key).verify(unsigned)


@pytest.mark.anyio
async def test_email_allow_list():
    policy = remote.AccessPolicy(TEAM, (AUD,), frozenset({"nick@example.com"}))
    await remote.AccessVerifier(policy, public_key).verify(token(email="NICK@example.com"))
    with pytest.raises(remote.AccessDenied, match="not in MCP_ALLOWED_EMAILS"):
        await remote.AccessVerifier(policy, public_key).verify(token(email="guest@example.com"))


# --- end to end over real HTTP ------------------------------------------------------
def tiny_server(starts: list[int]) -> MCPServer:
    @asynccontextmanager
    async def lifespan(_s: MCPServer):
        starts.append(1)  # stands in for opening the device connection
        yield

    m = MCPServer("tiny", lifespan=lifespan)

    @m.tool()
    def ping() -> str:
        """Answer pong."""
        return "pong"

    return m


@asynccontextmanager
async def running(cfg: remote.HttpConfig, starts: list[int]):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = remote.build_app(tiny_server(starts), cfg, remote.AccessVerifier(POLICY, public_key))
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        while not server.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True


def mcp_client(url: str, headers: dict[str, str]) -> Client:
    return Client(streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers)))


CFG = remote.HttpConfig(path="/thing/mcp", public_hosts=("mcp.example.com",), access=POLICY)


@pytest.mark.anyio
async def test_tools_work_with_a_valid_assertion_and_the_lifespan_runs_once():
    starts: list[int] = []
    async with running(CFG, starts) as base:
        for _ in range(2):  # two separate sessions share one device connection
            async with mcp_client(f"{base}/thing/mcp", {remote.ACCESS_HEADER: token()}) as c:
                assert (await c.call_tool("ping", {})).content[0].text == "pong"
    assert starts == [1]


@pytest.mark.anyio
@pytest.mark.parametrize("headers", [{}, {remote.ACCESS_HEADER: "garbage"}, {"authorization": "Bearer x"}])
async def test_requests_without_a_valid_assertion_get_403(headers):
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        resp = await http.post(f"{base}/thing/mcp", json=body, headers=headers)
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_health_check_needs_no_assertion():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert (resp.status_code, resp.json()) == (200, {"status": "ok"})


@pytest.mark.anyio
async def test_unexpected_host_is_rejected_even_with_a_valid_assertion():
    # DNS-rebinding protection: only the public host and loopback are accepted.
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: token(), "host": "evil.example.net"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        resp = await http.post(f"{base}/thing/mcp", json=body, headers=headers)
        assert resp.status_code in (400, 421)


@pytest.mark.anyio
async def test_wildcard_bind_keeps_host_checks_on():
    # Binding to every interface with no --public-host used to switch the
    # Host checks off, leaving a LAN server open to DNS rebinding. Now only
    # loopback (and any --public-host) is accepted.
    cfg = remote.HttpConfig(bind="0.0.0.0", path="/thing/mcp", access=POLICY)
    sec = remote.transport_security(cfg)
    assert sec.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in sec.allowed_hosts and not any("evil" in h for h in sec.allowed_hosts)
    lan = remote.transport_security(remote.HttpConfig(bind="0.0.0.0", public_hosts=("192.168.1.10",)))
    assert "192.168.1.10:*" in lan.allowed_hosts and "http://192.168.1.10:*" in lan.allowed_origins
