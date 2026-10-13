#!/bin/sh
# One command: build the Mi A3 postmarketOS image in Docker and flash it.
# Usage: sh simple-install/mia3.sh [all|fetch|build|flash|check|generic|shell]   (default all)
# Env: PASSWORD (phone user password, default changeme), WIFI_SSID + WIFI_PSK (optional, baked in), PMOS_USER (philipp)
set -eu

H=$(cd "$(dirname "$0")" && pwd)
R=$(dirname "$H")
mkdir -p "$H/cache/ssh"
cp -n "$HOME"/.ssh/*.pub "$H/cache/ssh/" 2>/dev/null || true   # your public keys go into authorized_keys

docker build -q -t mia3-build "$H" >/dev/null
T=; [ -t 0 ] && T=-t
exec docker run --rm -i $T --privileged --net=host \
    -v mia3-work:/work -v "$R:/repo" -v "$H/cache:/cache" \
    -e PASSWORD="${PASSWORD:-changeme}" -e PMOS_USER="${PMOS_USER:-philipp}" \
    -e WIFI_SSID="${WIFI_SSID:-}" -e WIFI_PSK="${WIFI_PSK:-}" \
    mia3-build sh /repo/simple-install/build.sh "${1:-all}"
