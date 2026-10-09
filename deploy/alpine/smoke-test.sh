#!/bin/sh
# CI: install and run the service in a stock python:3.12-alpine container,
# against the simulated Shield. Proves the dependencies install on musl
# (cryptography, protobuf, zeroconf, pydantic-core), the install layout and
# permissions, and that OpenRC starts and stops it.
#   docker run --rm -v "$PWD:/src" -w /src python:3.12-alpine sh deploy/alpine/smoke-test.sh
set -eux

apk add --no-cache openrc curl >/dev/null
sh deploy/alpine/install.sh /src

# The full test suite on musl, from the installed venv
/opt/mcp-server-shieldtv/venv/bin/pip install --quiet "/src[dev]"
cd /src && /opt/mcp-server-shieldtv/venv/bin/pytest -q -p no:cacheprovider && cd /

STATE=/var/lib/mcp-server-shieldtv
BIN=/opt/mcp-server-shieldtv/venv/bin/mcp-server-shieldtv
# A simulated Shield, already paired, writing its credentials as the service user
su -s /bin/sh mcp-shieldtv -c "$BIN simulate --config-dir $STATE --paired" &
for _ in $(seq 100); do grep -q '"mac"' "$STATE/config.json" 2>/dev/null && break; sleep 0.1; done

# OpenRC in a container: it needs to believe it has booted, and that the
# network (which the service `need`s) is provided by the host
printf 'rc_sys="docker"\nrc_provide="loopback net"\n' >>/etc/rc.conf
mkdir -p /run/openrc && touch /run/openrc/softlevel
rc-service mcp-server-shieldtv start
for _ in $(seq 50); do curl -fsS http://127.0.0.1:8712/healthz && break; sleep 0.2; done
rc-service mcp-server-shieldtv status

# The process runs as the service user, not root (busybox has no pgrep -u with -f)
# shellcheck disable=SC2009
ps -o user,args | grep '[m]cp-server-shieldtv serve' | grep -q '^mcp-shieldtv'
# The credentials are the service user's alone
[ "$(stat -c %a $STATE)" = 700 ]
[ "$(stat -c '%a %U' $STATE/key.pem)" = "600 mcp-shieldtv" ]
[ "$(stat -c '%a %U' $STATE/cert.pem)" = "600 mcp-shieldtv" ]
# doctor as the service user, against the simulator
su -s /bin/sh mcp-shieldtv -c "SHIELDTV_CONFIG_DIR=$STATE $BIN doctor --no-mdns --timeout 2"

rc-service mcp-server-shieldtv stop
cat /var/log/mcp-server-shieldtv/server.log
echo "Alpine smoke test passed"
