"""Every protocol detail this server relies on, and whether a real Shield has confirmed it.

Why a registry instead of comments: a comment saying "unverified" is easy to
miss and never goes away. Here each claim has an id that:
  - code cites next to where it depends on it (``ASSUMPTION S-CERT-MAC``),
  - HARDWARE_VALIDATION.md cites in the step that confirms it,
  - the README's verification table lists, with its status,
  - ``mcp-server-shieldtv doctor`` prints.

tests/test_assumptions.py fails if they drift apart.

The owner's first hardware pass (a SHIELD Android TV, remote service 7.00,
2026-10) confirmed a good part of this; those claims are "hardware-verified"
with what was seen in ``note``. The rest are what the simulator
(sim/fake_shield.py) implements and nothing has confirmed.

To record a hardware result, change ``status`` to "hardware-verified" (or
"hardware-contradicted", with what you saw in ``note``) and commit it.

Confidence is about the *claim*, judged from its source:
  high   - seen on hardware, or what androidtvremote2 (used by Home Assistant
           against real Shields) does and depends on
  medium - from another project or Android documentation, not seen here
  low    - our own guess; the simulator implements it but nothing confirms it
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""


ASSUMPTIONS: tuple[Assumption, ...] = (
    # --- the protocol, as androidtvremote2 speaks it ---------------------------------
    Assumption(
        "S-PAIRING",
        "Pairing (TLS, port 6467): pairing_request, options, configuration; the TV shows a 6-hex-digit code; "
        "the client sends a SHA-256 secret over both certificates' RSA numbers and the code; secret_ack means "
        "the Shield now trusts our certificate.",
        "androidtvremote2's pairing.py; the owner paired a Shield with it",
        "high",
        "hardware-verified",
        "`pair` worked on the owner's Shield (remote service 7.00), 2026-10",
    ),
    Assumption(
        "S-REMOTE-HANDSHAKE",
        "Remote session (TLS, port 6466): the Shield sends remote_configure and remote_set_active, then "
        "remote_start (power), volume and the foreground app, then pushes changes and pings.",
        "androidtvremote2's remote.py; get_status worked on the owner's Shield",
        "high",
        "hardware-verified",
        "get_status showed power, app and volume on the owner's Shield, 2026-10",
    ),
    Assumption(
        "S-TLS-REJECT",
        "A certificate the Shield doesn't trust (unpaired on the TV) fails the TLS handshake, which the library "
        "reports as InvalidAuth, not as a connection failure.",
        "androidtvremote2's async_connect maps ssl.SSLError to InvalidAuth",
        "medium",
        note="Never seen: the Shield was not unpaired during the first hardware pass.",
    ),
    Assumption(
        "S-CERT-MAC",
        "The Shield's certificate subject ends in its MAC address (CN=atvremote/<board>/<board>/<model>/<MAC>), "
        "the MAC stays the same when its IP address changes, and reading the certificate (a TLS connect to the "
        "pairing port) shows nothing on the TV.",
        "androidtvremote2's _parse_name_and_mac (its example is a SHIELD); `pair` prints the name and MAC",
        "medium",
        note="`pair` read a name and MAC on the owner's Shield; stability across an address change and "
        "silence on the TV are not confirmed.",
    ),
    Assumption(
        "S-MDNS",
        "The Shield advertises _androidtvremote2._tcp over mDNS with its IPv4 address.",
        "androidtvremote2 and Home Assistant's Android TV Remote integration discover devices this way",
        "high",
        note="Whether `discover` found the owner's Shield isn't recorded (pairing may have used --host).",
    ),
    Assumption(
        "S-RECONNECT",
        "After a dropped session the library reconnects on its own within about 0.1 s, so `available` can "
        "flicker between two looks; drops are counted instead.",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
        "seen after a rejected launch, 2026-10",
    ),
    Assumption(
        "S-SLEEP-CONNECTION",
        "The remote session stays open while the Shield is in standby: SLEEP is reported as standby at once, and "
        "a later key over the same session wakes it.",
        "the owner's Shield reported standby at once and woke on any key",
        "medium",
        note="Consistent with what was seen, but whether the session survives a long standby (minutes, "
        "overnight) is unknown. The simulator also tests the other case (Faults.drop_on_sleep).",
    ),
    # --- seen on the owner's Shield, and relied on ------------------------------------
    Assumption(
        "S-MARKET-REJECT",
        "A bare package name (sent as market://launch?id=<pkg>) is rejected: remote_error, then the Shield drops "
        "the connection; commands sent on the dead connection are silently discarded.",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
        "remote service 7.00, 2026-10; why launch_app confirms the foreground app",
    ),
    Assumption(
        "S-LINK-UNHANDLED",
        "A link no installed app handles is accepted and changes nothing (no error).",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "S-WAKE-ANY-KEY",
        "WAKEUP and SLEEP change the power state, reported at once; any key, not just WAKEUP, wakes it.",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "S-CEC-VOLUME",
        "With volume handled over HDMI-CEC, the Shield reports volume with max 0 (reported as unknown); in "
        "standby it reports its own volume instead (e.g. 1/15).",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
    ),
    Assumption(
        "S-APP-LINKS",
        "The default apps' links open these packages: youtube, youtube-tv, netflix, prime-video, disney+, hulu "
        "(https links), plex (plex://), spotify (spotify:).",
        "each checked on the owner's Shield (config.py)",
        "high",
        "hardware-verified",
        "2026-10; an app's first launch after install can take over 10 s",
    ),
    # --- ADB (optional tools) -----------------------------------------------------------
    Assumption(
        "S-DUMPSYS-MEDIA",
        "`dumpsys media_session` lists sessions with state, position at `updated` (ms of uptime, the clock of "
        "/proc/uptime), and a 'title, subtitle, description' line.",
        "observed on the owner's Shield",
        "high",
        "hardware-verified",
        "YouTube, YouTube Music and YouTube TV, 2026-10",
    ),
    Assumption(
        "S-LIVE-TV-PACKAGES",
        "Live-TV apps report a stream offset as their position: YouTube TV (seen, ~13 h) and Sling "
        "(package com.sling, not seen), so no position is shown for them.",
        "YouTube TV observed; the Sling package name is from its Play Store listing, not checked",
        "medium",
        note="YouTube TV confirmed; Sling unconfirmed (adb shell pm list packages | grep sling would show it).",
    ),
    Assumption(
        "S-STUCK-REMOTES",
        "After a reboot a Bluetooth remote can be HID-connected (dumpsys bluetooth_manager state 2) with no input "
        "device (no dumpsys input entry with bus 0x0005 and its address), so its buttons do nothing.",
        "observed twice on the owner's Shield",
        "high",
        "hardware-verified",
        "a Harmony hub and a Shield remote, 2026-10",
    ),
    Assumption(
        "S-REBOOT-TIME",
        "After ADB's reboot, ADB and the remote service are back within about 40 s (sys.boot_completed=1).",
        "observed on the owner's Shield: 33-38 s",
        "high",
        "hardware-verified",
    ),
)

BY_ID = {a.id: a for a in ASSUMPTIONS}


def unverified() -> list[Assumption]:
    return [a for a in ASSUMPTIONS if a.status != "hardware-verified"]
