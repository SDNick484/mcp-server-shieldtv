"""`mcp-server-shieldtv doctor`: check everything between this machine and the Shield, layer by layer.

"Can't reach the Shield" has a handful of causes, and the server can only say
which one it saw last. doctor checks each layer in order, and the first
failure says what it means, what to try, and which HARDWARE_VALIDATION.md step
covers it (STEP below):

  config    - config.json parses; the pairing files exist, load, and are private (0600)
  tcp       - the Shield accepts TCP on the remote port (6466) and the pairing port (6467)
  identity  - the pairing port's certificate names the Shield and the MAC `pair` saved
              (a different MAC means the address now belongs to another device) (S-CERT-MAC)
  session   - a remote session starts with our certificate: power, app, volume (S-REMOTE-HANDSHAKE)
  adb       - (if enabled) the read-only now-playing command runs (S-DUMPSYS-MEDIA)
  mdns      - (once) which Android TV devices answer mDNS, and whether the Shield is one (S-MDNS)

Nothing here changes anything: no keys, no launches, no pairing. Opening a
session is what the server does all day; reading the pairing port's
certificate doesn't start pairing (ASSUMPTION S-CERT-MAC says it shows
nothing on the TV; step 2 checks that).
"""

from __future__ import annotations

import asyncio
import json
import platform
import stat
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from androidtvremote2 import AndroidTVRemote, CannotConnect, ConnectionClosed, InvalidAuth

from .assumptions import unverified
from .client import check_credentials
from .config import CLIENT_NAME, Settings, config_dir
from .logsafe import redact

# HARDWARE_VALIDATION.md step that covers each layer, for the hints.
STEP = {"config": 1, "tcp": 2, "identity": 2, "session": 2, "adb": 6, "mdns": 3}


@dataclass
class Check:
    layer: str
    ok: bool
    detail: str
    hint: str = ""


@dataclass
class Report:
    versions: dict[str, str]
    host: str | None
    dry_run: bool
    checks: list[Check] = field(default_factory=list)
    mdns: list[str] = field(default_factory=list)
    unverified_assumptions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def versions() -> dict[str, str]:
    out = {"python": platform.python_version()}
    for pkg in ("mcp-server-shieldtv", "mcp", "androidtvremote2", "zeroconf", "adb-shell"):
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = "not installed"
    return out


def _hint(layer: str, text: str) -> str:
    return f"{text} (HARDWARE_VALIDATION.md step {STEP[layer]})"


def _config_checks(s: Settings) -> list[Check]:
    checks: list[Check] = []
    if s.problems:
        checks.append(Check("config", False, "; ".join(s.problems), _hint("config", "Fix config.json")))
    if not s.host:
        checks.append(Check("config", False, "no Shield address", _hint("config", "Run `mcp-server-shieldtv pair`")))
        return checks
    if not s.paired:
        checks.append(
            Check("config", False, f"not paired with {s.host}", _hint("config", "Run `mcp-server-shieldtv pair`"))
        )
        return checks
    problem = check_credentials(s)
    if problem:
        checks.append(Check("config", False, problem, _hint("config", "Re-run `mcp-server-shieldtv pair`")))
        return checks
    loose = [
        p.name
        for p in (s.cert_path, s.key_path, config_dir() / "config.json")
        if p.exists() and stat.S_IMODE(p.stat().st_mode) & 0o077
    ]
    if loose:
        checks.append(
            Check(
                "config",
                False,
                f"readable by other users: {', '.join(loose)}",
                f"chmod 600 {' '.join(loose)} in {config_dir()}: the certificate and key control the Shield",
            )
        )
        return checks
    who = f"{s.name} (MAC {s.mac})" if s.mac else "a Shield (no name/MAC saved: re-pair to enable rediscovery)"
    checks.append(Check("config", True, f"paired with {who} at {s.host}; credentials load and are private"))
    return checks


async def _tcp(host: str, port: int, timeout: float) -> str | None:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, TimeoutError) as exc:
        return str(exc) or type(exc).__name__
    writer.close()
    return None


RemoteFactory = Callable[..., Any]
Finder = Callable[[float], Awaitable[dict[str, str]]]


async def _mdns(timeout: float) -> dict[str, str]:
    from .discovery import discover

    return await discover(timeout)


async def run_doctor(
    settings: Settings,
    timeout: float = 5.0,
    remote_factory: RemoteFactory = AndroidTVRemote,
    finder: Finder | None = _mdns,
) -> Report:
    s = settings
    report = Report(
        versions=versions(),
        host=s.host,
        dry_run=s.dry_run,
        unverified_assumptions=[f"{a.id} ({a.confidence}): {a.claim}" for a in unverified()],
    )
    report.checks += _config_checks(s)
    if finder is None:
        report.mdns = ["skipped"]
    else:
        try:
            found = await finder(min(timeout, 3.0))
            report.mdns = [f"{name} at {ip}" + (" (this Shield)" if ip == s.host else "") for name, ip in found.items()]
        except Exception as exc:  # zeroconf fails in OS-specific ways; it's only informative
            report.mdns = [f"mDNS search failed: {exc}"]
    if not report.ok:
        return report
    assert s.host is not None

    # TCP: is anything listening on either port?
    for port, what in ((s.port, "remote"), (s.pairing_port, "pairing")):
        err = await _tcp(s.host, port, timeout)
        if err:
            report.checks.append(
                Check(
                    "tcp",
                    False,
                    f"no TCP connection to {s.host}:{port} ({what} port): {err}",
                    _hint(
                        "tcp",
                        "Is the Shield on and on this network? A firewall, guest network or IoT VLAN in between "
                        "shows up this way; so does an address that changed (DHCP)",
                    ),
                )
            )
            return report
    report.checks.append(Check("tcp", True, f"ports {s.port} and {s.pairing_port} accept connections"))

    remote = remote_factory(
        CLIENT_NAME, str(s.cert_path), str(s.key_path), s.host, api_port=s.port, pair_port=s.pairing_port
    )
    # Identity: the certificate on the pairing port names the device.
    try:
        name, mac = await asyncio.wait_for(remote.async_get_name_and_mac(), timeout)
    except (TimeoutError, CannotConnect, OSError) as exc:
        report.checks.append(Check("identity", False, f"couldn't read its certificate: {exc}", ""))
        return report
    if s.mac and mac.lower() != s.mac.lower():
        report.checks.append(
            Check(
                "identity",
                False,
                f"{s.host} is {name} (MAC {mac}), not the paired {s.name} (MAC {s.mac})",
                _hint("identity", "The address now belongs to another device. Re-pair with the Shield's new address"),
            )
        )
        return report
    report.checks.append(Check("identity", True, f"{name}, MAC {mac}"))

    # Session: our certificate is accepted and the Shield reports its state.
    t0 = time.perf_counter()
    try:
        await asyncio.wait_for(remote.async_connect(), 3 * timeout)
    except InvalidAuth:
        report.checks.append(
            Check(
                "session",
                False,
                "the Shield rejected our certificate (unpaired on the TV, or the Shield was reset)",
                _hint("session", "Re-run `mcp-server-shieldtv pair`"),
            )
        )
        return report
    except (TimeoutError, CannotConnect, ConnectionClosed, OSError) as exc:
        detail = "no remote session started" if isinstance(exc, TimeoutError) else str(exc)
        report.checks.append(Check("session", False, detail, _hint("session", "Restart the Shield and try again")))
        return report
    try:
        took = 1000 * (time.perf_counter() - t0)
        info = remote.device_info or {}
        vol = remote.volume_info
        volume = f"{vol['level']}/{vol['max']}" if vol and vol["max"] else "behind HDMI-CEC (not reported)"
        report.checks.append(
            Check(
                "session",
                True,
                f"{info.get('manufacturer', '?')} {info.get('model', '?')} (remote service "
                f"{info.get('sw_version', '?')}); {'on' if remote.is_on else 'standby'}, "
                f"{remote.current_app or 'no app'} in front, volume {volume}; started in {took:.0f} ms",
            )
        )
    finally:
        remote.disconnect()

    if s.adb:
        from .adb import read_now_playing
        from .client import ShieldError

        try:
            playing = await read_now_playing(s)
            report.checks.append(
                Check("adb", True, f"now playing: {playing['title'] or 'nothing'} ({playing['state']})")
            )
        except ShieldError as exc:
            report.checks.append(Check("adb", False, str(exc), _hint("adb", "Re-run `mcp-server-shieldtv adb-setup`")))
    return report


def render(report: Report, redacted: bool = True) -> str:
    lines = ["mcp-server-shieldtv doctor", ""]
    lines += [f"  {k}: {v}" for k, v in report.versions.items()]
    if report.dry_run:
        lines += ["", "  note: dry run is on (SHIELDTV_DRY_RUN): actions are reported, not sent"]
    lines += ["", f"Shield {report.host or '(not configured)'}"]
    for c in report.checks:
        lines.append(f"   {'ok' if c.ok else 'x '} {c.layer:<9} {c.detail}")
        if c.hint:
            lines.append(f"      -> {c.hint}")
    lines += ["", "mDNS (_androidtvremote2._tcp)"]
    lines += [f"   {m}" for m in report.mdns] or [
        f"   nothing answered: multicast may be filtered here (WSL2 NAT, VLANs). Pairing by address still "
        f"works. (HARDWARE_VALIDATION.md step {STEP['mdns']})"
    ]
    lines += ["", f"Not yet confirmed on hardware ({len(report.unverified_assumptions)}):"]
    lines += [f"   {u}" for u in report.unverified_assumptions]
    lines += ["", "OK" if report.ok else "PROBLEMS FOUND"]
    text = "\n".join(lines)
    return redact(text) if redacted else text


def to_json(report: Report, redacted: bool = True) -> str:
    text = json.dumps({**asdict(report), "ok": report.ok}, indent=2)
    return redact(text) if redacted else text
