#!/bin/sh
# Runs inside the mia3-build container (started by mia3.sh). Steps: fetch, build, flash, check; "all" runs them in order.
set -eu

ROM_URL=https://cdnorg.d.miui.com/V12.0.26.0.RFQMIXM/laurel_sprout_global_images_V12.0.26.0.RFQMIXM_20220808.0000.00_11.0_2292e2c0b1.tgz
ROM_MD5=2292e2c0b1   # Xiaomi names the file after the first 10 hex digits of its md5
BLESH_URL=https://github.com/akinomyoga/ble.sh/releases/download/v0.4.0-devel3/ble-0.4.0-devel3.tar.xz
BLESH_SHA256=c8612ee612bc6b10dbfd6e85c6cbdfd7caf152a12d1f9de22ea0a9d735b3080c
PMAPORTS_COMMIT=ec98dddc8d525c874603a8e91ae6113d2b0c66d1
DEV=xiaomi-laurel
R=/repo S=/repo/simple-install C=/cache W=/work
PA=$W/pmaports PW=$W/pmb OUT=$C/out SSH_KEYS=True K=$PA/device/testing/linux-nura-qcom-sm61x5

say() { printf '\n\033[1;36m== %s ==\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
pmb() { sudo -u build -H pmbootstrap "$@"; }

# Official ROM -> firmware tarball (modem, CDSP, ADSP, Wi-Fi, GPU zap, Bluetooth), via the same script used on the phone
fetch() {
    [ -s "$C/fw.tar" ] && { echo "have $C/fw.tar"; return; }
    if [ ! -f "$C/rom.tgz" ]; then
        say "download official ROM (2.4 GB)"
        curl -fL -C - -o "$C/rom.tgz.part" "$ROM_URL"
        mv "$C/rom.tgz.part" "$C/rom.tgz"
    fi
    say "verify + unpack ROM"
    [ "$(md5sum < "$C/rom.tgz" | cut -c1-10)" = "$ROM_MD5" ] || die "rom.tgz md5 mismatch, delete $C/rom.tgz and retry"
    rm -rf "$C/rom" "$C/fw" && mkdir -p "$C/rom"
    tar -xzf "$C/rom.tgz" -C "$C/rom" --strip-components=2 --wildcards \
        '*/images/NON-HLOS.bin' '*/images/BTFM.bin' '*/images/vendor.img'
    mv "$C/rom/NON-HLOS.bin" "$C/rom/modem_a"
    mv "$C/rom/BTFM.bin" "$C/rom/bluetooth_a"
    simg2img "$C/rom/vendor.img" "$C/rom/vendor_a" && rm "$C/rom/vendor.img"
    say "extract firmware"
    L=$C/rom FW=$C/fw/fw sh "$R/firmware/extract-firmware.sh"
    tar -C "$C/fw" -cf "$C/fw.tar" fw
    rm -rf "$C/rom" "$C/fw"
}

pmaports() {
    say "pmaports at ${PMAPORTS_COMMIT%"${PMAPORTS_COMMIT#???????}"} + this repo's changes"
    # pmbootstrap insists on a remote pointing at the official URL
    [ -d "$PA/.git" ] || { git init -q "$PA" && git -C "$PA" remote add origin https://gitlab.postmarketos.org/postmarketOS/pmaports.git; }
    git -C "$PA" fetch -q --depth 1 origin "$PMAPORTS_COMMIT"
    git -C "$PA" update-ref refs/remotes/origin/main FETCH_HEAD   # pmbootstrap reads channels.cfg from origin/main
    git -C "$PA" checkout -qfB main FETCH_HEAD && git -C "$PA" clean -qfdx
    git -C "$PA" apply "$R/pmaports/pmaports-ec98ddd.diff"
    cp "$R"/pmaports/linux-nura-qcom-sm61x5/*.patch "$K/"
    # k7.dtb (cpufreq, Bluetooth, IR, Wi-Fi) replaces the kernel's laurel dtb, so boot.img gets it appended
    cp "$R/dts/k7.dtb" "$K/"
    sed -i 's/^\tconfig-\$_flavor\.aarch64$/&\n\tk7.dtb/' "$K/APKBUILD"
    sed -i '/usr\/lib\/modules\/\*\/build/a\	install -Dm644 "$srcdir/k7.dtb" "$pkgdir/boot/dtbs/qcom/sm6125-xiaomi-laurel-sprout.dtb"' "$K/APKBUILD"
    grep -q 'srcdir/k7.dtb' "$K/APKBUILD" || die "k7.dtb patch into the kernel APKBUILD failed"
    rm -rf "$PA/device/testing/firmware-xiaomi-laurel-cdsp"
    cp -r "$S/aports/firmware-xiaomi-laurel-cdsp" "$PA/device/testing/"
    cp "$C/fw.tar" "$PA/device/testing/firmware-xiaomi-laurel-cdsp/"
    mkdir -p "$PA/temp" && cp -r "$S/aports/mia3-simple" "$PA/temp/"
    : > "$PA/temp/mia3-simple/wifi.nmconnection"
    if [ -n "$WIFI_SSID" ]; then
        printf '[connection]\nid=mia3-wifi\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=%s\n\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n' \
            "$WIFI_SSID" "$WIFI_PSK" > "$PA/temp/mia3-simple/wifi.nmconnection"
    fi
}

# Out-of-tree modules (charging, fuel gauge, audio, thermal sensors) against the image's kernel
modules() {
    say "kernel modules (charging, fuel gauge, audio, thermal sensors)"
    KDIR=$W/kernel/linux
    if [ ! -f "$KDIR/scripts/mod/modpost" ]; then
        rm -rf "$W/kernel" && mkdir -p "$W/kernel"
        tar -xzf "$PW/cache_distfiles/linux-nura-qcom-sm61x5-v7.2.3-r0.tar.gz" -C "$W/kernel"
        cp "$K/config-nura-qcom-sm61x5.aarch64" "$KDIR/.config"
        make -s -C "$KDIR" ARCH=arm64 LLVM=1 olddefconfig modules_prepare
    fi
    export ARCH=arm64 LLVM=1 KBUILD_MODPOST_WARN=1
    for d in charging battery audio; do make -s -C "$R/$d" KDIR="$KDIR"; done
    cp "$(KDIR=$KDIR sh "$R/thermal-sensors/build.sh" | tail -n1)" "$R/thermal-sensors/"
    # quiet/xo-thermal: adc-tm5 without the stale IIO_VAL_INT check ("Unable to get temperature, disabling!")
    cp "$(KDIR=$KDIR sh "$R/thermal-sensors/build-adc-tm5.sh" | tail -n1)" "$R/thermal-sensors/"
    # speaker: QDSP6 + sm8250 card drivers, not enabled in the kernel config (audio/README.md "Speaker card")
    q=SND_SOC_QDSP6; make -s -C "$KDIR" M=sound/soc/qcom/qdsp6 CONFIG_${q}_COMMON=m CONFIG_${q}_CORE=m CONFIG_${q}_AFE=m \
        CONFIG_${q}_AFE_DAI=m CONFIG_${q}_AFE_CLOCKS=m CONFIG_${q}_ADM=m CONFIG_${q}_ROUTING=m CONFIG_${q}_ASM=m \
        CONFIG_${q}_ASM_DAI=m CONFIG_${q}_USB= modules
    make -s -C "$KDIR" M=sound/soc/qcom CONFIG_SND_SOC_SM8250=m CONFIG_SND_SOC_QCOM_SDW=m CONFIG_${q}_USB= modules
    chown -R "$(stat -c %u:%g "$R")" "$R/charging" "$R/battery" "$R/audio" "$R/thermal-sensors"
}

# install.sh's layout as a file tree baked into mia3-simple (/usr/lib instead of /usr/local, which abuild rejects;
# the units are rewritten to match): no sudo/systemctl here, the package's post-install
# enables the units and tmpfiles.d hands the state dirs to $PMOS_USER at boot (the user doesn't exist yet at package time)
stage() {
    say "stage dashboard, display, charging, fuel gauge, sensors, thermal guard, shell"
    X=$W/extras/extras U=$PMOS_USER
    KDIR=$W/kernel/linux; kr=$(make -s -C "$KDIR" kernelrelease) E=/usr/lib/modules/$kr/extra
    rm -rf "$W/extras" && mkdir -p "$X"
    i() { m=$1 d=$2; shift 2; install -d "$X$d" && install -m"$m" "$@" "$X$d/"; }
    install -d "$X/usr/lib/mia3-display"
    sed -e "s|/home/philipp/projects/mia3-dashboard/dashboard|/usr/lib/mia3-dashboard|" -e "s|/home/philipp|/home/$U|g" \
        "$R/display/display.py" > "$X/usr/lib/mia3-display/display.py" && chmod 755 "$X/usr/lib/mia3-display/display.py"
    i 644 /etc/nftables.d "$R/display/54_mia3_dashboard.nft"
    i 644 /usr/lib/mia3-dashboard "$R"/dashboard/*.py "$R/dashboard/index.html"
    i 755 /usr/lib/chargectl "$R/charging/chargectl.py"
    i 755 /usr/lib/thermal-guard "$R/thermal-guard/thermal-guard.py"
    i 644 "$E" "$R/charging/pmi632_chg.ko" "$R/battery/mia3_qg.ko"
    i 644 "$E/mia3-tsens" "$R/thermal-sensors/mia3_tsens.ko" "$R/audio/mia3_dtbo.ko"
    i 644 "/usr/lib/modules/$kr/updates" "$R/thermal-sensors/qcom-spmi-adc-tm5.ko"
    i 644 /usr/lib/mia3-audio "$R"/audio/*.ko
    i 755 /usr/lib/mia3-audio "$R/audio/bringup.sh" "$R/audio/max98937-init.sh"
    i 644 "$E/mia3-audio" "$KDIR"/sound/soc/qcom/qdsp6/*.ko "$KDIR"/sound/soc/qcom/snd-soc-sm8250.ko \
        "$KDIR"/sound/soc/qcom/snd-soc-qcom-sdw.ko
    i 755 /usr/bin "$R/audio/mia3-chime"
    i 644 /usr/lib/udev/rules.d "$R/audio/60-mia3-dashboard.rules"
    install -d "$X/usr/lib/firmware"
    for o in battery/mia3-qg audio/mia3-adsp audio/mia3-apr audio/mia3-speaker thermal-sensors/mia3-tsens; do
        dtc -q -I dts -O dtb -o "$X/usr/lib/firmware/${o#*/}.dtbo" "$R/$o.dts"
    done
    i 644 /etc/systemd/system "$R/charging/chargectl.service" "$R/battery/mia3-qg.service" \
        "$R/thermal-sensors/mia3-tsens.service" "$R/audio/mia3-audio.service" "$R/thermal-guard/thermal-guard.service"
    sed -i 's|/usr/local/lib/|/usr/lib/|g' "$X"/etc/systemd/system/*.service
    install -d "$X/etc/systemd/user"
    sed -e "s|/home/philipp|/home/$U|g" -e 's|/usr/local/lib/|/usr/lib/|g' "$R/display/mia3-display.service" > "$X/etc/systemd/system/mia3-display.service"
    sed -e "s|^WorkingDirectory=.*|WorkingDirectory=/usr/lib/mia3-dashboard|" \
        -e "s|/home/philipp/projects/mia3-dashboard/dashboard|/usr/lib/mia3-dashboard|" \
        "$R/dashboard/mia3-dashboard.service" > "$X/etc/systemd/user/mia3-dashboard.service"
    for t in 1 2; do   # tty1: plain console, tty2: sway (tabs tty2-4), both autologin
        install -d "$X/etc/systemd/system/getty@tty$t.service.d"
        sed "s/--autologin philipp/--autologin $U/" "$R/display/getty-autologin.conf" \
            > "$X/etc/systemd/system/getty@tty$t.service.d/autologin.conf"
    done
    install -d "$X/usr/lib/tmpfiles.d"
    cat > "$X/usr/lib/tmpfiles.d/mia3.conf" <<EOF
d /var/lib/chargectl 0755 $U $U -
f /var/lib/chargectl/chargectl.json 0644 $U $U - {"target": 50}
d /var/lib/mia3-dashboard 0755 $U $U -
f /var/lib/mia3-dashboard/listen.json 0644 $U $U - {"wifi": false, "usb": false, "tailscale": false}
d /var/lib/mia3-audio 0755 $U $U -
d /var/lib/thermal-guard 0700 root root -
f /var/lib/systemd/linger/$U 0644 root root -
EOF
    # shell + sway, copied into the new user's home by adduser
    i 644 /etc/skel "$R/shell/.bashrc" "$R/shell/.bash_profile"
    i 644 /etc/skel/.bashrc.d "$R"/shell/bashrc.d/*
    i 644 /etc/skel/.config "$R/shell/starship.toml" "$R/shell/starship-plain.toml"
    i 644 /etc/skel/.config/sway "$R/sway/config"
    i 755 /etc/skel/.config/sway "$R/sway/ttybar.py" "$R/sway/respawn.sh"
    i 644 /etc/skel/.config/foot "$R/sway/foot.ini"
    # pmbootstrap gives the user ash, which skips .bash_profile (sway on tty2, starship, ble.sh): hand logins to bash
    printf '%s\n' 'case $- in *i*) [ -z "$BASH_VERSION" ] && command -v bash >/dev/null && exec bash -l;; esac' > "$X/etc/skel/.profile"
    [ -s "$C/blesh.tar.xz" ] || curl -fsSL -o "$C/blesh.tar.xz" "$BLESH_URL"
    echo "$BLESH_SHA256  $C/blesh.tar.xz" | sha256sum -c - >/dev/null || die "blesh.tar.xz checksum mismatch, delete it and retry"
    install -d "$X/etc/skel/.local/share/blesh"
    tar -C "$X/etc/skel/.local/share/blesh" --strip-components=1 -xJf "$C/blesh.tar.xz"
    tar -C "$W/extras" -cf "$PA/temp/mia3-simple/extras.tar" extras
}

build() {
    fetch
    pmaports
    install -d -o build -g build "$PW" /home/build/.config
    cat > /home/build/.config/pmbootstrap_v3.cfg <<EOF
[pmbootstrap]
aports = $PA
work = $PW
device = $DEV
ui = console
service_manager = systemd
user = $PMOS_USER
hostname = mia3
extra_packages = openssh,linux-firmware-ath10k,firmware-qcom-adreno-a630-sqe
locale = en_IE.UTF-8
timezone = Europe/Dublin
ssh_keys = $SSH_KEYS
ssh_key_glob = $C/ssh/*.pub
EOF
    chown -R build:build "$PA" /home/build/.config
    say "pmbootstrap init"
    printf '\n%.0s' $(seq 40) | pmb init >/dev/null
    pmb checksum linux-nura-qcom-sm61x5 firmware-xiaomi-laurel-cdsp
    say "kernel (first run: 30-60 min)"
    pmb build linux-nura-qcom-sm61x5   # modules() needs its source tarball
    modules
    stage
    chown -R build:build "$PA/temp"
    pmb checksum mia3-simple
    pmb build --force mia3-simple   # its files (Wi-Fi profile) change between builds without a version bump
    pmb build --force firmware-xiaomi-laurel-cdsp   # fw.tar can change (ADSP added) without a version bump
    pmb -y zap                      # fresh rootfs; built packages stay
    say "pmbootstrap install"
    pmb -y install --password "$PASSWORD" --add mia3-simple
    pmb shutdown
    say "export images -> $OUT"
    mkdir -p "$OUT"
    cp "$PW/chroot_rootfs_$DEV/boot/boot.img" "$OUT/boot.img"
    cp "$PW/chroot_native/home/pmos/rootfs/$DEV.img" "$OUT/rootfs.img"
    avbtool make_vbmeta_image --flags 2 --padding_size 4096 --output "$OUT/vbmeta.img"
    # an image without filesystems (loop-partition trouble) would boot to the initramfs debug shell
    LC_ALL=C grep -qa -m1 -E 'postmarketos|alpine-baselayout' "$OUT/rootfs.img" || die "rootfs.img is empty, don't flash it"
    ls -lh "$OUT"
}

flash() {
    [ -f "$OUT/rootfs.img" ] || die "no images in $OUT, run build first"
    [ -t 0 ] || [ "${FLASH:-}" = yes ] || die "set FLASH=yes to flash without a terminal"
    sh "$S/flash.sh" "$OUT"
}

# Wait for the phone over USB and show that the baked-in services came up
check() {
    install -d -m700 /root/.ssh
    [ -f "$C/ssh/id_ed25519" ] || die "no $C/ssh/id_ed25519 (made by build) to log in with"
    cat > /root/.ssh/config <<EOF
Host mia3-usb
    HostName 172.16.42.1
    User $PMOS_USER
    IdentityFile $C/ssh/id_ed25519
    StrictHostKeyChecking accept-new
    UserKnownHostsFile $C/ssh/known_hosts
    ConnectTimeout 5
EOF
    say "wait for the phone at 172.16.42.1 (first boot ~1 min)"
    i=0
    until ssh -o BatchMode=yes mia3-usb true 2>/dev/null; do
        i=$((i + 1)); [ $i -le 60 ] || die "phone not reachable over USB after 5 min"
        # host side of the USB link. Only the Mi A3's interface, and a /32 route, since other pmOS phones on USB use 172.16.42.1 too
        for n in /sys/class/net/*; do
            [ "$(cat "$n/device/../product" 2>/dev/null)" = "Xiaomi Mi A3" ] || continue
            d=${n##*/}
            ip link set "$d" up
            ip addr replace 172.16.42.2/32 dev "$d"
            ip route replace 172.16.42.1/32 dev "$d" src 172.16.42.2
            trap "ip route del 172.16.42.1/32 dev $d 2>/dev/null" EXIT
        done
        sleep 5
    done

    say "service states"
    ssh mia3-usb 'for u in mia3-display chargectl mia3-qg mia3-tsens thermal-guard rmtfs tqftpserv NetworkManager; do
        printf "%-16s %s\n" $u "$(systemctl is-active $u)"; done
        printf "%-16s %s\n" mia3-dashboard "$(systemctl --user is-active mia3-dashboard)"'
}

# Release for anyone: no SSH keys, no Wi-Fi, user/changeme, packed with flash.sh
generic() {
    PMOS_USER=user PASSWORD=changeme WIFI_SSID= WIFI_PSK= SSH_KEYS=False OUT=$C/out-generic
    build
    n=mia3-postmarketos-generic-$(date +%Y%m%d)
    cp "$S/flash.sh" "$OUT/"
    printf 'Mi A3 postmarketOS, generic build %s\nLogin: user / changeme (change it: passwd)\nFlash: sh flash.sh  (phone unlocked, in fastboot mode)\nThen: ssh user@172.16.42.1 over USB\nhttps://github.com/philipplinux/mia3-postmarketos\n' "$(date +%F)" > "$OUT/README.txt"
    (cd "$OUT" && sha256sum boot.img rootfs.img vbmeta.img > SHA256SUMS)
    say "pack $n.tar.xz"
    tar -C "$C" --transform "s,^out-generic,$n," -cJf "$C/$n.tar.xz" \
        out-generic/boot.img out-generic/rootfs.img out-generic/vbmeta.img out-generic/flash.sh out-generic/README.txt out-generic/SHA256SUMS
    ls -lh "$C/$n.tar.xz"
}

keygen() {
    [ -f "$C/ssh/id_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -C mia3-simple-install -f "$C/ssh/id_ed25519"
}

case ${1:-all} in
    fetch) fetch ;;
    build) keygen; build ;;
    flash) flash ;;
    check) check ;;
    all) keygen; build; flash; check ;;
    generic) generic ;;
    shell) exec sh ;;
    *) die "usage: build.sh all|fetch|build|flash|check|generic|shell" ;;
esac
