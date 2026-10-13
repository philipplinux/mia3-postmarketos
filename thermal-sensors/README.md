# CPU/GPU temperature (TSENS)

The mainline dtb has no TSENS node, so there are no CPU temperature zones. This adds the stock
`tsens@4410000` (v2, 16 sensors) at runtime and exposes 8 zones: `cpuss0-2-thermal` (little cluster),
`cpu4-7-thermal` (big cores), `gpu-thermal`. The dashboard shows the hottest CPU zone on the CPU tile and the GPU zone on the GPU tile; thermal-guard's CPU/GPU limit covers them.

| File | What |
|---|---|
| `build.sh` | builds `mia3_tsens.ko` from the kernel tree (`KDIR=`): mainline driver, `__init` dropped, compatible `mia3,tsens-v2` |
| `mia3-tsens.dts` | overlay, applied by `mia3_dtbo.ko` (see `audio/`) |
| `mia3-tsens.service` | loads both at boot |
| `build-adc-tm5.sh` | fixes `quiet-thermal`/`xo-thermal` ("Temperature check failed (-22)"): rebuilds `qcom-spmi-adc-tm5.ko` without a stale `IIO_VAL_INT` check; install steps in the script. `rf-pa0-thermal` then reads -40 °C (no thermistor fitted) |

Install: copy `mia3_tsens.ko` and `mia3_dtbo.ko` to `/lib/modules/$(uname -r)/extra/mia3-tsens/`, `depmod -a`,
`dtc -I dts -O dtb -o /lib/firmware/mia3-tsens.dtbo mia3-tsens.dts`, then enable the unit.

Gotchas: the built-in `qcom,tsens-v2` driver oopses if probed late (its init code is freed after boot).
Inside an overlay, refer to new nodes by label: raw phandles get renumbered on apply.

Measured: 30 s of load on all 8 cores takes the big cores to ~95 °C, so thermal-guard's CPU/GPU limit (`soc_max`)
must stay well above 75 °C or sustained load powers the phone off. 100 °C is the default (kernel critical trip: 110 °C).
