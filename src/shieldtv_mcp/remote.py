"""Serve an MCP server over Streamable HTTP, behind Cloudflare Access.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why HTTP: stdio means the MCP client launches the server on its own machine.
To use one server from Claude Code, Claude Desktop and the mobile app, it has
to run somewhere always on (an LXC) and be reachable as a URL. The Claude apps
connect to custom connectors from Anthropic's side, so that URL is public.

Why Cloudflare Access: a public URL that can turn TVs on needs real auth, and
Claude's connectors speak OAuth. Access's "Managed OAuth" makes Access the
OAuth server: Claude does the sign-in dance with Access (your usual Access
login), and every request Access lets through arrives at the tunnel's origin
with a signed JWT in the ``Cf-Access-Jwt-Assertion`` header. This module
checks that JWT on every request: signature (Access's published keys), the
application's audience tag, issuer, expiry, and optionally an email
allow-list. So the server is safe even from the LAN, where nothing passes
through Access.

What it deliberately doesn't do: run its own OAuth server, or answer 401 with
``WWW-Authenticate``. Access does that at the edge, and Cloudflare's docs warn
that an origin doing its own OAuth conflicts with Managed OAuth. A request
without a valid assertion gets a plain 403.

Without Access, the server is meant for this machine only (Claude Code or
Desktop on the same box, a local test). Binding to 127.0.0.1 isn't enough to
guarantee that: a tunnel or reverse proxy running here reaches the port from
loopback too, and would carry anyone's request in. So with no Access and no
--insecure-no-auth, two more things are refused: a --public-host (a public
name only makes sense behind a tunnel or proxy) at startup, and, per request,
anything carrying a proxy's forwarding headers (Cloudflare adds
Cf-Connecting-IP and Cf-Ray, most proxies X-Forwarded-For or Forwarded).

Settings come from flags or the environment (flags win):

    --http                 serve HTTP instead of stdio
    --bind / MCP_HTTP_BIND         default 127.0.0.1
    --port / MCP_HTTP_PORT         default per server
    --path / MCP_HTTP_PATH         default per server, e.g. /harmony/mcp
    --public-host / MCP_PUBLIC_HOSTS   e.g. mcp.example.com (the Host Cloudflare sends)
    CF_ACCESS_TEAM_DOMAIN          e.g. example.cloudflareaccess.com
    CF_ACCESS_AUD                  the Access application's audience tag(s), comma-separated
    MCP_ALLOWED_EMAILS             optional, comma-separated
    --insecure-no-auth     serve without Access beyond this machine (trusted-LAN testing only)

Stopping: OpenRC's supervise-daemon (every sibling's init script) stops a
service with SIGTERM and SIGKILLs it 5 seconds later by default
(``--retry SIGTERM/5``). A SIGKILL skips the lifespan shutdown, where the
siblings close device connections and disconnect from MQTT, so everything
uvicorn does on SIGTERM has to fit in those 5 seconds. uvicorn's default is to
wait for every open connection to finish first, for as long as that takes.
Today the MCP SDK's responses are SSE streams that sse-starlette closes as soon
as it notices uvicorn exiting (it introspects ``Server.should_exit``), so in
practice the wait is short, but that hook has broken before across versions
(sse-starlette #132, #211). ``SHUTDOWN_TIMEOUT`` bounds the wait regardless:
after it, uvicorn closes what's left and runs the lifespan shutdown.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import anyio
import jwt
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.types import ASGIApp, Receive, Scope, Send

log = logging.getLogger(__name__)

ACCESS_HEADER = "cf-access-jwt-assertion"
HEALTH_PATH = "/healthz"
# Headers a proxy adds to what it forwards. A request with one didn't start on this machine.
FORWARDED_HEADERS = (
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-real-ip",
    "true-client-ip",
    "cf-connecting-ip",
    "cf-ray",
    "via",
)
CORS_ALLOW_HEADERS = b"content-type, cf-access-jwt-assertion, authorization, mcp-session-id, mcp-protocol-version"
# Seconds uvicorn gives open connections on SIGTERM before closing them and running the lifespan
# shutdown. 2 of supervise-daemon's 5: the rest is for the siblings' own stop timeouts.
SHUTDOWN_TIMEOUT = 2
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class ConfigError(ValueError):
    """The HTTP settings are unsafe or incomplete; the message says how to fix them."""


class AccessDenied(Exception):
    """A request's Access assertion is missing or doesn't check out."""


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


@dataclass(frozen=True)
class AccessPolicy:
    """Which Cloudflare Access application's assertions to accept."""

    team_domain: str  # "example.cloudflareaccess.com"
    audiences: tuple[str, ...]  # the application's AUD tag(s)
    # Empty: anyone Access let through. repr=False: a config repr in a log or a traceback (uvicorn
    # prints the app's state on some startup failures) must not list who is allowed in.
    emails: frozenset[str] = field(default=frozenset(), repr=False)

    @property
    def issuer(self) -> str:
        return f"https://{self.team_domain}"

    @property
    def certs_url(self) -> str:
        return f"{self.issuer}/cdn-cgi/access/certs"


@dataclass(frozen=True)
class HttpConfig:
    bind: str = "127.0.0.1"
    port: int = 8000
    path: str = "/mcp"
    public_hosts: tuple[str, ...] = ()
    access: AccessPolicy | None = None
    insecure_no_auth: bool = False

    @property
    def local_only(self) -> bool:
        """No auth, so only requests that start on this machine may be served (see the module docstring)."""
        return self.access is None and not self.insecure_no_auth

    @property
    def loopback(self) -> bool:
        if self.bind == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.bind).is_loopback
        except ValueError:
            return False

    def check(self) -> None:
        """Refuse settings that would expose device control without auth."""
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"--port must be between 1 and 65535 (got {self.port}).")
        if not self.path.startswith("/") or self.path == HEALTH_PATH:
            raise ConfigError(f"--path must start with '/' and can't be {HEALTH_PATH} (got {self.path!r}).")
        if self.access is not None:
            if not _HOSTNAME_RE.fullmatch(self.access.team_domain):
                raise ConfigError(f"CF_ACCESS_TEAM_DOMAIN must be a hostname (got {self.access.team_domain!r}).")
            if not self.access.audiences:
                raise ConfigError(
                    "CF_ACCESS_TEAM_DOMAIN is set but CF_ACCESS_AUD is empty; set the application's AUD tag."
                )
        if not self.loopback and self.access is None and not self.insecure_no_auth:
            raise ConfigError(
                f"Refusing to serve on {self.bind} without Cloudflare Access: anyone who can reach this port could "
                "control your devices. Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD, bind to 127.0.0.1, or pass "
                "--insecure-no-auth for a trusted-LAN test."
            )
        if self.public_hosts and self.local_only:
            raise ConfigError(
                f"Refusing --public-host {', '.join(self.public_hosts)} without Cloudflare Access: requests for a "
                "public name come through a tunnel or reverse proxy, which reaches this port from loopback, so "
                "binding to 127.0.0.1 doesn't keep them out. Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD, or pass "
                "--insecure-no-auth for a trusted-LAN test."
            )


# --- configuration from flags and environment -----------------------------------
def add_http_arguments(parser: argparse.ArgumentParser, *, default_port: int, default_path: str) -> None:
    """Add the HTTP flags to a `serve` (sub)parser."""
    g = parser.add_argument_group("HTTP transport")
    g.add_argument("--http", action="store_true", help="serve Streamable HTTP instead of stdio")
    g.add_argument("--bind", help="address to listen on (MCP_HTTP_BIND, default 127.0.0.1)")
    g.add_argument("--port", type=int, help=f"port (MCP_HTTP_PORT, default {default_port})")
    g.add_argument("--path", help=f"MCP endpoint path (MCP_HTTP_PATH, default {default_path})")
    g.add_argument(
        "--public-host",
        action="append",
        default=[],
        help="public hostname clients use, e.g. mcp.example.com (MCP_PUBLIC_HOSTS); repeatable",
    )
    g.add_argument(
        "--insecure-no-auth",
        action="store_true",
        help="serve without Cloudflare Access beyond this machine (trusted-LAN testing only)",
    )
    parser.set_defaults(_http_default_port=default_port, _http_default_path=default_path)


def http_config(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> HttpConfig:
    """Merge flags over environment over defaults."""
    env = os.environ if env is None else env
    team = (env.get("CF_ACCESS_TEAM_DOMAIN") or "").strip().removeprefix("https://").rstrip("/")
    access = None
    if team:
        access = AccessPolicy(
            team_domain=team,
            audiences=_csv(env.get("CF_ACCESS_AUD")),
            emails=frozenset(e.lower() for e in _csv(env.get("MCP_ALLOWED_EMAILS"))),
        )
    raw_port = args.port if args.port is not None else (env.get("MCP_HTTP_PORT") or args._http_default_port)
    try:
        port = int(raw_port)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"--port / MCP_HTTP_PORT must be an integer (got {raw_port!r}).") from exc
    return HttpConfig(
        bind=args.bind or env.get("MCP_HTTP_BIND") or "127.0.0.1",
        port=port,
        path=args.path or env.get("MCP_HTTP_PATH") or args._http_default_path,
        public_hosts=tuple(args.public_host) or _csv(env.get("MCP_PUBLIC_HOSTS")),
        access=access,
        insecure_no_auth=bool(args.insecure_no_auth),
    )


# --- verifying Access assertions ---------------------------------------------------
KeyResolver = Callable[[str], Awaitable[Any]]
HealthChecker = Callable[[], Awaitable[Mapping[str, Any] | bool]]


class AccessVerifier:
    """Checks a ``Cf-Access-Jwt-Assertion`` against an AccessPolicy.

    The signing keys come from the team's published certs endpoint. PyJWT's
    PyJWKClient caches them and refetches when a token names an unknown key id,
    which is how Cloudflare's key rotation is picked up. Its fetch is blocking,
    so it runs in a worker thread. Tests pass their own ``key_resolver``.
    """

    leeway = 30  # seconds of clock skew tolerated on exp/iat
    min_refetch_interval = 60.0  # minimum seconds between JWKS network fetches on unknown kid

    def __init__(self, policy: AccessPolicy, key_resolver: KeyResolver | None = None) -> None:
        self.policy = policy
        if key_resolver is None:
            client = jwt.PyJWKClient(policy.certs_url, cache_jwk_set=True, lifespan=3600, timeout=10)
            lock = anyio.Lock()
            last_fetch: float | None = None

            async def resolve(token: str) -> Any:  # pragma: no cover
                nonlocal last_fetch
                async with lock:
                    header = jwt.get_unverified_header(token)
                    kid = header.get("kid")
                    now = time.monotonic()
                    if client.jwk_set_cache is not None and not client.jwk_set_cache.is_expired():
                        cached_set = client.jwk_set_cache.get()
                        if cached_set is not None:
                            cached_kids = {k.key_id for k in cached_set.keys}
                            if (
                                kid not in cached_kids
                                and last_fetch is not None
                                and (now - last_fetch) < self.min_refetch_interval
                            ):
                                raise jwt.PyJWKClientError(f"Unable to find a signing key that matches {kid!r}")
                    try:
                        key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
                    finally:
                        last_fetch = time.monotonic()
                return key.key

            key_resolver = resolve
        self._resolve = key_resolver

    async def verify(self, token: str) -> dict[str, Any]:
        try:
            key = await self._resolve(token)
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                audience=list(self.policy.audiences),
                issuer=self.policy.issuer,
                leeway=self.leeway,
                options={"require": ["exp", "iat", "iss", "aud"]},
            )
        except jwt.PyJWTError as exc:
            raise AccessDenied(f"invalid Access assertion: {exc}") from exc
        if self.policy.emails:
            email = str(claims.get("email", "")).lower()
            if email not in self.policy.emails:
                # The identity is somebody Access let through but this server doesn't want; name it
                # only enough to tell logins apart. logsafe doesn't redact e-mail addresses (and this
                # module can't import it: shared modules stay self-contained), so mask it here.
                local, _, domain = email.partition("@")
                shown = f"{local[:1]}***@{domain}" if domain else "this identity"
                raise AccessDenied(f"{shown} is not in MCP_ALLOWED_EMAILS")
        return claims


# --- ASGI wrappers ---------------------------------------------------------------------
async def _plain(send: Send, status: int, body: Mapping[str, Any]) -> None:
    data = json.dumps(dict(body)).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": data})


class AccessMiddleware:
    """Lets a request through only with a valid Access assertion.

    Lifespan events pass untouched (the MCP session manager starts there), and
    the health check is answered before auth so a local monitor can use it.
    With no verifier and ``local_only``, a request a proxy forwarded is refused
    instead (see the module docstring).
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: AccessVerifier | None,
        *,
        local_only: bool = False,
        health_check: HealthChecker | None = None,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.local_only = local_only
        self.health_check = health_check

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if self.verifier is None and self.local_only:
            forwarded = [h for h in FORWARDED_HEADERS if h.encode() in headers]
            if forwarded:
                client = scope.get("client") or ("?", 0)
                log.warning(
                    "Denied %s %s from %s: forwarded by a proxy (%s), and without Cloudflare Access this server only "
                    "serves requests from this machine. Set CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD.",
                    scope.get("method"),
                    scope["path"],
                    client[0],
                    ", ".join(forwarded),
                )
                await _plain(send, 403, {"error": "forbidden"})
                return
        if scope["path"] == HEALTH_PATH and scope.get("method") in ("GET", "HEAD"):
            if self.health_check is None:
                await _plain(send, 200, {"status": "ok"})
                return
            try:
                res = await self.health_check()
            except Exception as exc:
                log.warning("Health check failed: %s", exc)
                await _plain(send, 503, {"status": "degraded", "error": "health check failed"})
                return
            if res is False:
                await _plain(send, 503, {"status": "degraded", "error": "health check returned False"})
                return
            if res is True:
                await _plain(send, 200, {"status": "ok"})
                return
            if res.get("status") == "degraded" or "error" in res:
                await _plain(send, 503, {**res, "status": "degraded"})
            else:
                await _plain(send, 200, {**res, "status": "ok"})
            return
        if scope.get("method") == "OPTIONS":
            origin = headers.get(b"origin", b"*")
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", origin),
                        (b"access-control-allow-methods", b"POST, GET, DELETE, OPTIONS"),
                        (b"access-control-allow-headers", CORS_ALLOW_HEADERS),
                        (b"access-control-max-age", b"86400"),
                        (b"vary", b"Origin"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return
        if self.verifier is not None:
            token = headers.get(ACCESS_HEADER.encode(), b"").decode("latin-1")
            try:
                if not token:
                    raise AccessDenied("no Cloudflare Access assertion (request didn't come through Access)")
                claims = await self.verifier.verify(token)
            except AccessDenied as exc:
                client = scope.get("client") or ("?", 0)
                log.warning("Denied %s %s from %s: %s", scope.get("method"), scope["path"], client[0], exc)
                await _plain(send, 403, {"error": "forbidden"})
                return
            log.debug("Access OK for %s", claims.get("email") or claims.get("common_name") or claims.get("sub"))
        await self.app(scope, receive, send)


def transport_security(cfg: HttpConfig) -> TransportSecuritySettings:
    """Host/Origin checks (DNS-rebinding protection) for the hosts we expect.

    cloudflared passes the public Host through, so the public hostname must be
    allowed; so are loopback and the bind address, for local checks.

    The checks stay on even when binding to every interface (0.0.0.0 or ::)
    with no --public-host. That combination is the one where they matter
    most: with --insecure-no-auth, a web page open in any browser on the LAN
    could otherwise reach the server through a rebinding hostname. LAN
    clients then need their address of this machine passed as --public-host.
    """
    hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    for h in cfg.public_hosts:
        # A hostname as Cloudflare sends it (no port), or a LAN IP on any port
        hosts.append(f"{h}:*" if _is_ip(h) else h)
    if cfg.bind not in ("0.0.0.0", "::"):
        hosts.append(f"[{cfg.bind}]:*" if ":" in cfg.bind else f"{cfg.bind}:*")
    elif not cfg.public_hosts:
        log.warning(
            "Binding to %s with no --public-host: only loopback Host headers are accepted. Pass "
            "--public-host <this machine's LAN address> for clients on the LAN.",
            cfg.bind,
        )
    origins = [f"https://{h}" for h in cfg.public_hosts] + ["http://127.0.0.1:*", "http://localhost:*"]
    origins += [f"http://{h}:*" for h in cfg.public_hosts if _is_ip(h)]  # plain-HTTP LAN testing
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts, allowed_origins=origins)


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip("[]"))
        return True
    except ValueError:
        return False


def build_app(
    mcp: MCPServer,
    cfg: HttpConfig,
    verifier: AccessVerifier | None = None,
    *,
    health_check: HealthChecker | None = None,
) -> ASGIApp:
    """The ASGI app: the SDK's Streamable HTTP app, wrapped in the Access check."""
    cfg.check()
    if verifier is None and cfg.access is not None:
        verifier = AccessVerifier(cfg.access)
    app = mcp.streamable_http_app(
        streamable_http_path=cfg.path, transport_security=transport_security(cfg), host=cfg.bind
    )
    return AccessMiddleware(app, verifier, local_only=cfg.local_only, health_check=health_check)


def uvicorn_settings(cfg: HttpConfig) -> dict[str, Any]:
    """The keyword arguments ``serve_http`` runs uvicorn with, so a test can run the very same server.

    - log_config=None: uvicorn's own log lines (access logs carry client addresses) go through the
      root logger, so the server's redacting handler (logsafe.py) formats them, not uvicorn's own.
    - proxy_headers=False: the client address stays the real peer (loopback, behind cloudflared);
      this module decides for itself what to trust from forwarding headers.
    - timeout_graceful_shutdown: bounds the wait for open connections on SIGTERM (see "Stopping"
      in the module docstring). When it fires, uvicorn logs "Cancel N running task(s), timeout
      graceful shutdown exceeded" at ERROR; with a client attached that is the expected path.
    """
    return {
        "host": cfg.bind,
        "port": cfg.port,
        "log_level": "info",
        "log_config": None,
        "proxy_headers": False,
        "lifespan": "on",
        "timeout_graceful_shutdown": SHUTDOWN_TIMEOUT,
    }


def serve_http(  # pragma: no cover
    mcp: MCPServer, cfg: HttpConfig, *, health_check: HealthChecker | None = None
) -> None:
    """Run until interrupted. The server's lifespan (device connections) runs once, shared by all sessions."""
    import uvicorn

    app = build_app(mcp, cfg, health_check=health_check)
    if cfg.access is None:
        log.warning("Serving %s on %s:%s WITHOUT Cloudflare Access checks.", cfg.path, cfg.bind, cfg.port)
    else:
        log.info("Serving %s on %s:%s; requests need a %s assertion.", cfg.path, cfg.bind, cfg.port, cfg.access.issuer)
    uvicorn.run(app, **uvicorn_settings(cfg))
