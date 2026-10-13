obj-m := mia3_adsp.o mia3_dtbo.o spdif_transmitter.o
KDIR ?= $(HOME)/projects/pmi632-chg/src/linux
# spdif_transmitter = generic "linux,spdif-dit" dummy codec for the MAX98937 link (not built in this kernel)
all:
	cp $(KDIR)/sound/soc/codecs/spdif_transmitter.c .
	$(MAKE) LLVM=1 KBUILD_MODPOST_WARN=1 -C $(KDIR) M=$(CURDIR) modules
