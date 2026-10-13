#!/bin/sh
# Builds mia3_tsens.ko from the matching kernel tree: mainline qcom tsens with __init dropped,
# compatible "mia3,tsens-v2" so the built-in driver (whose init code is freed after boot) never binds.
# Usage: KDIR=<kernel source matching uname -r> sh build.sh   (needs clang lld llvm make)
set -e
: "${KDIR:?set KDIR}"
D=$(mktemp -d)
cp "$KDIR"/drivers/thermal/qcom/tsens*.c "$KDIR"/drivers/thermal/qcom/tsens.h "$KDIR"/drivers/thermal/thermal_hwmon.h "$D"
cd "$D"
sed -i 's/ __init / /; s/ __initconst / /; s/ __initdata / /; s|"\.\./thermal_hwmon.h"|"thermal_hwmon.h"|' tsens*.c tsens.h
sed -i 's/"qcom,tsens-v2"/"mia3,tsens-v2"/; s/\.name = "qcom-tsens"/.name = "mia3-tsens"/' tsens.c
printf 'obj-m := mia3_tsens.o\nmia3_tsens-y := tsens.o tsens-v0_1.o tsens-v1.o tsens-v2.o tsens-8960.o\n' > Makefile
make -s -C "$KDIR" LLVM=1 KBUILD_MODPOST_WARN=1 M="$D" modules
echo "$D/mia3_tsens.ko"
