# Changelog

## [Unreleased]

## [0.1.0]

First release on PyPI.

### Added

- MCP server for the NVIDIA Shield TV over the Android TV Remote protocol v2 (`androidtvremote2`): status, now playing, allow-listed keys and apps, power, and the Bluetooth remotes' state.
- Optional ADB tools (a fixed set of read-only commands, plus reboot), never a shell.
- CLI: `serve` (stdio, or `--http` behind Cloudflare Access), `pair`, `discover`, `adb-setup`, `doctor`, `call` and `simulate` (a simulated Shield on the wire, no hardware needed).
- `deploy/alpine/`: OpenRC service and install script.
