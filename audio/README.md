# ADSP (audio DSP) boot

Boots the Q6 ADSP at runtime via a device-tree overlay, without reflashing the dtb. Status: the ADSP runs
(`/sys/class/remoteproc/remoteproc2` = `adsp running`) and its glink edge exposes `apr_audio_svc`,
`fastrpcglink-apps-dsp` and `IPCRTR`. A sound card `Mi-A3` (speaker only) now registers, but **playback hangs
the kernel**: see "Speaker card" below.

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

## Speaker card (2026-10-13)

The QDSP6 drivers (`q6core q6afe q6afe-clocks q6afe-dai q6asm q6asm-dai q6adm q6routing`), `snd-soc-max98927`
and `snd-soc-sm8250` are built out of tree from the matching kernel source and installed to
`/lib/modules/$(uname -r)/extra/mia3-audio`. The speaker is a MAX98927-family amp (stock: max98937) at 0x3a on
`i2c@4a84000`, fed over SEC_MI2S (gpio125-128).

| File | What |
|---|---|
| `mia3_dtbo.c` | Generic loader: `dtbo=<file in /lib/firmware>`, `ph=path=phandle` (sets a phandle on a live node, `force=1` overrides), `pdev=<path>` (creates a platform device after apply) |
| `mia3-apr.dts` | APR on the ADSP glink edge: q6core, q6afe (+ clocks), q6asm, q6adm |
| `mia3-amp.dts` | Enables `i2c@4a84000` and adds `amplifier@3a` |
| `mia3-card2.dts` | AFE `dai@18` (SECONDARY_MI2S_RX), sec_mi2s pins, `/sound2` card (`qcom,qrb4210-rb2-sndcard`) |

Order: `insmod mia3_adsp.ko` → `mia3_dtbo dtbo=mia3-apr.dtbo` → modprobe the q6 modules → restart the ADSP
(remoteproc stop/start) → amp overlay + `snd-soc-max98927` → card overlay with `ph=` for the dais/routing/amp →
`snd-soc-sm8250` → `card2` with `pdev=/sound2`. Run `rmmod mia3_dtbo` between overlays (it stays loaded).

Gotchas:
- Links must be listed speaker-first: overlay children attach in reverse order, and q6routing fails (-19)
  if its routes reference q6asm widgets that don't exist yet.
- Overlay children reversed also flipped q6asm dai order: the "MultiMedia1" PCM drives DSP stream MM3,
  so the route is `SEC_MI2S_RX Audio Mixer MultiMedia3`.
- New nodes get phandles renumbered above the live maximum; refer to them by label, never raw numbers.
- The first `aplay` (-40 dBFS, amp at minimum) hung, then hard-locked the kernel (CPU stall watchdog).
  Not debugged yet: check the AFE port config (MCLK/bit clock, `qcom,sd-lines`) before playing again.

## Missing for full audio

Kernel 7.2.3-sm61x5 config has `QCOM_APR=m`, `SOUNDWIRE_QCOM=m`, `PINCTRL_SM6115_LPASS_LPI=m`, but not:
`SND_SOC_QDSP6` (q6afe/q6asm/q6adm), `SND_SOC_LPASS_{RX,TX,VA}_MACRO`, `SND_SOC_WCD937X_SDW` (the codec),
`SND_SOC_SM8250` (machine driver used by SM6115/SM4250 boards). Needs a kernel rebuild with those plus dt nodes
for APR services, the LPASS macros, SoundWire masters, WCD937x and a sound card (model on `sm4250`/`qrb4210-rb2`).
