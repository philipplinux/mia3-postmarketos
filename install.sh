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
# apk exits 1 ("1 error") when the world holds locally built packages (firmware, kernel) that no repo has
apk_add() { run sudo apk add "$@" || echo "apk reported an error (locally built packages?), continuing"; }
apk_add python3 py3-pillow font-opensans font-droid networkmanager-cli bluez bluez-deprecated \
    bluez-tools v4l-utils iputils py3-dbus-fast font-nerd-fonts-symbols \
    openssh-client nftables
[ "$KMOD" = 0 ] || apk_add clang lld llvm make kmod dtc i2c-tools alsa-utils
[ "$SHELLSETUP" = 0 ] || apk_add bash starship fzf fd font-jetbrains-mono-nerd sway foot wvkbd grim

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
if [ "$SHELLSETUP" = 1 ]; then   # sway on tty2 (tabs tty2-4, second power-key view), fbcon autologin on tty1
    for t in 1 2; do
        run sudo install -D -m644 "$R/display/getty-autologin.conf" "/etc/systemd/system/getty@tty$t.service.d/autologin.conf"
    done
    run sudo systemctl daemon-reload
    run sudo systemctl enable getty@tty1.service getty@tty2.service
    run sudo systemctl disable getty@tty3.service getty@tty4.service getty@tty5.service
    for t in 3 4 5; do   # older layouts had autologin there
        [ ! -d "/etc/systemd/system/getty@tty$t.service.d" ] || run sudo rm -r "/etc/systemd/system/getty@tty$t.service.d"
    done
fi

say "dashboard"
run sudo install -d -o "$ME" -g "$ME" /var/lib/mia3-dashboard /var/lib/chargectl
write_default /var/lib/chargectl/chargectl.json '{"target": 50}'
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
elif [ ! -f "$R/charging/pmi632_chg.ko" ] && [ ! -f "$KDIR/Makefile" ]; then
    echo "skipped: no prebuilt charging/pmi632_chg.ko (deploy.sh) and no kernel source at KDIR=$KDIR (must match $(uname -r))"
else
    [ -f "$R/charging/pmi632_chg.ko" ] || run make -C "$R/charging" KDIR="$KDIR"
    run sudo install -D -m644 "$R/charging/pmi632_chg.ko" "/lib/modules/$(uname -r)/extra/pmi632_chg.ko"
    run sudo depmod -a
    run sudo install -D -m755 "$R/charging/chargectl.py" /usr/local/lib/chargectl/chargectl.py
    run sudo install -D -m644 "$R/charging/chargectl.service" /etc/systemd/system/chargectl.service
    run sudo systemctl daemon-reload
    run sudo systemctl enable --now chargectl.service
fi

say "fuel gauge"
if [ "$KMOD" = 0 ] || [ ! -f "$R/battery/mia3_qg.ko" ]; then
    echo "skipped: build battery/mia3_qg.ko first (battery/README.md)"
else
    run sudo install -D -m644 "$R/battery/mia3_qg.ko" "/lib/modules/$(uname -r)/extra/mia3_qg.ko"
    run sudo depmod -a
    run sudo dtc -q -I dts -O dtb -o /lib/firmware/mia3-qg.dtbo "$R/battery/mia3-qg.dts"
    run sudo install -D -m644 "$R/battery/mia3-qg.service" /etc/systemd/system/mia3-qg.service
    run sudo systemctl daemon-reload
    run sudo systemctl enable mia3-qg.service
fi

say "speaker + temperature sensors"
X=/lib/modules/$(uname -r)/extra
if [ "$KMOD" = 0 ] || [ ! -f "$R/audio/mia3_dtbo.ko" ] || [ ! -f "$R/thermal-sensors/mia3_tsens.ko" ]; then
    echo "skipped: build the audio/ and thermal-sensors/ modules first (deploy.sh)"
else
    for o in audio/mia3-adsp audio/mia3-apr audio/mia3-speaker thermal-sensors/mia3-tsens; do
        run sudo dtc -q -I dts -O dtb -o "/lib/firmware/${o#*/}.dtbo" "$R/$o.dts"
    done
    run sudo install -d "$X/mia3-tsens" /usr/local/lib/mia3-audio
    run sudo install -m644 "$R/thermal-sensors/mia3_tsens.ko" "$R/audio/mia3_dtbo.ko" -t "$X/mia3-tsens"
    run sudo depmod -a
    run sudo install -m644 "$R"/audio/*.ko -t /usr/local/lib/mia3-audio
    run sudo install -m755 "$R/audio/bringup.sh" "$R/audio/max98937-init.sh" -t /usr/local/lib/mia3-audio
    run sudo install -D -m755 "$R/audio/mia3-chime" /usr/local/bin/mia3-chime
    run sudo install -m644 "$R/thermal-sensors/mia3-tsens.service" "$R/audio/mia3-audio.service" -t /etc/systemd/system
    run sudo systemctl daemon-reload
    run sudo systemctl enable mia3-tsens.service
    if [ -f "$R/thermal-sensors/qcom-spmi-adc-tm5.ko" ]; then   # quiet/xo-thermal fix, overrides the stock module
        run sudo install -D -m644 "$R/thermal-sensors/qcom-spmi-adc-tm5.ko" "/lib/modules/$(uname -r)/updates/qcom-spmi-adc-tm5.ko"
        run sudo depmod -a
    fi
    if [ -f "$X/mia3-audio/snd-soc-sm8250.ko" ]; then
        run sudo systemctl enable mia3-audio.service
    else
        echo "mia3-audio not enabled: the q6/sm8250 modules are missing from $X/mia3-audio (audio/README.md 'Speaker card')"
    fi
fi

say "usb rebind"   # re-enumerates the USB gadget after boot so ssh over USB works without a replug
run sudo install -D -m644 "$R/simple-install/aports/mia3-simple/mia3-usb-rebind.service" /etc/systemd/system/mia3-usb-rebind.service
run sudo systemctl daemon-reload
run sudo systemctl enable mia3-usb-rebind.service

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
    run install -D -m644 "$R/sway/config" "$HOME/.config/sway/config"
    run install -D -m755 "$R/sway/ttybar.py" "$HOME/.config/sway/ttybar.py"
    run install -D -m755 "$R/sway/respawn.sh" "$HOME/.config/sway/respawn.sh"
    run install -D -m644 "$R/sway/foot.ini" "$HOME/.config/foot/foot.ini"
    echo "note: sway starts on tty2 at the next login there (sudo systemctl restart getty@tty2)"
    # pmbootstrap gives the user ash, which skips .bash_profile: hand interactive logins to bash
    [ -e "$HOME/.profile" ] || run sh -c "printf '%s\\n' 'case \$- in *i*) [ -z \"\$BASH_VERSION\" ] && command -v bash >/dev/null && exec bash -l;; esac' > '$HOME/.profile'"
    if [ ! -d "$HOME/.local/share/blesh" ]; then
        run install -d "$HOME/.local/share/blesh"
        [ "$DRY" = 1 ] || curl -fsSL https://github.com/akinomyoga/ble.sh/releases/download/v0.4.0-devel3/ble-0.4.0-devel3.tar.xz \
            | tar -C "$HOME/.local/share/blesh" --strip-components=1 -xJf -
    fi
    [ -f "$HOME/.config/starship-plain.toml" ] || run sh -c "starship preset plain-text-symbols -o '$HOME/.config/starship-plain.toml'"
fi

say "not automated"
echo "- Bluetooth agent: not installed (the display has its own pairing popup); bluetooth/bt-agent.service is for display-less setups"
echo "- Firmware: deploy.sh copies it from the private repo, else README 'Kernel and pmaports' (firmware/extract-firmware.sh)"
echo "- q6/sm8250 audio modules (the simple-install image has them) in /lib/modules/\$(uname -r)/extra/mia3-audio: audio/README.md 'Speaker card'"
echo "- Kernel/dtb: README 'Boot image with the dtb' (flash from a PC with fastboot)"

say "service states"
for u in mia3-display mia3-tsens mia3-audio mia3-qg chargectl thermal-guard nftables mia3-usb-rebind getty@tty2; do
    printf '%-16s %s\n' "$u" "$(systemctl is-active "$u" 2>&1 || true)"
done
printf '%-16s %s\n' mia3-dashboard "$(systemctl --user is-active mia3-dashboard 2>&1 || true)"
