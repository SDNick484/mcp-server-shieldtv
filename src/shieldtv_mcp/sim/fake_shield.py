"""A simulated Shield that speaks Android TV Remote protocol v2 on the wire.

The unit tests' ``FakeRemote`` replaces the *library*. This replaces the
*Shield*: it listens on TCP (TLS) like the real one, so the real
``androidtvremote2`` code runs against it: the TLS handshake with our client
certificate, pairing (polo, port 6467), the remote session (port 6466), the
library's reconnect loop. What it can show is that the server copes with
what the library does when a Shield behaves like this; not that a Shield
behaves like this. Every behavior below names its source:

  from the library's client code (it works against real Shields, so the
  server side must match it; ASSUMPTION S-REMOTE-HANDSHAKE, S-PAIRING):
    - remote session: the Shield sends remote_configure, the client answers;
      remote_set_active, the client answers; then remote_start (power),
      remote_set_volume_level and remote_ime_key_inject (foreground app).
      Pings every few seconds; the client drops a connection idle for 16s.
    - pairing: pairing_request -> pairing_request_ack, options -> options,
      configuration -> configuration_ack (the TV shows a code), secret ->
      secret_ack. The code is 6 hex digits; its first byte is the first byte
      of SHA-256 over both certificates' RSA numbers and the code's last two
      bytes, so the client can check a typo before sending it.
    - the Shield's certificate subject: CN=atvremote/<board>/<board>/<model>/<MAC>
      (the library's own example), which is where `pair` reads the name and MAC.
  seen on the owner's Shield (CLAUDE.md):
    - a market:// link (what a bare package name becomes) gets remote_error,
      then the connection is dropped;
    - a link no app handles is accepted and changes nothing;
    - SLEEP reports standby at once, and any key wakes it.
  from the library's error handling: an unpaired certificate fails the TLS
  handshake, which it reports as InvalidAuth ("pair again"). ASSUMPTION
  S-TLS-REJECT.
  invented here, as switches (``Faults``) for tests:
    - an app link while in standby opens nothing (what a real one does is unknown);
    - never sending remote_start, dropping after N messages, garbling a frame,
      refusing connections, dropping the session when going to sleep
      (ASSUMPTION S-SLEEP-CONNECTION says the real one keeps it).

One limit of Python's ssl module: a TLS server can't accept an *unknown*
client certificate (it can only verify against certificates it trusts). A
real Shield accepts any certificate on the pairing port and reads it from
the handshake. The fake instead trusts the certificate ``client_cert()``
returns, normally the cert.pem in the config directory being paired. It
changes nothing the client does on the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
import ssl
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from androidtvremote2.polo_pb2 import OuterMessage
from androidtvremote2.remotemessage_pb2 import RemoteKeyCode, RemoteMessage
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from google.protobuf.internal.decoder import _DecodeVarint
from google.protobuf.internal.encoder import _VarintBytes

log = logging.getLogger("shieldtv_mcp.sim")

LAUNCHER = "com.google.android.tvlauncher"
# Features the fake offers in remote_configure (the library's Feature flags):
# PING 1, KEY 2, IME 4, POWER 32, VOLUME 64, APP_LINK 512.
FEATURES = 1 | 2 | 4 | 32 | 64 | 512


@dataclass
class Faults:
    """Misbehavior on purpose. All off by default."""

    refuse: bool = False  # close every connection before TLS
    no_start: bool = False  # complete TLS and configure, but never send remote_start
    drop_after: int | None = None  # close the session after this many client messages
    garble_next: bool = False  # send one corrupt frame (a bad length varint), once
    drop_on_sleep: bool = False  # SLEEP closes the session (the real one doesn't seem to)
    reject_secret: bool = False  # the TV says the pairing code was wrong (STATUS_BAD_SECRET)
    cancel_pairing: bool = False  # someone presses Cancel on the TV: the pairing connection closes


@dataclass
class FakeShield:
    host: str = "127.0.0.1"
    name: str = "SHIELD Android TV"
    mac: str = "00:04:4B:A1:B2:C3"  # NVIDIA's OUI; the rest invented
    board: str = "darcy"
    vendor: str = "NVIDIA"
    app_version: str = "fake-1.0"
    remote_port: int = 0  # 0: a free port
    pairing_port: int = 0
    ping_interval: float = 5.0
    # Links the "installed apps" handle: link -> package that comes to the front.
    handlers: dict[str, str] = field(
        default_factory=lambda: {
            "https://www.youtube.com": "com.google.android.youtube.tv",
            "https://www.netflix.com/title": "com.netflix.ninja",
            "plex://": "com.plexapp.android",
        }
    )
    client_cert: Callable[[], bytes | None] = lambda: None  # see the module docstring
    on_code: Callable[[str], None] | None = None  # called with the code the "TV" shows
    is_on: bool = True
    current_app: str = LAUNCHER
    volume: tuple[int, int, bool] = (0, 0, False)  # level, max, muted; max 0 = behind HDMI-CEC
    faults: Faults = field(default_factory=Faults)

    def __post_init__(self) -> None:
        self.trusted: list[bytes] = []  # PEM certificates of paired clients
        self.code: str | None = None  # the code "on the TV" during pairing
        self.keys: list[str] = []
        self.launched: list[str] = []
        self.sessions: list[_Session] = []
        self.connections = 0  # remote sessions opened, ever
        self._servers: list[asyncio.Server] = []
        self._writers: set[asyncio.StreamWriter] = set()  # every open connection, pairing ones too
        self._tmp = tempfile.TemporaryDirectory(prefix="fake-shield-")
        self._cert_path, self._key_path = self._make_server_cert()

    # --- identity ---------------------------------------------------------------------
    def _make_server_cert(self) -> tuple[str, str]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, f"atvremote/{self.board}/{self.board}/{self.name}/{self.mac}")]
        )
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        d = Path(self._tmp.name)
        (d / "server.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (d / "server.key").write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
            )
        )
        self.server_cert = cert
        return str(d / "server.pem"), str(d / "server.key")

    def _tls(self, trust: list[bytes]) -> ssl.SSLContext:
        """A server context that requires a client certificate from `trust`.
        Built per connection, so pairing a client takes effect at once. With
        nothing trusted, it trusts only its own certificate, which no client
        has, so every handshake fails as an unpaired one does."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self._cert_path, self._key_path)
        ctx.verify_mode = ssl.CERT_REQUIRED
        own = self.server_cert.public_bytes(serialization.Encoding.PEM)
        ctx.load_verify_locations(cadata="\n".join(p.decode() for p in (trust or [own])))
        return ctx

    def pair_with(self, cert_pem: bytes) -> None:
        """Trust a client certificate as if it had paired.

        A new certificate with the same subject as a trusted one replaces it.
        That's a choice the fake has to make (OpenSSL looks trusted
        certificates up by subject, so two with one subject can't both be
        trusted); what a real Shield does is unknown. It only matters when
        `pair` had to make a new certificate (the old one was unusable), and
        then the old one is gone anyway."""
        subject = x509.load_pem_x509_certificate(cert_pem).subject
        self.trusted = [p for p in self.trusted if x509.load_pem_x509_certificate(p).subject != subject]
        self.trusted.append(cert_pem)

    def forget(self) -> None:
        """Unpair every client (Settings > Remotes & accessories on a real one)."""
        self.trusted.clear()

    # --- lifecycle --------------------------------------------------------------------
    async def start(self) -> FakeShield:
        remote = await asyncio.start_server(self._remote_conn, self.host, self.remote_port)
        pairing = await asyncio.start_server(self._pairing_conn, self.host, self.pairing_port)
        self._servers = [remote, pairing]
        self.remote_port = remote.sockets[0].getsockname()[1]
        self.pairing_port = pairing.sockets[0].getsockname()[1]
        log.info("Fake Shield on %s (remote %s, pairing %s)", self.host, self.remote_port, self.pairing_port)
        return self

    async def stop(self) -> None:
        """Stop listening and drop every connection, as a Shield leaving the network would.

        The connections are closed by hand: since Python 3.12, Server.wait_closed()
        waits for every connection to end, and a client in the middle of pairing
        (waiting for someone to type the code) would never end its own."""
        for s in self._servers:
            s.close()
        for w in list(self._writers):
            w.close()
        for session in list(self.sessions):
            session.close()
        for s in self._servers:
            with contextlib.suppress(Exception):
                await s.wait_closed()
        self._servers = []

    def drop_all(self) -> None:
        """Close every open session (Wi-Fi blip, Shield restarting its remote service)."""
        for session in list(self.sessions):
            session.close()

    # --- state changes, pushed to every session ----------------------------------------
    def set_power(self, on: bool) -> None:
        self.is_on = on
        self._broadcast(lambda m: setattr(m.remote_start, "started", on))
        if not on and self.faults.drop_on_sleep:
            self.drop_all()

    def set_app(self, package: str) -> None:
        self.current_app = package
        self._broadcast(lambda m: setattr(m.remote_ime_key_inject.app_info, "app_package", package))

    def set_volume(self, level: int, maximum: int, muted: bool = False) -> None:
        self.volume = (level, maximum, muted)
        self._broadcast(self._volume_message)

    def _volume_message(self, m: RemoteMessage) -> None:
        level, maximum, muted = self.volume
        m.remote_set_volume_level.volume_level = level
        m.remote_set_volume_level.volume_max = maximum
        m.remote_set_volume_level.volume_muted = muted

    def _broadcast(self, fill: Callable[[RemoteMessage], None]) -> None:
        for session in self.sessions:
            if session.started:
                msg = RemoteMessage()
                fill(msg)
                session.send(msg)

    # --- the remote session (6466) ------------------------------------------------------
    async def _remote_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._track(writer)
        if self.faults.refuse:
            writer.close()
            return
        tls = _TlsStream(reader, writer, self._tls(self.trusted))
        try:
            await tls.handshake()
        except (ssl.SSLError, OSError, asyncio.IncompleteReadError) as exc:
            log.debug("remote: TLS handshake failed (%s)", exc)
            writer.close()
            return
        session = _Session(self, tls)
        self.sessions.append(session)
        self.connections += 1
        try:
            await session.run()
        finally:
            self.sessions.remove(session)
            session.close()

    def handle_key(self, key: str) -> None:
        self.keys.append(key)
        if key == "SLEEP":
            self.set_power(False)
        elif not self.is_on or key == "WAKEUP":
            self.set_power(True)  # any key wakes it (seen on the owner's Shield)
        if key == "HOME":
            self.set_app(LAUNCHER)
        elif key in ("VOLUME_UP", "VOLUME_DOWN", "VOLUME_MUTE") and self.volume[1] > 0:
            level, maximum, muted = self.volume
            if key == "VOLUME_MUTE":
                muted = not muted
            else:
                level = max(0, min(maximum, level + (1 if key == "VOLUME_UP" else -1)))
            self.set_volume(level, maximum, muted)

    def handle_link(self, session: _Session, link: str) -> None:
        self.launched.append(link)
        if link.startswith("market://"):
            # Seen on the owner's Shield: an error, then the connection drops
            err = RemoteMessage()
            err.remote_error.value = True
            session.send(err)
            asyncio.get_running_loop().call_later(0.05, session.close)
            return
        package = self.handlers.get(link)
        if package and not self.is_on:
            # Invented: what a sleeping Shield does with an app link is unknown.
            # The fake ignores it, which is the case the server must explain.
            return
        if package:
            self.set_app(package)
        # else: accepted, nothing opens (seen on the owner's Shield)

    # --- pairing (6467) ---------------------------------------------------------------
    def _track(self, writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        task = asyncio.current_task()
        if task is not None:
            task.add_done_callback(lambda _: self._writers.discard(writer))

    async def _pairing_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._track(writer)
        cert = self.client_cert()
        tls = _TlsStream(reader, writer, self._tls([cert] if cert else []))
        try:
            await tls.handshake()
        except (ssl.SSLError, OSError, asyncio.IncompleteReadError) as exc:
            log.debug("pairing: TLS handshake failed (%s)", exc)
            writer.close()
            return
        try:
            while True:
                msg = OuterMessage()
                msg.ParseFromString(await _read_frame(tls))
                reply = OuterMessage(protocol_version=2, status=OuterMessage.Status.STATUS_OK)
                if msg.HasField("pairing_request"):
                    reply.pairing_request_ack.server_name = self.name
                elif msg.HasField("options"):
                    reply.options.CopyFrom(msg.options)
                elif msg.HasField("configuration"):
                    reply.configuration_ack.SetInParent()
                    self.code = self._new_code(cert)
                    log.info("Pairing code on the 'TV': %s", self.code)
                    if self.on_code:
                        self.on_code(self.code)
                    if self.faults.cancel_pairing:
                        tls.close()
                        return
                elif msg.HasField("secret"):
                    if self.faults.reject_secret or cert is None or msg.secret.secret != self._expected_secret(cert):
                        reply.status = OuterMessage.Status.STATUS_BAD_SECRET
                        _write_frame(tls, reply)
                        await tls.drain()
                        tls.close()
                        return
                    reply.secret_ack.secret = msg.secret.secret
                    self.pair_with(cert)
                    self.code = None
                else:
                    reply.status = OuterMessage.Status.STATUS_ERROR
                _write_frame(tls, reply)
                await tls.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    def _hash(self, client_pem: bytes, tail: bytes) -> bytes:
        client_key = x509.load_pem_x509_certificate(client_pem).public_key()
        server_key = self.server_cert.public_key()
        assert isinstance(client_key, rsa.RSAPublicKey) and isinstance(server_key, rsa.RSAPublicKey)
        client, server = client_key.public_numbers(), server_key.public_numbers()
        h = hashlib.sha256()
        # Exactly as the library hashes them: moduli as is, exponents with a
        # leading 0 nibble (65537 -> "010001")
        h.update(bytes.fromhex(f"{client.n:X}"))
        h.update(bytes.fromhex(f"0{client.e:X}"))
        h.update(bytes.fromhex(f"{server.n:X}"))
        h.update(bytes.fromhex(f"0{server.e:X}"))
        h.update(tail)
        return h.digest()

    def _new_code(self, cert: bytes | None) -> str:
        tail = secrets.token_bytes(2)
        if cert is None:
            return "000000"
        return f"{self._hash(cert, tail)[0]:02X}{tail.hex().upper()}"

    def _expected_secret(self, cert: bytes) -> bytes | None:
        if self.code is None:
            return None
        return self._hash(cert, bytes.fromhex(self.code[2:]))


class _Session:
    """One remote-protocol connection."""

    def __init__(self, shield: FakeShield, tls: _TlsStream) -> None:
        self.shield = shield
        self.tls = tls
        self.started = False
        self.received: list[str] = []
        self._closed = False

    def send(self, msg: RemoteMessage) -> None:
        if self._closed:
            return
        if self.shield.faults.garble_next:
            self.shield.faults.garble_next = False
            self.tls.write(b"\xff" * 11)  # a varint that never ends: the client must resync or drop
            return
        _write_frame(self.tls, msg)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self.tls.close()

    async def run(self) -> None:
        s = self.shield
        configure = RemoteMessage()
        configure.remote_configure.code1 = FEATURES
        info = configure.remote_configure.device_info
        info.model, info.vendor, info.app_version = s.name, s.vendor, s.app_version
        self.send(configure)
        pinger = asyncio.create_task(self._ping())
        try:
            count = 0
            while not self._closed:
                msg = RemoteMessage()
                msg.ParseFromString(await _read_frame(self.tls))
                count += 1
                fields = msg.ListFields()  # RemoteMessage has no oneof: one field is set
                self.received.append(fields[0][0].name if fields else "?")
                if s.faults.drop_after is not None and count > s.faults.drop_after:
                    self.close()
                    return
                self._handle(msg)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, ValueError):
            pass
        finally:
            pinger.cancel()

    def _handle(self, msg: RemoteMessage) -> None:
        s = self.shield
        if msg.HasField("remote_configure"):
            active = RemoteMessage()
            active.remote_set_active.SetInParent()
            self.send(active)
        elif msg.HasField("remote_set_active"):
            if s.faults.no_start:
                return
            self.started = True
            start = RemoteMessage()
            start.remote_start.started = s.is_on
            self.send(start)
            volume = RemoteMessage()
            s._volume_message(volume)
            self.send(volume)
            app = RemoteMessage()
            app.remote_ime_key_inject.app_info.app_package = s.current_app
            self.send(app)
        elif msg.HasField("remote_key_inject"):
            s.handle_key(RemoteKeyCode.Name(msg.remote_key_inject.key_code).removeprefix("KEYCODE_"))
        elif msg.HasField("remote_app_link_launch_request"):
            s.handle_link(self, msg.remote_app_link_launch_request.app_link)

    async def _ping(self) -> None:
        n = 0
        while not self._closed:
            await asyncio.sleep(self.shield.ping_interval)
            n += 1
            ping = RemoteMessage()
            ping.remote_ping_request.val1 = n
            self.send(ping)


class _TlsStream:
    """TLS over an asyncio stream, done by hand with ssl.MemoryBIO.

    Why not asyncio's own TLS (start_server(ssl=...) or start_tls)? When it
    rejects a client certificate it drops the connection without sending the
    TLS alert, so the client sees a reset ("can't connect") instead of a
    rejected certificate ("pair again"). Driving the handshake ourselves lets
    the alert reach the client, as it does from any ordinary TLS server.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, ctx: ssl.SSLContext) -> None:
        self.reader, self.writer = reader, writer
        self._in, self._out = ssl.MemoryBIO(), ssl.MemoryBIO()
        self.obj = ctx.wrap_bio(self._in, self._out, server_side=True)
        self._buf = bytearray()

    def _flush(self) -> None:
        data = self._out.read()
        if data and not self.writer.is_closing():
            self.writer.write(data)

    async def _fill(self) -> None:
        data = await self.reader.read(65536)
        if not data:
            raise asyncio.IncompleteReadError(b"", None)
        self._in.write(data)

    async def handshake(self) -> None:
        while True:
            try:
                self.obj.do_handshake()
                self._flush()
                return
            except ssl.SSLWantReadError:
                self._flush()
                await self.writer.drain()
                await self._fill()
            except ssl.SSLError:
                self._flush()  # the alert
                with contextlib.suppress(ConnectionError):
                    await self.writer.drain()
                raise

    async def readexactly(self, n: int) -> bytes:
        while len(self._buf) < n:
            try:
                chunk = self.obj.read(65536)
            except ssl.SSLWantReadError:
                self._flush()
                await self._fill()
                continue
            except ssl.SSLZeroReturnError:
                chunk = b""
            if not chunk:
                raise asyncio.IncompleteReadError(bytes(self._buf), n)
            self._buf += chunk
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def write(self, data: bytes) -> None:
        self.obj.write(data)
        self._flush()

    async def drain(self) -> None:
        await self.writer.drain()

    def close(self) -> None:
        self.writer.close()


# --- framing: a varint length, then the protobuf (as in androidtvremote2/base.py) -------
async def _read_frame(reader: _TlsStream) -> bytes:
    raw = b""
    while True:
        raw += await reader.readexactly(1)
        if not raw[-1] & 0x80:
            break
        if len(raw) > 10:
            raise ValueError("bad length varint")
    length, _ = _DecodeVarint(raw, 0)
    if length > 1 << 20:
        raise ValueError("frame too large")
    return await reader.readexactly(length)


def _write_frame(writer: _TlsStream, msg: Any) -> None:
    data = msg.SerializeToString()
    writer.write(_VarintBytes(len(data)) + data)
