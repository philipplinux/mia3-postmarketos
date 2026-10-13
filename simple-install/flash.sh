#!/bin/sh
# Flash a built image onto an unlocked Mi A3 in fastboot mode: slot b + userdata, stock Android stays on slot a.
# Usage: sh flash.sh [dir with boot.img rootfs.img vbmeta.img]   (default: this script's dir). Needs only fastboot.
# FLASH=yes skips the confirmation prompt.
set -eu
D=${1:-$(cd "$(dirname "$0")" && pwd)}
die() { echo "ERROR: $*" >&2; exit 1; }

for f in boot.img rootfs.img vbmeta.img; do [ -f "$D/$f" ] || die "no $D/$f"; done
command -v fastboot >/dev/null || die "fastboot not found (Android platform-tools)"
fastboot devices | grep -q fastboot || die "no phone in fastboot mode (power off, then Volume down + Power, USB plugged in)"
[ "$(fastboot devices | grep -c fastboot)" = 1 ] || die "more than one phone in fastboot mode, unplug the others"
p=$(fastboot getvar product 2>&1 | sed -n 's/^product: *//p')
[ "$p" = laurel_sprout ] || die "phone reports product '$p', expected laurel_sprout (Mi A3)"
fastboot getvar unlocked 2>&1 | grep -q 'unlocked: yes' || die "bootloader is locked (fastboot flashing unlock, wipes the phone)"
if [ "${FLASH:-}" != yes ]; then
    printf 'This ERASES the phone (userdata, slot b). Type yes to flash: '
    read -r a; [ "$a" = yes ] || die "aborted"
fi
fastboot flash vbmeta_b "$D/vbmeta.img"
fastboot erase dtbo_b
fastboot flash userdata "$D/rootfs.img"
fastboot flash boot_b "$D/boot.img"
fastboot set_active b
fastboot reboot
echo "flashed; first boot takes about a minute, then: ssh <user>@172.16.42.1 over USB"
