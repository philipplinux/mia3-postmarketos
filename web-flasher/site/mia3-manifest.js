// Xiaomi Mi A3 (laurel_sprout). Refill with `node scripts/fill-manifest.mjs <out-dir> [release]` after a rebuild.
// Size 0 or an empty sha256 makes the flasher refuse to run (fail closed).
export const mia3 = {
  product: 'laurel_sprout',
  release: 'v2026.10.17',
  // CORS-enabled mirror, one folder per release: <mirror><release>/<name>. Tried when the file is not next to the page.
  mirror: 'https://huggingface.co/PhilippLinux/mia3-postmarketos/resolve/main/',
  // Same order and targets as simple-install/flash.sh. Stock Android stays on slot a.
  images: [
    { name: 'vbmeta.img', partition: 'vbmeta_b', size: 4096, sha256: 'e9f0f23cf362fd9ed921ef7da04716e987370949cef7480d26afc60fd761bf5b' },
    { name: 'rootfs.img', partition: 'userdata', size: 1450632092, sha256: '836e1bb266fbca7fe4e53498e54d6a49796b4c014d88a5698fe179f5195ea63f' },
    { name: 'boot.img', partition: 'boot_b', size: 28131328, sha256: '19d812d034d4f24809518a77ad8af4e739c6897b44b94a086bc6b3e34200962b' }
  ]
};
