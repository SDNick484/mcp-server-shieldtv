"""Log redaction.

Shared by the sibling servers along with logsafe.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.
"""

from __future__ import annotations

import logging
import re
import time

import pytest

from shieldtv_mcp import logsafe
from shieldtv_mcp.logsafe import (
    RedactingFormatter,
    clear_custom_patterns,
    clear_identifier_patterns,
    mask_email,
    mask_host_port,
    mask_ip,
    mask_url,
    redact,
    redact_secrets,
    register_custom_pattern,
    register_identifier_pattern,
    setup_logging,
)

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9.eyJpc3MiOiJodHRwczovL3RlYW0iLCJlbWFpbCI6Im5AZXhhbXBsZS5jb20ifQ.c2lnbmF0dXJl"


@pytest.fixture
def custom_patterns(monkeypatch):
    """An empty custom-rule list per test, put back afterwards, so rules registered (or cleared)
    here don't leak into other tests, and rules a sibling registers at import don't leak in."""
    monkeypatch.setattr(logsafe, "_CUSTOM_PATTERNS", [])
    monkeypatch.setattr(logsafe, "_IDENTIFIER_PATTERNS", [])


def test_addresses_and_macs():
    assert redact("eISCP -> 192.0.2.147 PWR01") == "eISCP -> x.x.x.147 PWR01"
    assert redact("mac 00:09:B0:F7:6C:FD / 0009B0F76CFD") == "mac xx:xx:xx:xx:6C:FD / xxxxxxxx6CFD"
    assert redact("simulator at 127.0.0.1:60128") == "simulator at 127.0.0.1:60128"  # loopback kept
    assert redact("bound to 0.0.0.0:8000") == "bound to 0.0.0.0:8000"  # unspecified kept


def test_lowercase_bare_macs():
    assert redact("topic sofabaton/0009b0f76cfd/up") == "topic sofabaton/xxxxxxxx6cfd/up"
    assert redact("id 0009b0F76cFd") == "id xxxxxxxx6cFd"  # mixed case: the kept four keep theirs
    # Twelve hex digits inside a longer token (a git SHA, a UUID part) are left alone.
    assert redact("commit 16809a6f0a1b2c3d4e5f") == "commit 16809a6f0a1b2c3d4e5f"
    assert redact("uuid c5c5c5c5-0000-4000-8000-000000000025") == "uuid c5c5c5c5-0000-4000-8000-000000000025"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("connect 2001:db8::1 ok", "connect x:x::1 ok"),
        ("link fe80::1ff:fe00:abcd", "link x:x::abcd"),
        ("full 2001:0db8:85a3:0000:0000:8a2e:0370:7334", "full x:x::7334"),
        ("upper 2001:DB8::ABCD", "upper x:x::ABCD"),
        ("url http://[2001:db8::abcd]:8080/", "url http://[x:x::abcd]:8080/"),
        ("scoped fe80::1%eth0", "scoped x:x::1%eth0"),
        ("prefix 2001:db8::", "prefix x:x::0"),
        ("connected to 2001:db8::1.", "connected to x:x::1."),  # end of a sentence
        ("ipv6:fe80::1 up", "ipv6:x:x::1 up"),  # after a label's colon
        ("mapped ::ffff:192.0.2.5", "mapped ::ffff:x.x.x.5"),  # the IPv4 rule takes the tail
    ],
)
def test_ipv6_addresses(line, expected):
    assert redact(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "loopback [::1]:8000",
        "listening on ::",
        "loopback 0:0:0:0:0:0:0:1",
        "at 10:23:45 on C++ std::vector",
        "Error::Io from serde_json::de",
    ],
)
def test_ipv6_leaves_loopback_unspecified_and_look_alikes(line):
    assert redact(line) == line


def test_ipv6_and_macs_dont_clobber_each_other():
    # A six-pair MAC isn't an IPv6 address (no "::", too few groups): the MAC rule still has it.
    assert redact("mac 00:09:B0:F7:6C:FD") == "mac xx:xx:xx:xx:6C:FD"
    assert redact("mac 00:09:b0:f7:6c:fd.") == "mac xx:xx:xx:xx:6c:fd."
    assert redact("mac 00-09-B0-F7-6C-FD") == "mac xx:xx:xx:xx:6C-FD"
    # Eight two-digit groups are an address, redacted whole, not a MAC with its tail masked.
    assert redact("addr fe80:0:0:0:aa:bb:cc:dd") == "addr x:x::dd"
    assert redact("addr 20:01:0d:b8:00:00:00:01") == "addr x:x::01"
    # And one kept as loopback isn't then picked apart by the MAC rule.
    assert redact("lo 00:00:00:00:00:00:00:01") == "lo 00:00:00:00:00:00:00:01"
    both = "mac 00:09:B0:F7:6C:FD on 2001:db8::6cfd"
    assert redact(both) == "mac xx:xx:xx:xx:6C:FD on x:x::6cfd"


def test_ipv4_at_the_end_of_a_sentence():
    assert redact("connected to 192.0.2.5.") == "connected to x.x.x.5."
    assert redact("version 1.2.3.4.5") == "version 1.2.3.4.5"  # five groups: not an address


def test_ipv6_is_an_address_not_a_secret():
    assert redact_secrets("from 2001:db8::1") == "from 2001:db8::1"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("?token=secret123&foo=bar", "?token=***&foo=bar"),
        (
            "GET http://10.0.0.5/api?volume=3&API_KEY=abc&apikey=def#frag",
            "GET http://10.0.0.5/api?volume=3&API_KEY=***&apikey=***#frag",
        ),
        ("/x?key=k1&secret=s2&password=p3&pass=p4 done", "/x?key=***&secret=***&password=***&pass=*** done"),
        ("/x?Token=AbC", "/x?Token=***"),  # names match in any case and keep theirs
        ('{"url": "http://h/?token=abc", "n": 1}', '{"url": "http://h/?token=***", "n": 1}'),  # quote kept
        ("(see /x?key=abc)", "(see /x?key=***)"),
        # Only whole parameter names, and only in a query string: these aren't secrets.
        ("/x?monkey=1&keyboard=2&passphrase_hint=3", "/x?monkey=1&keyboard=2&passphrase_hint=3"),
        ("key=value outside a URL", "key=value outside a URL"),
        ("/x?token=&next=1", "/x?token=&next=1"),  # nothing to hide
        ("http://127.0.0.1:8000?email=user@example.com", "http://127.0.0.1:8000?email=user@example.com"),
        ("http://127.0.0.1:8000#frag@section", "http://127.0.0.1:8000#frag@section"),
    ],
)
def test_url_query_secrets(line, expected):
    assert redact_secrets(line) == expected


def test_json_secret_redaction():
    json_text = (
        '{"access_token": "secret-token", "refresh_token": "refresh-123", "client_secret": "cs-456", '
        '"client-key": "ck-789", "api_key": "ak-1", "apikey": "ak-2", "password": "pw", "secret": "s", "n": 1}'
    )
    assert redact_secrets(json_text) == (
        '{"access_token": "***", "refresh_token": "***", "client_secret": "***", '
        '"client-key": "***", "api_key": "***", "apikey": "***", "password": "***", "secret": "***", "n": 1}'
    )
    py_dict = "{'password': 'my-password', 'apikey': 'key-abc'}"
    assert redact_secrets(py_dict) == "{'password': '***', 'apikey': '***'}"


@pytest.mark.parametrize(
    "line, expected",
    [
        # OAuth callbacks and token endpoints: the names _QUERY_SECRET used to miss
        ("GET /cb?access_token=S3CRET&state=x", "GET /cb?access_token=***&state=x"),
        ("GET /cb?code=S3CRET&id_token=S3CRET&refresh_token=S3CRET", "GET /cb?code=***&id_token=***&refresh_token=***"),
        ("https://h/x?client_secret=S3CRET&sig=S3CRET&auth=S3CRET", "https://h/x?client_secret=***&sig=***&auth=***"),
        # Every Authorization scheme, not just Bearer
        ("Authorization: Basic dXNlcjpwYXNzd29yZA==", "Authorization: Basic [redacted]"),
        ("authorization: Token S3CRET-abc", "authorization: Token [redacted]"),
        ("Proxy-Authorization: Digest S3CRET", "Proxy-Authorization: Digest [redacted]"),
        ("{'authorization': 'Basic S3CRET'}", "{'authorization': 'Basic [redacted]'}"),
        ("a basic check", "a basic check"),  # the word in prose is not a credential
        # Cloudflare Access service tokens and session cookie
        ("CF-Access-Client-Id: 0123abcd.access", "CF-Access-Client-Id: ***"),
        ("CF-Access-Client-Secret: 0123456789abcdef0123456789abcdef", "CF-Access-Client-Secret: ***"),
        ("Cookie: CF_Authorization=S3CRET; theme=dark", "Cookie: CF_Authorization=[redacted]; theme=dark"),
        ("Set-Cookie: CF_Authorization=S3CRET; Path=/", "Set-Cookie: CF_Authorization=[redacted]; Path=/"),
        # Dict/headers reprs, as httpx and aiohttp exceptions print them
        (
            "headers={'cf-access-client-secret': 'S3CRET', 'x-api-key': 'S3CRET', 'token': 'S3CRET', 'host': 'h'}",
            "headers={'cf-access-client-secret': '***', 'x-api-key': '***', 'token': '***', 'host': 'h'}",
        ),
        # Device pairing secrets in JSON blobs (webOS client-key, pairing pins, Wi-Fi PSKs)
        (
            '{"pairing_key": "S3CRET", "pin": "1234", "psk": "S3CRET", "code": 200}',
            '{"pairing_key": "***", "pin": "***", "psk": "***", "code": 200}',
        ),
        # .env / shell style; names that aren't secrets stay
        (
            "MQTT_PASSWORD=S3CRET CF_ACCESS_AUD=abc123 MQTT_HOST=broker",
            "MQTT_PASSWORD=*** CF_ACCESS_AUD=abc123 MQTT_HOST=broker",
        ),
        ("HA_TOKEN=S3CRET", "HA_TOKEN=***"),
        ("MQTT_PASSWORD= (unset)", "MQTT_PASSWORD= (unset)"),
        # Header-style lines at the start of a line; a sentence mentioning the word is left alone
        ("x-api-key: S3CRET, next", "x-api-key: ***, next"),
        ("the token: expired at noon", "the token: expired at noon"),
    ],
)
def test_more_secret_shapes(line, expected):
    assert redact_secrets(line) == expected
    assert redact(line) == expected  # the address rules don't disturb these


def test_mask_email():
    assert mask_email("user@example.com") == "u***@example.com"
    assert mask_email("a@b") == "a***@b"
    assert mask_email("not-an-email") == "not-an-email"
    assert mask_email("") == ""


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("denied: user@example.com", "denied: u***@example.com"),
        ("from=first.last+tag@mail.example.org,", "from=f***@mail.example.org,"),
        ("<User_1@Example.COM>", "<U***@Example.COM>"),
        ("send to user@example.com.", "send to u***@example.com."),
        ("user@example.com: not allowed", "u***@example.com: not allowed"),
        ("mailto:user@example.com", "mailto:u***@example.com"),
        ('{"email": "user@example.com"}', '{"email": "u***@example.com"}'),
        ("MCP_ALLOWED_EMAILS=a@example.com,bob@example.com", "MCP_ALLOWED_EMAILS=a***@example.com,b***@example.com"),
        ("https://example.com/users/user@example.com/", "https://example.com/users/u***@example.com/"),
    ],
)
def test_email_addresses_are_masked(line, expected):
    assert redact(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # URL userinfo: a password is a secret (_URL_PASSWORD); a bare user name isn't an address.
        ("mqtt://user:hunter2@broker.example.com:1883", "mqtt://user:***@broker.example.com:1883"),
        ("ssh://git@git.example.com/repo.git", "ssh://git@git.example.com/repo.git"),
        ("https://user@example.com/path", "https://user@example.com/path"),
        # scp-style git remotes.
        ("git@github.com:owner/repo.git", "git@github.com:owner/repo.git"),
        # Decorators in a traceback, and a JWT that happens to sit next to an @.
        ('    @app.route("/x")', '    @app.route("/x")'),
        ("@functools.wraps(fn)", "@functools.wraps(fn)"),
        ("eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl@example.com", "[jwt redacted]@example.com"),
        ("dGVzdA==@example.com", "dGVzdA==@example.com"),
        # Not a domain: no letters-only TLD.
        ("admin@192.0.2.5:22", "admin@x.x.x.5:22"),
        ("user@localhost", "user@localhost"),
    ],
)
def test_email_rule_leaves_look_alikes_alone(line, expected):
    assert redact(line) == expected


@pytest.mark.parametrize("blob", ["a" * 100_000, "a" * 100_000 + "@", "0" * 100_000 + "://", "a." * 50_000 + "@"])
def test_a_long_token_is_redacted_in_linear_time(blob):
    # Logging runs on the event loop: a rule that backtracks over a long hex/base64 blob stalls the
    # server. 100 KB took 20 s with the old unanchored URL-password rule; linear rules take ms.
    start = time.perf_counter()
    redact(f"payload {blob} end")
    assert time.perf_counter() - start < 2.0


def test_email_addresses_show_when_address_redaction_is_off():
    # An address identifies a person; it isn't a credential, so --no-redact shows it like an IP.
    assert redact_secrets("denied: user@example.com") == "denied: user@example.com"


# --- property-based: the rules must be idempotent and leave no address behind ------------------
# hypothesis is optional for the siblings (it isn't in their dev dependencies): without it only
# these tests are skipped, never the module (a module-level importorskip would skip everything).
try:
    import hypothesis
    import hypothesis.strategies as st
except ImportError:  # pragma: no cover
    hypothesis = None  # type: ignore[assignment]

needs_hypothesis = pytest.mark.skipif(hypothesis is None, reason="property tests need hypothesis")
if hypothesis is not None:
    _settings = hypothesis.settings(max_examples=300, deadline=None, suppress_health_check=list(hypothesis.HealthCheck))

    @needs_hypothesis
    @_settings
    @hypothesis.given(st.text(alphabet=st.sampled_from("0123456789abcdefABCDEF:.-x /?&=@eyJ,;+_*"), max_size=40))
    def test_redact_is_idempotent(text):
        """Redacting a redacted line changes nothing: a line can safely pass through two handlers."""
        once = redact(text)
        assert redact(once) == once

    @needs_hypothesis
    @_settings
    @hypothesis.given(st.text(max_size=8), st.ip_addresses(v=4), st.text(max_size=8))
    def test_no_ipv4_survives_redact(pre, ip, post):
        text = str(ip)
        hypothesis.assume(not text.startswith(("127.", "0.")))
        assert text not in redact(f"{pre} {text} {post}")

    @needs_hypothesis
    @_settings
    @hypothesis.given(st.ip_addresses(v=6), st.text(alphabet="abcxyz ,.;", max_size=8))
    def test_no_ipv6_survives_redact(ip, around):
        hypothesis.assume(not (ip.is_loopback or ip.is_unspecified))
        # A dotted tail of 0.x or 127.x ("::ffff:127.0.0.1") is kept on purpose, as the bare 0.x and
        # 127.x addresses are (test_no_ipv4_survives_redact makes the same assumption).
        hypothesis.assume(not str(ip).rsplit(":", 1)[1].startswith(("127.", "0.")))
        assert str(ip) not in redact(f"{around} {ip} {around}")

    @needs_hypothesis
    @_settings
    @hypothesis.given(st.emails(), st.sampled_from([" ", ", ", "<", "(", "=", ": ", '"']))
    def test_no_email_local_part_survives_redact(email, sep):
        local, _, domain = email.rpartition("@")
        hypothesis.assume(len(local) > 1 and "." in domain and domain.rsplit(".", 1)[1].isalpha())
        hypothesis.assume(all(c.isalnum() or c in "._%+-" for c in local))
        once = redact(f"to{sep}{email} ok")
        assert f"{local}@" not in once
        assert redact(once) == once

    @needs_hypothesis
    @_settings
    @hypothesis.given(st.text(alphabet=st.sampled_from("ab.@:/+-_=*xyz.com"), max_size=40))
    def test_email_rule_is_idempotent(text):
        once = redact(text)
        assert redact(once) == once


def test_identifier_patterns(custom_patterns):
    register_identifier_pattern(r"device-[0-9a-f]{4}")
    line = "connected to device-abcd at 192.0.2.10"
    assert redact(line) == "connected to [redacted] at x.x.x.10"
    # Identifier patterns are skipped when address redaction is disabled
    assert redact_secrets(line) == "connected to device-abcd at 192.0.2.10"
    clear_identifier_patterns()
    assert redact(line) == "connected to device-abcd at x.x.x.10"


def test_custom_patterns(custom_patterns):
    register_custom_pattern(re.compile(r'("client-key":\s*")[^"]+'), r"\1***")
    register_custom_pattern(r"\bPIN (\d+)", lambda m: "PIN " + "#" * len(m.group(1)))  # a string compiles
    line = 'register {"client-key": "a1b2c3d4"} PIN 1234 at 192.0.2.20'
    assert redact_secrets(line) == 'register {"client-key": "***"} PIN #### at 192.0.2.20'
    # Custom rules are secrets: the formatter applies them with addresses shown or hidden.
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", (line,), None)
    assert RedactingFormatter("%(message)s", addresses=False).format(record) == (
        'register {"client-key": "***"} PIN #### at 192.0.2.20'
    )
    assert RedactingFormatter("%(message)s").format(record) == 'register {"client-key": "***"} PIN #### at x.x.x.20'


def test_custom_pattern_default_replacement(custom_patterns):
    register_custom_pattern(r"pairing-[0-9a-f]{6}")
    assert redact_secrets("code pairing-c0ffee ok") == "code [redacted] ok"


def test_a_bad_custom_pattern_fails_at_registration(custom_patterns):
    with pytest.raises(re.error):
        register_custom_pattern(r"(unclosed")
    assert redact_secrets("(unclosed") == "(unclosed"


def test_registering_a_rule_twice_applies_it_once(custom_patterns):
    calls = []

    def mask(m):
        calls.append(m.group(0))
        return "***"

    register_custom_pattern(re.compile(r"hunter2"), mask)
    register_custom_pattern(re.compile(r"hunter2"), mask)  # an equal pattern counts as the same rule
    assert redact_secrets("pw hunter2") == "pw ***"
    assert calls == ["hunter2"]


def test_clear_custom_patterns(custom_patterns):
    register_custom_pattern(r"hunter2")
    assert redact_secrets("pw hunter2") == "pw [redacted]"
    clear_custom_patterns()
    assert redact_secrets("pw hunter2") == "pw hunter2"
    assert redact_secrets(f"jwt {JWT}") == "jwt [jwt redacted]"  # the built-in rules stay


@pytest.fixture
def root_logger():
    """Put the root logger back as it was, since setup_logging replaces its handlers."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield root
    root.handlers[:] = handlers
    root.setLevel(level)


def test_setup_logging_redacts_to_stderr(root_logger, capsys):
    setup_logging(logging.INFO)
    assert len(root_logger.handlers) == 1 and root_logger.level == logging.INFO
    assert isinstance(root_logger.handlers[0].formatter, RedactingFormatter)
    logging.getLogger("dev").info("reached %s with %s", "192.0.2.147", f"Bearer {JWT}")
    logging.getLogger("dev").debug("below the level")
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to the MCP stdio transport
    assert err == "INFO dev: reached x.x.x.147 with Bearer [jwt redacted]\n"


def test_setup_logging_unredacted_still_hides_secrets(root_logger, capsys):
    setup_logging(logging.DEBUG, redacted=False, fmt="%(message)s")
    logging.getLogger("dev").debug("reached %s?token=%s", "192.0.2.147", "abc")
    assert capsys.readouterr().err == "reached 192.0.2.147?token=***\n"


def test_setup_logging_replaces_handlers_rather_than_stacking(root_logger, capsys):
    setup_logging(logging.INFO)
    setup_logging(logging.INFO)
    assert len(root_logger.handlers) == 1
    logging.getLogger("dev").info("once")
    assert capsys.readouterr().err == "INFO dev: once\n"


def test_formatter_datefmt():
    fmt = RedactingFormatter("%(asctime)s %(message)s", datefmt="%Y")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "at %s", ("192.0.2.2",), None)
    out = fmt.format(record)
    assert out.endswith(" at x.x.x.2") and len(out.split()[0]) == 4


def test_secrets_are_always_redacted():
    line = f"cf-access-jwt-assertion: {JWT} Authorization: Bearer abcdef0123456789 mqtt://user:hunter2@10.0.0.2"
    assert redact_secrets(line) == (
        "cf-access-jwt-assertion: [jwt redacted] Authorization: Bearer [redacted] mqtt://user:***@10.0.0.2"
    )
    pem = "key:\n-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END PRIVATE KEY-----\nok"
    assert redact_secrets(pem) == "key:\n[pem redacted]\nok"


def test_formatter_without_address_redaction_still_hides_secrets():
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "Denied %s from %s", (JWT, "192.0.2.9"), None)
    assert RedactingFormatter("%(message)s", addresses=False).format(record) == "Denied [jwt redacted] from 192.0.2.9"
    assert RedactingFormatter("%(message)s").format(record) == "Denied [jwt redacted] from x.x.x.9"


def test_tracebacks_are_redacted_too():
    try:
        raise ConnectionError(f"connect to 192.0.2.147 failed, token {JWT}")
    except ConnectionError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = RedactingFormatter("%(message)s").format(record)
    assert "192.0.2.147" not in out and JWT not in out and "x.x.x.147" in out


def test_mask_ip():
    # IPv4 LAN addresses
    assert mask_ip("192.0.2.60") == "x.x.x.60"
    assert mask_ip("192.0.2.20") == "x.x.x.20"
    assert mask_ip("10.0.0.1") == "x.x.x.1"
    assert mask_ip("172.16.0.5") == "x.x.x.5"

    # IPv6 addresses
    assert mask_ip("2001:db8::1") == "x:x::1"
    assert mask_ip("2001:0db8:85a3:0000:0000:8a2e:0370:7334") == "x:x::7334"
    assert mask_ip("fe80::1") == "x:x::1"
    assert mask_ip("fe80::1%eth0") == "x:x::1%eth0"
    # Dotted IPv6 forms embed a whole IPv4 address after the last ":"; it must not come through whole
    assert mask_ip("::ffff:192.0.2.5") == "x:x::x.x.x.5"
    assert mask_ip("64:ff9b::192.0.2.5") == "x:x::x.x.x.5"

    # Loopback and unspecified preserved
    assert mask_ip("127.0.0.1") == "127.0.0.1"
    assert mask_ip("127.0.0.2") == "127.0.0.2"
    assert mask_ip("0.0.0.0") == "0.0.0.0"
    assert mask_ip("::1") == "::1"
    assert mask_ip("::") == "::"

    # Hostnames & non-IPs preserved
    assert mask_ip("localhost") == "localhost"
    assert mask_ip("myhub.local") == "myhub.local"
    assert mask_ip("harmony-hub") == "harmony-hub"
    assert mask_ip("") == ""
    assert mask_ip("x.x.x.60") == "x.x.x.60"  # already masked


def test_mask_host_port():
    assert mask_host_port("192.0.2.100:1883") == "x.x.x.100:1883"
    assert mask_host_port("192.0.2.20:5222") == "x.x.x.20:5222"
    assert mask_host_port("127.0.0.1:8000") == "127.0.0.1:8000"
    assert mask_host_port("localhost:1883") == "localhost:1883"
    assert mask_host_port("[2001:db8::1]:1883") == "[x:x::1]:1883"
    assert mask_host_port("[::1]:8000") == "[::1]:8000"
    assert mask_host_port("192.0.2.100") == "x.x.x.100"
    assert mask_host_port("") == ""


def test_mask_url():
    assert mask_url("http://192.0.2.50:8480/") == "http://x.x.x.50:8480/"
    url_with_query = "http://192.0.2.50:8480/api/v1?device=tv#section"
    assert mask_url(url_with_query) == "http://x.x.x.50:8480/api/v1?device=tv#section"
    assert mask_url("http://user:pass@192.0.2.50:8480/") == "http://user:pass@x.x.x.50:8480/"
    assert mask_url("http://[2001:db8::1]:8480/") == "http://[x:x::1]:8480/"
    assert mask_url("http://localhost:8480/") == "http://localhost:8480/"
    assert mask_url("http://127.0.0.1:8000/healthz") == "http://127.0.0.1:8000/healthz"
    assert mask_url("mqtt://192.0.2.200:1883") == "mqtt://x.x.x.200:1883"
    assert mask_url("") == ""
    assert mask_url("not-a-url") == "not-a-url"
    # A helper for log paths must never raise: urlsplit().port does for these, so the port text is
    # kept as written and only the host is masked.
    assert mask_url("http://192.0.2.5:abc/") == "http://x.x.x.5:abc/"
    assert mask_url("http://192.0.2.5:70000/x") == "http://x.x.x.5:70000/x"
    assert mask_url("http://[::ffff:192.0.2.5]:80/") == "http://[x:x::x.x.x.5]:80/"


# hypothesis is optional for the siblings (it isn't in their dev dependencies): without it only
# this test is skipped, never the module.
try:
    import hypothesis
    import hypothesis.strategies as st
except ImportError:  # pragma: no cover
    hypothesis = None  # type: ignore[assignment]

if hypothesis is not None:

    @hypothesis.settings(max_examples=300, deadline=None, suppress_health_check=list(hypothesis.HealthCheck))
    @hypothesis.given(st.ip_addresses(v=6))
    def test_mask_ip_hides_every_ipv6_but_the_last_group(ip):
        hypothesis.assume(not (ip.is_loopback or ip.is_unspecified))
        masked = mask_ip(str(ip))
        assert masked.startswith("x:x::")
        if ip.ipv4_mapped is not None:  # the "::ffff:a.b.c.d" form
            assert str(ip.ipv4_mapped) not in masked

    @hypothesis.settings(max_examples=300, deadline=None, suppress_health_check=list(hypothesis.HealthCheck))
    @hypothesis.given(st.text(alphabet=st.sampled_from("abc:/[]@?#.0123456789%"), max_size=30))
    def test_mask_url_never_raises(tail):
        mask_url("http://" + tail)
