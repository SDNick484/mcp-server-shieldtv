#!/bin/sh
# Install mcp-server-shieldtv as an OpenRC service on Alpine (e.g. a Proxmox LXC).
#
#   sh deploy/alpine/install.sh [SOURCE]      # SOURCE: a checkout dir or a pip spec
#
# Layout (each part owned so the service can read its settings, keep its
# credentials to itself, and change nothing else):
#   /opt/mcp-server-shieldtv/venv     the code                     root:root            0755
#   /opt/mcp-server-shieldtv/run.sh   the service command          root:root            0755
#   /etc/mcp-server-shieldtv/env      HTTP and Access settings     root:mcp-shieldtv    0640
#   /var/lib/mcp-server-shieldtv/     cert.pem, key.pem, adbkey,   mcp-shieldtv         0700 / files 0600
#                                     config.json (SHIELDTV_CONFIG_DIR)
#   /var/log/mcp-server-shieldtv/     server.log                   mcp-shieldtv         0750
#   /etc/init.d/mcp-server-shieldtv   the OpenRC script
# The credentials live in /var/lib, not /etc, because they are the service's
# own: `pair` (run as the service user) writes them, and the server rewrites
# config.json when the Shield moves to a new address.
# Re-running upgrades the code and keeps settings and pairing.
set -eu

SRC="${1:-git+https://github.com/SDNick484/mcp-server-shieldtv}"
USER_NAME=mcp-shieldtv
PREFIX=/opt/mcp-server-shieldtv
ETC=/etc/mcp-server-shieldtv
STATE=/var/lib/mcp-server-shieldtv
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# python3 and its venv module; every compiled dependency ships a musl wheel,
# so no compiler is needed (see README: Alpine).
apk add --no-cache python3 py3-pip openrc >/dev/null

if ! id "$USER_NAME" >/dev/null 2>&1; then
	addgroup -S "$USER_NAME"
	adduser -S -D -H -h "$STATE" -s /sbin/nologin -G "$USER_NAME" "$USER_NAME"
fi

python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet --upgrade "$SRC"
install -m 0755 "$HERE/run.sh" "$PREFIX/run.sh"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
[ -e "$ETC/env" ] || install -m 0640 -o root -g "$USER_NAME" "$HERE/env.example" "$ETC/env"
install -d -m 0700 -o "$USER_NAME" -g "$USER_NAME" "$STATE"
install -d -m 0750 -o "$USER_NAME" -g "$USER_NAME" /var/log/mcp-server-shieldtv
install -m 0755 "$HERE/mcp-server-shieldtv.initd" /etc/init.d/mcp-server-shieldtv

AS_SERVICE="su -s /bin/sh $USER_NAME -c"
CMD="SHIELDTV_CONFIG_DIR=$STATE $PREFIX/venv/bin/mcp-server-shieldtv"
echo "Installed $("$PREFIX/venv/bin/mcp-server-shieldtv" --version)."
echo "Next: pair as the service user (a code appears on the TV), check, then start:"
echo "  $AS_SERVICE '$CMD pair --host <shield ip>'"
echo "  $AS_SERVICE '$CMD doctor'"
echo "  rc-update add mcp-server-shieldtv default && rc-service mcp-server-shieldtv start"
