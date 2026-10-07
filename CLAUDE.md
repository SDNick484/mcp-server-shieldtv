# CLAUDE.md

## What this is

`mcp-server-shieldtv`: an MCP server that controls an NVIDIA Shield TV over the Android TV
Remote protocol v2 (via the `androidtvremote2` library). Sibling project to
`mcp-server-onkyo`.

**This is a learning project.** The owner wants to understand how MCP servers are built, so
explain the reasoning behind non-obvious changes instead of only making them.

## Layout

- `src/shieldtv_mcp/config.py`: settings, file locations, and the key and app allow-lists
- `src/shieldtv_mcp/client.py`: `ShieldClient`, the long-lived connection plus pushed-state cache
- `src/shieldtv_mcp/server.py`: MCP tools (`MCPServer` from `mcp` 2.x)
- `src/shieldtv_mcp/adb.py`: optional ADB: constant `COMMANDS`, their parsers, and the reboot flow
- `src/shieldtv_mcp/cli.py`: `serve` (default), `pair`, `discover`, `adb-setup`
- `src/shieldtv_mcp/discovery.py`: mDNS lookup for `_androidtvremote2._tcp`
- `tests/`: `conftest.py` (`FakeRemote`, which mirrors the library's real data shapes),
  `test_config`, `test_client`, `test_adb` (parser fed real dumpsys output, `FakeDevice`),
  `test_tools` (in-process MCP `Client`), `test_stdio`
  (installed entry point). Async tests use anyio's plugin, not pytest-asyncio.

## Rules for changes

- **Never expose `adb shell`, arbitrary key codes, or any escape hatch to a shell.** Keys
  and apps are allow-lists; widen them deliberately and update README and tests together.
  ADB is touched only in `adb.py`, and only with the constants in `COMMANDS` (`run_command`
  refuses anything else); no tool argument may ever reach a shell command
  (`test_adb_tools_take_no_arguments` guards this). `reboot_shield` is the one tool with
  `destructive_hint=True`.
- Raise `ShieldError` (a `ToolError`) for anything the model or user can act on. Other
  exceptions reach the model only as "Error executing tool".
- Log to **stderr only**. stdout is the MCP stdio transport.
- Credentials (`cert.pem`, `key.pem`, `adbkey`, `adbkey.pub`) stay `0600`, are never logged,
  and are never committed.
- `send_key_command` and `send_launch_app_command` in `androidtvremote2` are synchronous;
  `async_*` methods are the awaitable ones. `volume_info` and `device_info` are dicts
  (TypedDicts), not objects. Key names are `RemoteKeyCode` minus `KEYCODE_`; `MUTE` is the
  microphone, `VOLUME_MUTE` the sound.
- Every tool has a `title`, explicit `ToolAnnotations`, and constrained args via
  `Literal`/`Annotated[..., Field(...)]`. `test_tools.py` enforces this.

## Status

Verified on a real Shield (remote service 7.00, 2026-10): pair, `get_status`, keys,
`launch_app`, `set_power` (`SLEEP` reports `standby` at once; any key, not just
`WAKEUP`, wakes it), `adb-setup`, `get_now_playing` and `get_remotes`.

Hardware facts the code depends on (the library hides them):
- `market://launch?id=<pkg>` (what a bare package becomes) is rejected: the Shield sends
  `remote_error`, which the library only logs, then drops the connection. https links work.
- After a drop, sends on the old connection are silently discarded, so one bad command can
  make later ones look like they succeeded. Never trust a send as proof; `launch` checks the
  foreground app.
- A link no installed app handles is accepted; the foreground app just doesn't change.
  Custom schemes (`plex://`, `spotify:`) are accepted too; prefer them where the https link
  also matches the browser stub. Find handlers with `adb shell cmd package query-activities`
  (see README). An app's first launch after install can take over 10s, so a one-off launch
  timeout right after installing isn't proof the link is wrong.
- `dumpsys media_session`: the position is a snapshot at `updated` (ms of uptime, the same
  clock as `/proc/uptime`). The description is "title, subtitle, description"; YouTube puts
  the artist (music) or channel (YouTube TV) in subtitle. YouTube TV's position is a stream
  offset (~13h). YouTube Music plays through `com.google.android.youtube.tv`. The Shield's
  ADB prompt must be answered on the TV; `adb-setup` waits 60s.
- After a reboot, Bluetooth remotes can be HID-connected (`dumpsys bluetooth_manager`
  mInputDevices state 2) with no input device (no `dumpsys input` entry with `bus=0x0005` and
  `UniqueId` = their address), so buttons do nothing. Harmony fix: Off, then start the
  activity. `svc bluetooth disable` from ADB is silently ignored on this build. After the
  reboot command, ADB and the remote service were back in 33-38s. The Harmony hub doesn't
  reconnect on its own after a reboot (it stayed disconnected 2+ minutes, then reconnected
  and worked when a button was pressed), so `reboot_shield` doesn't wait for it, and
  mInputDevices may not list it (paired input devices come from `HID_HOST=100` in the
  Metadata section instead).
- Volume behind HDMI-CEC arrives with `max == 0`; it is reported as `None`. In standby the
  Shield reports its own volume (e.g. 1/15) instead.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-shieldtv discover | pair [--host IP] | adb-setup | (serve)
```
