# mcp-server-shieldtv

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

The Shield and the machine running the server must be on the same network.

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

## Tools

| Tool | What it does |
|---|---|
| `get_status` | Reachability, power (`on`/`standby`), foreground app, volume, device info |
| `list_apps` | The app names `launch_app` accepts |
| `send_key` | Press an allow-listed remote key, optionally repeated 1-10 times (see below) |
| `launch_app` | Launch an allow-listed app by friendly name |
| `set_power` | Wake (`on`) or sleep (`off`) using `WAKEUP`/`SLEEP`, not the `POWER` toggle |

This protocol reports the foreground *app* but not what is playing (title, artist,
position). `get_status` returns structured content with a published output schema.

Allowed keys: `HOME`, `BACK`, `MENU`, `DPAD_UP`/`DOWN`/`LEFT`/`RIGHT`/`CENTER`,
`MEDIA_PLAY_PAUSE`, `MEDIA_PLAY`, `MEDIA_PAUSE`, `MEDIA_STOP`, `MEDIA_NEXT`,
`MEDIA_PREVIOUS`, `MEDIA_REWIND`, `MEDIA_FAST_FORWARD`, `VOLUME_UP`, `VOLUME_DOWN`,
`VOLUME_MUTE`.

### Add your own apps

Edit `~/.config/mcp-server-shieldtv/config.json`:

```json
{ "host": "192.168.1.50", "apps": { "crunchyroll": "com.crunchyroll.crunchyroid" } }
```

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
  or IR), so results depend on your setup.

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

- Verify against real hardware: connect, key presses, app launch, `WAKEUP`/`SLEEP` behavior
- Check which package names and deep links work on the Shield for each default app
- Optional ADB-backed read-only tools (now playing), kept separate and typed
- Publish to PyPI and the MCP registry

## License

MIT. See [LICENSE](LICENSE).
