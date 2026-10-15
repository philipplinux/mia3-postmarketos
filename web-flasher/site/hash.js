import { createSHA256 } from 'hash-wasm';

/** Streaming SHA-256 in 4 MiB chunks, so large images never load into RAM at once.
 * @param {Blob} file @param {(fraction: number) => void} progress */
export async function checksum(file, progress = () => {}) {
  const hasher = await createSHA256();
  hasher.init();
  for (let offset = 0; offset < file.size; offset += 4 * 1024 * 1024) {
    const chunk = new Uint8Array(await file.slice(offset, offset + 4 * 1024 * 1024).arrayBuffer());
    hasher.update(chunk);
    progress(Math.min(1, (offset + chunk.length) / file.size));
  }
  return hasher.digest('hex');
}
