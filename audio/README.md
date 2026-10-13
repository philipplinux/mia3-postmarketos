# ADSP (audio DSP) boot

Boots the Q6 ADSP at runtime via a device-tree overlay, without reflashing the dtb. Status: the ADSP runs
(`/sys/class/remoteproc/remoteproc2` = `adsp running`) and its glink edge exposes `apr_audio_svc`,
`fastrpcglink-apps-dsp` and `IPCRTR`. **No sound yet**: see "Missing for sound".

| File | What |
|---|---|
| `mia3-adsp.dts` | Overlay: `smp2p-adsp` + `remoteproc@ab00000` (`qcom,sm6115-adsp-pas`, glink edge `lpass`) |
| `mia3_adsp.c` | Module: gives `/reserved-memory/memory@55300000` phandle `0x7ff0` (overlays can't add one), applies the overlay |

Firmware: `adsp.*` from the stock `modem_a` partition (`image/`) into `/lib/firmware/qcom/sm6125/xiaomi/laurel/`.

```
dtc -I dts -O dtb -o mia3-adsp.dtbo mia3-adsp.dts   # no -@: the live tree has no __symbols__
sudo cp mia3-adsp.dtbo /lib/firmware/
make KBUILD_MODPOST_WARN=1 KDIR=<kernel source>     # tree has no Module.symvers
sudo insmod mia3_adsp.ko
```

No `module_exit`: reboot to undo. Raw phandles (`0x01` intc, `0x0d` apcs mailbox, `0x13` rpmcc) are for `dts/k7.dtb` only.

## Missing for sound

Kernel 7.2.3-sm61x5 config has `QCOM_APR=m`, `SOUNDWIRE_QCOM=m`, `PINCTRL_SM6115_LPASS_LPI=m`, but not:
`SND_SOC_QDSP6` (q6afe/q6asm/q6adm), `SND_SOC_LPASS_{RX,TX,VA}_MACRO`, `SND_SOC_WCD937X_SDW` (the codec),
`SND_SOC_SM8250` (machine driver used by SM6115/SM4250 boards). Needs a kernel rebuild with those plus dt nodes
for APR services, the LPASS macros, SoundWire masters, WCD937x and a sound card (model on `sm4250`/`qrb4210-rb2`).
