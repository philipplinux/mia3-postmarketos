import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';
import { createHash } from 'node:crypto';
import { mia3 } from '../site/mia3-manifest.js';
import { existsSync, openAsBlob } from 'node:fs';
import { CONFIRMATION, downloadSize, patientGetvar, expandedSize, fetchPinned, flash, selectImages, STEPS } from '../site/mia3.js';

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
  for (const reason of ['product', 'locked', 'hash', 'partition', 'logical', 'small', 'consent', 'lost']) {
    const { images, fb, calls, vars } = fixture();
    let ok = consent, assertSession = () => {};
    if (reason === 'product') vars.product = 'sdm845';
    if (reason === 'locked') vars.unlocked = 'no';
    if (reason === 'hash') mia3.images[1].sha256 = '0'.repeat(64), images[1].sha256 = '0'.repeat(64);
    if (reason === 'partition') delete vars['partition-size:dtbo_b'];
    if (reason === 'logical') vars['is-logical:boot_b'] = 'yes';
    if (reason === 'small') vars['partition-size:boot_b'] = '0x1';
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

test('sparse rootfs counts its expanded size, raw files their own size', async () => {
  assert.equal(await expandedSize(new Blob(['raw'])), 3);
  const rootfs = '../simple-install/cache/out-generic/rootfs.img';
  // build output is not in git, so CI only runs the raw case
  if (existsSync(rootfs)) assert.equal(await expandedSize(await openAsBlob(rootfs)), 580352 * 4096);
});

test('max-download-size: decimal like desktop fastboot, hex with 0x, library cap and default', () => {
  assert.equal(downloadSize('804261888'), 804261888); // Mi A3 bootloader reply (decimal)
  assert.equal(downloadSize('0x20000000'), 0x20000000);
  assert.equal(downloadSize('0x80000000'), 1024 * 1024 * 1024);
  assert.equal(downloadSize(null), 512 * 1024 * 1024);
});

test('getvar: trims, FAIL means missing, times out instead of hanging', async () => {
  assert.equal(await patientGetvar(async () => ({ text: ' b \n' }))('current-slot'), 'b');
  assert.equal(await patientGetvar(async () => { throw Object.assign(Error('x'), { status: 'FAIL' }); })('nope'), null);
  await assert.rejects(patientGetvar(() => new Promise(() => {}), 20)('slow'), /Timeout of 20 ms exceeded/);
});

test('a write step that times out is retried twice, then stops; other errors stop at once', async () => {
  const { images, fb, calls } = fixture();
  let fails = 2;
  fb.flashBlob = async partition => {
    if (partition === 'boot_b' && fails-- > 0) throw Error('Timeout of 60000 ms exceeded');
    calls.push(`flash:${partition}`);
  };
  await flash(fb, images, consent, () => {}, () => {});
  assert.ok(calls.includes('flash:boot_b'));
  fails = 3;
  await assert.rejects(flash(fb, images, consent, () => {}, () => {}), /Timeout/);
  fb.flashBlob = async () => { fails += 1; throw Error('FAIL: bad image'); };
  fails = 0;
  await assert.rejects(flash(fb, images, consent, () => {}, () => {}), /bad image/);
  assert.equal(fails, 1);
});

test('previous build (revision) is accepted; skipHash lifts size and SHA-256 pins', async () => {
  const { fb, calls } = fixture();
  const old = new File(['OLD-ROOTFS'], 'rootfs.img');
  const files = mia3.images.map(image => image.name === 'rootfs.img' ? old : new File([contents[image.name]], image.name));
  assert.throws(() => selectImages(files), /wrong size/);
  mia3.images[1].revisions = [{ size: old.size, sha256: sha('OLD-ROOTFS') }];
  await flash(fb, selectImages(files), consent, () => {}, () => {});
  assert.ok(calls.includes('flash:userdata'));
  mia3.images[1].revisions = [{ size: old.size, sha256: '0'.repeat(64) }];
  await assert.rejects(flash(fb, selectImages(files), consent, () => {}, () => {}), /mismatch/);
  mia3.images[1].revisions = [];
  calls.length = 0;
  await flash(fb, selectImages(files, true), { ...consent, skipHash: true }, () => {}, () => {});
  assert.ok(calls.includes('flash:userdata'));
});
