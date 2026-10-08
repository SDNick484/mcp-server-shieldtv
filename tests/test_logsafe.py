"""Log redaction (logsafe.py is shared with the sibling servers)."""

from __future__ import annotations

import logging

from shieldtv_mcp.logsafe import RedactingFormatter, redact, redact_secrets

JWT = "eyJhbGciOiJSUzI1NiIsImtpZCI6ImFiYyJ9.eyJpc3MiOiJodHRwczovL3RlYW0iLCJlbWFpbCI6Im5AZXhhbXBsZS5jb20ifQ.c2lnbmF0dXJl"


def test_addresses_and_macs():
    assert redact("eISCP -> 192.168.1.147 PWR01") == "eISCP -> x.x.x.147 PWR01"
    assert redact("mac 00:09:B0:F7:6C:FD / 0009B0F76CFD") == "mac xx:xx:xx:xx:6C:FD / xxxxxxxx6CFD"
    assert redact("simulator at 127.0.0.1:60128") == "simulator at 127.0.0.1:60128"  # loopback kept


def test_secrets_are_always_redacted():
    line = f"cf-access-jwt-assertion: {JWT} Authorization: Bearer abcdef0123456789 mqtt://nick:hunter2@10.0.0.2"
    assert redact_secrets(line) == (
        "cf-access-jwt-assertion: [jwt redacted] Authorization: Bearer [redacted] mqtt://nick:***@10.0.0.2"
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
