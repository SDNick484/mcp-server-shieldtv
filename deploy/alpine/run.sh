#!/bin/sh
# What the OpenRC service runs (as user mcp-shieldtv). Loads the environment
# file, then replaces itself with the server, so supervise-daemon watches the
# server process itself. Run it by hand to see exactly what the service does:
#   su -s /bin/sh mcp-shieldtv -c /opt/mcp-server-shieldtv/run.sh
set -eu
ENV_FILE="${MCP_SHIELDTV_ENV:-/etc/mcp-server-shieldtv/env}"
if [ -r "$ENV_FILE" ]; then
	set -a  # export every variable the file sets
	# shellcheck disable=SC1090
	. "$ENV_FILE"
	set +a
fi
export SHIELDTV_CONFIG_DIR="${SHIELDTV_CONFIG_DIR:-/var/lib/mcp-server-shieldtv}"
# MCP_SERVE_ARGS is split into words on purpose (e.g. "--dry-run --no-redact")
# shellcheck disable=SC2086
exec /opt/mcp-server-shieldtv/venv/bin/mcp-server-shieldtv serve --http ${MCP_SERVE_ARGS:-}
