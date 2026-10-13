obj-m := mia3_qg.o
KDIR ?= /tmp/qgw/k/linux	# kernel source matching uname -r (config from /boot/config, `make modules_prepare`)

all:
	$(MAKE) ARCH=arm64 LLVM=1 KBUILD_MODPOST_WARN=1 -C $(KDIR) M=$(CURDIR) modules
