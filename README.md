# mcp-server-shieldtv

[![CI](https://github.com/SDNick484/mcp-server-shieldtv/actions/workflows/ci.yml/badge.svg)](https://github.com/SDNick484/mcp-server-shieldtv/actions/workflows/ci.yml)

An [MCP](https://modelcontextprotocol.io) server for the **NVIDIA Shield TV**, built on the
Android TV Remote protocol v2 (the same protocol the Google TV phone app uses). It lets an
MCP client such as Claude press remote keys, launch apps, wake or sleep the Shield, and
read its power state and foreground app.

> **Status: early / untested on hardware.** The code is covered by unit tests against a fake
> remote and the MCP tool surface is smoke-tested over stdio, but it has not yet been run
> against a real Shield. Expect rough edges, and please open issues.

This is not an official NVIDIA project, and NVIDIA publishes no MCP server for the Shield.

## Why this protocol

| | Remote protocol v2 (this project) | ADB |
|---|---|---|
| Setup | One-time pairing with an on-screen code | Enable *Network debugging* on the Shield |
| Good for | D-pad, media keys, app launch, power/app state | Deep device inspection |
| Risk if exposed to an LLM | Bounded: it can only press keys | High: `adb shell` is arbitrary code execution |

ADB features (such as now-playing metadata) may come later as separate, typed tools. The
server will never expose a raw shell.

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

A firewall, a guest network, or a separate IoT VLAN between the two will show up as
"can't reach the Shield".

```sh
mcp-server-shieldtv discover            # optional: list Android TV devices via mDNS
mcp-server-shieldtv pair                # finds the Shield, or: pair --host 192.168.1.50
```

A code appears on the TV; type it into the terminal. Pairing generates a client
certificate and key under `~/.config/mcp-server-shieldtv/` (override with
`SHIELDTV_CONFIG_DIR`) and saves the Shield's address in `config.json`.

**Treat `cert.pem` and `key.pem` like a password.** Anyone who has them can control your
Shield. The directory is created `0700` and the files `0600`, and `.gitignore` excludes
them. To revoke access, remove the device from the Shield's settings and re-pair.

WSL2 note: mDNS discovery needs mirrored networking (`networkingMode=mirrored` in
`%UserProfile%\.wslconfig`, then `wsl --shutdown`). Otherwise pass `--host`.

## Use it with an MCP client

```json
{
  "mcpServers": {
    "shieldtv": { "command": "/path/to/mcp-server-shieldtv/.venv/bin/mcp-server-shieldtv" }
  }
}
```

For Claude Code: `claude mcp add shieldtv -- /path/to/.venv/bin/mcp-server-shieldtv`.
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

`config.json` (written by `pair`, safe to edit by hand):

| Key | What it does |
|---|---|
| `host` | The Shield's address, saved by `pair` |
| `apps` | Extra or overriding app names for `launch_app` (see below) |

When the host is set in more than one place, the most specific wins: `pair --host`, then
`SHIELDTV_HOST`, then `config.json`.

## Tools

| Tool | What it does |
|---|---|
| `get_status` | Reachability, power (`on`/`standby`), foreground app, volume (`null` when not reported), device info |
| `list_apps` | The app names `launch_app` accepts |
| `send_key` | Press an allow-listed remote key, optionally repeated 1-10 times (see below) |
| `launch_app` | Launch an allow-listed app by friendly name, and confirm it reached the foreground |
| `set_power` | Wake (`on`) or sleep (`off`) using `WAKEUP`/`SLEEP`, not the `POWER` toggle |

This protocol reports the foreground *app* but not what is playing (title, artist,
position). `get_status` returns structured content with a published output schema.

Allowed keys: `HOME`, `BACK`, `MENU`, `DPAD_UP`/`DOWN`/`LEFT`/`RIGHT`/`CENTER`,
`MEDIA_PLAY_PAUSE`, `MEDIA_PLAY`, `MEDIA_PAUSE`, `MEDIA_STOP`, `MEDIA_NEXT`,
`MEDIA_PREVIOUS`, `MEDIA_REWIND`, `MEDIA_FAST_FORWARD`, `VOLUME_UP`, `VOLUME_DOWN`,
`VOLUME_MUTE`.

### Apps

Default apps, each checked on a real Shield: `youtube`, `netflix`, `prime-video`.

Apps launch through **https deep links**. On the Shield (remote service 7.x), a bare
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

**Finding the link and package:** try the service's website address (`https://www.<service>.com`)
as the link. Open the app on the Shield, then call `get_status` (or ask "what app is open on
the Shield?"); `current_app_package` is the package.

## Safety design

- **Keys are an allow-list.** The tool schema is an enum, and the client re-checks it.
  `POWER`, `SEARCH` (starts voice capture), `SETTINGS`, `MUTE` (Android's *microphone*
  mute), raw numeric key codes, and the library's `text:` typing are not available.
- **Apps are an allow-list.** Only names in `list_apps` can be launched.
- **No shell, no ADB, no arbitrary key codes**, so a prompt-injected model has a small blast
  radius.
- **Credentials are private** (`0600`) and never logged. Logs go to stderr because stdout
  belongs to the MCP transport.
- Tools carry titles and MCP annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) so clients can decide what needs confirmation.

## Behavior notes

- The server keeps **one long-lived connection** and caches pushed state. If the Shield is
  asleep or offline at startup it retries in the background, and tool calls return a clear
  "can't reach the Shield" message in the meantime.
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
remote/connected-device settings. Run `pair` again.

**"Can't reach the Shield at ..."** The server is paired but has no connection. The Shield
may be asleep, rebooting, or off the network, its IP may have changed (a DHCP reservation
helps), or TCP 6466 may be blocked (see the port table above). The server keeps retrying in
the background, so the next call may succeed without a restart.

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

## Development

```sh
pip install -e ".[dev]"
pytest              # unit + in-process MCP + stdio end-to-end tests, no Shield needed
ruff check . && ruff format --check .
mypy                # strict type checking of src/
```

Tests are layered: `test_config.py` (allow-lists, checked against the protocol's own key
enum), `test_client.py` (connection lifecycle against a fake remote), `test_tools.py`
(the MCP contract through an in-process client: schemas, annotations, results, errors),
and `test_stdio.py` (the installed entry point over stdio). CI runs all of it on
Python 3.11-3.14.

To poke at the tools interactively: `npx @modelcontextprotocol/inspector mcp-server-shieldtv`.

## Roadmap

- Find working links for more apps (Plex, Disney+, Hulu, Spotify, Kodi were dropped from the
  defaults until checked on a Shield that has them installed)
- Optional ADB-backed read-only tools (now playing), kept separate and typed
- Publish to PyPI and the MCP registry

## License

MIT. See [LICENSE](LICENSE).
