// Usage: node scripts/fill-manifest.mjs <out-dir> [release]
// Fills size + sha256 of the Mi A3 images in site/mia3-manifest.js from files in <out-dir>
// (hashes the files itself, so no SHA256 file format assumptions; compare with it by eye).
import { createHash } from 'node:crypto';
import { createReadStream } from 'node:fs';
import { readFile, stat, writeFile } from 'node:fs/promises';
import { mia3 } from '../site/mia3-manifest.js';

const [dir, release] = process.argv.slice(2);
if (!dir) throw Error('usage: fill-manifest.mjs <out-dir> [release]');
const path = new URL('../site/mia3-manifest.js', import.meta.url);
let source = await readFile(path, 'utf8');
for (const image of mia3.images) {
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(`${dir}/${image.name}`)) hash.update(chunk);
  const { size } = await stat(`${dir}/${image.name}`);
  source = source.replace(
    new RegExp(`(name: '${image.name.replace('.', '\\.')}', partition: '${image.partition}', )size: \\d+, sha256: '[0-9a-f]*'`),
    `$1size: ${size}, sha256: '${hash.digest('hex')}'`);
}
if (release) source = source.replace(/release: '[^']*'/, `release: '${release}'`);
await writeFile(path, source);
console.log('updated site/mia3-manifest.js');
