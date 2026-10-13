# Xiaomi Mi A3 (laurel_sprout, SM6125) on postmarketOS

A Mi A3 running postmarketOS edge as a headless home server with a touch dashboard on its own screen.
Tested on kernel `linux-nura-qcom-sm61x5` 7.2.3, booting slot b. No warranty: back up your boot image first.

| Folder | What |
|---|---|
| `dts/` | Device-tree with cpufreq-hw, Bluetooth (WCN3988 on GENI UART) and the IR blaster. `CPUFREQ.md` has the cpufreq-only write-up |
| `display/` | Framebuffer + touch UI (root service): status pages, Wi-Fi picker, Network page (SSH, Tailscale, USB modes, Bluetooth finder) |
| `dashboard/` | Web dashboard (user service, port 8090): CPU/GPU/battery/thermals/services/processes |
| `charging/` | `pmi632_chg` kernel module (charge on/off, USB suspend) + `chargectl.py` that holds the battery near 50 % |
| `thermal-guard/` | Powers the phone off on sustained overheating |
| `shell/` | bash + starship + ble.sh + fzf setup (plain-text prompt on the framebuffer console) |

## Boot image with the dtb

laurel uses `deviceinfo_append_dtb=true`, so the kernel in boot.img is `vmlinuz` + dtb.

1. `sudo cp dts/k7.dtb /boot/sm6125-xiaomi-laurel-sprout.dtb` (same file to `/boot/dtbs/qcom/`)
2. `sudo mkinitfs`
3. `systemctl reboot --reboot-argument=bootloader`, then from a PC `fastboot flash boot_b boot.img && fastboot reboot`

`fastboot boot` doesn't work on this bootloader. A kernel package upgrade replaces the dtb: re-apply after upgrades.
`k7.dts` is a decompiled dtb with raw phandles; they are only valid for that build, re-derive them for another kernel.

## Bluetooth

- Chip reports SOC `0x40070120`, ROM `0x0102` (Comanche). With `qcom,wcn3988-bt` the kernel asks for
  `qca/apbtfw0a.tlv` + `qca/apnv0a.bin`. Copy `cmbtfw12.tlv` / `cmnv12.bin` from the stock `bluetooth_a` partition
  into `/lib/firmware/qca/` and symlink them to those names.
- The controller has no address in OTP, so the dts sets `local-bd-address`. Change it to your own
  (locally administered, bytes reversed in the dts).
- `apk add bluez bluez-deprecated bluez-btmgmt && systemctl enable --now bluetooth`

## IR blaster

`ir-spi-led` on `spi@4a80000`, needs the GPI DMA controller `dma-controller@4a00000` enabled.
Test: `apk add v4l-utils; ir-ctl -d /dev/lirc0 -S nec:0x0408` (LG TV power).

## Services

- `display/mia3-display.service` → `/etc/systemd/system/`, script in `/usr/local/lib/mia3-display/`
- `dashboard/mia3-dashboard.service` → `~/.config/systemd/user/` (adjust the path in `ExecStart`)
- `charging/`: `make` builds the module against the running kernel; `chargectl.service` runs the controller
- `thermal-guard/`: set `NOTIFY=user@host` in the unit to get an ssh notification before power-off (key in
  `/var/lib/thermal-guard/notify_key`), empty = off

Each Python script has a `--selftest`.

## Shell

Copy `shell/.bashrc` and `shell/.bash_profile` to `~`, `shell/bashrc.d/` to `~/.bashrc.d/`, `shell/starship.toml` to `~/.config/`.
Install: `apk add bash starship fzf fd font-jetbrains-mono-nerd`, ble.sh from https://github.com/akinomyoga/ble.sh
into `~/.local/share/blesh`, and `starship preset plain-text-symbols -o ~/.config/starship-plain.toml` for the console.

## Gotchas

- Holding power ~12 s is a PMIC hardware reset (S2 timer), not a software restart.
- Don't `cat` every `/sys/kernel/debug/regmap/*/registers`: it hard-crashes the phone.
- `Unable to load a610_zap.mbn` at boot is the early probe before rootfs; the GPU comes up on first open.
