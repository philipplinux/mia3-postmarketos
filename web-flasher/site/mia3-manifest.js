// Xiaomi Mi A3 (laurel_sprout). Refill with `node scripts/fill-manifest.mjs <out-dir> [release]` after a rebuild.
// Size 0 or an empty sha256 makes the flasher refuse to run (fail closed).
export const mia3 = {
  product: 'laurel_sprout',
  release: 'v2026.10.16',
  // CORS-enabled mirror, one folder per release: <mirror><release>/<name>. Tried when the file is not next to the page.
  mirror: 'https://huggingface.co/PhilippLinux/mia3-postmarketos/resolve/main/',
  // Same order and targets as simple-install/flash.sh. Stock Android stays on slot a.
  images: [
    { name: 'vbmeta.img', partition: 'vbmeta_b', size: 4096, sha256: 'e9f0f23cf362fd9ed921ef7da04716e987370949cef7480d26afc60fd761bf5b' },
    { name: 'rootfs.img', partition: 'userdata', size: 1451496388, sha256: '723db89f483cd5f3caf372cb5cb9bc964d7f1d36ea04f3639138b4e0a00cd9a7' },
    { name: 'boot.img', partition: 'boot_b', size: 28131328, sha256: '3a5becdcce428a6db3327e1080ef282f080c92dc3568712f51ea1eac83c741e2' }
  ]
};
