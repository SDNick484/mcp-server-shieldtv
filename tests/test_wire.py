"""The real androidtvremote2 library against the simulated Shield, over TLS on localhost.

test_client.py replaces the library with FakeRemote. These tests replace the
*Shield* instead (sim/fake_shield.py), so pairing, the TLS handshake, the
remote session and the library's reconnect loop are the library's own code.
What they show is how the server copes with a Shield that behaves like the
simulator; the simulator's behaviors and their sources are listed in its
docstring, and the unconfirmed ones are S-* assumptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import stat

import pytest

from shieldtv_mcp import cli
from shieldtv_mcp.client import ShieldClient, ShieldError
from shieldtv_mcp.config import App, load_settings
from shieldtv_mcp.sim.fake_shield import LAUNCHER, FakeShield

pytestmark = pytest.mark.anyio


async def eventually(condition, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty config directory: nothing paired yet."""
    monkeypatch.setenv("SHIELDTV_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SHIELDTV_HOST", raising=False)
    monkeypatch.delenv("SHIELDTV_DRY_RUN", raising=False)
    return tmp_path


def client_cert(home):
    def read() -> bytes | None:
        path = home / "cert.pem"
        return path.read_bytes() if path.exists() else None

    return read


@pytest.fixture
async def shield(home):
    s = await FakeShield(client_cert=client_cert(home), ping_interval=1.0).start()
    # Only the ports: the host comes from pairing (or a test writes it)
    (home / "config.json").write_text(json.dumps({"port": s.remote_port, "pairing_port": s.pairing_port}))
    yield s
    await s.stop()


def typed(shield: FakeShield, *codes: str):
    """A read_code for `pair`: types each given code in turn; "TV" means the
    code currently on the simulated TV."""
    pending = list(codes)

    async def read(prompt: str) -> str:
        if not pending:
            raise EOFError
        code = pending.pop(0)
        return shield.code or "" if code == "TV" else code

    return read


def wrong_code(shield: FakeShield, home) -> str:
    """A code whose check byte doesn't match: the library rejects it before
    sending (InvalidAuth), as it would a typo."""
    real = shield.code
    assert real
    cert = (home / "cert.pem").read_bytes()
    for n in range(256):
        tail = bytes([n, n])
        if shield._hash(cert, tail)[0] != int(real[:2], 16):
            return real[:2] + tail.hex().upper()
    raise AssertionError("no wrong code found")


async def pair(home, shield: FakeShield, *codes: str) -> int:
    return await cli._cmd_pair(shield.host, read_code=typed(shield, *codes))


@pytest.fixture
async def paired(home, shield):
    assert await pair(home, shield, "TV") == 0
    return shield


@pytest.fixture
async def connected(paired, monkeypatch):
    monkeypatch.setattr(ShieldClient, "watch_interval", 0.05)
    c = ShieldClient(load_settings())
    await c.start()
    await eventually(lambda: c.available)
    yield c
    await c.stop()


# --- pairing -----------------------------------------------------------------------------
async def test_pairing_saves_host_name_mac_and_private_credentials(home, shield, capsys):
    assert await pair(home, shield, "TV") == 0
    saved = json.loads((home / "config.json").read_text())
    assert (saved["host"], saved["name"], saved["mac"]) == ("127.0.0.1", "SHIELD Android TV", "00:04:4B:A1:B2:C3")
    assert saved["port"] == shield.remote_port  # the rest of the file is kept
    for f in ("cert.pem", "key.pem", "config.json"):
        assert stat.S_IMODE((home / f).stat().st_mode) == 0o600, f
    assert shield.trusted == [(home / "cert.pem").read_bytes()]
    assert "Paired and connected: NVIDIA SHIELD Android TV" in capsys.readouterr().out


async def test_a_mistyped_code_gets_another_try(home, shield, capsys):
    attempts = iter(["wrong", "TV"])

    async def read(prompt: str) -> str:
        return wrong_code(shield, home) if next(attempts) == "wrong" else shield.code or ""

    assert await cli._cmd_pair(shield.host, read_code=read) == 0
    out = capsys.readouterr().out
    assert "that code doesn't match the one on the TV" in out and "a new code will appear" in out


async def test_a_code_the_tv_rejects_gets_another_try(home, shield, capsys):
    shield.faults.reject_secret = True
    first = asyncio.ensure_future(pair(home, shield, "TV", "TV", "TV"))
    await eventually(lambda: shield.code is not None)
    assert await first == 1  # rejected all three times
    out = capsys.readouterr()
    assert "the TV rejected the code" in out.out and "Giving up after 3 attempts" in out.err
    assert shield.trusted == [] and not json.loads((home / "config.json").read_text()).get("host")


async def test_cancel_on_the_tv(home, shield, capsys):
    shield.faults.cancel_pairing = True
    assert await pair(home, shield, "TV", "TV", "TV") == 1
    assert "pairing was cancelled on the TV" in capsys.readouterr().out


async def test_shield_gone_between_attempts_is_a_message_not_a_traceback(home, shield, capsys):
    shield.faults.reject_secret = True

    async def read(prompt: str) -> str:
        code = shield.code or ""
        await shield.stop()  # it goes to sleep while the user types
        return code

    assert await cli._cmd_pair(shield.host, read_code=read) == 1
    assert "Lost the Shield while pairing" in capsys.readouterr().err


async def test_unreachable_shield(home, shield, capsys):
    await shield.stop()
    assert await pair(home, shield, "TV") == 1
    assert "Can't reach 127.0.0.1" in capsys.readouterr().err


async def test_no_terminal(home, shield, capsys):
    assert await pair(home, shield) == 1  # read_code raises EOFError at once
    assert "interactive terminal" in capsys.readouterr().err


async def test_unusable_old_credentials_are_set_aside(home, shield, capsys):
    (home / "cert.pem").write_text("not a certificate")
    (home / "key.pem").write_text("not a key")
    assert await pair(home, shield, "TV") == 0
    assert (home / "cert.pem.broken").read_text() == "not a certificate"
    assert "Set aside the old credentials" in capsys.readouterr().out


# --- the remote session -----------------------------------------------------------------
async def test_connects_and_reads_pushed_state(connected, paired):
    status = connected.snapshot()
    assert (status["reachable"], status["power"], status["current_app_package"]) == (True, "on", LAUNCHER)
    assert status["device"] == {"manufacturer": "NVIDIA", "model": "SHIELD Android TV", "sw_version": "fake-1.0"}
    assert status["volume"] is None  # max 0: volume is behind HDMI-CEC


async def test_keys_power_and_launch(connected, paired):
    connected.send_key("DPAD_DOWN")
    await connected.set_power(False)
    assert connected.is_on is False
    connected.send_key("HOME")  # any key wakes it (seen on the owner's Shield)
    await eventually(lambda: connected.is_on is True)
    assert await connected.launch(App("https://www.netflix.com/title", "com.netflix.ninja")) == "com.netflix.ninja"
    assert paired.keys == ["DPAD_DOWN", "SLEEP", "HOME"]


async def test_a_rejected_link_is_reported_and_the_session_recovers(connected, paired):
    with pytest.raises(ShieldError, match="rejected the launch request"):
        await connected.launch(App("com.netflix.ninja", "com.netflix.ninja"))  # becomes market://
    await eventually(lambda: connected.available)
    assert await connected.launch(App("plex://", "com.plexapp.android")) == "com.plexapp.android"


async def test_a_link_nothing_handles_changes_nothing(connected, monkeypatch):
    monkeypatch.setattr(ShieldClient, "launch_timeout", 0.3)
    with pytest.raises(ShieldError, match="didn't change"):
        await connected.launch(App("https://example.com/nothing", "com.example.nothing"))


async def test_unknown_package_in_front_is_reported_raw(connected, paired):
    paired.set_app("com.example.sideloaded")
    await eventually(lambda: connected.current_app == "com.example.sideloaded")
    status = connected.snapshot()
    assert (status["current_app_package"], status["current_app"]) == ("com.example.sideloaded", None)


# --- drops, sleep, and stale state ---------------------------------------------------------
async def test_a_dropped_session_reconnects_by_itself(connected, paired):
    before = paired.connections
    paired.drop_all()
    await eventually(lambda: connected.drops == 1 and connected.available)
    assert paired.connections == before + 1  # one new session, opened by the library


async def test_a_garbled_frame_drops_and_reconnects(connected, paired):
    paired.faults.garble_next = True
    paired.set_app("com.netflix.ninja")  # this push is the garbled one
    await eventually(lambda: connected.drops == 1 and connected.available)
    # After reconnecting, the state is the Shield's again (the push wasn't lost)
    await eventually(lambda: connected.current_app == "com.netflix.ninja")


async def test_drop_mid_command_sequence(connected, paired):
    paired.faults.drop_after = 3  # the session closes partway through these presses
    for _ in range(3):
        with contextlib.suppress(ShieldError):
            connected.send_key("DPAD_DOWN")
    await eventually(lambda: connected.drops >= 1 and connected.available)
    paired.faults.drop_after = None
    connected.send_key("DPAD_UP")
    await eventually(lambda: "DPAD_UP" in paired.keys)


async def test_sleep_that_drops_the_session(connected, paired):
    # ASSUMPTION S-SLEEP-CONNECTION says the session survives standby; if the
    # real one drops it instead, the server must still converge on "standby".
    paired.faults.drop_on_sleep = True
    await connected.set_power(False)
    await eventually(lambda: connected.drops == 1 and connected.available)
    assert connected.is_on is False
    await connected.set_power(True)


async def test_while_unreachable_status_is_marked_stale(connected, paired):
    await paired.stop()
    await eventually(lambda: not connected.available)
    status = connected.snapshot()
    assert status["stale"] is True and status["as_of"] is not None
    assert (status["power"], status["current_app_package"]) == ("on", LAUNCHER)  # last known, flagged
    with pytest.raises(ShieldError, match="Can't reach the Shield at 127.0.0.1"):
        connected.send_key("HOME")


async def test_accepts_tls_but_never_starts_the_session(home, paired, monkeypatch):
    # Without a bound on each attempt, the library would wait for remote_start forever
    monkeypatch.setattr(ShieldClient, "connect_timeout", 0.3)
    paired.faults.no_start = True
    c = ShieldClient(load_settings())
    await c.start()
    await eventually(lambda: c.error is not None)
    assert "didn't start a remote session" in c.error
    assert c.snapshot()["error"] == c.error
    paired.faults.no_start = False
    await eventually(lambda: c.available, timeout=5)
    await c.stop()


# --- credentials ------------------------------------------------------------------------
async def test_unpaired_on_the_tv_is_auth_failure_not_unreachable(connected, paired):
    paired.forget()
    paired.drop_all()
    await eventually(lambda: connected.auth_failed)  # ASSUMPTION S-TLS-REJECT
    with pytest.raises(ShieldError, match="rejected our pairing"):
        connected.send_key("HOME")
    assert "rejected our certificate" in connected.snapshot()["error"]


async def test_re_pairing_is_picked_up_without_a_restart(home, connected, paired):
    paired.forget()
    paired.drop_all()
    await eventually(lambda: connected.auth_failed)
    (home / "cert.pem").unlink()  # a fresh identity, as after a factory reset
    (home / "key.pem").unlink()
    assert await pair(home, paired, "TV") == 0
    await eventually(lambda: connected.available and not connected.auth_failed)
    connected.send_key("HOME")


async def test_corrupt_credentials_are_reported_not_retried(home, paired, monkeypatch):
    monkeypatch.setattr(ShieldClient, "watch_interval", 0.05)
    (home / "key.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\ngarbage\n-----END RSA PRIVATE KEY-----\n")
    before = paired.connections
    c = ShieldClient(load_settings())
    await c.start()
    assert c.cert_error and "can't be used" in c.cert_error
    with pytest.raises(ShieldError, match="Re-run `mcp-server-shieldtv pair`"):
        c.send_key("HOME")
    assert c.snapshot()["error"] == c.cert_error
    await asyncio.sleep(0.2)
    assert paired.connections == before  # no hopeless connection attempts
    # Re-pairing replaces the files; the running client picks them up
    assert await pair(home, paired, "TV") == 0
    await eventually(lambda: c.available)
    await c.stop()


# --- a new address ------------------------------------------------------------------------
@pytest.fixture
def two_addresses():
    """127.0.0.2 and .3: two "LAN addresses" on loopback (Linux routes all of 127/8)."""
    import socket

    for ip in ("127.0.0.2", "127.0.0.3"):
        with socket.socket() as s:
            try:
                s.bind((ip, 0))
            except OSError:
                pytest.skip(f"can't bind {ip} here (macOS needs an alias)")
    return "127.0.0.2", "127.0.0.3"


async def moved_shield(home, two_addresses, monkeypatch, *, same_device: bool):
    old_ip, new_ip = two_addresses
    first = await FakeShield(host=old_ip, client_cert=client_cert(home)).start()
    (home / "config.json").write_text(json.dumps({"port": first.remote_port, "pairing_port": first.pairing_port}))
    assert await pair(home, first, "TV") == 0
    monkeypatch.setattr(ShieldClient, "watch_interval", 0.05)
    monkeypatch.setattr(ShieldClient, "rediscover_after", 0.2)

    async def finder(timeout: float) -> dict[str, str]:
        return {"SHIELD": new_ip}  # what mDNS would answer

    c = ShieldClient(load_settings(), finder=finder)
    await c.start()
    await eventually(lambda: c.available)
    # DHCP hands it another address: same ports, same device (or not)
    await first.stop()
    second = FakeShield(
        host=new_ip,
        mac=first.mac if same_device else "00:04:4B:FF:FF:FF",
        remote_port=first.remote_port,
        pairing_port=first.pairing_port,
    )
    second.trusted = list(first.trusted)
    await second.start()
    return c, second, new_ip


async def test_a_moved_shield_is_found_by_its_mac(home, two_addresses, monkeypatch):
    c, second, new_ip = await moved_shield(home, two_addresses, monkeypatch, same_device=True)
    try:
        await eventually(lambda: c.available and c.settings.host == new_ip)
        assert json.loads((home / "config.json").read_text())["host"] == new_ip  # saved for next time
        assert c.moved_from == "127.0.0.2"
        c.send_key("HOME")
        await eventually(lambda: second.keys == ["HOME"])
    finally:
        await c.stop()
        await second.stop()


async def test_a_different_device_at_the_new_address_is_not_adopted(home, two_addresses, monkeypatch):
    c, second, _ = await moved_shield(home, two_addresses, monkeypatch, same_device=False)
    try:
        await asyncio.sleep(0.6)  # several rediscovery rounds
        assert not c.available and c.settings.host == "127.0.0.2"  # never guess a device
        assert json.loads((home / "config.json").read_text())["host"] == "127.0.0.2"
    finally:
        await c.stop()
        await second.stop()
