// SPDX-License-Identifier: GPL-2.0
/*
 * Minimal PMI632 (SMB5) charge on/off switch for devices without a charger driver.
 * /sys/module/pmi632_chg/parameters/charging_enabled: 1 = charge, 0 = hold (USB still powers the system).
 * /sys/module/pmi632_chg/parameters/usb_suspend: 1 = ignore USB power, run from battery.
 * Restores both on unload so a crashed controller can't leave the battery draining.
 */
#include <linux/device.h>
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/regmap.h>

#define PMIC_CHILD		"1c40000.spmi:pmic@2:typec@1500"
#define CHGR_SUBTYPE		0x1005	/* 0x80 on this PMI632 */
#define CHGR_CFG2		0x1051	/* bit7 CHG_EN_SRC (0 = command), bit6 polarity */
#define CHGR_EN_CMD		0x1042
#define CHGR_EN_BIT		BIT(0)
#define USBIN_CMD_IL		0x1340
#define USBIN_SUSPEND_BIT	BIT(0)

static struct regmap *map;

/* kp->arg packs reg << 8 | bit mask */
static int bit_set(const char *val, const struct kernel_param *kp)
{
	unsigned long a = (unsigned long)kp->arg;
	bool on;
	int ret = kstrtobool(val, &on);

	if (ret)
		return ret;
	return regmap_update_bits(map, a >> 8, a & 0xff, on ? a & 0xff : 0);
}

static int bit_get(char *buf, const struct kernel_param *kp)
{
	unsigned long a = (unsigned long)kp->arg;
	unsigned int v;
	int ret = regmap_read(map, a >> 8, &v);

	return ret ? ret : sysfs_emit(buf, "%d\n", !!(v & a));
}

static const struct kernel_param_ops bit_ops = { .set = bit_set, .get = bit_get };
module_param_cb(charging_enabled, &bit_ops, (void *)(CHGR_EN_CMD << 8 | CHGR_EN_BIT), 0644);
module_param_cb(usb_suspend, &bit_ops, (void *)(USBIN_CMD_IL << 8 | USBIN_SUSPEND_BIT), 0644);

static int __init pmi632_chg_init(void)
{
	struct device *dev = bus_find_device_by_name(&platform_bus_type, NULL, PMIC_CHILD);
	unsigned int sub, cfg;

	if (!dev)
		return -ENODEV;
	map = dev_get_regmap(dev->parent, NULL);
	put_device(dev);
	if (!map || regmap_read(map, CHGR_SUBTYPE, &sub) || regmap_read(map, CHGR_CFG2, &cfg))
		return -ENODEV;
	/* refuse anything but the layout verified on the Mi A3: command-controlled, normal polarity */
	if (sub != 0x80 || (cfg & 0xc0)) {
		pr_err("pmi632_chg: unexpected subtype %#x cfg2 %#x\n", sub, cfg);
		return -ENODEV;
	}
	return 0;
}

static void __exit pmi632_chg_exit(void)
{
	regmap_update_bits(map, USBIN_CMD_IL, USBIN_SUSPEND_BIT, 0);
	regmap_update_bits(map, CHGR_EN_CMD, CHGR_EN_BIT, CHGR_EN_BIT);
}

module_init(pmi632_chg_init);
module_exit(pmi632_chg_exit);
MODULE_DESCRIPTION("PMI632 charge enable switch");
MODULE_LICENSE("GPL");
