// SPDX-License-Identifier: GPL-2.0
/* Applies /lib/firmware/<dtbo> as a device-tree overlay. Unloading keeps the overlay: reboot to undo.
 * ph=<path>=<phandle>,... gives existing nodes a phandle first (overlays can't add one).
 * force=1 overwrites a phandle this loader set earlier (never one from the base dtb).
 * pdev=<path> creates a platform device for a new root-level node (the bus notifier skips those). */
#include <linux/firmware.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
static char *dtbo = "mia3-apr.dtbo";
static char *ph[8];
static int nph;
static char *pdev;
static bool force;
module_param(force, bool, 0444);
module_param(dtbo, charp, 0444);
module_param_array(ph, charp, &nph, 0444);
module_param(pdev, charp, 0444);
static int __init mia3_dtbo_init(void)
{
	const struct firmware *fw;
	struct device_node *np;
	int i, ret, id = 0;
	for (i = 0; i < nph; i++) {
		char *eq = strrchr(ph[i], '=');
		u32 val;
		if (!eq || kstrtou32(eq + 1, 0, &val))
			return -EINVAL;
		*eq = 0;
		np = of_find_node_by_path(ph[i]);
		if (!np)
			return -ENODEV;
		if (!np->phandle || force)
			np->phandle = val;
		ret = np->phandle == val ? 0 : -EEXIST;
		pr_info("mia3_dtbo: %s phandle 0x%x\n", ph[i], np->phandle);
		of_node_put(np);
		if (ret)
			return ret;
	}
	ret = request_firmware(&fw, dtbo, NULL);
	if (ret)
		return ret;
	ret = of_overlay_fdt_apply(fw->data, fw->size, &id, NULL);
	release_firmware(fw);
	pr_info("mia3_dtbo: %s -> %d (ovcs %d)\n", dtbo, ret, id);
	if (ret || !pdev)
		return ret;
	np = of_find_node_by_path(pdev);
	if (np && !of_find_device_by_node(np))
		of_platform_device_create(np, NULL, NULL);
	of_node_put(np);
	return 0;
}
static void __exit mia3_dtbo_exit(void) {}
module_init(mia3_dtbo_init);
module_exit(mia3_dtbo_exit);
MODULE_DESCRIPTION("Mi A3 runtime dt overlay loader");
MODULE_LICENSE("GPL");
