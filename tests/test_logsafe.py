"""Log redaction.

Shared by the sibling servers along with logsafe.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.
"""

from __future__ import annotations

import logging
import re

import pytest

from shieldtv_mcp import logsafe
from shieldtv_mcp.logsafe import (
    RedactingFormatter,
    clear_custom_patterns,
    clear_identifier_patterns,
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
    assert redact("eISCP -> 192.168.1.147 PWR01") == "eISCP -> x.x.x.147 PWR01"
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
        ("mapped ::ffff:192.168.1.5", "mapped ::ffff:x.x.x.5"),  # the IPv4 rule takes the tail
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
    assert redact("connected to 192.168.1.5.") == "connected to x.x.x.5."
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


def test_identifier_patterns(custom_patterns):
    register_identifier_pattern(r"device-[0-9a-f]{4}")
    line = "connected to device-abcd at 192.168.1.10"
    assert redact(line) == "connected to [redacted] at x.x.x.10"
    # Identifier patterns are skipped when address redaction is disabled
    assert redact_secrets(line) == "connected to device-abcd at 192.168.1.10"
    clear_identifier_patterns()
    assert redact(line) == "connected to device-abcd at x.x.x.10"


def test_custom_patterns(custom_patterns):
    register_custom_pattern(re.compile(r'("client-key":\s*")[^"]+'), r"\1***")
    register_custom_pattern(r"\bPIN (\d+)", lambda m: "PIN " + "#" * len(m.group(1)))  # a string compiles
    line = 'register {"client-key": "a1b2c3d4"} PIN 1234 at 192.168.1.20'
    assert redact_secrets(line) == 'register {"client-key": "***"} PIN #### at 192.168.1.20'
    # Custom rules are secrets: the formatter applies them with addresses shown or hidden.
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "%s", (line,), None)
    assert RedactingFormatter("%(message)s", addresses=False).format(record) == (
        'register {"client-key": "***"} PIN #### at 192.168.1.20'
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
    logging.getLogger("dev").info("reached %s with %s", "192.168.1.147", f"Bearer {JWT}")
    logging.getLogger("dev").debug("below the level")
    out, err = capsys.readouterr()
    assert out == ""  # stdout belongs to the MCP stdio transport
    assert err == "INFO dev: reached x.x.x.147 with Bearer [jwt redacted]\n"


def test_setup_logging_unredacted_still_hides_secrets(root_logger, capsys):
    setup_logging(logging.DEBUG, redacted=False, fmt="%(message)s")
    logging.getLogger("dev").debug("reached %s?token=%s", "192.168.1.147", "abc")
    assert capsys.readouterr().err == "reached 192.168.1.147?token=***\n"


def test_setup_logging_replaces_handlers_rather_than_stacking(root_logger, capsys):
    setup_logging(logging.INFO)
    setup_logging(logging.INFO)
    assert len(root_logger.handlers) == 1
    logging.getLogger("dev").info("once")
    assert capsys.readouterr().err == "INFO dev: once\n"


def test_formatter_datefmt():
    fmt = RedactingFormatter("%(asctime)s %(message)s", datefmt="%Y")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "at %s", ("192.168.1.2",), None)
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
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "Denied %s from %s", (JWT, "192.168.1.9"), None)
    assert RedactingFormatter("%(message)s", addresses=False).format(record) == "Denied [jwt redacted] from 192.168.1.9"
    assert RedactingFormatter("%(message)s").format(record) == "Denied [jwt redacted] from x.x.x.9"


def test_tracebacks_are_redacted_too():
    try:
        raise ConnectionError(f"connect to 192.168.1.147 failed, token {JWT}")
    except ConnectionError:
        import sys

        record = logging.LogRecord("x", logging.ERROR, __file__, 1, "boom", (), sys.exc_info())
    out = RedactingFormatter("%(message)s").format(record)
    assert "192.168.1.147" not in out and JWT not in out and "x.x.x.147" in out
