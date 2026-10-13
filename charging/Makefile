obj-m := pmi632_chg.o
KDIR ?= $(HOME)/projects/pmi632-chg/src/linux

all:
	$(MAKE) LLVM=1 -C $(KDIR) M=$(CURDIR) modules
