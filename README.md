# mcp-server-shieldtv

[![CI](https://github.com/SDNick484/mcp-server-shieldtv/actions/workflows/ci.yml/badge.svg)](https://github.com/SDNick484/mcp-server-shieldtv/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server for the **NVIDIA Shield TV**, built on the
Android TV Remote protocol v2 (the same protocol the Google TV phone app uses). It lets an
MCP client such as Claude press remote keys, launch apps, wake or sleep the Shield, and
read its power state and foreground app.

> **Status.** Pairing, `get_status`, keys, `launch_app`, `set_power` and the ADB tools were
> verified on a real Shield (remote service 7.00) in 2026-10. The features added since
> (pairing retries, reconnect and rediscovery hardening, dry run, `doctor`, resources and
> prompts, HTTP, the Alpine service) are **verified against the simulator only**, a
> wire-level fake that the real protocol library pairs and connects with.
> [Verification status](#verification-status) lists every protocol assumption and whether a
> Shield has confirmed it; [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) is the checklist.

It's one of four sibling servers with the same conventions (dry run, `doctor`, typed results,
Streamable HTTP behind Cloudflare Access):
[mcp-server-onkyo](https://github.com/SDNick484/mcp-server-onkyo),
[mcp-server-harmony](https://github.com/SDNick484/mcp-server-harmony) and
[mcp-server-sofabaton](https://github.com/SDNick484/mcp-server-sofabaton).

This is not an official NVIDIA project, and NVIDIA publishes no MCP server for the Shield.

## Why this protocol

| | Remote protocol v2 (this project) | ADB |
|---|---|---|
| Setup | One-time pairing with an on-screen code | Enable *Network debugging* on the Shield |
| Good for | D-pad, media keys, app launch, power/app state | Deep device inspection |
| Risk if exposed to an LLM | Bounded: it can only press keys | High: `adb shell` is arbitrary code execution |

Three optional ADB tools do what the remote protocol can't: `get_now_playing`,
`get_remotes` and `reboot_shield`. They are off until you run `adb-setup`, and they run
only fixed commands; the server never exposes a raw shell (see
[ADB tools](#adb-tools-optional)).

## Install

```sh
git clone https://github.com/SDNick484/mcp-server-shieldtv.git
cd mcp-server-shieldtv
python -m venv .venv && . .venv/bin/activate
pip install -e .
```

Requires Python 3.11+.

## Pair with your Shield (once)

The Shield and the machine running the server must be on the same network, and the
machine must be able to reach the Shield on these ports:

| Port | Used for |
|---|---|
| TCP 6466 | Remote control (every tool call) |
| TCP 6467 | Pairing (only during `pair`) |
| UDP 5353 (mDNS) | `discover`, and `pair` without `--host` |
| TCP 5555 | ADB, only if you enable the ADB tools with `adb-setup` |

A firewall, a guest network, or a separate IoT VLAN between the two will show up as
"can't reach the Shield".

```sh
mcp-server-shieldtv discover            # optional: list Android TV devices via mDNS
mcp-server-shieldtv pair                # finds the Shield, or: pair --host 192.168.1.50
```

A code appears on the TV; type it into the terminal. A typo, a rejected code or Cancel on
the TV starts over with a new code (three tries). Pairing generates a client certificate
and key under `~/.config/mcp-server-shieldtv/` (override with `SHIELDTV_CONFIG_DIR`) and
saves the Shield's address, name and MAC in `config.json`. The MAC is how the server finds
the Shield again if its address changes. Unusable old credentials are set aside
(`cert.pem.broken`) and replaced. A running server notices a new pairing and reconnects
with it; it doesn't need a restart.

Then check everything between this machine and the Shield:

```sh
mcp-server-shieldtv doctor
```

```
Shield 192.168.1.50
   ok config    paired with SHIELD Android TV (MAC xx:xx:xx:xx:B2:C3) at x.x.x.50; credentials load and are private
   ok tcp       ports 6466 and 6467 accept connections
   ok identity  SHIELD Android TV, MAC xx:xx:xx:xx:B2:C3
   ok session   NVIDIA SHIELD Android TV (remote service <version>); on, com.google.android.tvlauncher in front, ...
```

`doctor` checks each layer in order and stops at the first failure, saying what it means and
which [HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md) step covers it. It never presses a
key, launches anything or pairs. Output is redacted (addresses, MACs) unless `--no-redact`.

**Treat `cert.pem` and `key.pem` like a password.** Anyone who has them can control your
Shield. The directory is created `0700` and the files `0600`, and `.gitignore` excludes
them. To revoke access, remove the device from the Shield's settings and re-pair.

WSL2 note: mDNS discovery needs mirrored networking (`networkingMode=mirrored` in
`%UserProfile%\.wslconfig`, then `wsl --shutdown`). Otherwise pass `--host`.

### ADB tools (optional)

The remote protocol knows which app is in front, but not what it's playing, whether your
Bluetooth remotes work, or how to reboot. ADB can do all three. To enable the ADB tools:

1. On the Shield: Settings > Device Preferences > About, select **Build** seven times, then
   Developer options > **Network debugging** on.
2. Run `mcp-server-shieldtv adb-setup` (after `pair`). The TV asks "Allow USB debugging?":
   tick "Always allow from this computer" and choose Allow.

`adb-setup` creates its own ADB key (`adbkey`, `adbkey.pub`, `0600`) next to the pairing
files and sets `"adb": true` in `config.json`. **The ADB key grants a shell on the Shield;
guard it more carefully than the pairing files.** To revoke it, use Developer options >
Revoke USB debugging authorizations.

**`get_now_playing`** reports, checked on a real Shield: the app, title, subtitle (the artist for music,
the channel for live TV), play state, and position. Live TV (YouTube TV, Sling) reports no
position, since its "position" is an offset into the stream. Music played from YouTube Music
shows up as the YouTube app. Duration and album aren't available this way. A title that
contains ", " can rarely split wrong (the system prints title and artist joined by ", ").

**`get_remotes`** lists Bluetooth remotes (a Harmony hub shows up as "Harmony Keyboard")
(every paired input device, even ones that haven't connected since the Shield started)
as `working`, `disconnected` (normal when not in use), or `stuck`: Bluetooth says it is
connected, but Android never created its input device, so its buttons do nothing. This
happened to both remotes on a real Shield after a reboot. The fix for a Harmony hub is to
press Off and start the activity again.

**`reboot_shield`** notes which remotes work, restarts the Shield, and waits until it has
booted (about 35 seconds on a real Shield) plus 20 seconds for remotes that reconnect on
their own. It reports any remote that came back stuck, with the fix, and any that worked
before but hasn't reconnected yet. A Harmony hub doesn't reconnect until a button is
pressed, so expect it to be listed that way; if it then does nothing, `get_remotes` says
whether it's stuck. The whole call takes about a minute. It is marked destructive, so MCP
clients ask before running it.

## Use it with an MCP client

```json
{
  "mcpServers": {
    "shieldtv": { "command": "/path/to/mcp-server-shieldtv/.venv/bin/mcp-server-shieldtv" }
  }
}
```

For Claude Code: `claude mcp add shieldtv -- /path/to/.venv/bin/mcp-server-shieldtv`.

### HTTP and Cloudflare Access

To share one server between Claude Code, Claude Desktop and the mobile app, run it as a
service and reach it through a Cloudflare Tunnel with Access in front:

```sh
CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com CF_ACCESS_AUD=<aud tag> \
  mcp-server-shieldtv --http --public-host mcp.example.com      # 127.0.0.1:8712/shieldtv/mcp
```

| Flag | Variable | Default |
|---|---|---|
| `--bind` | `MCP_HTTP_BIND` | `127.0.0.1` |
| `--port` | `MCP_HTTP_PORT` | `8712` (Onkyo 8711, Harmony 8713) |
| `--path` | `MCP_HTTP_PATH` | `/shieldtv/mcp` |
| `--public-host` | `MCP_PUBLIC_HOSTS` | none: only loopback Host headers pass |
| | `CF_ACCESS_TEAM_DOMAIN`, `CF_ACCESS_AUD` | none: no Access checks |
| | `MCP_ALLOWED_EMAILS` | anyone Access lets in |

With Access configured, every request needs a valid `Cf-Access-Jwt-Assertion`: the signature
(the team's published keys), audience, issuer, expiry, and the email allow-list if set.
Anything else gets a plain 403. The server **refuses to start** on a non-loopback address
without Access unless you pass `--insecure-no-auth` (for trusted-LAN testing only). It also
checks `Host` and `Origin` (DNS-rebinding protection), including on `--bind 0.0.0.0`.
`GET /healthz` answers without auth. `src/shieldtv_mcp/remote.py` is identical in all four
servers.

### As a service on Alpine (Proxmox LXC)

```sh
sh deploy/alpine/install.sh                      # as root; or: install.sh /path/to/checkout
su -s /bin/sh mcp-shieldtv -c 'SHIELDTV_CONFIG_DIR=/var/lib/mcp-server-shieldtv \
  /opt/mcp-server-shieldtv/venv/bin/mcp-server-shieldtv pair --host <shield ip>'
vi /etc/mcp-server-shieldtv/env                  # HTTP and Access settings
rc-update add mcp-server-shieldtv default && rc-service mcp-server-shieldtv start
```

| Path | Owner, mode | Holds |
|---|---|---|
| `/opt/mcp-server-shieldtv/venv` | root, 0755 | The code |
| `/etc/mcp-server-shieldtv/env` | root:mcp-shieldtv, 0640 | HTTP and Access settings |
| `/var/lib/mcp-server-shieldtv/` | mcp-shieldtv, 0700 (files 0600) | `cert.pem`, `key.pem`, `adbkey`, `config.json` |
| `/var/log/mcp-server-shieldtv/server.log` | mcp-shieldtv, 0750 | Logs, redacted |
| `/etc/init.d/mcp-server-shieldtv` | root, 0755 | OpenRC script (`supervise-daemon`, restarts on exit) |

The credentials live in `/var/lib`, owned by the unprivileged `mcp-shieldtv` user, because
they're the service's own: `pair` runs as that user, and the server rewrites `config.json`
when the Shield moves. Every dependency has a musl wheel, so nothing compiles.
`deploy/alpine/smoke-test.sh` runs the whole install in a `python:3.12-alpine` container
against the simulator, and CI runs it on every push.
To point it at a particular Shield without editing `config.json`, pass the host as an
environment variable: `claude mcp add shieldtv -e SHIELDTV_HOST=192.168.1.50 -- ...`.
That Shield must already be paired with the same credentials (`pair --host <ip>` once per
Shield; note that `pair` also saves its host as the default in `config.json`).

## Configuration

| Setting | Default | What it does |
|---|---|---|
| `SHIELDTV_HOST` | *(from `config.json`)* | The Shield's IP address or hostname |
| `SHIELDTV_CONFIG_DIR` | `$XDG_CONFIG_HOME/mcp-server-shieldtv` | Where the certificate, key and `config.json` live |
| `XDG_CONFIG_HOME` | `~/.config` | Standard base directory, used when `SHIELDTV_CONFIG_DIR` is unset |
| `SHIELDTV_DRY_RUN` | off | Read the Shield, send nothing that changes anything (`serve --dry-run`) |
| `SHIELDTV_DEBUG` | off | Log the protocol traffic to stderr (`--debug`) |
| `SHIELDTV_LOG_UNREDACTED` | off | Show LAN addresses and MACs in logs (`--no-redact`); keys and tokens stay hidden |

`config.json` (written by `pair`, safe to edit by hand):

| Key | What it does |
|---|---|
| `host` | The Shield's address, saved by `pair` (and updated if the Shield moves) |
| `name`, `mac` | The Shield's name and MAC, read from its certificate by `pair` |
| `apps` | Extra or overriding app names for `launch_app` (see below) |
| `adb` | `true` once `adb-setup` succeeds; enables the ADB tools |
| `port`, `pairing_port` | `6466` and `6467`; other values are for the simulator |

Problems in `config.json` (invalid JSON, a bad app entry) are logged at startup and shown by
`doctor`, and only the bad part is ignored.

When the host is set in more than one place, the most specific wins: `pair --host`, then
`SHIELDTV_HOST`, then `config.json`.

## Tools

| Tool | What it does |
|---|---|
| `get_status` | Reachability, power (`on`/`standby`), foreground app, volume (`null` when not reported), device info; while unreachable, `stale: true` with `as_of` and `error` |
| `list_apps` | The app names `launch_app` accepts |
| `send_key` | Press an allow-listed remote key, optionally repeated 1-10 times (see below) |
| `launch_app` | Launch an allow-listed app by friendly name, and confirm it reached the foreground |
| `set_power` | Wake (`on`) or sleep (`off`) using `WAKEUP`/`SLEEP`, not the `POWER` toggle |
| `get_now_playing` | App, title, subtitle (artist or channel), play state and position. Needs `adb-setup` |
| `get_remotes` | Each Bluetooth remote and whether it works, with a fix for stuck ones. Needs `adb-setup` |
| `reboot_shield` | Restart the Shield, wait until it's back, then check the remotes. Needs `adb-setup` |

Every tool returns structured content with a published output schema. The actions
(`send_key`, `launch_app`, `set_power`, and `reboot_shield`'s first fields) return the same
**ActionResult** shape as the sibling servers:

```json
{"shield": "SHIELD Android TV", "outcome": "done",
 "detail": "Launched netflix (com.netflix.ninja is in the foreground)",
 "sent": ["app_link https://www.netflix.com/title"], "warnings": []}
```

`outcome` is `done`, `unchanged` (e.g. `set_power off` while already in standby: nothing is
sent) or `dry_run`. Failures are MCP errors with a sentence that says what to do.

**Dry run** (`serve --dry-run`, `call --dry-run` or `SHIELDTV_DRY_RUN=1`) reads the Shield and
validates every call as usual, but sends nothing; results say what would have been sent.

### Resources and prompts

Resources are context a client attaches (in Claude Code: `@shieldtv:shieldtv://apps`). Reading
them sends nothing to the Shield.

| Resource | What it holds |
|---|---|
| `shieldtv://apps` | The apps `launch_app` accepts: link, package, and whether each is a default or from `config.json` |
| `shieldtv://keys` | The keys `send_key` accepts, and the ones left out on purpose, with why |

Prompts are workflows you pick (in Claude Code: `/mcp__shieldtv__watch netflix`).

| Prompt | Arguments | Does |
|---|---|---|
| `watch` | `app`, `what` | Wakes the Shield, opens the app, navigates with the D-pad while asking you what's on screen |
| `remotes_not_working` | | Runs `get_remotes` and repeats its advice; doesn't reboot unless you ask |

### Movie night across servers

The servers don't know about each other: each works alone, and a client with several
connected composes them. With all four connected, *"movie night in the theater: Plex on the
Shield, receiver in Dolby Surround at 45"* becomes (tool names as of these versions):

```
harmony.start_activity   activity="Watch Shield"          # TV and receiver on, inputs switched
shieldtv.launch_app      app="plex"
onkyo.set_listening_mode receiver="Theater" mode="dolby-surround"
onkyo.set_volume         receiver="Theater" level=45      # capped server-side if above the cap
```

What makes this safe to hand to a model is that limits live in each server, not in the
prompt: only allow-listed keys and apps here, and the volume cap and never-guess-a-receiver
rule in Onkyo.

Allowed keys: `HOME`, `BACK`, `MENU`, `DPAD_UP`/`DOWN`/`LEFT`/`RIGHT`/`CENTER`,
`MEDIA_PLAY_PAUSE`, `MEDIA_PLAY`, `MEDIA_PAUSE`, `MEDIA_STOP`, `MEDIA_NEXT`,
`MEDIA_PREVIOUS`, `MEDIA_REWIND`, `MEDIA_FAST_FORWARD`, `VOLUME_UP`, `VOLUME_DOWN`,
`VOLUME_MUTE`.

### Apps

Default apps, each checked on a real Shield: `youtube`, `youtube-tv`, `netflix`,
`prime-video`, `disney+`, `hulu`, `plex`, `spotify`.

Apps launch through **deep links**: https links, or an app's own scheme (`plex://`,
`spotify:`) where the https link would go to a browser instead. On the Shield (remote service 7.x), a bare
package name is sent as `market://launch?id=<package>`, which the Shield rejects and then
drops the connection. `launch_app` waits for the app to reach the foreground (up to 10s)
and returns an error that says what happened if it doesn't: the request was rejected, or it
was accepted but nothing opened (the app isn't installed, or no app handles the link; the
TV shows "You don't have an app that can do this").

### Add your own apps

Edit `~/.config/mcp-server-shieldtv/config.json`:

```json
{
  "host": "192.168.1.50",
  "apps": {
    "crunchyroll": { "link": "https://www.crunchyroll.com", "package": "com.crunchyroll.crunchyroid" },
    "example": "https://example.com/tv"
  }
}
```

An entry is either a link, or `{ "link", "package" }`. Adding the package is recommended:
`launch_app` then confirms that exact app opened, and `get_status` shows the friendly name.
Without it, any app other than the home screen coming to the front counts as success.
Names are case-insensitive, and an entry with the same name as a default replaces it.
Restart the server (or your MCP client) to pick up changes.

**Finding the link and package:** open the app on the Shield, then call `get_status` (or ask
"what app is open on the Shield?"); `current_app_package` is the package. For the link, try
the service's website address (`https://www.<service>.com`). With ADB enabled, Android can
tell you which app a link opens without launching anything:

```sh
adb shell cmd package query-activities --brief -a android.intent.action.VIEW \
  -c android.intent.category.BROWSABLE -d 'https://tv.youtube.com'
```

A result of `com.google.android.tv.frameworkpackagestubs/.Stubs$BrowserStub` means "no app"
(the TV shows "You don't have an app that can do this"). If the stub is listed alongside the
app, the app usually still opens, but its own scheme (if it has one) avoids the ambiguity.

## Safety design

- **Keys are an allow-list.** The tool schema is an enum, and the client re-checks it.
  `POWER`, `SEARCH` (starts voice capture), `SETTINGS`, `MUTE` (Android's *microphone*
  mute), raw numeric key codes, and the library's `text:` typing are not available.
- **Apps are an allow-list.** Only names in `list_apps` can be launched.
- **No shell and no arbitrary key codes**, so a prompt-injected model has a small blast
  radius. ADB is off by default. When enabled, the ADB tools take no arguments and run only
  constant commands (`dumpsys media_session`, `dumpsys bluetooth_manager`, `dumpsys input`,
  `getprop sys.boot_completed`, `/proc/uptime`), plus ADB's own reboot service. Code that
  tries any other command is refused, so nothing the model writes reaches the Shield's shell.
- **`reboot_shield` is the only tool marked destructive**: it interrupts playback, so
  clients should confirm with you first.
- **Credentials are private** (`0600`) and never logged. Logs go to stderr because stdout
  belongs to the MCP transport.
- Tools carry titles and MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) so clients can decide what needs confirmation.

## Behavior notes

- The server keeps **one long-lived connection** and caches pushed state. If the Shield is
  asleep or offline at startup it retries in the background (1 s doubling to 60 s), and tool
  calls return a clear "can't reach the Shield" message in the meantime. Each attempt is
  bounded (15 s), so a Shield that accepts the connection but never starts a session can't
  hang the server.
- **While disconnected, `get_status` still shows the last known values**, marked
  `stale: true`, with `as_of` (when they arrived) and `error` (why it isn't reachable).
- **If the Shield moves to another address** (DHCP), after 60 s unreachable the server looks
  for it over mDNS and checks each candidate's certificate for the MAC that `pair` saved. Only
  an exact match is adopted, and saved to `config.json`. Any other device is never guessed at.
- **If the Shield stops trusting the certificate** (unpaired on the TV, factory reset), the
  error says to re-run `pair`, and the running server picks up the new pairing by itself.
- Volume keys act on whatever the Shield is configured to control (the Shield itself, HDMI-CEC,
  or IR), so results depend on your setup. When volume goes to a TV or receiver over CEC, the
  Shield doesn't report a level, and `get_status` returns `volume: null`.

## Troubleshooting

The server's errors are written to be actionable, so the model will usually relay one of
these:

**"Not paired with a Shield yet."** No host or credentials were found. Run
`mcp-server-shieldtv pair`. If you did pair, check that the server and `pair` use the same
`SHIELDTV_CONFIG_DIR` (an MCP client may launch the server with a different environment).

**"The Shield rejected our pairing."** The Shield no longer trusts this certificate,
typically after a factory reset or after removing the device under the Shield's
remote/connected-device settings. Run `pair` again; a running server reconnects by itself.

**"Can't connect: the pairing files ... can't be used."** `cert.pem` or `key.pem` is
damaged. Run `pair` again; it sets the old files aside.

**"Can't reach the Shield at ..."** The server is paired but has no connection. The Shield
may be asleep, rebooting, or off the network, its IP may have changed (a DHCP reservation
helps), or TCP 6466 may be blocked (see the port table above). The server keeps retrying in
the background, so the next call may succeed without a restart. If the address changed and
`config.json` has the Shield's `mac` (saved by `pair` since this version), the server finds
it again by itself; otherwise re-pair with the new address. `doctor` tells these cases apart.

**"The connection to the Shield dropped; try again in a moment."** The connection closed
during the command. The library reconnects on its own; retry.

**"The Shield rejected the launch request ..."** The app entry is a bare package name (or a
link the Shield refuses). Use an https link; see [Apps](#apps).

**"The Shield accepted ..., but the foreground app didn't change"** The app is probably not
installed, or nothing on the Shield handles that link. A very slow cold start can also do
this; the next call then reports the app as already open.

**`pair` says "No input to read the code from".** It was run without a terminal (for
example through a tool that doesn't attach one). Run it in a regular terminal.

**`discover` (or `pair` without `--host`) finds nothing.** mDNS doesn't cross most VLANs or
guest networks, and on WSL2 it needs mirrored networking (see above). Pass `--host <ip>`;
the Shield shows its IP in its network/about settings, and your router's client list has
it too.

Server logs (connection attempts, retries, auth failures) go to stderr, which most MCP
clients save in their own logs. Running `mcp-server-shieldtv` in a terminal shows them
directly; it waits for MCP messages on stdin, so stop it with Ctrl+C.

## A session against the simulator

`simulate` runs a **simulated Shield**: TLS on two ports, speaking Android TV Remote v2 the way
androidtvremote2 does. The real library pairs and connects with it. Pair with it exactly as
with a real one (the code "on the TV" is printed by the simulator), or pass `--paired`:

```
$ mcp-server-shieldtv simulate --config-dir /tmp/sim        # terminal 1
  ...
  The TV shows the pairing code: 3B26B3

$ export SHIELDTV_CONFIG_DIR=/tmp/sim                         # terminal 2
$ mcp-server-shieldtv pair --host 127.0.0.1
Pairing with SHIELD Android TV (00:04:4B:A1:B2:C3). A code will appear on the TV screen.
Enter the code shown on the TV: 3B26B3
Paired and connected: NVIDIA SHIELD Android TV

$ mcp-server-shieldtv call launch_app app=netflix
{ "shield": "SHIELD Android TV", "outcome": "done",
  "detail": "Launched netflix (com.netflix.ninja is in the foreground)",
  "sent": ["app_link https://www.netflix.com/title"], "warnings": [] }

$ mcp-server-shieldtv call set_power state=off
{ ..., "outcome": "done", "detail": "The Shield is in standby", "sent": ["KEYCODE_SLEEP"] }

$ mcp-server-shieldtv call set_power state=off
{ ..., "outcome": "unchanged", "detail": "The Shield is already in standby", "sent": [] }

$ mcp-server-shieldtv call launch_app app=hulu
Error executing tool launch_app: The Shield accepted https://www.hulu.com/welcome, but the
foreground app didn't change within 10s (still com.netflix.ninja). The app may not be
installed, or no installed app handles that link; the TV may be showing an error. The Shield
is in standby, which may be why: wake it with set_power first.
```

(Lines wrapped, some fields elided with `...`.) What the simulator does and where each behavior
comes from (the library's client code, the owner's Shield, or invented for a test) is listed
at the top of `src/shieldtv_mcp/sim/fake_shield.py`.

## Development

```sh
pip install -e ".[dev]"
pytest -q           # ~200 tests, ~15 s, no Shield needed
ruff check . && ruff format --check .
mypy                # strict type checking of src/
```

| Tests | Cover |
|---|---|
| `test_config.py` | Allow-lists (checked against the protocol's own key enum), settings, config problems |
| `test_client.py` | Connection lifecycle and state against `FakeRemote`, a stand-in for the library |
| `test_wire.py` | The real library against the simulated Shield: pairing (typo, rejection, Cancel, Shield gone), session, drops, garbled frames, sleep, stale state, unpairing, re-pair pickup, an address change (127.0.0.2 to .3) |
| `test_tools.py`, `test_resources.py` | The MCP contract: schemas, annotations, ActionResults, errors, dry run, resources, prompts |
| `test_adb.py` | ADB parsers on real dumpsys output; only constant commands reach the shell |
| `test_tooling.py`, `test_cli.py` | `doctor`, `simulate`, `call`, argument parsing |
| `test_http_tools.py`, `test_remote.py` | Streamable HTTP end to end; Access JWTs (valid, expired, wrong audience, wrong issuer, missing) with locally generated keys |
| `test_stdio.py` | The installed entry point over stdio |
| `test_assumptions.py` | Every assumption is cited by code, and listed here and in HARDWARE_VALIDATION.md |

CI runs it all on Python 3.11-3.14, plus the Alpine smoke test.
`npx @modelcontextprotocol/inspector mcp-server-shieldtv` browses tools, resources and prompts
by hand. [docs/ADB_TOOLS.md](docs/ADB_TOOLS.md) reviews the ADB tools and proposes read-only
additions (design only).

## Verification status

Hardware-verified means seen on the owner's Shield (what was seen is in each assumption's
`note` in `src/shieldtv_mcp/assumptions.py`). Simulator-only means the simulator implements
it and nothing has confirmed it.

| Assumption | Confidence | Status |
|---|---|---|
| `S-PAIRING` | high | hardware-verified |
| `S-REMOTE-HANDSHAKE` | high | hardware-verified |
| `S-TLS-REJECT` | medium | simulator-only |
| `S-CERT-MAC` | medium | simulator-only |
| `S-MDNS` | high | simulator-only |
| `S-RECONNECT` | high | hardware-verified |
| `S-SLEEP-CONNECTION` | medium | simulator-only |
| `S-MARKET-REJECT` | high | hardware-verified |
| `S-LINK-UNHANDLED` | high | hardware-verified |
| `S-WAKE-ANY-KEY` | high | hardware-verified |
| `S-CEC-VOLUME` | high | hardware-verified |
| `S-APP-LINKS` | high | hardware-verified |
| `S-DUMPSYS-MEDIA` | high | hardware-verified |
| `S-LIVE-TV-PACKAGES` | medium | simulator-only |
| `S-STUCK-REMOTES` | high | hardware-verified |
| `S-REBOOT-TIME` | high | hardware-verified |

`test_assumptions.py` fails if this table and `assumptions.py` disagree.

## Roadmap

- Hardware validation of this branch ([HARDWARE_VALIDATION.md](HARDWARE_VALIDATION.md))
- The ADB proposals in [docs/ADB_TOOLS.md](docs/ADB_TOOLS.md), once their outputs are captured
- Several Shields from one server, by name (as the sibling servers do)
- Publish to PyPI and the MCP registry

## License

MIT. See [LICENSE](LICENSE).
