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
    MCP_ALLOWED_EMAILS             optional, comma-separated; applies to people (JWTs with an email)
    MCP_ALLOWED_SERVICE_TOKENS     optional, comma-separated Access service-token client IDs
                                   (the JWT's common_name); unset admits no service token
    MCP_AUTOMATION_TIERS           tiers an admitted service token may use, comma-separated;
                                   default read,power-off; may add everyday; costly or an
                                   unknown name refuses startup
    --insecure-no-auth     serve without Access beyond this machine (trusted-LAN testing only)

What each caller may do: AccessMiddleware turns each verified request into a ``Caller``: a
person (the JWT has an ``email``), an automated caller (no email, and a ``common_name`` listed in
MCP_ALLOWED_SERVICE_TOKENS) or local (stdio, or no Access). A JWT that is none of these gets a 403,
so a service token is refused unless its client ID is listed, even with no e-mail allow-list. Every
tool declares a tier with ``@tier`` under ``@mcp.tool(...)``: ``read`` (changes nothing),
``power-off`` (turns something off), ``everyday`` or ``costly`` (a chore to undo). People and local
callers may use every tier; an automated caller only those in MCP_AUTOMATION_TIERS (never
``costly``). A costly call from a person asks first, in an MCP elicitation prompt showing the tool's
``confirm`` sentence: anything but a yes refuses it, one yes runs it once, and in an app that can't
show the prompt (today everything but the Claude Code CLI) it is refused. The sibling passes
``request_state_security=request_state_security()`` to its ``MCPServer``. ``@tier`` adds a hidden
``tier_authorized`` parameter (``AUTHORIZED_PARAM``; the SDK rejects names starting with ``_``), so
calling a tiered tool directly from Python is refused: a test that means to skip the check calls
``fn.__wrapped__``. The README's "What each caller may do" has the tiers, the settings and the wiring.

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
import contextvars
import functools
import hashlib
import inspect
import ipaddress
import json
import logging
import os
import re
import time
import typing
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypeVar, cast

import anyio
import jwt
from mcp.server.context import ServerRequestContext
from mcp.server.elicitation import AcceptedElicitation, CancelledElicitation, DeclinedElicitation, ElicitationResult
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve, find_resolved_parameters
from mcp.server.mcpserver.utilities.context_injection import find_context_parameter
from mcp.server.request_state import AESGCMRequestStateCodec, RequestStateSecurity
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, Field
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

# What a tool can do, least to most. A person may use all of them; an automated caller only the
# tiers in MCP_AUTOMATION_TIERS, which can never include "costly".
TIERS: tuple[str, ...] = ("read", "power-off", "everyday", "costly")
Tier = Literal["read", "power-off", "everyday", "costly"]
DEFAULT_AUTOMATION_TIERS: frozenset[str] = frozenset({"read", "power-off"})
# Where AccessMiddleware leaves the Caller in the ASGI scope: scope["state"][CALLER_STATE_KEY].
CALLER_STATE_KEY = "mcp_caller"


class ConfigError(ValueError):
    """The HTTP settings are unsafe or incomplete; the message says how to fix them."""


class AccessDenied(Exception):
    """A request's Access assertion is missing or doesn't check out."""


def _csv(value: str | None) -> tuple[str, ...]:
    return tuple(v.strip() for v in (value or "").split(",") if v.strip())


def _mask_email(email: str) -> str:
    """``user@example.com`` as ``u***@example.com``: enough to tell logins apart in a log line.

    logsafe's redact() would mask it too, but not with redaction off, and this module can't import
    logsafe's mask_email (shared modules stay self-contained), so mask it here.
    """
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "this identity"


@dataclass(frozen=True)
class Caller:
    """Who sent a request, as far as the server can tell.

    ``kind``: a ``person`` (an Access login), an ``automated`` caller (an admitted service token) or
    ``local`` (stdio, or HTTP with no Access: someone on this machine, treated as a person).
    ``ident``: the login's e-mail (lower-cased), the token's client ID, or ``"local"``.
    ``tiers``: the tiers this caller may use.
    """

    kind: Literal["person", "automated", "local"]
    ident: str
    tiers: frozenset[str]

    @property
    def shown(self) -> str:
        """The caller as it may appear in a log line: an e-mail masked, a client ID as is."""
        return _mask_email(self.ident) if self.kind == "person" else self.ident


LOCAL = Caller("local", "local", frozenset(TIERS))


@dataclass(frozen=True)
class AccessPolicy:
    """Which Cloudflare Access application's assertions to accept."""

    team_domain: str  # "example.cloudflareaccess.com"
    audiences: tuple[str, ...]  # the application's AUD tag(s)
    # Empty: anyone Access let through. repr=False: a config repr in a log or a traceback (uvicorn
    # prints the app's state on some startup failures) must not list who is allowed in.
    emails: frozenset[str] = field(default=frozenset(), repr=False)
    # Access service tokens (the JWT's common_name, the token's client ID) allowed in as automated
    # callers. Empty: none is admitted, whatever Access lets through. repr=False for the same reason.
    service_tokens: frozenset[str] = field(default=frozenset(), repr=False)

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
    # What an automated caller (a service token) may do. Never "costly": that needs a person's yes.
    automation_tiers: frozenset[str] = DEFAULT_AUTOMATION_TIERS

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
        wrong = sorted(t for t in self.automation_tiers if t not in TIERS or t == "costly")
        if wrong:
            raise ConfigError(
                f"MCP_AUTOMATION_TIERS can't include {', '.join(repr(t) for t in wrong)}: "
                f"use {', '.join(t for t in TIERS if t != 'costly')} ('costly' always needs a person's yes)."
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
            service_tokens=frozenset(_csv(env.get("MCP_ALLOWED_SERVICE_TOKENS"))),
        )
    raw_tiers = env.get("MCP_AUTOMATION_TIERS")
    # Unset: the default. Set, even to nothing: exactly what it says, so a blank never widens.
    automation_tiers = DEFAULT_AUTOMATION_TIERS if raw_tiers is None else frozenset(t.lower() for t in _csv(raw_tiers))
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
        automation_tiers=automation_tiers,
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
            # One lock around the whole lookup, network fetch included, so every verification waits
            # while a refetch is in flight (up to the 10 s timeout). That is the point: a burst of
            # tokens naming a key we don't have yet makes one fetch, not one each. Fetches are rare
            # (cache expiry, key rotation), and a hit in the cache holds the lock for microseconds.
            lock = anyio.Lock()
            # When PyJWKClient last went to the network, successful or not: the throttle below
            # counts fetches, so a steady stream of tokens served from the cache must not move it,
            # or a rotated key would be refused until the cache expired. fetch_data is the one
            # method that does network I/O, so wrapping it on this instance catches every fetch.
            last_fetch: float | None = None
            fetch_data = client.fetch_data

            def fetch_and_record() -> Any:
                nonlocal last_fetch
                try:
                    return fetch_data()
                finally:
                    last_fetch = time.monotonic()

            client.fetch_data = fetch_and_record  # type: ignore[method-assign]

            async def resolve(token: str) -> Any:
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
                    key = await anyio.to_thread.run_sync(client.get_signing_key_from_jwt, token)
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
        # The e-mail allow-list is for people: a service token's JWT has no email (see caller()).
        if self.policy.emails and "email" in claims:
            email = str(claims["email"]).lower()
            if email not in self.policy.emails:
                # The identity is somebody Access let through but this server doesn't want; name it
                # only enough to tell logins apart.
                raise AccessDenied(f"{_mask_email(email)} is not in MCP_ALLOWED_EMAILS")
        return claims

    def caller(self, claims: Mapping[str, Any], automation_tiers: frozenset[str]) -> Caller:
        """Who the verified ``claims`` belong to: a person, an admitted service token, or nobody.

        An ``email`` makes a person (the e-mail allow-list was applied in ``verify``). Without one,
        the ``common_name`` (a service token's client ID) must be in ``policy.service_tokens``, and
        then the caller is automated, limited to ``automation_tiers``. Anything else is refused:
        an Access login this server doesn't know is never given the benefit of the doubt.
        """
        email = claims.get("email")
        if isinstance(email, str) and email:
            return Caller("person", email.lower(), frozenset(TIERS))
        name = claims.get("common_name")
        if isinstance(name, str) and name and name in self.policy.service_tokens:
            return Caller("automated", name, frozenset(automation_tiers))
        raise AccessDenied("unknown caller: no email, and no common_name in MCP_ALLOWED_SERVICE_TOKENS")


# --- per-tool authorization --------------------------------------------------------------
# Each sibling tool declares its tier with @tier, directly under @mcp.tool(...). The decorator adds
# one hidden keyword-only parameter, filled by the SDK's resolver machinery (Resolve) before the
# tool body runs and left out of the tool's input schema. The resolver reads the caller from the
# HTTP request the SDK exposes on the tool's Context (AccessMiddleware put it in the ASGI scope),
# decides the tier, and refuses what the caller may not do. A costly call asks the person first
# (an Elicit on 2026-07-28, ctx.elicit under CONFIRM_TIMEOUT before that), and a second resolver
# turns the answer into a go-ahead or a refusal. A contextvar set in the middleware would not do
# for the caller: a Streamable HTTP session runs in its own long-lived task.

TierFn = Callable[[Mapping[str, Any]], Tier]
F = TypeVar("F", bound=Callable[..., Any])
# Where @tier leaves the TierSpec on the function it returns (functools.wraps copies it outward).
TIER_ATTR = "__mcp_tier__"
# The hidden parameter @tier adds, and the name its resolver takes the Context under.
AUTHORIZED_PARAM = "tier_authorized"
_CTX_PARAM = "_tier_ctx"  # a resolver parameter, not a tool one, so the SDK allows the underscore
_ASKED_PARAM = "_tier_asked"  # the second resolver's parameter that takes the first one's outcome
# Seconds a person has to answer a costly call's prompt, and how long its sealed request state lasts.
CONFIRM_TIMEOUT = 120
# The first protocol revision that asks through an InputRequiredResult instead of a request mid-call.
_INPUT_REQUIRED_VERSION = "2026-07-28"
CANT_ASK = "This needs your confirmation, and this app can't ask for it. Run it from Claude Code."
NOT_CONFIRMED = "Not confirmed; nothing was done."
ALREADY_USED = "This confirmation was already used."


class Confirmation(BaseModel):
    """What a person answers when a costly tool asks them (a single yes/no)."""

    confirm: bool = Field(title="Run it?")


@dataclass(frozen=True)
class Allowed:
    """The resolver's answer when the call may go ahead without asking anyone."""


@dataclass(frozen=True)
class TierSpec:
    """A tool's tier, or a function of the call's arguments giving it, and the costly-call prompt."""

    tier: Tier | TierFn
    confirm: str | None = None

    def resolve(self, args: Mapping[str, Any]) -> Tier:
        """The tier of a call with these ``args``. Raises (``ValueError`` or the function's own error) if
        there is none; the caller of this refuses the call then."""
        level = self.tier if isinstance(self.tier, str) else self.tier(args)
        if level not in TIERS:
            raise ValueError(f"{level!r} is not a tier (use one of {', '.join(TIERS)})")
        return level


def tier(level: Tier | TierFn, *, confirm: str | None = None) -> Callable[[F], F]:
    """Declare what a tool can do, so callers outside that tier are refused. Goes directly under
    ``@mcp.tool(...)``::

        @mcp.tool(title="Set power", annotations=ACT_IDEMPOTENT)
        @tier(lambda args: "power-off" if args["state"] == "off" else "everyday")
        async def webos_set_power(state: Literal["on", "off"], tv: TvArg = None) -> str: ...

    ``level`` is one of ``TIERS``, or a function of the call's arguments (by name) returning one.
    ``confirm`` is the sentence a person sees before a ``costly`` call; a literal ``costly`` without
    it is a ``ValueError`` here, a function that returns ``costly`` without it is refused per call.

    The returned function is not the tool's own: it takes one more keyword-only parameter, which
    the SDK fills and leaves out of the input schema, and refuses to run without an allowing
    value there. A direct Python call (not through MCP) that means to skip the check unwraps it:
    ``fn.__wrapped__``.
    """
    if isinstance(level, str):
        if level not in TIERS:
            raise ValueError(f"{level!r} is not a tier (use one of {', '.join(TIERS)})")
        if level == "costly" and not confirm:
            raise ValueError("a costly tool needs confirm=, the sentence a person confirms before it runs")
    elif not callable(level):
        raise ValueError(f"a tier is one of {', '.join(TIERS)} or a function of the arguments (got {level!r})")
    spec = TierSpec(level, confirm)

    def decorate(fn: F) -> F:
        return cast(F, _with_tier(fn, spec))

    return decorate


def tier_of(fn: Callable[..., Any]) -> TierSpec | None:
    """The ``TierSpec`` ``@tier`` gave ``fn`` (or a function wrapping it with functools.wraps)."""
    spec = getattr(fn, TIER_ATTR, None)
    return spec if isinstance(spec, TierSpec) else None


def caller_of(ctx: Context[Any, Any]) -> Caller:
    """Who sent the request this tool call came in.

    No request (stdio, the in-process client, or ``MCPServer.call_tool`` in a test): ``LOCAL``.
    An HTTP request without a ``Caller`` in its scope never passed through ``AccessMiddleware``,
    so it is refused rather than guessed at.
    """
    try:
        request = ctx.request_context.request
    except ValueError:  # Context.request_context: no request context at all
        return LOCAL
    return LOCAL if request is None else _caller_in(request)


def _caller_in(request: Any) -> Caller:
    """The ``Caller`` ``AccessMiddleware`` left in an HTTP request's scope; refuse if there is none."""
    scope = getattr(request, "scope", None)
    state = scope.get("state") if isinstance(scope, Mapping) else None
    caller = state.get(CALLER_STATE_KEY) if isinstance(state, Mapping) else None
    if not isinstance(caller, Caller):
        raise ToolError("Refused: the server can't tell who sent this request.")
    return caller


def _log_outcome(caller: Caller | None, tool: str, level: str, outcome: str) -> None:
    kind, shown = (caller.kind, caller.shown) if caller is not None else ("unknown", "unknown")
    log.info("Authorization: %s caller %s, tool %s, tier %s: %s", kind, shown, tool, level, outcome)


async def _authorize(spec: TierSpec, tool: str, ctx: Context[Any, Any], args: Mapping[str, Any]) -> Any:
    """The resolver body: allow, or raise ``ToolError`` with why not. Never allows by default."""
    caller: Caller | None = None
    level = "?"
    try:
        level = spec.resolve(args)
        caller = caller_of(ctx)
    except ToolError as exc:
        _log_outcome(caller, tool, level, f"refused ({exc})")
        raise
    except Exception as exc:
        # A tier function that raises or names no tier: a bug in the sibling, never a reason to run.
        _log_outcome(caller, tool, level, f"refused (no tier: {type(exc).__name__})")
        raise ToolError("Refused: this tool's tier couldn't be worked out, so nothing was done.") from exc
    if level not in caller.tiers:
        allowed = ", ".join(t for t in TIERS if t in caller.tiers) or "nothing"
        _log_outcome(caller, tool, level, "refused")
        raise ToolError(f"This tool needs a person; automated callers may only use: {allowed}.")
    if level != "costly":
        return Allowed()
    # Only a person or a local caller gets here: automated callers never have costly in their tiers.
    if not spec.confirm:
        # A function-form tier that says costly, with no sentence to ask with: never asks, never runs.
        _log_outcome(caller, tool, level, "refused (costly, but no confirm sentence to ask with)")
        raise ToolError(NOT_CONFIRMED)
    if not _can_ask(ctx):
        # Never fall back to anything the model could answer for the person.
        _log_outcome(caller, tool, level, "refused (this app can't ask)")
        raise ToolError(CANT_ASK)
    arguments = json.dumps(dict(args), sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    prompt = f"{spec.confirm}\n\nServer: {ctx.mcp_server.name} · Tool: {tool} · Arguments: {arguments}"
    if _asks_by_input_required(ctx):
        # The SDK sends this inside an InputRequiredResult and gives _confirmed the answer from the retry.
        return Elicit(prompt, Confirmation)
    # The older revisions ask mid-call; nobody answering must not hold the call (and session) forever.
    try:
        with anyio.fail_after(CONFIRM_TIMEOUT):
            return await ctx.elicit(prompt, Confirmation)
    except TimeoutError:
        return _TimedOut()


@dataclass(frozen=True)
class _TimedOut:
    """The older revisions' prompt went unanswered for ``CONFIRM_TIMEOUT`` seconds."""


def _can_ask(ctx: Context[Any, Any]) -> bool:
    """Whether the client declared form elicitation (a bare ``elicitation: {}`` counts, as in the SDK)."""
    try:
        capabilities = ctx.client_capabilities
    except ValueError:  # no request at all (MCPServer.call_tool): nobody to ask
        return False
    elicitation = capabilities.elicitation if capabilities is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _asks_by_input_required(ctx: Context[Any, Any]) -> bool:
    """The same test the SDK's resolver machinery makes to pick how it asks."""
    version = ctx.protocol_version
    return version is not None and is_version_at_least(version, _INPUT_REQUIRED_VERSION)


async def _confirmed(tool: str, ctx: Context[Any, Any], asked: Any) -> Allowed:
    """The second resolver: turn ``_authorize``'s outcome into ``Allowed()`` or a refusal.

    Separate from ``_authorize`` because on 2026-07-28 the answer arrives after ``_authorize``
    returned its ``Elicit``: the SDK fills it in here, from the retry. The outcome comes in two
    shapes: as is (an ``Elicit`` answered, or ``Allowed`` wrapped as accepted), or, on the older
    revisions, ``_authorize``'s own ``ctx.elicit`` outcome wrapped once more as an accepted value.
    """
    answer = asked
    if isinstance(answer, AcceptedElicitation) and isinstance(
        answer.data, AcceptedElicitation | DeclinedElicitation | CancelledElicitation | _TimedOut
    ):
        answer = answer.data
    if isinstance(answer, AcceptedElicitation) and isinstance(answer.data, Allowed):
        return answer.data
    caller, level = caller_of(ctx), "costly"  # only a costly call asks
    if isinstance(answer, _TimedOut):
        _log_outcome(caller, tool, level, f"timed out after {CONFIRM_TIMEOUT} s; refused")
        raise ToolError(NOT_CONFIRMED)
    if isinstance(answer, CancelledElicitation):
        _log_outcome(caller, tool, level, "cancelled; refused")
        raise ToolError(NOT_CONFIRMED)
    if not (isinstance(answer, AcceptedElicitation) and isinstance(answer.data, Confirmation)):
        _log_outcome(caller, tool, level, "declined; refused")
        raise ToolError(NOT_CONFIRMED)
    if answer.data.confirm is not True:
        _log_outcome(caller, tool, level, "declined (answered no); refused")
        raise ToolError(NOT_CONFIRMED)
    if _asks_by_input_required(ctx):
        # The answer came in a retry anyone holding it could send again: one yes runs once.
        try:
            _remember_confirmation(_confirmation_key(ctx, caller))
        except ToolError:
            _log_outcome(caller, tool, level, "replayed; refused")
            raise
    _log_outcome(caller, tool, level, "confirmed")
    return Allowed()


# --- one yes, one run (2026-07-28) -----------------------------------------------------------
# An answered retry is a request anyone holding it can send again within the sealed state's TTL.
# The SDK checks the state's seal, expiry, tool, arguments and caller, but keeps no record of use,
# so a used confirmation is remembered here until its state would have expired anyway.
_used_confirmations: dict[str, float] = {}
# The digest of the sealed token this request carried, and the plaintext it unsealed to, set by
# _RecordingCodec when the SDK unseals it (in this request's own context, before any handler).
_unsealed: contextvars.ContextVar[tuple[str, str] | None] = contextvars.ContextVar("mcp_unsealed", default=None)


def _remember_confirmation(key: str) -> None:
    """Record ``key`` as used, or raise ``ToolError`` if it already was (and hasn't expired)."""
    now = time.monotonic()
    for old in [k for k, expires in _used_confirmations.items() if expires <= now]:
        del _used_confirmations[old]
    if key in _used_confirmations:
        raise ToolError(ALREADY_USED)
    _used_confirmations[key] = now + CONFIRM_TIMEOUT


def _confirmation_key(ctx: Context[Any, Any], caller: Caller) -> str:
    """What identifies this confirmation: the sealed token it came back in.

    The plaintext ``ctx.request_state`` is the same for every prompt for the same call (same tool,
    same arguments), so keyed on that alone a second, fresh yes within the TTL would read as a
    replay. Each sealed token is unique (a random nonce), so with ``request_state_security()``
    installed that is the key. Under another policy (the SDK's default) the plaintext and the
    caller are: still one run per yes, but one confirmed run per caller and call per TTL.
    """
    state = ctx.request_state or ""
    unsealed = _unsealed.get()
    if unsealed is not None and unsealed[0] == state:
        return f"sealed:{unsealed[1]}"
    plain = f"{caller.kind}:{caller.ident}\n{state}"
    return "plain:" + hashlib.sha256(plain.encode("utf-8", "surrogatepass")).hexdigest()


class _RecordingCodec:
    """The SDK's AES-GCM codec, noting which sealed token each request's state came in."""

    def __init__(self, inner: AESGCMRequestStateCodec) -> None:
        self._inner = inner

    def seal(self, payload: bytes) -> str:
        return self._inner.seal(payload)

    def unseal(self, token: str) -> bytes:
        payload = self._inner.unseal(token)
        try:
            inner = json.loads(payload)["s"]  # the SDK's envelope: the handler's state is under "s"
        except (ValueError, KeyError, TypeError):
            inner = None
        if isinstance(inner, str):
            _unsealed.set((inner, hashlib.sha256(token.encode()).hexdigest()))
        return payload


def caller_principal(ctx: ServerRequestContext[Any, Any]) -> str | None:
    """Who sealed request state belongs to: ``"<kind>:<ident>"`` of the request's ``Caller``.

    ``None`` with no HTTP request (stdio, the in-process client): nothing to bind to. An HTTP
    request without a ``Caller`` raises, which the SDK turns into a refusal (it fails closed).
    """
    if ctx.request is None:
        return None
    caller = _caller_in(ctx.request)
    return f"{caller.kind}:{caller.ident}"


def request_state_security() -> RequestStateSecurity:
    """The request-state policy every sibling passes to ``MCPServer(..., request_state_security=...)``.

    A process-local key (as ``RequestStateSecurity.ephemeral()`` makes), state that expires with
    the prompt (``CONFIRM_TIMEOUT``), bound to the caller who was asked (``caller_principal``): a
    person's sealed yes re-sent in another person's request is rejected by the SDK.
    """
    return RequestStateSecurity(
        codec=_RecordingCodec(AESGCMRequestStateCodec([os.urandom(32)])),
        ttl=CONFIRM_TIMEOUT,
        bind_principal=caller_principal,
    )


def _check_authorized(outcome: Any) -> None:
    """The wrapper's own check: run only behind an accepted, allowing outcome from the resolvers."""
    if isinstance(outcome, AcceptedElicitation) and isinstance(outcome.data, Allowed):
        return
    raise ToolError(NOT_CONFIRMED)


def _with_tier(fn: Callable[..., Any], spec: TierSpec) -> Callable[..., Any]:
    """Build the function @tier returns: ``fn``'s parameters plus the hidden resolved one.

    The hints are evaluated here, in ``fn``'s own module (``typing.get_type_hints`` uses
    ``fn.__globals__``), and set on the wrapper as objects: with ``from __future__ import
    annotations`` they are strings, which the SDK would otherwise evaluate in this module's
    namespace, where a sibling's ``TvArg`` doesn't exist.
    """
    sig = inspect.signature(fn)
    for reserved in (AUTHORIZED_PARAM, _CTX_PARAM):
        if reserved in sig.parameters:
            raise ValueError(f"@tier adds a parameter named {reserved!r}; {fn.__name__} already has one")
    hints = typing.get_type_hints(fn, include_extras=True)
    params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()]
    tool = getattr(fn, "__name__", repr(fn))

    # The resolver takes the Context and every tool argument by name (the SDK passes exactly the
    # names in the input schema, so the Context parameter and other resolved ones are left out).
    skip = set(find_resolved_parameters(fn)) | {find_context_parameter(fn)}
    by_name = [
        p.name
        for p in params
        if p.name not in skip and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    ]

    async def resolver(**kwargs: Any) -> Any:
        ctx = kwargs.pop(_CTX_PARAM)
        return await _authorize(spec, tool, ctx, kwargs)

    resolver.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [inspect.Parameter(_CTX_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=Context)]
        + [inspect.Parameter(n, inspect.Parameter.KEYWORD_ONLY) for n in by_name]
    )
    resolver.__annotations__ = {_CTX_PARAM: Context}
    resolver.__module__ = getattr(fn, "__module__", __name__)
    resolver.__qualname__ = resolver.__name__ = f"{getattr(fn, '__qualname__', tool)}.<tier>"

    # The second resolver gets the first one's outcome, whole (the person's answer, on 2026-07-28,
    # arrives only here), and the Context, and decides.
    async def confirmed(**kwargs: Any) -> Allowed:
        return await _confirmed(tool, kwargs[_CTX_PARAM], kwargs[_ASKED_PARAM])

    asked = Annotated[ElicitationResult[Confirmation], Resolve(resolver)]
    confirmed.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        [
            inspect.Parameter(_CTX_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=Context),
            inspect.Parameter(_ASKED_PARAM, inspect.Parameter.KEYWORD_ONLY, annotation=asked),
        ]
    )
    confirmed.__annotations__ = {_CTX_PARAM: Context, _ASKED_PARAM: asked}
    confirmed.__module__ = resolver.__module__
    confirmed.__qualname__ = confirmed.__name__ = f"{getattr(fn, '__qualname__', tool)}.<confirmed>"

    hidden = inspect.Parameter(
        AUTHORIZED_PARAM,
        inspect.Parameter.KEYWORD_ONLY,
        annotation=Annotated[ElicitationResult[Confirmation], Resolve(confirmed)],
    )
    # A keyword-only parameter goes before **kwargs, if the tool has one.
    at = next((i for i, p in enumerate(params) if p.kind is inspect.Parameter.VAR_KEYWORD), len(params))
    params.insert(at, hidden)

    wrapper: Callable[..., Any]
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            _check_authorized(kwargs.pop(AUTHORIZED_PARAM, None))
            return await fn(*args, **kwargs)

    else:

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            _check_authorized(kwargs.pop(AUTHORIZED_PARAM, None))
            return fn(*args, **kwargs)

    return_annotation = hints.get("return", sig.return_annotation)
    wrapper.__signature__ = sig.replace(parameters=params, return_annotation=return_annotation)  # type: ignore[attr-defined]
    annotations = {p.name: p.annotation for p in params if p.annotation is not inspect.Parameter.empty}
    if return_annotation is not inspect.Signature.empty:
        annotations["return"] = return_annotation
    wrapper.__annotations__ = annotations
    setattr(wrapper, TIER_ATTR, spec)
    return wrapper


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

    Every request it passes on carries who sent it, as a ``Caller`` in
    ``scope["state"][CALLER_STATE_KEY]``: the person or admitted service token the assertion names,
    or ``LOCAL`` when there is no verifier.
    """

    def __init__(
        self,
        app: ASGIApp,
        verifier: AccessVerifier | None,
        *,
        local_only: bool = False,
        health_check: HealthChecker | None = None,
        automation_tiers: frozenset[str] = DEFAULT_AUTOMATION_TIERS,
    ) -> None:
        self.app = app
        self.verifier = verifier
        self.local_only = local_only
        self.health_check = health_check
        self.automation_tiers = automation_tiers

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
        caller = LOCAL
        if self.verifier is not None:
            token = headers.get(ACCESS_HEADER.encode(), b"").decode("latin-1")
            try:
                if not token:
                    raise AccessDenied("no Cloudflare Access assertion (request didn't come through Access)")
                claims = await self.verifier.verify(token)
                caller = self.verifier.caller(claims, self.automation_tiers)
            except AccessDenied as exc:
                client = scope.get("client") or ("?", 0)
                log.warning("Denied %s %s from %s: %s", scope.get("method"), scope["path"], client[0], exc)
                await _plain(send, 403, {"error": "forbidden"})
                return
            log.debug("Access OK for %s caller %s", caller.kind, caller.shown)
        scope.setdefault("state", {})[CALLER_STATE_KEY] = caller
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
    return AccessMiddleware(
        app,
        verifier,
        local_only=cfg.local_only,
        health_check=health_check,
        automation_tiers=cfg.automation_tiers,
    )


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
