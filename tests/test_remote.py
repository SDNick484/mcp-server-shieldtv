"""The HTTP transport and the Cloudflare Access check.

Shared by the sibling servers along with remote.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync. The end-to-end tests run a real uvicorn server on a
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
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "user@example.com"}
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
        "MCP_ALLOWED_EMAILS": "User@Example.com",
    }
    cfg = parse([], env)
    assert (cfg.bind, cfg.port, cfg.public_hosts) == ("10.0.0.5", 9000, ("mcp.example.com",))
    assert cfg.access == remote.AccessPolicy(TEAM, (AUD, "other"), frozenset({"user@example.com"}))
    assert parse(["--port", "9100", "--bind", "127.0.0.1"], env).port == 9100


@pytest.mark.parametrize(
    "argv,env,message",
    [
        (["--port", "0"], {}, "--port must be between 1 and 65535"),
        (["--port", "-1"], {}, "--port must be between 1 and 65535"),
        (["--port", "65536"], {}, "--port must be between 1 and 65535"),
        (["--bind", "0.0.0.0"], {}, "Refusing to serve on 0.0.0.0 without Cloudflare Access"),
        (["--path", "mcp"], {}, "must start with '/'"),
        (["--path", "/healthz"], {}, "can't be /healthz"),
        ([], {"MCP_HTTP_PORT": "not-an-int"}, "must be an integer"),
        ([], {"CF_ACCESS_TEAM_DOMAIN": "evil.com/path", "CF_ACCESS_AUD": AUD}, "must be a hostname"),
        ([], {"CF_ACCESS_TEAM_DOMAIN": TEAM}, "CF_ACCESS_AUD is empty"),
        # A public name means a tunnel or proxy, which reaches a loopback bind too.
        (["--public-host", "mcp.example.com"], {}, "Refusing --public-host mcp.example.com without Cloudflare Access"),
        ([], {"MCP_PUBLIC_HOSTS": "mcp.example.com"}, "Refusing --public-host"),
    ],
)
def test_unsafe_or_incomplete_settings_are_refused(argv, env, message):
    with pytest.raises(remote.ConfigError, match=message):
        parse(argv, env).check()


def test_insecure_flag_is_an_explicit_opt_out():
    parse(["--bind", "0.0.0.0", "--insecure-no-auth"]).check()
    parse(["--bind", "0.0.0.0", "--public-host", "192.168.1.10", "--insecure-no-auth"]).check()


def test_a_public_host_is_fine_with_access():
    parse(["--public-host", "mcp.example.com"], {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD}).check()


def test_loopback_recognizes_localhost_ipv6_and_rejects_non_loopback():
    assert remote.HttpConfig(bind="localhost").loopback is True
    assert remote.HttpConfig(bind="::1").loopback is True
    assert remote.HttpConfig(bind="127.0.0.1").loopback is True
    assert remote.HttpConfig(bind="0.0.0.0").loopback is False
    assert remote.HttpConfig(bind="device.lan").loopback is False
    with pytest.raises(remote.ConfigError, match="Refusing to serve on device.lan"):
        remote.HttpConfig(bind="device.lan").check()


def test_certs_url_and_default_verifier_wiring():
    assert POLICY.certs_url == f"https://{TEAM}/cdn-cgi/access/certs"
    app = remote.build_app(tiny_server([]), CFG)
    assert isinstance(app, remote.AccessMiddleware)
    assert isinstance(app.verifier, remote.AccessVerifier)
    assert app.verifier.policy == POLICY


# --- the verifier ---------------------------------------------------------------
@pytest.mark.anyio
async def test_valid_assertion():
    claims = await remote.AccessVerifier(POLICY, public_key).verify(token())
    assert claims["email"] == "user@example.com"


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
    policy = remote.AccessPolicy(TEAM, (AUD,), frozenset({"user@example.com"}))
    await remote.AccessVerifier(policy, public_key).verify(token(email="USER@example.com"))
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
async def running(
    cfg: remote.HttpConfig,
    starts: list[int],
    health_check: remote.HealthChecker | None = None,
):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    verifier = remote.AccessVerifier(POLICY, public_key) if cfg.access else None
    app = remote.build_app(tiny_server(starts), cfg, verifier, health_check=health_check)
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
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {remote.ACCESS_HEADER: "garbage"},
        [(remote.ACCESS_HEADER.encode(), b"\xff\xfe")],
        {"authorization": "Bearer x"},
    ],
)
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
@pytest.mark.parametrize(
    "result,expected_status,expected_body",
    [
        (True, 200, {"status": "ok"}),
        ({"hub": "connected"}, 200, {"status": "ok", "hub": "connected"}),
        (False, 503, {"status": "degraded", "error": "health check returned False"}),
        ({"status": "degraded", "hub": "offline"}, 503, {"status": "degraded", "hub": "offline"}),
        ({"error": "timeout"}, 503, {"status": "degraded", "error": "timeout"}),
        ({"status": "ok", "error": "timeout"}, 503, {"status": "degraded", "error": "timeout"}),
    ],
)
async def test_health_check_custom_readiness(result, expected_status, expected_body):
    async def check():
        return result

    async with running(CFG, [], health_check=check) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert (resp.status_code, resp.json()) == (expected_status, expected_body)


@pytest.mark.anyio
async def test_health_check_exception_returns_503():
    async def broken():
        raise RuntimeError("hub unreachable")

    async with running(CFG, [], health_check=broken) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert (resp.status_code, resp.json()) == (503, {"status": "degraded", "error": "health check failed"})


@pytest.mark.anyio
async def test_options_preflight_succeeds_without_assertion():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.options(f"{base}/thing/mcp", headers={"origin": "https://claude.ai"})
        assert resp.status_code == 204
        assert resp.text == ""
        assert resp.headers["access-control-allow-origin"] == "https://claude.ai"
        assert resp.headers["access-control-allow-methods"] == "POST, GET, DELETE, OPTIONS"
        assert (
            resp.headers["access-control-allow-headers"]
            == "content-type, cf-access-jwt-assertion, authorization, mcp-session-id, mcp-protocol-version"
        )
        assert resp.headers["access-control-max-age"] == "86400"
        assert resp.headers["vary"] == "Origin"

        resp_no_origin = await http.options(f"{base}/thing/mcp")
        assert resp_no_origin.status_code == 204
        assert resp_no_origin.headers["access-control-allow-origin"] == "*"
        assert resp_no_origin.headers["vary"] == "Origin"


@pytest.mark.anyio
async def test_unexpected_host_is_rejected_even_with_a_valid_assertion():
    # DNS-rebinding protection: only the public host and loopback are accepted.
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        headers = {remote.ACCESS_HEADER: token(), "host": "evil.example.net"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        resp = await http.post(f"{base}/thing/mcp", json=body, headers=headers)
        assert resp.status_code in (400, 421)


@pytest.mark.anyio
async def test_wildcard_bind_keeps_host_checks_on(caplog):
    # Binding to every interface with no --public-host used to switch the
    # Host checks off, leaving a LAN server open to DNS rebinding. Now only
    # loopback (and any --public-host) is accepted.
    cfg = remote.HttpConfig(bind="0.0.0.0", path="/thing/mcp", access=POLICY)
    sec = remote.transport_security(cfg)
    assert sec.enable_dns_rebinding_protection is True
    assert "127.0.0.1:*" in sec.allowed_hosts and not any("evil" in h for h in sec.allowed_hosts)
    assert "Binding to 0.0.0.0 with no --public-host" in caplog.text
    lan = remote.transport_security(remote.HttpConfig(bind="0.0.0.0", public_hosts=("192.168.1.10",)))
    assert "192.168.1.10:*" in lan.allowed_hosts and "http://192.168.1.10:*" in lan.allowed_origins


def test_ipv6_bind_and_public_hosts_in_transport_security(caplog):
    v6_local = remote.transport_security(remote.HttpConfig(bind="::1"))
    assert "[::1]:*" in v6_local.allowed_hosts

    v6_lan = remote.transport_security(
        remote.HttpConfig(bind="2001:db8::10", public_hosts=("[2001:db8::10]",), insecure_no_auth=True)
    )
    assert "[2001:db8::10]:*" in v6_lan.allowed_hosts
    assert "http://[2001:db8::10]:*" in v6_lan.allowed_origins

    v6_wild = remote.transport_security(remote.HttpConfig(bind="::", insecure_no_auth=True))
    assert "[::]:*" not in v6_wild.allowed_hosts
    assert "Binding to :: with no --public-host" in caplog.text


# --- no Access: this machine only ----------------------------------------------------
LOCAL = remote.HttpConfig(path="/thing/mcp")  # loopback, no Access, no --insecure-no-auth


@pytest.mark.anyio
async def test_without_access_local_requests_work():
    async with running(LOCAL, []) as base, mcp_client(f"{base}/thing/mcp", {}) as c:
        assert (await c.call_tool("ping", {})).content[0].text == "pong"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    [
        {"cf-connecting-ip": "203.0.113.7", "cf-ray": "8a1b2c3d4e5f-SJC"},  # through a Cloudflare Tunnel
        {"x-forwarded-for": "203.0.113.7"},  # nginx, Caddy, Traefik, ...
        {"forwarded": "for=203.0.113.7"},  # RFC 7239
    ],
)
async def test_without_access_forwarded_requests_get_403(header):
    # A tunnel to 127.0.0.1 with the tunnel's Host header rewritten to localhost gets past the
    # bind and the Host check; the proxy's own headers still give it away.
    async with running(LOCAL, []) as base, httpx2.AsyncClient() as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        resp = await http.post(f"{base}/thing/mcp", json=body, headers=header)
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_insecure_no_auth_serves_forwarded_requests_too():
    cfg = remote.HttpConfig(path="/thing/mcp", insecure_no_auth=True)
    async with running(cfg, []) as base, mcp_client(f"{base}/thing/mcp", {"x-forwarded-for": "192.168.1.20"}) as c:
        assert (await c.call_tool("ping", {})).content[0].text == "pong"


@pytest.mark.anyio
async def test_health_check_refuses_forwarded_when_local_only():
    async with running(LOCAL, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz", headers={"x-forwarded-for": "203.0.113.7"})
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_health_check_head_request():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.head(f"{base}/healthz")
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_health_check_non_get_head_requires_assertion():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.post(f"{base}/healthz")
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_jwks_dos_protection_throttles_unknown_kid(monkeypatch):
    class FakeKey:
        def __init__(self, key_id: str) -> None:
            self.key_id = key_id

    class FakeJWKSet:
        def __init__(self, keys: list[FakeKey]) -> None:
            self.keys = keys

    class FakeCache:
        def __init__(self, jwk_set: FakeJWKSet) -> None:
            self._set = jwk_set

        def is_expired(self) -> bool:
            return False

        def get(self) -> FakeJWKSet:
            return self._set

    calls = 0

    class FakePyJWKClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.jwk_set_cache = FakeCache(FakeJWKSet([FakeKey("known-kid")]))

        def get_signing_key_from_jwt(self, _token: str) -> object:
            nonlocal calls
            calls += 1
            raise jwt.PyJWKClientError("unknown kid")

    monkeypatch.setattr(jwt, "PyJWKClient", FakePyJWKClient)
    # Start clock near 0 to ensure freshly booted machines (monotonic < 60s) don't falsely throttle first fetch
    fake_now = [5.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])

    verifier = remote.AccessVerifier(POLICY)
    headers1 = {"kid": "unknown-1"}
    headers2 = {"kid": "unknown-2"}
    headers3 = {"kid": "unknown-3"}
    now = int(time.time())
    claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "user@example.com"}
    tok1 = jwt.encode(claims, KEY, algorithm="RS256", headers=headers1)
    tok2 = jwt.encode(claims, KEY, algorithm="RS256", headers=headers2)
    tok3 = jwt.encode(claims, KEY, algorithm="RS256", headers=headers3)

    # First unknown kid triggers fetch
    with pytest.raises(remote.AccessDenied, match="unknown kid"):
        await verifier.verify(tok1)
    assert calls == 1

    # Second unknown kid within min_refetch_interval throttles without network fetch
    fake_now[0] = 10.0
    with pytest.raises(remote.AccessDenied, match="Unable to find a signing key"):
        await verifier.verify(tok2)
    assert calls == 1

    # After min_refetch_interval passes, network fetch is allowed again
    fake_now[0] = 70.0
    with pytest.raises(remote.AccessDenied, match="unknown kid"):
        await verifier.verify(tok3)
    assert calls == 2
