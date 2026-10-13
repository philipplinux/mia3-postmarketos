#!/bin/sh
# One shot from the PC: builds the out-of-tree kernel modules in docker, copies this repo (and the private
# firmware, if checked out next to it) to the phone, then runs install.sh there (asks for the phone's sudo password).
# Usage: sh deploy.sh [ssh-host] [install.sh flags]   default host mia3; env KDIR=, PRIVATE=, NOBUILD=1
set -eu

R=$(cd "$(dirname "$0")" && pwd)
H=${1:-mia3}; [ $# -eq 0 ] || shift
KDIR=${KDIR:-$HOME/projects/mia3-kernel/linux-7.2.3-r0}
PRIVATE=${PRIVATE:-$R/../mia3-postmarketos-private}

if [ "${NOBUILD:-0}" != 1 ]; then
    [ -f "$KDIR/Makefile" ] || { echo "no kernel tree at KDIR=$KDIR (README 'Kernel and pmaports')" >&2; exit 1; }
    docker run --rm -e U="$(id -u):$(id -g)" -v "$KDIR:/src:Z" -v "$R:/m:Z" alpine:edge sh -ec '
        apk add -q clang lld llvm make flex bison openssl-dev bc perl elfutils-dev python3 diffutils linux-headers \
            musl-dev gcc libgcc pahole
        export ARCH=arm64 LLVM=1 KBUILD_MODPOST_WARN=1   # cross build: the PC is x86
        for d in charging battery audio; do make -s -C /m/$d KDIR=/src; done
        cp "$(KDIR=/src sh /m/thermal-sensors/build.sh | tail -n1)" /m/thermal-sensors/
        chown -R "$U" /m/charging /m/battery /m/audio /m/thermal-sensors /src   # container runs as root'
fi

rsync -a --delete --exclude .git "$R/" "$H:projects/mia3-postmarketos/"
FW=
if [ -d "$PRIVATE/firmware" ]; then
    rsync -a "$PRIVATE/firmware/" "$H:projects/mia3-firmware/"
    FW='sudo cp -an ~/projects/mia3-firmware/. /lib/firmware/ &&'   # -n: never overwrites packaged firmware
else
    echo "no private firmware at $PRIVATE, skipped"
fi
ssh -t "$H" "$FW cd projects/mia3-postmarketos && sh install.sh $*"
