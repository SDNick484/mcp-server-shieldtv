"""Log redaction: keep secrets, LAN addresses and MACs out of logs you might paste into an issue.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Logs mention devices by name wherever we control the message, but libraries
log addresses freely, and tracebacks carry URLs and headers. So instead of
editing messages one by one, the stderr handler's *formatter* redacts the
final text, tracebacks included:

    192.0.2.60                     -> x.x.x.60      (last octet kept, to tell devices apart)
    2001:db8::1, fe80::1ff:fe00:1    -> x:x::1        (IPv6: last group kept, likewise)
    AA:BB:CC:DD:EE:FF                -> xx:xx:xx:xx:EE:FF
    AABBCCDDEEFF, 0009b0f76cfd       -> xxxxxxxxEEFF, xxxxxxxx6cfd  (bare hex, as in MQTT topics)
    user@example.com                 -> u***@example.com  (domain kept: it says which IdP)
    mqtt://user:secret@host          -> mqtt://user:***@host
    http://host/x?token=abc&key=def  -> http://host/x?token=***&key=***
    /cb?access_token=abc&code=def    -> /cb?access_token=***&code=***  (OAuth callbacks)
    eyJhbGciOi...(a JWT)             -> [jwt redacted]   (Cloudflare Access assertions)
    Authorization: Bearer abc123     -> Authorization: Bearer [redacted]
    Authorization: Basic dXNlcjpwdw  -> Authorization: Basic [redacted]  (any scheme)
    CF-Access-Client-Secret: 0a1b... -> CF-Access-Client-Secret: ***  (Access service tokens)
    Cookie: CF_Authorization=abc     -> Cookie: CF_Authorization=[redacted]
    {"client-key": "abc", "pin": "1"} -> {"client-key": "***", "pin": "***"}  (JSON and dict reprs)
    MQTT_PASSWORD=hunter2            -> MQTT_PASSWORD=***  (.env / shell style)
    -----BEGIN PRIVATE KEY----- ...  -> [pem redacted]   (certificates and keys)

Loopback and unspecified addresses (127.x, 0.0.0.0, ::1, ::) are left alone:
they say nothing about your network and matter when debugging the simulator.
So are things shaped like an e-mail address that aren't one: URL userinfo
(ssh://git@host, mqtt://user:***@host), scp-style remotes (git@github.com:x),
decorators, and hosts without a letters-only TLD (user@localhost, admin@10.0.0.1).

Each server's CLI turns redaction off with --no-redact or its
<NAME>_LOG_UNREDACTED=1, e.g. when you are the only reader. Secrets (JWTs,
bearer tokens, keys, URL passwords and query tokens) are redacted even then.

A sibling with a device-specific secret (a pairing key, a client-key in a
JSON blob) adds a rule with register_custom_pattern() at import time instead
of carrying its own formatter; custom rules count as secrets, so they too are
applied even with redaction off.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import urllib.parse
from collections.abc import Callable

# A trailing "." ends a sentence ("connected to 10.0.0.5."), so only a dot
# followed by a digit (a fifth group, as in a version string) disqualifies.
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?!\d|\.\d)")
# A candidate only: two to seven colons between hex groups (so "::" and full
# eight-group forms both qualify), then ipaddress decides. That keeps the regex
# readable and rejects look-alikes such as MACs (six groups, no "::") and
# times ("10:23:45"). The lookarounds stop it starting or ending inside a
# longer token, e.g. the IPv4 tail of "::ffff:192.0.2.5", which _IPV4 handles.
# It may also start right after a label's colon ("ipv6:fe80::1"). That can only
# matter where no match starts further left, and ipaddress still has to accept
# what's left, so at worst it redacts the valid tail of a longer colon run.
_IPV6 = re.compile(r"(?:(?<![\w:.])|(?<=\w:))(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:]|\.\d)")
# No lookaround lets _MAC start or end next to a colon, so it can't take two-digit
# groups out of a longer colon run such as an eight-group IPv6 address.
_MAC = re.compile(r"(?<![0-9A-Fa-f:])((?:[0-9A-Fa-f]{2}[:-]){4})([0-9A-Fa-f]{2}[:-][0-9A-Fa-f]{2})(?![0-9A-Fa-f:])")
_BARE_MAC = re.compile(r"(?<![0-9A-Za-z-])([0-9A-Fa-f]{8})([0-9A-Fa-f]{4})(?![0-9A-Za-z-])")
# An e-mail address. The local part can't start inside a longer token (the first lookbehind) or
# right after "://", where "user@host" is URL userinfo, not an address. The domain needs a
# letters-only TLD, so "user@localhost" and "admin@10.0.0.1" don't count, and it can't be followed
# by ":" and a non-space, which is an scp-style git remote (git@github.com:owner/repo). Possessive
# quantifiers (++) because a log line can hold a long token: no backtracking into it.
_EMAIL = re.compile(
    r"(?<![\w.%+-])(?<!://)([A-Za-z0-9._%+-]++)@((?:[A-Za-z0-9-]++\.)+[A-Za-z]{2,63})(?![A-Za-z0-9-]|\.[A-Za-z0-9]|:\S)"
)
# Anchored at the start of a word: unanchored, a long run of word characters (a hex or base64 blob)
# was rescanned from every offset, quadratic in the length of the line (20 s for 100 KB).
_URL_PASSWORD = re.compile(r"(?<!\w)(\w++://[^/\s:@?#]+):[^@/\s?#]+@")
# The names whose value is a credential wherever they appear: as a query parameter, as a key in a
# JSON object or a Python dict/headers repr, or as a header line. One vocabulary, so a name added
# for one shape (an OAuth callback's ?access_token=) is caught in the others too. `pin` and `psk`
# are the pairing secrets of TVs and hubs; the cf-access-client-* pair is a Cloudflare Access
# service token (the id alone is harmless, but it's only ever logged next to the secret).
_SECRET_NAMES = (
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|token|api[_-]?key|apikey|x-api-key|client[_-]?secret"
    r"|client[_-]?key|secret|password|passwd|pass|pwd|pin|passcode|psk|pairing[_-]?key|auth|signature|sig"
    r"|cf-access-client-secret|cf-access-client-id"
)
# Only directly after ? or &, so "monkey=1" or "access_token=" in prose don't count.
# The value stops at quotes and brackets too, so a URL quoted in JSON keeps its closing quote.
# `key` and `code` only here: ?key= and ?code= (an OAuth authorization code) carry credentials, but
# "key" and "code" as JSON keys are usually a map key and a status.
_QUERY_SECRET = re.compile(rf"(?i)([?&](?:{_SECRET_NAMES}|key|code)=)[^&#\s\"'<>()\[\]{{}}]+")
_JSON_SECRET = re.compile(rf"""(?i)(["'](?:{_SECRET_NAMES})["']\s*:\s*["'])[^"']+(["'])""")
# A header line or "Name: value" pair outside JSON ("CF-Access-Client-Secret: 0123..."). The value
# runs to the end of the line or the next comma/semicolon, so a header dump redacts one value each.
_HEADER_SECRET = re.compile(rf"(?im)^(\s*(?:{_SECRET_NAMES})\s*:\s*)[^\s,;][^,;\n]*")
# Shell/.env style, as a sibling might log its environment at debug: MQTT_PASSWORD=..., CF_ACCESS_AUD stays.
_ENV_SECRET = re.compile(r"(?m)(\b[A-Z][A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|PSK)=)(?!\s|$)[^\s]+")
# Cloudflare Access's session cookie, in Cookie: and Set-Cookie: lines and in cookie-jar reprs.
_CF_COOKIE = re.compile(r"(?i)(\bCF_Authorization=)[^;\s\"']+")
# A JWT is three base64url parts; its header always starts with {" -> "eyJ".
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
# Any Authorization scheme, not just Bearer: Basic carries base64(user:password), Token/Digest/ApiKey
# carry the credential outright. Only after "Authorization:" (or a dict key) for the generic schemes,
# so the word "basic" in prose is left alone; a bare "Bearer <token>" is still caught anywhere.
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_AUTHORIZATION = re.compile(
    r"""(?i)(\b(?:proxy-)?authorization["']?\s*[:=]\s*["']?\s*(?:basic|digest|token|apikey|api-key|negotiate|ntlm|hoba|mutual|dpop|scram-sha-\d+)\s+)[^\s"',;]+"""
)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?-----END [A-Z0-9 ]+-----", re.S)

_Replacement = str | Callable[[re.Match[str]], str]
_CUSTOM_PATTERNS: list[tuple[re.Pattern[str], _Replacement]] = []
_IDENTIFIER_PATTERNS: list[tuple[re.Pattern[str], _Replacement]] = []


def register_custom_pattern(
    pattern: re.Pattern[str] | str, replacement: str | Callable[[re.Match[str]], str] = "[redacted]"
) -> None:
    """Redact ``pattern`` too, wherever this module's built-in secret rules apply.

    ``pattern`` is a compiled pattern or a string to compile (a bad one raises
    ``re.error`` here, at registration, rather than on the first log line).
    ``replacement`` is what ``re.sub`` takes: a string (backreferences such as
    ``\\1`` work) or a function of the match. For example, webOS's client key::

        register_custom_pattern(r'("client-key":\\s*")[^"]+', r"\\1***")

    Custom rules run inside ``redact_secrets``, so they apply even with address
    redaction off. They run after the built-in rules, in the order registered,
    and see text those already redacted. Registering the same rule twice is a
    no-op, so a module re-imported in tests doesn't apply it twice.
    """
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    rule = (compiled, replacement)
    if rule not in _CUSTOM_PATTERNS:
        _CUSTOM_PATTERNS.append(rule)


def clear_custom_patterns() -> None:
    """Forget every rule ``register_custom_pattern`` added (the built-in rules stay)."""
    _CUSTOM_PATTERNS.clear()


def register_identifier_pattern(
    pattern: re.Pattern[str] | str, replacement: str | Callable[[re.Match[str]], str] = "[redacted]"
) -> None:
    """Redact a device identifier (UUID, serial number, socket id) when address redaction is on."""
    compiled = re.compile(pattern) if isinstance(pattern, str) else pattern
    rule = (compiled, replacement)
    if rule not in _IDENTIFIER_PATTERNS:
        _IDENTIFIER_PATTERNS.append(rule)


def clear_identifier_patterns() -> None:
    """Forget every rule ``register_identifier_pattern`` added."""
    _IDENTIFIER_PATTERNS.clear()


def mask_email(address: str) -> str:
    """``user@example.com`` -> ``u***@example.com``: enough to tell identities apart, not enough to paste.

    ``redact()`` applies this to every address it finds in a log line; call it directly
    for text that isn't logged (an error returned to a client) or that is logged
    with address redaction off. Anything without an ``@`` comes back unchanged.
    """
    if not address or not isinstance(address, str) or "@" not in address:
        return address
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_ip(address: str) -> str:
    """Mask an IPv4 or IPv6 address while preserving loopback, unspecified, and hostnames."""
    if not address or not isinstance(address, str):
        return address
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.is_loopback or ip.is_unspecified:
        return address
    if isinstance(ip, ipaddress.IPv4Address):
        return f"x.x.x.{address.split('.')[-1]}"
    # The text after the last ":" is the last hextet for "2001:db8::1", but for the dotted forms
    # ("::ffff:192.0.2.5", "64:ff9b::192.0.2.5") it's a whole IPv4 address, which would leak the
    # first three octets. Mask that embedded address the IPv4 way instead.
    tail = address.rsplit(":", 1)[1] if ":" in address else ""
    if "." in tail:
        return f"x:x::x.x.x.{tail.split('.')[-1]}"
    return f"x:x::{tail or '0'}"


def mask_host_port(host_port: str) -> str:
    """Mask host or host:port strings, preserving port numbers and non-IP hostnames."""
    if not host_port or not isinstance(host_port, str):
        return host_port
    if host_port.startswith("[") and "]:" in host_port:
        host, port = host_port[1:].split("]:", 1)
        return f"[{mask_ip(host)}]:{port}"
    if host_port.startswith("[") and host_port.endswith("]"):
        return f"[{mask_ip(host_port[1:-1])}]"
    if ":" in host_port and host_port.count(":") == 1:
        host, port = host_port.split(":", 1)
        return f"{mask_ip(host)}:{port}"
    return mask_ip(host_port)


def mask_url(url: str) -> str:
    """Mask the host IP in a URL while preserving scheme, port, path, query, and userinfo."""
    if not url or not isinstance(url, str):
        return url
    try:
        parsed = urllib.parse.urlsplit(url)
    except Exception:
        return url
    if not parsed.netloc:
        return url
    hostname = parsed.hostname
    if not hostname:
        return url
    masked = mask_ip(hostname)
    if masked == hostname:
        return url
    netloc = parsed.netloc
    if f"[{hostname}]" in netloc:
        netloc = netloc.replace(f"[{hostname}]", f"[{masked}]", 1)
    else:
        userinfo = ""
        if "@" in netloc:
            userinfo = netloc.split("@", 1)[0] + "@"
        # parsed.port raises ValueError for a port that isn't a number or is out of range. This
        # runs inside log formatting, where raising would lose the whole line, so keep whatever
        # text follows the host instead (only the host is masked anyway).
        host_and_port = netloc.rsplit("@", 1)[-1]
        port_part = host_and_port[len(hostname) :] if host_and_port.lower().startswith(hostname) else ""
        host_repr = f"[{masked}]" if ":" in masked else masked
        netloc = f"{userinfo}{host_repr}{port_part}"
    return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def _ip(m: re.Match[str]) -> str:
    first = m.group(1)
    if first in ("127", "0"):
        return m.group(0)
    return f"x.x.x.{m.group(4)}"


def _ipv6(m: re.Match[str]) -> str:
    text = m.group(0)
    try:
        addr = ipaddress.IPv6Address(text)
    except ValueError:  # a look-alike, not an address
        return text
    if addr.is_loopback or addr.is_unspecified:
        return text
    return "x:x::" + (text.rsplit(":", 1)[1] or "0")


def redact_secrets(text: str) -> str:
    """Credentials only: always applied, even when addresses are shown."""
    text = _PEM.sub("[pem redacted]", text)
    text = _JWT.sub("[jwt redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    text = _AUTHORIZATION.sub(r"\1[redacted]", text)
    text = _CF_COOKIE.sub(r"\1[redacted]", text)
    text = _URL_PASSWORD.sub(r"\1:***@", text)
    text = _QUERY_SECRET.sub(r"\1***", text)
    text = _JSON_SECRET.sub(r"\1***\2", text)
    text = _HEADER_SECRET.sub(r"\1***", text)
    text = _ENV_SECRET.sub(r"\1***", text)
    for pattern, replacement in tuple(_CUSTOM_PATTERNS):
        text = pattern.sub(replacement, text)
    return text


def redact(text: str) -> str:
    text = redact_secrets(text)
    for pattern, replacement in tuple(_IDENTIFIER_PATTERNS):
        text = pattern.sub(replacement, text)
    text = _EMAIL.sub(lambda m: mask_email(m.group(0)), text)
    # IPv6 before MACs: an eight-group address of two-digit groups is an address, not a MAC.
    text = _IPV6.sub(_ipv6, text)
    text = _IPV4.sub(_ip, text)
    text = _MAC.sub(lambda m: "xx:xx:xx:xx:" + m.group(2), text)
    return _BARE_MAC.sub(lambda m: "xxxxxxxx" + m.group(2), text)


class RedactingFormatter(logging.Formatter):
    def __init__(self, fmt: str | None = None, datefmt: str | None = None, *, addresses: bool = True) -> None:
        super().__init__(fmt, datefmt)
        self.addresses = addresses

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        return redact(text) if self.addresses else redact_secrets(text)


def setup_logging(level: int, *, redacted: bool = True, fmt: str = "%(levelname)s %(name)s: %(message)s") -> None:
    """Log to stderr (stdout belongs to the MCP stdio transport). Addresses are
    redacted unless asked not to; secrets always are."""
    import sys

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingFormatter(fmt, addresses=redacted))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
