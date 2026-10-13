// SPDX-License-Identifier: GPL-2.0-only
/*
 * PMI632 QGauge, READ-ONLY power_supply. Adapted from Marc Lainez's qcom_pmi632_qg.c
 * (msm8953-mainline/linux PR #242, GPL-2.0): all register writes, IRQs, FIFO master hold,
 * the SOC-from-OCV lookup and the thermistor IIO channel are dropped. Reads only.
 */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/power_supply.h>
#include <linux/property.h>
#include <linux/regmap.h>

#define PMIC_SIBLING	"1c40000.spmi:pmic@2:typec@1500"
#define QG_PERPH_TYPE		0x04	/* reads 0x0d */
#define QG_STATUS1		0x08	/* bit0 battery present */
#define QG_S7_PON_OCV_V		0x70
#define QG_S3_GOOD_OCV_V	0x74
#define QG_LAST_ADC_V		0xc0
#define QG_LAST_ADC_I		0xc2
#define QG_LAST_BURST_AVG_I	0xc6

struct qg {
	struct regmap *map;
	unsigned int base;
};

static int rd16(struct qg *qg, unsigned int reg, u16 *v)
{
	__le16 raw;
	int ret = regmap_bulk_read(qg->map, qg->base + reg, &raw, sizeof(raw));

	*v = le16_to_cpu(raw);
	return ret;
}

/* raw * 194637 nV = uV, raw * 152588 pA = uA (downstream qpnp-qg constants) */
static int rd_uv(struct qg *qg, unsigned int reg, int *uv)
{
	u16 r;
	int ret = rd16(qg, reg, &r);

	if (!ret && r == 0x8000)	/* "not written yet" sentinel (no good OCV since boot) */
		return -ENODATA;
	*uv = div_u64(194637ULL * r, 1000);
	return ret;
}

static int rd_ua(struct qg *qg, unsigned int reg, int *ua)
{
	u16 r;
	int ret = rd16(qg, reg, &r);

	/* hardware: positive = discharge; power_supply ABI: negative = discharge */
	*ua = -div_s64(152588LL * (s16)r, 1000);
	return ret;
}

static int get_prop(struct power_supply *psy, enum power_supply_property p,
		    union power_supply_propval *val)
{
	struct qg *qg = power_supply_get_drvdata(psy);
	unsigned int st;
	int ret;

	switch (p) {
	case POWER_SUPPLY_PROP_PRESENT:
		ret = regmap_read(qg->map, qg->base + QG_STATUS1, &st);
		val->intval = st & 1;
		return ret;
	case POWER_SUPPLY_PROP_VOLTAGE_NOW:
		return rd_uv(qg, QG_LAST_ADC_V, &val->intval);
	case POWER_SUPPLY_PROP_VOLTAGE_OCV:	/* last good (sleep-state) OCV; 0 until one is taken */
		return rd_uv(qg, QG_S3_GOOD_OCV_V, &val->intval);
	case POWER_SUPPLY_PROP_VOLTAGE_BOOT:	/* S7 power-on OCV */
		return rd_uv(qg, QG_S7_PON_OCV_V, &val->intval);
	case POWER_SUPPLY_PROP_CURRENT_NOW:
		return rd_ua(qg, QG_LAST_ADC_I, &val->intval);
	case POWER_SUPPLY_PROP_CURRENT_AVG:
		return rd_ua(qg, QG_LAST_BURST_AVG_I, &val->intval);
	default:
		return -EINVAL;
	}
}

static const enum power_supply_property props[] = {
	POWER_SUPPLY_PROP_PRESENT, POWER_SUPPLY_PROP_VOLTAGE_NOW, POWER_SUPPLY_PROP_VOLTAGE_OCV,
	POWER_SUPPLY_PROP_VOLTAGE_BOOT, POWER_SUPPLY_PROP_CURRENT_NOW, POWER_SUPPLY_PROP_CURRENT_AVG,
};

static const struct power_supply_desc desc = {
	.name = "mia3-qg",
	.type = POWER_SUPPLY_TYPE_BATTERY,
	.properties = props,
	.num_properties = ARRAY_SIZE(props),
	.get_property = get_prop,
};

static int qg_probe(struct platform_device *pdev)
{
	struct power_supply_config cfg = {};
	struct qg *qg = devm_kzalloc(&pdev->dev, sizeof(*qg), GFP_KERNEL);
	unsigned int type;
	int ret;

	if (!qg)
		return -ENOMEM;
	/* an overlay node's device isn't parented to the SPMI device: borrow a sibling's, as pmi632_chg does */
	struct device *sib = bus_find_device_by_name(&platform_bus_type, NULL, PMIC_SIBLING);

	if (sib) {
		qg->map = dev_get_regmap(sib->parent, NULL);
		put_device(sib);
	}
	if (!qg->map)
		return dev_err_probe(&pdev->dev, -ENODEV, "no parent regmap\n");
	if (device_property_read_u32(&pdev->dev, "reg", &qg->base))
		return dev_err_probe(&pdev->dev, -ENODEV, "no reg\n");
	ret = regmap_read(qg->map, qg->base + QG_PERPH_TYPE, &type);
	if (ret || type != 0x0d)
		return dev_err_probe(&pdev->dev, ret ?: -ENODEV, "not a QG (type 0x%x)\n", type);
	cfg.drv_data = qg;
	return PTR_ERR_OR_ZERO(devm_power_supply_register(&pdev->dev, &desc, &cfg));
}

static const struct of_device_id ids[] = { { .compatible = "mia3,pmi632-qg" }, {} };
MODULE_DEVICE_TABLE(of, ids);

static struct platform_driver drv = {
	.driver = { .name = "mia3-qg", .of_match_table = ids },
	.probe = qg_probe,
};
module_platform_driver(drv);

MODULE_DESCRIPTION("PMI632 QGauge read-only power_supply");
MODULE_LICENSE("GPL");
