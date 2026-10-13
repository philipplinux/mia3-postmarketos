#!/bin/sh
# One-shot installer. Run ON the phone from a checkout of this repo, as the normal user (uses sudo).
# Usage: sh install.sh [--no-kernel-module] [--no-shell] [--dry-run]
set -eu

KMOD=1 SHELLSETUP=1 DRY=0
for a in "$@"; do
    case $a in
        --no-kernel-module) KMOD=0 ;;
        --no-shell) SHELLSETUP=0 ;;
        --dry-run) DRY=1 ;;
        -h|--help) sed -n '2,3p' "$0"; exit 0 ;;
        *) echo "unknown flag: $a" >&2; exit 2 ;;
    esac
done

R=$(cd "$(dirname "$0")" && pwd)
ME=$(id -un)
KDIR=${KDIR:-$HOME/projects/pmi632-chg/src/linux}
DASH=$HOME/projects/mia3-dashboard/dashboard

say() { printf '\n==> %s\n' "$*"; }
run() {
    printf '+ %s\n' "$*"
    [ "$DRY" = 1 ] || "$@"
}
# write_default FILE CONTENT: create FILE (as root) only if absent
write_default() {
    if [ -e "$1" ]; then echo "keep existing $1"; return; fi
    printf '+ write %s: %s\n' "$1" "$2"
    [ "$DRY" = 1 ] || printf '%s\n' "$2" | sudo tee "$1" >/dev/null
}

[ "$(id -u)" -ne 0 ] || { echo "run as the normal user, not root" >&2; exit 1; }
[ -f "$R/display/display.py" ] || { echo "run from the repo checkout" >&2; exit 1; }

say "apk packages"
run sudo apk add python3 py3-pillow font-opensans font-droid networkmanager-cli bluez bluez-deprecated \
    bluez-tools v4l-utils iputils py3-dbus-fast font-nerd-fonts-symbols \
    openssh-client nftables
[ "$KMOD" = 0 ] || run sudo apk add clang lld llvm make kmod
[ "$SHELLSETUP" = 0 ] || run sudo apk add bash starship fzf fd font-jetbrains-mono-nerd kmscon kmscon-systemd

say "display"
run sudo install -D -m755 "$R/display/display.py" /usr/local/lib/mia3-display/display.py
run sudo install -D -m644 "$R/display/mia3-display.service" /etc/systemd/system/mia3-display.service
if [ -f "$R/display/54_mia3_dashboard.nft" ]; then
    run sudo install -D -m644 "$R/display/54_mia3_dashboard.nft" /etc/nftables.d/54_mia3_dashboard.nft
    if systemctl is-active --quiet nftables; then
        if [ "$(systemctl show -p CanReload --value nftables)" = yes ]; then
            run sudo systemctl reload nftables
        else
            run sudo systemctl restart nftables
        fi
    fi
fi
run sudo systemctl daemon-reload
run sudo systemctl enable --now mia3-display.service
if [ "$SHELLSETUP" = 1 ]; then   # GPU terminal on tty2, third power-key view
    run sudo install -D -m644 "$R/display/kmsconvt@tty2.conf" /etc/systemd/system/kmsconvt@tty2.service.d/mia3.conf
    run sudo systemctl daemon-reload
    run sudo systemctl enable --now kmsconvt@tty2.service
fi

say "dashboard"
run sudo install -d -o "$ME" -g "$ME" /var/lib/mia3-dashboard /var/lib/chargectl
write_default /var/lib/chargectl/chargectl.json '{"target": 50, "drain": true}'
write_default /var/lib/mia3-dashboard/listen.json '{"wifi": false, "usb": false, "tailscale": false}'
run install -d "$DASH" "$HOME/.config/systemd/user"
for f in "$R"/dashboard/*.py "$R"/dashboard/index.html; do run install -m644 "$f" "$DASH/"; done
echo "+ sed /home/philipp -> $HOME into ~/.config/systemd/user/mia3-dashboard.service"
[ "$DRY" = 1 ] || sed "s|/home/philipp|$HOME|g" "$R/dashboard/mia3-dashboard.service" \
    > "$HOME/.config/systemd/user/mia3-dashboard.service"
run sudo loginctl enable-linger "$ME"
run systemctl --user daemon-reload
run systemctl --user enable --now mia3-dashboard.service

say "charging"
if [ "$KMOD" = 0 ]; then
    echo "skipped (--no-kernel-module)"
elif [ ! -f "$KDIR/Makefile" ]; then
    echo "skipped: no kernel source at KDIR=$KDIR (must match $(uname -r)); set KDIR= and rerun"
else
    run make -C "$R/charging" KDIR="$KDIR"
    run sudo install -D -m644 "$R/charging/pmi632_chg.ko" "/lib/modules/$(uname -r)/extra/pmi632_chg.ko"
    run sudo depmod -a
    run sudo install -D -m755 "$R/charging/chargectl.py" /usr/local/lib/chargectl/chargectl.py
    run sudo install -D -m644 "$R/charging/chargectl.service" /etc/systemd/system/chargectl.service
    run sudo systemctl daemon-reload
    run sudo systemctl enable --now chargectl.service
fi

say "thermal guard"
run sudo install -D -m755 "$R/thermal-guard/thermal-guard.py" /usr/local/lib/thermal-guard/thermal-guard.py
run sudo install -d -m700 /var/lib/thermal-guard
run sudo install -D -m644 "$R/thermal-guard/thermal-guard.service" /etc/systemd/system/thermal-guard.service
run sudo systemctl daemon-reload
run sudo systemctl enable --now thermal-guard.service
echo "note: NOTIFY= in the unit is left as shipped; edit /etc/systemd/system/thermal-guard.service for ssh notify"

say "shell"
if [ "$SHELLSETUP" = 0 ]; then
    echo "skipped (--no-shell)"
else
    if [ -e "$HOME/.bashrc" ] && ! cmp -s "$R/shell/.bashrc" "$HOME/.bashrc"; then
        run cp -p "$HOME/.bashrc" "$HOME/.bashrc.bak"
    fi
    run install -m644 "$R/shell/.bashrc" "$HOME/.bashrc"
    run install -m644 "$R/shell/.bash_profile" "$HOME/.bash_profile"
    run install -d "$HOME/.bashrc.d" "$HOME/.config"
    for f in "$R"/shell/bashrc.d/*; do run install -m644 "$f" "$HOME/.bashrc.d/"; done
    run install -m644 "$R/shell/starship.toml" "$HOME/.config/starship.toml"
    [ -d "$HOME/.local/share/blesh" ] || echo "note: ble.sh not installed, see README 'Shell' (github.com/akinomyoga/ble.sh -> ~/.local/share/blesh)"
    [ -f "$HOME/.config/starship-plain.toml" ] || run sh -c "starship preset plain-text-symbols -o '$HOME/.config/starship-plain.toml'"
fi

say "not automated"
echo "- Bluetooth agent: not installed (the display has its own pairing popup); bluetooth/bt-agent.service is for display-less setups"
echo "- Firmware: README 'Kernel and pmaports' (firmware/extract-firmware.sh, as root)"
echo "- Kernel/dtb: README 'Boot image with the dtb' (flash from a PC with fastboot)"

say "service states"
for u in mia3-display chargectl thermal-guard nftables kmsconvt@tty2; do
    printf '%-16s %s\n' "$u" "$(systemctl is-active "$u" 2>&1 || true)"
done
printf '%-16s %s\n' mia3-dashboard "$(systemctl --user is-active mia3-dashboard 2>&1 || true)"
