# Changelog

## [Unreleased]

### Added

- Every tool now has an authorization tier (`read`, `power-off`, `everyday` or `costly`), from the shared `@tier` in `remote.py` (synced to mcp-server-shared f4719e3). `shield_set_power` is `power-off` for `off` and `everyday` for `on`. `shield_reboot` is `costly`: it asks the person to confirm in a prompt the model can't answer, and refuses where the app can't show the prompt (today, everything but the Claude Code CLI). It runs once per yes.
- `MCP_ALLOWED_SERVICE_TOKENS` (comma-separated Access service-token client IDs) and `MCP_AUTOMATION_TIERS` (what those callers may use; default `read,power-off`, never `costly`).

### Changed

- **Behavior change:** a Cloudflare Access service token now gets 403 unless its client ID is in `MCP_ALLOWED_SERVICE_TOKENS`, and an admitted token gets only the tiers in `MCP_AUTOMATION_TIERS` (default `read,power-off`). People's logins are unaffected, except that `shield_reboot` now asks first.

- `shield_send_key` caps `VOLUME_UP` at 20 presses in any 5 minutes, counted across calls. The key reaches the TV or receiver over HDMI-CEC, which can't report the level, so the receiver's and TV's own `max_volume` never saw these presses. Over the limit, the call is refused whole and points to the receiver's or TV's own volume tool. Volume down and mute are never limited. `"volume_up_limit"` in `config.json` or `SHIELDTV_VOLUME_UP_LIMIT` lowers the cap (1-20). Uses the shared `WindowLimit` (shared `ratelimit.py` synced).

## [0.1.0]

First release on PyPI.

### Added

- MCP server for the NVIDIA Shield TV over the Android TV Remote protocol v2 (`androidtvremote2`): status, now playing, allow-listed keys and apps, power, and the Bluetooth remotes' state.
- Optional ADB tools (a fixed set of read-only commands, plus reboot), never a shell.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `pair`, `discover`, `adb-setup`, `doctor`, `call` and `simulate` (a simulated Shield on the wire, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
