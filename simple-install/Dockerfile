# Everything needed to build and flash the Mi A3 image: pmbootstrap, fastboot, firmware extraction, module build.
# Run --privileged (loop devices, chroots, USB). pmbootstrap refuses root, so build.sh drops to user "build".
FROM alpine:edge

RUN apk add --no-cache \
        pmbootstrap git sudo util-linux e2fsprogs e2fsprogs-extra parted openssl curl tar \
        android-tools android-tools-avbtool pil-squasher python3 openssh-client rsync iproute2 \
        clang lld llvm make flex bison openssl-dev bc perl elfutils-dev diffutils linux-headers \
        musl-dev gcc libgcc pahole dtc \
    && git config --system safe.directory '*' \
    && adduser -D build \
    && echo 'build ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/build

# Docker's /dev is a tmpfs that never gets the loopNpM nodes `losetup -P` creates, so pmbootstrap would write an
# empty image. A real devtmpfs fixes that and also brings /dev/bus/usb for fastboot.
RUN printf '#!/bin/sh\nmount -t devtmpfs devtmpfs /dev 2>/dev/null || true\nexec "$@"\n' > /entrypoint.sh \
    && chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
