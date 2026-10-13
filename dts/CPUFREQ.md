# Mi A3 (laurel_sprout, SM6125) – CPU frequency scaling on postmarketOS

Mainline `sm6125.dtsi` has no cpufreq-hw node, so without it the little cores sit at 1.3 GHz and the
**big cores at 300 MHz**. The SoC has the standard Qualcomm cpufreq-hw block; adding the node gives
schedutil scaling up to 1804.8 MHz (little) / 2016 MHz (big).

Tested on postmarketOS edge, kernel `linux-nura-qcom-sm61x5` 7.2.3-r4, Mi A3 booting slot b.
Wi-Fi, modem, GPU (FD610, glmark2 offscreen) all fine. No warranty; keep a backup of your boot image.

## Files
- `sm6125-xiaomi-laurel-sprout-cpufreq.dtb` – ready dtb (built from the 7.2.3-r4 dtb + this patch; also has
  `qcom,msa-fixed-perm` removed from the Wi-Fi node, which is what makes Wi-Fi/modem stable on that kernel)
- `cpufreq-hw-decompiled-dts.patch` – the change against the decompiled dtb (`dtc -I dtb -O dts`)
- `extras/` – unrelated bonus: `pmi632_chg` kernel module + `chargectl.py` to hold the battery near 50 %
  (on/off charge switch only, no fuel gauge on this PMIC)

## The change
```
cpufreq@f521000 {
    compatible = "qcom,sm6125-cpufreq-hw", "qcom,cpufreq-hw";
    reg = <0x00 0xf521000 0x00 0x1000>, <0x00 0xf523000 0x00 0x1000>;
    reg-names = "freq-domain0", "freq-domain1";
    clocks = <&rpmcc RPM_SMD_XO_CLK_SRC>, <&gcc 6>;   /* gcc index 6 as tested; map to the GPLL0 macro in qcom,gcc-sm6125.h */
    clock-names = "xo", "alternate";
    #freq-domain-cells = <1>;
    #clock-cells = <1>;
};
```
plus `qcom,freq-domain = <&cpufreq_hw 0>` on cpu0–3, `<&cpufreq_hw 1>` on cpu4–7 (cpu@100–103), and
`#cooling-cells = <2>`. Needs `CONFIG_ARM_QCOM_CPUFREQ_HW` (=m works).
Note: the decompiled patch uses raw phandles (`0x13`, `0x15`, `0x90`); they are only valid for that exact dtb.
Re-derive them if you patch a different build.

## Install (postmarketOS)
1. Back up: `cp /boot/boot.img ~/boot-backup.img` (and dump your boot partition from fastboot if you can)
2. `sudo cp sm6125-xiaomi-laurel-sprout-cpufreq.dtb /boot/sm6125-xiaomi-laurel-sprout.dtb`
   and the same file to `/boot/dtbs/qcom/sm6125-xiaomi-laurel-sprout.dtb`
3. Rebuild the boot image: `sudo mkinitfs` (laurel uses `deviceinfo_append_dtb=true`: kernel = vmlinuz + dtb)
4. Reboot to fastboot and `fastboot flash boot_b /boot/boot.img` (use your active slot;
   `fastboot boot` is not supported on this bootloader)
5. Check: `cat /sys/devices/system/cpu/cpufreq/policy*/scaling_cur_freq` → two policies, big at up to 2016000

A kernel package upgrade replaces the dtb; re-apply after upgrades.

## Gotchas found on the way
- An older 7.2.3 build (r3) with `qcom,msa-fixed-perm` crashed the modem in a loop and lost Wi-Fi; that was the
  kernel, not cpufreq.
- Boot log `Unable to load a610_zap.mbn` is the early probe before rootfs mounts; the GPU comes up on first open.
- Holding power ~12 s is a PMIC hardware reset (S2 timer), not a software restart.
- Don't `cat` every `/sys/kernel/debug/regmap/*/registers`: it hard-crashed the phone.
