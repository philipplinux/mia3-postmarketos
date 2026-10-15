import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { createHash } from 'node:crypto';
import { mia3 } from '../site/mia3-manifest.js';
import { CONFIRMATION, fetchPinned, flash, selectImages, STEPS } from '../site/mia3.js';

const original = structuredClone(mia3);
afterEach(() => { Object.assign(mia3, structuredClone(original)); });
const sha = data => createHash('sha256').update(data).digest('hex');
const contents = { 'vbmeta.img': 'VBMETA', 'rootfs.img': 'ROOTFS-DATA', 'boot.img': 'KERNEL' };
const consent = { consent: true, confirmation: CONFIRMATION };

function fixture() {
  const files = mia3.images.map(image => new File([contents[image.name]], image.name));
  mia3.images.forEach((image, i) => { image.size = files[i].size; image.sha256 = sha(contents[image.name]); });
  const vars = { product: 'laurel_sprout', unlocked: 'yes',
    ...Object.fromEntries(['vbmeta_b', 'userdata', 'boot_b', 'dtbo_b'].map(p => [`partition-size:${p}`, '0x4000000'])) };
  const calls = [];
  const fb = {
    async getVariable(name) { return vars[name] ?? null; },
    async runCommand(command) { calls.push(command); },
    async flashBlob(partition) { calls.push(`flash:${partition}`); }
  };
  return { images: selectImages(files), fb, calls, vars };
}

test('flashes exactly the flash.sh sequence and ends on the Done step', async () => {
  const { images, fb, calls } = fixture();
  const log = [];
  await flash(fb, images, consent, text => log.push(text), () => {});
  assert.deepEqual(calls, ['flash:vbmeta_b', 'erase:dtbo_b', 'flash:userdata', 'flash:boot_b', 'set_active:b', 'reboot']);
  assert.match(log.at(-1), new RegExp(`^Step ${STEPS}/${STEPS}: Done`));
  assert.ok(log.some(t => t.startsWith(`Step ${STEPS}/${STEPS}: Rebooting`)));
});

test('a reboot that drops USB before answering still counts as done', async () => {
  const { images, fb } = fixture();
  fb.runCommand = async command => { if (command === 'reboot') throw Error('transfer failed'); };
  await flash(fb, images, consent, () => {}, () => {});
});

test('wrong phone, locked bootloader, bad hash, missing partition, consent or lost USB write nothing', async () => {
  for (const reason of ['product', 'locked', 'hash', 'partition', 'consent', 'lost']) {
    const { images, fb, calls, vars } = fixture();
    let ok = consent, assertSession = () => {};
    if (reason === 'product') vars.product = 'sdm845';
    if (reason === 'locked') vars.unlocked = 'no';
    if (reason === 'hash') mia3.images[1].sha256 = '0'.repeat(64), images[1].sha256 = '0'.repeat(64);
    if (reason === 'partition') delete vars['partition-size:dtbo_b'];
    if (reason === 'consent') ok = { ...consent, confirmation: 'yes' };
    if (reason === 'lost') assertSession = () => { throw Error('lost'); };
    await assert.rejects(flash(fb, images, ok, () => {}, assertSession), undefined, reason);
    assert.deepEqual(calls, [], reason);
  }
});

test('selection needs exactly the three pinned files with pinned sizes', () => {
  fixture();
  assert.throws(() => selectImages([]), /exactly/);
  const files = mia3.images.map(image => new File([contents[image.name] + 'x'], image.name));
  assert.throws(() => selectImages(files), /wrong size/);
  mia3.images[0].sha256 = '';
  assert.throws(() => selectImages(files), /not filled/);
});

test('download falls back to the HF mirror and rejects a wrong size', async () => {
  const pin = { name: 'boot.img', size: 6 };
  const urls = [];
  const get = async url => {
    urls.push(url);
    return url.startsWith('./') ? new Response(null, { status: 404 }) : new Response('KERNEL');
  };
  const file = await fetchPinned(pin, () => {}, get);
  assert.equal(await file.text(), 'KERNEL');
  assert.equal(urls[1], `${mia3.mirror}${mia3.release}/boot.img`);
  await assert.rejects(fetchPinned({ ...pin, size: 3 }, () => {}, get), /larger/);
});
