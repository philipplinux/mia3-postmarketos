import { checksum } from './hash.js';
import { mia3 } from './mia3-manifest.js';

export const CONFIRMATION = 'ERASE MI A3';
export const STEPS = 12;
const MiB = 1024 * 1024;

/**
 * Downloads a pinned file (next to this page, else from the release mirror) into a File, so it goes through the
 * same name/size/SHA-256 checks as a picked one. Parts are folded into Blobs every 64 MiB so the browser
 * can page them to disk instead of holding ~1.4 GB in RAM.
 * @param {{name:string,size:number}} pin @param {(fraction:number)=>void} progress @param {typeof fetch} get
 */
export async function fetchPinned(pin, progress = () => {}, get = fetch) {
  const name = encodeURIComponent(pin.name);
  let response = await get(`./${name}`, { cache: 'no-store' });
  if (!response.ok && mia3.mirror) response = await get(`${mia3.mirror}${encodeURIComponent(mia3.release)}/${name}`);
  if (!response.ok || !response.body) throw Error(`${pin.name} could not be downloaded (HTTP ${response.status}). Pick the file instead.`);
  const reader = response.body.getReader();
  /** @type {BlobPart[]} */ let parts = [];
  /** @type {Blob[]} */ const blobs = [];
  let got = 0, pending = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    parts.push(value); got += value.length; pending += value.length;
    if (got > pin.size) { await reader.cancel(); throw Error(`${pin.name} is larger than pinned.`); }
    if (pending >= 64 * MiB) { blobs.push(new Blob(parts)); parts = []; pending = 0; }
    progress(got / pin.size);
  }
  const file = new File([...blobs, new Blob(parts)], pin.name);
  if (file.size !== pin.size) throw Error(`${pin.name} download is ${file.size} bytes, pinned ${pin.size}.`);
  return file;
}

/** Placeholder pins (size 0 / empty hash) make every entry unusable. */
export function pinsFilled() {
  return mia3.images.every(image => image.size > 0 && /^[0-9a-f]{64}$/.test(image.sha256));
}

/** @param {Iterable<File>} files */
export function selectImages(files) {
  if (!pinsFilled()) throw Error('Image pins are not filled in (site/mia3-manifest.js). Refusing to continue.');
  const selected = [...files];
  if (selected.length !== mia3.images.length) throw Error(`Select exactly ${mia3.images.map(i => i.name).join(', ')}.`);
  return mia3.images.map(image => {
    const matches = selected.filter(file => file.name === image.name);
    if (matches.length !== 1 || matches[0].size !== image.size) {
      throw Error(`${image.name} is missing, duplicated, or has the wrong size for ${mia3.release}.`);
    }
    return { ...image, file: matches[0] };
  });
}

/**
 * getvar with a longer timeout. android-fastboot 1.1.3 gives every getvar a fixed 10 s; right after the 1.4 GB userdata
 * write the Mi A3 bootloader takes longer to answer the next one (step 10 stopped there twice, desktop fastboot then
 * worked). A real unplug still fails at once: the pending USB transfer rejects. Same FAIL→null and trim as the library.
 * @param {(command:string)=>Promise<{text:string}>} run @param {number} ms
 */
export function patientGetvar(run, ms = 60000) {
  return async (/** @type {string} */ name) => {
    /** @type {ReturnType<typeof setTimeout>|undefined} */
    let timer;
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(Error(`Timeout of ${ms} ms exceeded`)), ms); });
    try {
      const { text } = await Promise.race([run(`getvar:${name}`), timeout]);
      return text?.trim() || null;
    } catch (error) {
      if (/** @type {any} */ (error)?.status === 'FAIL') return null;
      throw error;
    } finally { clearTimeout(timer); }
  };
}

/**
 * Upload chunk size from the max-download-size reply, read like desktop fastboot: "0x…" is hex, plain digits
 * are decimal. android-fastboot 1.1.3 always reads hex, so the Mi A3's "804261888" (767 MiB) looked huge, it sent
 * 1 GiB chunks and the phone refused them ("Requested download size is more than max allowed").
 * Same 1 GiB cap and 512 MiB default as the library. @param {string|null} reply
 */
export function downloadSize(reply) {
  const text = (reply ?? '').trim().toLowerCase();
  const size = /^\d+$/.test(text) ? parseInt(text, 10) : parseInt(text, 16);
  return size > 0 ? Math.min(size, 1024 * 1024 * 1024) : 512 * 1024 * 1024;
}

/** Bytes the image takes on the partition: the sparse header's block count, else the file size. @param {Blob} file */
export async function expandedSize(file) {
  const header = new DataView(await file.slice(0, 28).arrayBuffer());
  return header.byteLength === 28 && header.getUint32(0, true) === 0xed26ff3a
    ? header.getUint32(12, true) * header.getUint32(16, true) : file.size;
}

/** @typedef {{getVariable(name:string):Promise<string|null>, runCommand(command:string):Promise<unknown>,
 *   flashBlob(partition:string, blob:Blob, progress:(fraction:number)=>void):Promise<void>}} Fastboot */

/**
 * The simple-install/flash.sh sequence over WebUSB. Every image is size- and SHA-256-checked
 * before the first write; any mismatch or unexpected reply stops it (fail closed).
 * @param {Fastboot} fb
 * @param {ReturnType<typeof selectImages>} images
 * @param {{consent:boolean,confirmation:string}} consent
 * @param {(message:string,fraction:number)=>void} report
 * @param {()=>void} assertSession
 */
export async function flash(fb, images, consent, report, assertSession) {
  if (!consent.consent || consent.confirmation !== CONFIRMATION) throw Error(`Acknowledge the warning and type ${CONFIRMATION}.`);
  let step = 0;
  /** @param {string} text */
  const say = (text, fraction = 0) => report(`Step ${step}/${STEPS}: ${text}`, fraction);
  /** @param {string} text */
  const next = text => { step += 1; assertSession(); say(text); };
  /**
   * Re-run a write step up to twice when the phone only stopped answering (timeout) and USB is still attached.
   * Safe because each step rewrites its whole partition (or is an erase / set_active): repeating gives the same result.
   * @param {string} text @param {()=>Promise<unknown>} action
   */
  const attempt = async (text, action) => {
    for (let tries = 1; ; tries += 1) {
      try { return await action(); } catch (error) {
        if (tries > 2 || !/Timeout of \d+ ms exceeded/.test(String(/** @type {any} */ (error)?.message ?? error))) throw error;
        assertSession();
        say(`${text}: phone did not answer, retrying (${tries}/2)`);
      }
    }
  };

  next('Checking the phone model');
  const product = await fb.getVariable('product');
  if (product !== mia3.product) throw Error(`Phone reports product "${product}", expected ${mia3.product} (Mi A3). Nothing written.`);
  next('Checking the bootloader is unlocked');
  if (await fb.getVariable('unlocked') !== 'yes') throw Error('Bootloader is locked (fastboot flashing unlock, wipes the phone). Nothing written.');

  for (const image of selectImages(images.map(image => image.file))) {
    next(`Checking SHA-256: ${image.name}`);
    const digest = await checksum(image.file, fraction => say(`Checking SHA-256: ${image.name}`, fraction));
    if (digest !== image.sha256) throw Error(`SHA-256 mismatch: ${image.name}. Nothing written.`);
  }

  next('Checking the partitions');
  for (const { partition, file } of [...images, { partition: 'dtbo_b', file: new Blob() }]) {
    const size = parseInt(await fb.getVariable(`partition-size:${partition}`) ?? '', 16);
    if (!(size > 0)) throw Error(`Phone has no ${partition} partition. Nothing written.`);
    // flashBlob() would resize a logical partition; this recipe only knows physical ones.
    if (await fb.getVariable(`is-logical:${partition}`) === 'yes') throw Error(`${partition} is a logical partition. Nothing written.`);
    if (await expandedSize(file) > size) throw Error(`Image is larger than ${partition}. Nothing written.`);
  }

  for (const image of images) {
    next(`Writing ${image.name} → ${image.partition}`);
    const text = `Writing ${image.name} → ${image.partition}`;
    await attempt(text, () => fb.flashBlob(image.partition, image.file, fraction => say(text, fraction)));
    if (image.partition === 'vbmeta_b') {
      next('Erasing dtbo_b');
      await attempt('Erasing dtbo_b', () => fb.runCommand('erase:dtbo_b'));
    }
  }
  next('Switching to slot b');
  await attempt('Switching to slot b', () => fb.runCommand('set_active:b'));
  next('Rebooting');
  // The phone may drop USB before it answers; everything is already written.
  await fb.runCommand('reboot').catch(() => {});
  report(`Step ${STEPS}/${STEPS}: Done. First boot takes about a minute, then ssh user@172.16.42.1 over USB.`, 1);
}
