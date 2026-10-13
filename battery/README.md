# PMI632 fuel gauge (QGauge), read-only

Exposes the PMI632 QG block as `/sys/class/power_supply/mia3-qg`. Adapted from Marc Lainez's `qcom_pmi632_qg.c`
([msm8953-mainline/linux PR #242](https://github.com/msm8953-mainline/linux/pull/242), unmerged, GPL-2.0). All register writes,
IRQs, FIFO hold and SOC-from-OCV are dropped: this version only reads, never writes a PMIC register.

| File | What |
|---|---|
| `mia3_qg.c`, `Makefile` | the module; `make KDIR=<kernel source>` (needs the running kernel's `/boot/config`, `make modules_prepare`, pahole; built with `LLVM=1 ARCH=arm64`) |
| `mia3-qg.dts` | overlay adding `fuel-gauge@4800` under `pmic@2` (stock `qcom,qgauge@4800`), applied by `mia3_dtbo.ko` (see `audio/`) |

Try (not loaded at boot): `dtc -I dts -O dtb -o /lib/firmware/mia3-qg.dtbo mia3-qg.dts`, `rmmod mia3_dtbo; insmod mia3_dtbo.ko dtbo=mia3-qg.dtbo`, `insmod mia3_qg.ko`.
The overlay stays until reboot. The module borrows the regmap of the `typec@1500` sibling device (same trick as `pmi632_chg`).

Properties: `present`, `voltage_now` (LAST_ADC_V), `voltage_boot` (S7 PON OCV), `voltage_ocv` (S3 good OCV, -ENODATA until the
QG took one), `current_now` (LAST_ADC_I), `current_avg` (burst average). Voltage raw x 194637 nV, current raw x 152588 pA,
sign flipped to the power_supply ABI (negative = discharge).

Measured 2026-10-13 (usb_suspend=1, on battery): `voltage_now` 3.800/3.789/3.815/3.813/3.814/3.817 V vs IIO `vbat_sns` 3.780/3.794/3.809/3.808/3.812/3.814 V
(within ~20 mV, sampled a moment apart); `current_now` about -290 to -450 mA. Charging-direction sign not yet checked.

Kernel gotcha: module `struct module` size depends on config, so the build tree needs `CONFIG_PAHOLE_VERSION` = the phone's, else insmod says
".gnu.linkonce.this_module section size must match".
