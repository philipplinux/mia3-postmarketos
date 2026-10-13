# PMI632 fuel gauge (QGauge), read-only

Exposes the PMI632 QG block as `/sys/class/power_supply/mia3-qg`. Adapted from Marc Lainez's `qcom_pmi632_qg.c`
([msm8953-mainline/linux PR #242](https://github.com/msm8953-mainline/linux/pull/242), unmerged, GPL-2.0). All register writes,
IRQs, FIFO hold and SOC-from-OCV are dropped: this version only reads, never writes a PMIC register.

| File | What |
|---|---|
| `mia3_qg.c`, `Makefile` | the module; `make KDIR=<kernel source>` (needs the running kernel's `/boot/config`, `make modules_prepare`, pahole; built with `LLVM=1 ARCH=arm64`) |
| `mia3-qg.dts` | overlay adding `fuel-gauge@4800` under `pmic@2` (stock `qcom,qgauge@4800`), applied by `mia3_dtbo.ko` (see `audio/`) |

Boot: `mia3-qg.service` (installed by `install.sh`) applies the overlay via `mia3_dtbo` after tsens/audio, then `modprobe mia3_qg` (`/lib/modules/$(uname -r)/extra/`). A new kernel version needs a rebuild; until then the service fails and the dashboard falls back to the ADC estimate (same OCV table).
The overlay stays until reboot. The module borrows the regmap of the `typec@1500` sibling device (same trick as `pmi632_chg`).

Capacity: `ocv-capacity-table-0` in `mia3-qg.dts` is the stock `qcom,pc-temp-v1-lut` (25 degC column) of profile `S88512_mtp_Sunwoda_4V4_4030mAh` from the stock dtbo (`dtbo_a_16`, private repo `stock/dt/`); the Feimaotui and default 4030 mAh profiles are within 15 mV. `capacity` = `power_supply_batinfo_ocv2cap(vbat - I x rbatt)` at a fixed 25 degC; `rbatt_mohm` module param (default 150, the profile has no ESR) is writable at `/sys/module/mia3_qg/parameters/`. No coulomb counting yet. A second overlay on the same node merges (no reboot needed to change the table).

Properties: `present`, `voltage_now` (LAST_ADC_V), `voltage_boot` (S7 PON OCV), `voltage_ocv` (S3 good OCV, -ENODATA until the
QG took one), `current_now` (LAST_ADC_I), `current_avg` (burst average), `capacity`. Voltage raw x 194637 nV, current raw x 152588 pA,
sign flipped to the power_supply ABI (negative = discharge).

Measured 2026-10-13 (usb_suspend=1, on battery): `voltage_now` 3.800/3.789/3.815/3.813/3.814/3.817 V vs IIO `vbat_sns` 3.780/3.794/3.809/3.808/3.812/3.814 V
(within ~20 mV, sampled a moment apart); `current_now` about -290 to -450 mA. Charging-direction sign not yet checked.

Kernel gotcha: module `struct module` size depends on config, so the build tree needs `CONFIG_PAHOLE_VERSION` = the phone's, else insmod says
".gnu.linkonce.this_module section size must match".

## Kernel tree and build

1. `curl -L https://codeberg.org/sm61x5-mainline/linux/archive/v7.2.3-r0.tar.gz | tar xz` (kept at `~/projects/mia3-kernel/linux-7.2.3-r0` on the PC, prepared with `modules_prepare`; the Makefile's default `KDIR`).
2. `ssh mia3 cat /boot/config > linux/.config`, then in the tree: `make ARCH=arm64 LLVM=1 olddefconfig modules_prepare` (needs `pahole`).
3. `make KDIR=<tree>` here. The host has no clang: it was built in `podman run alpine:edge` with `apk add clang lld llvm make flex bison openssl-dev bc perl elfutils-dev python3 diffutils linux-headers musl-dev gcc libgcc pahole`, tree mounted at `/src`.
4. Phone has `dtc`: compile the `.dts` there.

Capacity check 2026-10-13 (chargectl holding, current ~0): `capacity` 47 % at 3.862 V; the dashboard/chargectl table gives ~54 % for the same voltage.
Current sign verified 2026-10-13: +140 to 260 mA while charging, 0 mA in chargectl hold.
