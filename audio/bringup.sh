#!/bin/sh
# Brings up the speaker card at runtime (as root). Needs, in /lib/firmware: mia3-adsp.dtbo, mia3-apr.dtbo,
# mia3-speaker.dtbo (dtc -I dts -O dtb); mia3_adsp.ko + mia3_dtbo.ko built here (make), the q6 + sm8250 +
# max98927 modules in /lib/modules/$(uname -r)/extra/mia3-audio (see README). Reboot to undo.
set -e
P=${P:-$(dirname "$0")}
A=/soc@0/remoteproc@ab00000/glink-edge/apr
rproc() { grep -l adsp /sys/class/remoteproc/*/name | xargs dirname; }
insmod "$P/mia3_adsp.ko"
sleep 1
lsmod | grep -q '^mia3_dtbo' && rmmod mia3_dtbo
insmod "$P/mia3_dtbo.ko" dtbo=mia3-apr.dtbo
for m in apr q6core q6afe-clocks q6afe-dai q6routing q6asm-dai snd-soc-max98927; do modprobe $m; done
R=$(rproc)
[ "$(cat $R/state)" = running ] && echo stop > $R/state
sleep 1
echo start > $R/state
for i in $(seq 1 20); do [ -e /sys/bus/apr/devices ] && ls /sys/bus/apr/devices | grep -q . && break; sleep 1; done
sleep 3
rmmod mia3_dtbo
insmod "$P/mia3_dtbo.ko" dtbo=mia3-speaker.dtbo ph=$A/service@7/dais=0x8001,$A/service@4/dais=0x8002,$A/service@8/routing=0x8003 pdev=/sound
modprobe snd-soc-sm8250
for i in $(seq 1 10); do grep -q Mi-A3 /proc/asound/cards && break; sleep 1; done
cat /proc/asound/cards
