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
- `src/shieldtv_mcp/cli.py`: `serve` (default), `pair`, `discover`
- `src/shieldtv_mcp/discovery.py`: mDNS lookup for `_androidtvremote2._tcp`
- `tests/`: `conftest.py` (`FakeRemote`, which mirrors the library's real data shapes),
  `test_config`, `test_client`, `test_tools` (in-process MCP `Client`), `test_stdio`
  (installed entry point). Async tests use anyio's plugin, not pytest-asyncio.

## Rules for changes

- **Never expose `adb shell`, arbitrary key codes, or any escape hatch to a shell.** Keys
  and apps are allow-lists; widen them deliberately and update README and tests together.
- Raise `ShieldError` (a `ToolError`) for anything the model or user can act on. Other
  exceptions reach the model only as "Error executing tool".
- Log to **stderr only**. stdout is the MCP stdio transport.
- Credentials (`cert.pem`, `key.pem`) stay `0600`, are never logged, and are never committed.
- `send_key_command` and `send_launch_app_command` in `androidtvremote2` are synchronous;
  `async_*` methods are the awaitable ones. `volume_info` and `device_info` are dicts
  (TypedDicts), not objects. Key names are `RemoteKeyCode` minus `KEYCODE_`; `MUTE` is the
  microphone, `VOLUME_MUTE` the sound.
- Every tool has a `title`, explicit `ToolAnnotations`, and constrained args via
  `Literal`/`Annotated[..., Field(...)]`. `test_tools.py` enforces this.

## Status

Unit-tested only; **not yet verified on a real Shield.** The first roadmap item is a
hardware pass: pair, `get_status`, keys, app launch, and `WAKEUP`/`SLEEP`.
Default app package names are best guesses until checked.

## Commands

```sh
pip install -e ".[dev]" && pytest && ruff check . && ruff format --check . && mypy
mcp-server-shieldtv discover | pair [--host IP] | (serve)
```
