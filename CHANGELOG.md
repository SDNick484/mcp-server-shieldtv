# Changelog

## [Unreleased]

### Added

- `shield_send_key` caps `VOLUME_UP` at 20 presses in any 5 minutes, counted across calls. The key reaches the TV or receiver over HDMI-CEC, which can't report the level, so the receiver's and TV's own `max_volume` never saw these presses. Over the limit, the call is refused whole and points to the receiver's or TV's own volume tool. Volume down and mute are never limited. `"volume_up_limit"` in `config.json` or `SHIELDTV_VOLUME_UP_LIMIT` lowers the cap (1-20). Uses the shared `WindowLimit` (shared `ratelimit.py` synced).

## [0.1.0]

First release on PyPI.

### Added

- MCP server for the NVIDIA Shield TV over the Android TV Remote protocol v2 (`androidtvremote2`): status, now playing, allow-listed keys and apps, power, and the Bluetooth remotes' state.
- Optional ADB tools (a fixed set of read-only commands, plus reboot), never a shell.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `pair`, `discover`, `adb-setup`, `doctor`, `call` and `simulate` (a simulated Shield on the wire, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
