obj-m := mia3_qg.o
KDIR ?= $(HOME)/projects/mia3-kernel/linux-7.2.3-r0	# kernel source matching uname -r (config from /boot/config, `make modules_prepare`)

all:
	$(MAKE) ARCH=arm64 LLVM=1 KBUILD_MODPOST_WARN=1 -C $(KDIR) M=$(CURDIR) modules
