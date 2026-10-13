// SPDX-License-Identifier: GPL-2.0
/* Test loader: applies /lib/firmware/mia3-adsp.dtbo (ADSP remoteproc + smp2p) at runtime. */
#include <linux/firmware.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_platform.h>
#include <linux/platform_device.h>

#define ADSP_MEM_PHANDLE 0x7ff0
static int ovcs_id;
static struct platform_device *smp2p_pdev;

static int __init mia3_adsp_init(void)
{
	const struct firmware *fw;
	struct device_node *np;
	int ret;

	np = of_find_node_by_path("/reserved-memory/memory@55300000");
	if (!np)
		return -ENODEV;
	if (!np->phandle)
		np->phandle = ADSP_MEM_PHANDLE;  /* overlays can't add a phandle to an existing node */
	ret = np->phandle == ADSP_MEM_PHANDLE ? 0 : -EEXIST;
	of_node_put(np);
	if (ret)
		return ret;

	ret = request_firmware(&fw, "mia3-adsp.dtbo", NULL);
	if (ret)
		return ret;
	ret = of_overlay_fdt_apply(fw->data, fw->size, &ovcs_id, NULL);
	release_firmware(fw);
	if (ret) {
		pr_err("mia3_adsp: overlay failed: %d\n", ret);
		of_overlay_remove(&ovcs_id);
		return ret;
	}
	/* root-level nodes added later are not picked up by the platform bus notifier */
	np = of_find_node_by_path("/smp2p-adsp");
	if (np && !of_find_device_by_node(np))
		smp2p_pdev = of_platform_device_create(np, NULL, NULL);
	of_node_put(np);
	pr_info("mia3_adsp: overlay %d applied, smp2p %s\n", ovcs_id, smp2p_pdev ? "created" : "existing");
	return 0;
}
module_init(mia3_adsp_init);

/* no exit: removing a live remoteproc overlay is not worth the risk for a test; reboot to undo */
MODULE_DESCRIPTION("Mi A3 ADSP overlay loader (test)");
MODULE_LICENSE("GPL");
