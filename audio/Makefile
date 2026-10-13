obj-m := mia3_adsp.o mia3_dtbo.o
KDIR ?= $(HOME)/projects/pmi632-chg/src/linux
all:
	$(MAKE) LLVM=1 -C $(KDIR) M=$(CURDIR) modules
