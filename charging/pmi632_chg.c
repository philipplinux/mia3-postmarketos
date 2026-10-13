// SPDX-License-Identifier: GPL-2.0
/*
 * Minimal PMI632 (SMB5) charge on/off switch for devices without a charger driver.
 * /sys/module/pmi632_chg/parameters/charging_enabled: 1 = charge, 0 = hold (USB still powers the system).
 * /sys/module/pmi632_chg/parameters/usb_suspend: 1 = ignore USB power, run from battery.
 * /sys/module/pmi632_chg/parameters/usb_icl_ma: USB input current limit. Without a charger driver it stays at the
 * USB default 500 mA, so a load spike comes out of the battery. Writing it switches to high-current mode the way the stock
 * smb5 driver does for a Type-C source (smblib_set_icl_current); hardware AICL still backs off if the source sags.
 * icl_status_ma (read-only): the limit actually in force after AICL.
 * Restores all of it on unload so a crashed controller can't leave the battery draining.
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
#define ICL_STATUS		0x1107	/* 50 mA steps */
#define CMD_ICL_OVERRIDE	0x1342	/* bit0: USB 5.1 mode override, off in HC mode */
#define USBIN_LOAD_CFG		0x1365	/* bit4: keep the SW ICL after APSD */
#define USBIN_ICL_OPTIONS	0x1366	/* bit0: high-current mode */
#define USBIN_CURRENT_LIMIT	0x1370	/* 50 mA steps */

static struct regmap *map;
static unsigned int saved_load, saved_opts, saved_icl;

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

static int icl_set(const char *val, const struct kernel_param *kp)
{
	unsigned int ma;
	int ret = kstrtouint(val, 10, &ma);

	if (ret)
		return ret;
	if (ma < 100 || ma > 3000)
		return -EINVAL;
	return regmap_write(map, USBIN_CURRENT_LIMIT, ma / 50) ?:
	       regmap_update_bits(map, USBIN_ICL_OPTIONS, BIT(0), BIT(0)) ?:
	       regmap_update_bits(map, CMD_ICL_OVERRIDE, BIT(0), 0) ?:
	       regmap_update_bits(map, USBIN_LOAD_CFG, BIT(4), BIT(4));
}

/* kp->arg is the 50 mA-step register */
static int ma_get(char *buf, const struct kernel_param *kp)
{
	unsigned int v;
	int ret = regmap_read(map, (unsigned long)kp->arg, &v);

	return ret ? ret : sysfs_emit(buf, "%u\n", v * 50);
}

static const struct kernel_param_ops icl_ops = { .set = icl_set, .get = ma_get };
static const struct kernel_param_ops ma_ro_ops = { .get = ma_get };
module_param_cb(usb_icl_ma, &icl_ops, (void *)USBIN_CURRENT_LIMIT, 0644);
module_param_cb(icl_status_ma, &ma_ro_ops, (void *)ICL_STATUS, 0444);

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
	if (regmap_read(map, USBIN_LOAD_CFG, &saved_load) || regmap_read(map, USBIN_ICL_OPTIONS, &saved_opts) ||
	    regmap_read(map, USBIN_CURRENT_LIMIT, &saved_icl))
		return -ENODEV;
	return 0;
}

static void __exit pmi632_chg_exit(void)
{
	regmap_update_bits(map, USBIN_CMD_IL, USBIN_SUSPEND_BIT, 0);
	regmap_update_bits(map, CHGR_EN_CMD, CHGR_EN_BIT, CHGR_EN_BIT);
	regmap_write(map, USBIN_CURRENT_LIMIT, saved_icl);
	regmap_write(map, USBIN_ICL_OPTIONS, saved_opts);
	regmap_write(map, USBIN_LOAD_CFG, saved_load);
}

module_init(pmi632_chg_init);
module_exit(pmi632_chg_exit);
MODULE_DESCRIPTION("PMI632 charge enable switch");
MODULE_LICENSE("GPL");
