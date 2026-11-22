"""The HTTP transport and the Cloudflare Access check.

Shared by the sibling servers along with remote.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync. The end-to-end tests run a real uvicorn server on a
free port and talk MCP to it over HTTP, signing Access assertions with a
throwaway RSA key, so nothing here needs Cloudflare.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import socket
import threading
import time
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer
from mcp.shared.exceptions import MCPError
from mcp.types import ElicitResult, InputRequiredResult

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
SHA = "0123456789abcdef0123456789abcdef01234567"
SHA_B = "fedcba9876543210fedcba9876543210fedcba98"
REPO = "SDNick484/mcp-server-demo"


@pytest.fixture(autouse=True)
def no_commit_file(monkeypatch, tmp_path):
    """No COMMIT file unless a test writes one: one beside this machine's venv mustn't leak into bodies."""
    monkeypatch.setenv(remote.COMMIT_FILE_ENV, str(tmp_path / "absent"))


def commit_file(monkeypatch, tmp_path, content: str | bytes) -> None:
    path = tmp_path / "COMMIT"
    if isinstance(content, str):
        path.write_text(content)
    else:
        path.write_bytes(content)
    monkeypatch.setenv(remote.COMMIT_FILE_ENV, str(path))


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
    parse(["--bind", "0.0.0.0", "--public-host", "192.0.2.10", "--insecure-no-auth"]).check()


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


# --- caller kinds and the automation settings ------------------------------------------
def test_automation_tiers_default_and_parse():
    assert parse(["--http"]).automation_tiers == frozenset({"read", "power-off"})
    assert remote.HttpConfig().automation_tiers == frozenset({"read", "power-off"})
    cfg = parse([], {"MCP_AUTOMATION_TIERS": "read, Everyday"})
    assert cfg.automation_tiers == frozenset({"read", "everyday"})
    cfg.check()
    # Set but empty narrows (automated callers get nothing); it never falls back to the wider default.
    assert parse([], {"MCP_AUTOMATION_TIERS": ""}).automation_tiers == frozenset()


@pytest.mark.parametrize("value,named", [("costly", "costly"), ("read,costly", "costly"), ("reed", "reed")])
def test_automation_tiers_refuse_costly_and_unknown(value, named):
    cfg = parse([], {"MCP_AUTOMATION_TIERS": value})
    with pytest.raises(remote.ConfigError, match="MCP_AUTOMATION_TIERS") as refused:
        cfg.check()
    assert named in str(refused.value)
    with pytest.raises(remote.ConfigError, match="MCP_AUTOMATION_TIERS"):
        remote.build_app(tiny_server([]), cfg)  # startup refuses, not just check()


def test_service_tokens_setting_parsed():
    env = {"CF_ACCESS_TEAM_DOMAIN": TEAM, "CF_ACCESS_AUD": AUD}
    assert parse([], env).access.service_tokens == frozenset()  # unset: none admitted
    cfg = parse([], {**env, "MCP_ALLOWED_SERVICE_TOKENS": "token-1.access, Token-2.access"})
    assert cfg.access.service_tokens == frozenset({"token-1.access", "Token-2.access"})  # IDs are exact
    assert "token-1.access" not in repr(cfg)  # like the e-mail allow-list, not for logs


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
    with pytest.raises(remote.AccessDenied, match="not in MCP_ALLOWED_EMAILS") as denied:
        await remote.AccessVerifier(policy, public_key).verify(token(email="guest@example.com"))
    # The rejected identity is named only enough to tell logins apart: the log line this becomes
    # may be pasted into an issue.
    assert "g***@example.com" in str(denied.value) and "guest@example.com" not in str(denied.value)
    with pytest.raises(remote.AccessDenied, match="this identity is not in MCP_ALLOWED_EMAILS"):
        await remote.AccessVerifier(policy, public_key).verify(token(email=""))


def automated_policy(*tokens: str, emails: frozenset[str] = frozenset()) -> remote.AccessPolicy:
    return remote.AccessPolicy(TEAM, (AUD,), emails, frozenset(tokens))


def service_token(**overrides: object) -> str:
    return token(email=None, common_name="token-1.access", **overrides)


READ_ONLY = frozenset({"read"})


@pytest.mark.anyio
async def test_caller_person_from_email():
    verifier = remote.AccessVerifier(POLICY, public_key)
    caller = verifier.caller(await verifier.verify(token(email="User@Example.com")), READ_ONLY)
    assert caller == remote.Caller("person", "user@example.com", frozenset(remote.TIERS))
    assert caller.tiers == {"read", "power-off", "everyday", "costly"}


@pytest.mark.anyio
async def test_caller_automated_from_admitted_common_name():
    verifier = remote.AccessVerifier(automated_policy("token-1.access"), public_key)
    caller = verifier.caller(await verifier.verify(service_token()), frozenset({"read", "power-off"}))
    assert caller == remote.Caller("automated", "token-1.access", frozenset({"read", "power-off"}))
    assert "costly" not in caller.tiers


@pytest.mark.anyio
async def test_service_token_refused_when_not_admitted():
    """Review Focus 1: no allow-lists at all, a service token (common_name only) gets in no more."""
    verifier = remote.AccessVerifier(POLICY, public_key)
    claims = await verifier.verify(service_token())  # signature, audience and expiry are fine
    with pytest.raises(remote.AccessDenied, match="unknown caller"):
        verifier.caller(claims, READ_ONLY)
    # Admitting some other token doesn't admit this one.
    other = remote.AccessVerifier(automated_policy("token-2.access"), public_key)
    with pytest.raises(remote.AccessDenied, match="unknown caller"):
        other.caller(claims, READ_ONLY)


@pytest.mark.anyio
async def test_service_token_admitted_alongside_email_allow_list():
    policy = automated_policy("token-1.access", emails=frozenset({"user@example.com"}))
    verifier = remote.AccessVerifier(policy, public_key)
    claims = await verifier.verify(service_token())  # no email, so the e-mail list doesn't apply
    assert verifier.caller(claims, READ_ONLY).kind == "automated"
    # ...but it still applies to anything that does carry one.
    with pytest.raises(remote.AccessDenied, match="not in MCP_ALLOWED_EMAILS"):
        await verifier.verify(token(email="guest@example.com", common_name="token-1.access"))


@pytest.mark.anyio
@pytest.mark.parametrize("claims", [{}, {"email": ""}, {"common_name": ""}, {"common_name": 7}, {"email": 7}])
async def test_jwt_with_neither_email_nor_common_name_refused(claims):
    verifier = remote.AccessVerifier(automated_policy("", "7"), public_key)
    with pytest.raises(remote.AccessDenied, match="unknown caller"):
        verifier.caller(claims, READ_ONLY)


@pytest.mark.anyio
async def test_a_signed_jwt_with_no_identity_is_refused_as_an_unknown_caller():
    verifier = remote.AccessVerifier(automated_policy("token-1.access"), public_key)
    claims = await verifier.verify(token(email=None))
    with pytest.raises(remote.AccessDenied, match="unknown caller"):
        verifier.caller(claims, READ_ONLY)


def test_caller_shown_masks_email():
    assert remote.Caller("person", "user@example.com", frozenset()).shown == "u***@example.com"
    assert remote.Caller("person", "nonsense", frozenset()).shown == "this identity"
    assert remote.Caller("automated", "token-1.access", frozenset()).shown == "token-1.access"
    assert remote.LOCAL.shown == "local"
    assert remote.Caller("local", "local", frozenset(remote.TIERS)) == remote.LOCAL
    assert remote.TIERS == ("read", "power-off", "everyday", "costly")


def test_policy_repr_hides_the_allow_list():
    """A config repr ends up in logs and tracebacks; the e-mail allow-list must not."""
    policy = remote.AccessPolicy(TEAM, (AUD,), frozenset({"user@example.com"}))
    assert "user@example.com" not in repr(policy)
    assert "user@example.com" not in repr(remote.HttpConfig(access=policy))
    assert policy.emails == frozenset({"user@example.com"})  # still there for the check itself


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
    server: MCPServer | None = None,
):
    import uvicorn

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    verifier = remote.AccessVerifier(cfg.access, public_key) if cfg.access else None
    app = remote.build_app(server or tiny_server(starts), cfg, verifier, health_check=health_check)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        while not server.started:
            await anyio.sleep(0.01)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True


@pytest.mark.anyio
async def test_shutdown_with_a_stuck_tool_call_still_runs_the_lifespan_shutdown():
    """What supervise-daemon's SIGTERM/5 must not hit: a tool call blocked on an unresponsive device
    keeps a response open, and uvicorn by default waits for it forever, so the lifespan shutdown
    (device disconnects) never runs before the SIGKILL. With serve_http's settings the response is
    closed within SHUTDOWN_TIMEOUT and the lifespan shutdown runs, inside the supervisor's budget."""
    import uvicorn

    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_s: MCPServer):
        events.append("start")
        yield
        events.append("stop")

    mcp = MCPServer("thing", lifespan=lifespan)

    @mcp.tool()
    async def stuck() -> str:
        """Never answers, like a TV that stopped replying mid-command."""
        await anyio.sleep(60)
        return "never"

    cfg = remote.HttpConfig(path="/thing/mcp")
    settings = remote.uvicorn_settings(cfg)
    assert settings["timeout_graceful_shutdown"] == remote.SHUTDOWN_TIMEOUT and settings["proxy_headers"] is False
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    # Everything serve_http passes except the address (the pre-bound socket stands in) and the log level
    config = uvicorn.Config(
        remote.build_app(mcp, cfg), **{k: v for k, v in settings.items() if k not in ("host", "port", "log_level")}
    )
    server = uvicorn.Server(config)
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        while not server.started:
            await anyio.sleep(0.01)

        async def call_stuck_tool() -> None:
            with anyio.move_on_after(remote.SHUTDOWN_TIMEOUT + 3):  # the call is expected to be cut off
                async with mcp_client(f"http://127.0.0.1:{port}/thing/mcp", {}) as c:
                    with contextlib.suppress(Exception):  # the server closes the connection under it
                        await c.call_tool("stuck", {})

        tg.start_soon(call_stuck_tool)
        await anyio.sleep(0.3)  # the call is in flight
        started = time.monotonic()
        server.should_exit = True  # what the SIGTERM handler sets
        with anyio.fail_after(remote.SHUTDOWN_TIMEOUT + 3):
            while "stop" not in events:
                await anyio.sleep(0.02)
        assert time.monotonic() - started < remote.SHUTDOWN_TIMEOUT + 2  # inside supervise-daemon's 5 s
    assert events == ["start", "stop"]


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


async def scope_seen_by_inner_app(verifier: remote.AccessVerifier | None, headers: list[tuple[bytes, bytes]]):
    seen: list[dict] = []

    async def inner(scope, receive, send):
        seen.append(scope)

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    app = remote.AccessMiddleware(inner, verifier, automation_tiers=frozenset({"read"}))
    await app({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, receive, send)
    return seen, sent


@pytest.mark.anyio
async def test_middleware_puts_caller_in_scope():
    assert remote.CALLER_STATE_KEY == "mcp_caller"
    verifier = remote.AccessVerifier(automated_policy("token-1.access"), public_key)
    header = remote.ACCESS_HEADER.encode()

    seen, _ = await scope_seen_by_inner_app(verifier, [(header, token().encode())])
    assert seen[0]["state"][remote.CALLER_STATE_KEY].kind == "person"
    assert seen[0]["state"][remote.CALLER_STATE_KEY].ident == "user@example.com"

    seen, _ = await scope_seen_by_inner_app(verifier, [(header, service_token().encode())])
    caller = seen[0]["state"][remote.CALLER_STATE_KEY]
    assert (caller.kind, caller.ident, caller.tiers) == ("automated", "token-1.access", frozenset({"read"}))

    seen, _ = await scope_seen_by_inner_app(None, [])
    assert seen[0]["state"][remote.CALLER_STATE_KEY] is remote.LOCAL


@pytest.mark.anyio
async def test_middleware_keeps_existing_scope_state():
    seen: list[dict] = []

    async def inner(scope, receive, send):
        seen.append(scope)

    async def receive():
        return {"type": "http.request"}

    async def send(message):
        pass

    app = remote.AccessMiddleware(inner, None)
    await app({"type": "http", "method": "POST", "path": "/mcp", "headers": [], "state": {"x": 1}}, receive, send)
    assert seen[0]["state"] == {"x": 1, "mcp_caller": remote.LOCAL}


@pytest.mark.anyio
async def test_middleware_denies_an_unadmitted_service_token_with_403():
    verifier = remote.AccessVerifier(POLICY, public_key)
    seen, sent = await scope_seen_by_inner_app(verifier, [(remote.ACCESS_HEADER.encode(), service_token().encode())])
    assert seen == [] and sent[0]["status"] == 403


@pytest.mark.anyio
async def test_unadmitted_service_token_gets_403_over_http():
    """Review Focus 1, end to end: today this token gets in; with nothing configured it must not."""
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        resp = await http.post(f"{base}/thing/mcp", json=body, headers={remote.ACCESS_HEADER: service_token()})
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_admitted_service_token_gets_in_over_http():
    cfg = remote.HttpConfig(
        path="/thing/mcp", public_hosts=("mcp.example.com",), access=automated_policy("token-1.access")
    )
    async with (
        running(cfg, []) as base,
        mcp_client(f"{base}/thing/mcp", {remote.ACCESS_HEADER: service_token()}) as c,
    ):
        assert (await c.call_tool("ping", {})).content[0].text == "pong"


# --- per-tool authorization over HTTP -----------------------------------------------
def tiered_server(ran: list[str]) -> MCPServer:
    m = MCPServer("tiered", request_state_security=remote.request_state_security())

    @m.tool()
    @remote.tier("read")
    def status() -> str:
        ran.append("status")
        return "ok"

    @m.tool()
    @remote.tier("power-off")
    async def power_off() -> str:
        ran.append("power_off")
        return "off"

    @m.tool()
    @remote.tier("everyday")
    async def play() -> str:
        ran.append("play")
        return "playing"

    @m.tool()
    @remote.tier("costly", confirm="Update the firmware?")
    async def update() -> str:
        ran.append("update")
        return "updated"

    @m.tool()
    @remote.tier(lambda args: "power-off" if args["state"] == "off" else "everyday")
    async def set_power(state: str, tv: str | None = None) -> str:
        ran.append(f"set_power {state}")
        return state

    return m


def automation_cfg(tiers: frozenset[str] = remote.DEFAULT_AUTOMATION_TIERS) -> remote.HttpConfig:
    return remote.HttpConfig(
        path="/thing/mcp",
        public_hosts=("mcp.example.com",),
        access=automated_policy("token-1.access"),
        automation_tiers=tiers,
    )


# Both protocol revisions: the request (and so the caller) reaches the tool by different paths.
@pytest.fixture(params=["legacy", "2026-07-28"])
def protocol(request):
    return request.param


@asynccontextmanager
async def tiered_client(cfg: remote.HttpConfig, ran: list[str], jwt_token: str, mode: str):
    async with running(cfg, [], server=tiered_server(ran)) as base:
        transport = streamable_http_client(
            f"{base}/thing/mcp", http_client=httpx2.AsyncClient(headers={remote.ACCESS_HEADER: jwt_token})
        )
        async with Client(transport, mode=mode) as c:
            yield c


EVERYDAY = frozenset({"read", "power-off", "everyday"})


@pytest.mark.anyio
async def test_automated_caller_gets_read_and_power_off(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(), ran, service_token(), protocol) as c:
        assert (await c.call_tool("status", {})).content[0].text == "ok"
        assert (await c.call_tool("power_off", {})).content[0].text == "off"
    assert ran == ["status", "power_off"]


@pytest.mark.anyio
async def test_automated_caller_refused_everyday_with_message(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(), ran, service_token(), protocol) as c:
        result = await c.call_tool("play", {})
    assert result.is_error
    message = result.content[0].text
    assert "This tool needs a person; automated callers may only use: read, power-off." in message
    assert ran == []


@pytest.mark.anyio
async def test_automation_tiers_everyday_allows_it(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(EVERYDAY), ran, service_token(), protocol) as c:
        assert (await c.call_tool("play", {})).content[0].text == "playing"
    assert ran == ["play"]


@pytest.mark.anyio
async def test_automated_caller_never_gets_costly(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(EVERYDAY), ran, service_token(), protocol) as c:
        result = await c.call_tool("update", {})
    assert result.is_error
    assert "automated callers may only use: read, power-off, everyday." in result.content[0].text
    assert ran == []


@pytest.mark.anyio
async def test_set_power_on_refused_off_allowed_for_automated(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(), ran, service_token(), protocol) as c:
        assert (await c.call_tool("set_power", {"state": "off", "tv": "tv-1"})).content[0].text == "off"
        assert (await c.call_tool("set_power", {"state": "on", "tv": "tv-1"})).is_error
    assert ran == ["set_power off"]


@pytest.mark.anyio
async def test_a_person_over_http_gets_everyday(protocol):
    ran: list[str] = []
    async with tiered_client(automation_cfg(), ran, token(), protocol) as c:
        assert (await c.call_tool("play", {})).content[0].text == "playing"
        assert (await c.call_tool("set_power", {"state": "on"})).content[0].text == "on"
    assert ran == ["play", "set_power on"]


@pytest.mark.anyio
async def test_refusal_logged_at_info(caplog, protocol):
    caplog.set_level("INFO", logger=remote.__name__)
    async with tiered_client(automation_cfg(), [], service_token(), protocol) as c:
        assert (await c.call_tool("set_power", {"state": "on"})).is_error
    (record,) = [r for r in caplog.records if r.name == remote.__name__ and "refused" in r.getMessage()]
    assert record.levelname == "INFO"
    message = record.getMessage()
    for part in ("automated", "token-1.access", "set_power", "everyday", "refused"):
        assert part in message


@pytest.mark.anyio
async def test_a_request_that_skipped_the_middleware_is_refused(caplog, protocol):
    """Nothing defaults open: an HTTP request with no Caller in its scope runs no tool, not even a read."""
    import uvicorn

    caplog.set_level("INFO", logger=remote.__name__)
    ran: list[str] = []
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    app = tiered_server(ran).streamable_http_app(streamable_http_path="/thing/mcp", host="127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, [sock])
        while not server.started:
            await anyio.sleep(0.01)
        transport = streamable_http_client(f"http://127.0.0.1:{port}/thing/mcp")
        async with Client(transport, mode=protocol) as c:
            result = await c.call_tool("status", {})
        server.should_exit = True
    assert result.is_error and "can't tell who sent this request" in result.content[0].text
    assert ran == []
    assert any("unknown caller unknown, tool status, tier read: refused" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_person_refusal_logged_with_masked_email(caplog, protocol):
    caplog.set_level("INFO", logger=remote.__name__)
    async with tiered_client(automation_cfg(), [], token(), protocol) as c:
        assert (await c.call_tool("update", {})).is_error
    messages = [r.getMessage() for r in caplog.records if r.name == remote.__name__ and "refused" in r.getMessage()]
    assert messages and all("u***@example.com" in m and "user@example.com" not in m for m in messages)


def http_client(base: str, jwt_token: str, **kwargs: Any) -> Client:
    transport = streamable_http_client(
        f"{base}/thing/mcp", http_client=httpx2.AsyncClient(headers={remote.ACCESS_HEADER: jwt_token})
    )
    return Client(transport, **kwargs)


async def say_yes(context: Any, params: Any) -> ElicitResult:
    return ElicitResult(action="accept", content={"confirm": True})


@pytest.mark.anyio
async def test_a_person_over_http_confirms_a_costly_call(protocol):
    ran: list[str] = []
    async with (
        running(automation_cfg(), [], server=tiered_server(ran)) as base,
        http_client(base, token(), mode=protocol, elicitation_callback=say_yes) as c,
    ):
        assert (await c.call_tool("update", {})).content[0].text == "updated"
        # A second, fresh yes to the very same call is its own confirmation, not a replay.
        assert (await c.call_tool("update", {})).content[0].text == "updated"
    assert ran == ["update", "update"]


@pytest.mark.anyio
async def test_sealed_confirmation_bound_to_caller(caplog):
    """Review Focus 3: person A's sealed state, answered and re-sent with person B's JWT, is rejected."""
    ran: list[str] = []
    async with running(automation_cfg(), [], server=tiered_server(ran)) as base:
        a = http_client(base, token(email="user@example.com"), mode="2026-07-28", elicitation_callback=say_yes)
        b = http_client(base, token(email="other@example.com"), mode="2026-07-28", elicitation_callback=say_yes)
        async with a, b:
            first = await a.session.call_tool("update", {}, allow_input_required=True)
            assert isinstance(first, InputRequiredResult)
            (key,) = first.input_requests
            yes = {key: ElicitResult(action="accept", content={"confirm": True})}
            with pytest.raises(MCPError, match="Invalid or expired requestState"):
                await b.session.call_tool(
                    "update", {}, input_responses=yes, request_state=first.request_state, allow_input_required=True
                )
            assert ran == []
            # The same answer from the person it was asked of still runs it.
            done = await a.session.call_tool(
                "update", {}, input_responses=yes, request_state=first.request_state, allow_input_required=True
            )
            assert done.content[0].text == "updated"
    assert ran == ["update"]
    assert any("requestState rejected" in r.getMessage() and "principal" in r.getMessage() for r in caplog.records)


def test_request_state_security_ttl_is_120():
    security = remote.request_state_security()
    assert security.ttl == 120 == remote.CONFIRM_TIMEOUT
    assert security.bind_principal is remote.caller_principal


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
async def test_health_reports_commit_and_repo(monkeypatch, tmp_path):
    commit_file(monkeypatch, tmp_path, f"{SHA}\n{REPO}\n")
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert (resp.status_code, resp.json()) == (200, {"status": "ok", "commit": SHA, "repo": REPO})


async def _false() -> bool:
    return False


async def _raises() -> bool:
    raise RuntimeError("device off")


async def _error() -> dict[str, Any]:
    return {"error": "x"}


@pytest.mark.anyio
@pytest.mark.parametrize("check", [_false, _raises, _error])
async def test_degraded_health_reports_commit(monkeypatch, tmp_path, check):
    commit_file(monkeypatch, tmp_path, f"{SHA}\n{REPO}\n")
    async with running(CFG, [], health_check=check) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        body = resp.json()
        assert resp.status_code == 503 and body["status"] == "degraded"
        assert (body["commit"], body["repo"]) == (SHA, REPO)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "content,expected",
    [
        (None, {}),  # missing
        ("", {}),
        ("not-a-sha\n", {}),
        ("ABCDEF0\n", {}),  # uppercase
        (f"{SHA}0\n", {}),  # 41 characters
        (f"{SHA}\n", {"commit": SHA}),  # one line only
        (f"{SHA}", {"commit": SHA}),  # no newline
        (f"{SHA}\nbad repo!\n", {"commit": SHA}),
        (f"zz\n{REPO}\n", {"repo": REPO}),
        ("dir", {}),  # a directory where the file should be
        (b"\xff\xfe" + SHA.encode(), {}),  # not text
        ("a" * 1_000_000, {}),  # far too big: someone pointed MCP_COMMIT_FILE at the wrong file
    ],
)
async def test_bad_commit_file_leaves_fields_out(monkeypatch, tmp_path, content, expected):
    if content == "dir":
        (tmp_path / "COMMIT").mkdir()
        monkeypatch.setenv(remote.COMMIT_FILE_ENV, str(tmp_path / "COMMIT"))
    elif content is not None:
        commit_file(monkeypatch, tmp_path, content)
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert (resp.status_code, resp.json()) == (200, {"status": "ok", **expected})


@pytest.mark.anyio
async def test_commit_file_is_read_on_every_request(monkeypatch, tmp_path):
    commit_file(monkeypatch, tmp_path, f"{SHA}\n{REPO}\n")
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        first = (await http.get(f"{base}/healthz")).json()
        commit_file(monkeypatch, tmp_path, f"{SHA_B}\n{REPO}\n")
        second = (await http.get(f"{base}/healthz")).json()
    assert (first["commit"], second["commit"]) == (SHA, SHA_B)


@pytest.mark.anyio
async def test_commit_file_wins_over_health_check_keys(monkeypatch, tmp_path):
    async def check():
        return {"commit": "from-the-check", "hub": "connected"}

    commit_file(monkeypatch, tmp_path, f"{SHA}\n")
    async with running(CFG, [], health_check=check) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert resp.json() == {"status": "ok", "hub": "connected", "commit": SHA}


@pytest.mark.anyio
async def test_health_under_the_mcp_path_needs_no_assertion():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        for path in ("/thing/mcp/healthz", "/healthz"):
            resp = await http.get(f"{base}{path}")
            assert (resp.status_code, resp.json()) == (200, {"status": "ok"}), path


@pytest.mark.anyio
@pytest.mark.parametrize("check,status", [(None, 200), (_false, 503)])
async def test_health_under_the_mcp_path_reports_commit(monkeypatch, tmp_path, check, status):
    commit_file(monkeypatch, tmp_path, f"{SHA}\n{REPO}\n")
    async with running(CFG, [], health_check=check) as base, httpx2.AsyncClient() as http:
        for path in ("/thing/mcp/healthz", "/healthz"):
            resp = await http.get(f"{base}{path}")
            body = resp.json()
            assert (resp.status_code, body["commit"], body["repo"]) == (status, SHA, REPO), path


@pytest.mark.anyio
async def test_health_under_a_multi_segment_path():
    cfg = remote.HttpConfig(path="/mcp/backlog", public_hosts=("mcp.example.com",), access=POLICY)
    async with running(cfg, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/mcp/backlog/healthz")
        assert (resp.status_code, resp.json()) == (200, {"status": "ok"})


def test_health_paths_strip_a_trailing_slash():
    assert remote.health_paths("/mcp/backlog/") == frozenset({"/healthz", "/mcp/backlog/healthz"})
    assert remote.health_paths(None) == frozenset({"/healthz"})


@pytest.mark.anyio
async def test_post_to_the_mcp_health_path_needs_an_assertion():
    async with running(CFG, []) as base, httpx2.AsyncClient() as http:
        resp = await http.post(f"{base}/thing/mcp/healthz")
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_mcp_health_path_refuses_forwarded_when_local_only():
    async with running(LOCAL, []) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/thing/mcp/healthz", headers={"x-forwarded-for": "203.0.113.7"})
        assert resp.status_code == 403 and resp.json() == {"error": "forbidden"}


@pytest.mark.anyio
async def test_health_check_keys_never_stand_in_for_the_commit_file():
    async def check():
        return {"commit": "firmware-hash", "repo": "someone/else", "hub": "connected"}

    async with running(CFG, [], health_check=check) as base, httpx2.AsyncClient() as http:
        resp = await http.get(f"{base}/healthz")
        assert resp.json() == {"status": "ok", "hub": "connected"}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a FIFO")
def test_a_fifo_at_the_commit_path_does_not_block(tmp_path):
    fifo = tmp_path / "COMMIT"
    os.mkfifo(fifo)
    result: list[dict[str, str]] = []
    t = threading.Thread(target=lambda: result.append(remote.running_commit(env={remote.COMMIT_FILE_ENV: str(fifo)})))
    t.daemon = True
    t.start()
    t.join(2)
    blocked = t.is_alive()
    if blocked:  # opened for reading, waiting for a writer: give it one so the thread ends
        with open(fifo, "wb"):
            pass
        t.join(2)
    assert not blocked and result == [{}]


def test_running_commit_defaults_beside_the_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(remote.sys, "prefix", str(tmp_path / "venv"))
    (tmp_path / "COMMIT").write_text(f"{SHA}\n{REPO}\n")
    assert remote.running_commit(env={}) == {"commit": SHA, "repo": REPO}


def test_running_commit_env_overrides_the_default(monkeypatch, tmp_path):
    monkeypatch.setattr(remote.sys, "prefix", str(tmp_path / "venv"))
    (tmp_path / "COMMIT").write_text(f"{SHA}\n")
    (tmp_path / "other").write_text(f"{SHA_B}\n")
    assert remote.running_commit(env={remote.COMMIT_FILE_ENV: str(tmp_path / "other")}) == {"commit": SHA_B}


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
    lan = remote.transport_security(remote.HttpConfig(bind="0.0.0.0", public_hosts=("192.0.2.10",)))
    assert "192.0.2.10:*" in lan.allowed_hosts and "http://192.0.2.10:*" in lan.allowed_origins


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
    async with running(cfg, []) as base, mcp_client(f"{base}/thing/mcp", {"x-forwarded-for": "192.0.2.20"}) as c:
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

        def fetch_data(self) -> object:
            nonlocal calls
            calls += 1
            return {"keys": []}

        def get_signing_key_from_jwt(self, _token: str) -> object:
            self.fetch_data()  # as PyJWKClient does on an unknown kid
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


@pytest.mark.anyio
async def test_jwks_throttle_counts_network_fetches_not_cache_hits(monkeypatch):
    # The real PyJWKClient, with only its HTTP request faked. Tokens served from the cache must not
    # count as fetches: if they did, steady traffic on the old key would keep a rotated key refused
    # until the cache expired.
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    published = [{**jwk, "kid": "old-kid", "use": "sig"}]
    fetches = 0

    def fetch_data(self: jwt.PyJWKClient) -> object:
        nonlocal fetches
        fetches += 1
        return {"keys": list(published)}

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", fetch_data)
    fake_now = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: fake_now[0])
    verifier = remote.AccessVerifier(POLICY)

    def signed(kid: str) -> str:
        now = int(time.time())
        claims = {"iss": f"https://{TEAM}", "aud": [AUD], "iat": now, "exp": now + 300, "email": "user@example.com"}
        return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": kid})

    await verifier.verify(signed("old-kid"))  # empty cache: one fetch
    assert fetches == 1

    fake_now[0] = 1100.0
    await verifier.verify(signed("old-kid"))  # a cache hit: no fetch
    assert fetches == 1

    # Cloudflare rotates: the new kid is 10 s after the last cache hit but 110 s after the last
    # fetch, so it is fetched, not throttled.
    published.append({**jwk, "kid": "new-kid", "use": "sig"})
    fake_now[0] = 1110.0
    await verifier.verify(signed("new-kid"))
    assert fetches == 2
