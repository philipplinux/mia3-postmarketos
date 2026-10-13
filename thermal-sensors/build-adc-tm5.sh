#!/bin/sh
# Rebuilds qcom-spmi-adc-tm5.ko without a stale check: since 7.x iio_read_channel_processed() returns 0 on
# success, but adc-tm5 still demands IIO_VAL_INT, so quiet/xo/rf-pa0-thermal fail with -EINVAL.
# Usage: KDIR=<kernel source matching uname -r> sh build-adc-tm5.sh, then as root:
#   install -D -m644 <printed .ko> /lib/modules/$(uname -r)/updates/qcom-spmi-adc-tm5.ko && depmod
#   rmmod qcom_spmi_adc_tm5; modprobe qcom_spmi_adc_tm5
set -e
: "${KDIR:?set KDIR}"
D=$(mktemp -d)
cp "$KDIR"/drivers/thermal/qcom/qcom-spmi-adc-tm5.c "$KDIR"/drivers/thermal/thermal_hwmon.h "$D"
cd "$D"
sed -i 's|"\.\./thermal_hwmon.h"|"thermal_hwmon.h"|; /if (ret != IIO_VAL_INT)/,/return -EINVAL;/d' qcom-spmi-adc-tm5.c
grep -q "IIO_VAL_INT" qcom-spmi-adc-tm5.c && { echo "patch did not apply" >&2; exit 1; }
echo 'obj-m := qcom-spmi-adc-tm5.o' > Makefile
make -s -C "$KDIR" LLVM=1 KBUILD_MODPOST_WARN=1 M="$D" modules
echo "$D/qcom-spmi-adc-tm5.ko"
