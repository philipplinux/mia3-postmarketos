// Xiaomi Mi A3 (laurel_sprout). Refill with `node scripts/fill-manifest.mjs <out-dir> [release]` after a rebuild.
// Size 0 or an empty sha256 makes the flasher refuse to run (fail closed).
export const mia3 = {
  product: 'laurel_sprout',
  release: 'v2026.10.13',
  // CORS-enabled mirror, one folder per release: <mirror><release>/<name>. Tried when the file is not next to the page.
  mirror: 'https://huggingface.co/PhilippLinux/mia3-postmarketos/resolve/main/',
  // Same order and targets as simple-install/flash.sh. Stock Android stays on slot a.
  images: [
    { name: 'vbmeta.img', partition: 'vbmeta_b', size: 4096, sha256: 'e9f0f23cf362fd9ed921ef7da04716e987370949cef7480d26afc60fd761bf5b' },
    { name: 'rootfs.img', partition: 'userdata', size: 1447425052, sha256: 'f136ba022c58460d4539bb39428abaf1e0a8cbb53d9ff456a2e84632a999e830' },
    { name: 'boot.img', partition: 'boot_b', size: 28131328, sha256: 'cd6f984c5ecf43ae6c7fd0753a096d74cf57798852a9821e129c767451b6cd4c' }
  ]
};
