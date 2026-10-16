#!/bin/sh
# Copy the Mi A3's proprietary firmware from its own stock partitions (slot a, read-only) into /lib/firmware.
# Run as root on the phone. Needs: python3, pil-squasher (apk add pil-squasher).
set -eu
here=$(cd "$(dirname "$0")" && pwd)
L=${L:-/dev/disk/by-partlabel}  # L=<dir with modem_a, bluetooth_a, vendor_a image files> to read a ROM instead
FW=${FW:-/lib/firmware}  # FW=/tmp/x to test without touching the system
laurel=$FW/qcom/sm6125/xiaomi/laurel
mnt=$(mktemp -d)
trap 'cd /; umount "$mnt"/* 2>/dev/null; rm -rf "$mnt"' EXIT

for p in modem bluetooth vendor; do
    mkdir "$mnt/$p" && mount -o ro "$L/${p}_a" "$mnt/$p"
done
install -d "$laurel" "$FW/qca" "$FW/ath10k/WCN3990/hw1.0/laurel"

# modem, CDSP, ADSP (speaker), Wi-Fi (remoteprocs + ath10k)
cd "$mnt/modem/image"
cp modem.* modemr.jsn modemuw.jsn cdsp.* cdspr.jsn adsp* wlanmdsp.mbn bdwlan.* "$laurel/"
python3 "$here/ath10k-bdenc.py" bdwlan.* "$FW/ath10k/WCN3990/hw1.0/laurel/board-2.bin"

# GPU zap shader: split .mdt/.bNN -> one .mbn
pil-squasher "$laurel/a610_zap.mbn" "$mnt/vendor/firmware/a610_zap.mdt"

# Bluetooth: the chip is Comanche (cm*), the kernel asks for the wcn3988 names (ap*0a)
cp "$mnt"/bluetooth/image/* "$FW/qca/"
ln -sf cmbtfw12.tlv "$FW/qca/apbtfw0a.tlv"
ln -sf cmnv12.bin "$FW/qca/apnv0a.bin"

echo "firmware installed; reboot to load it"
