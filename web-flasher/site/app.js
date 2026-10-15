import { FastbootDevice } from 'android-fastboot/dist/fastboot.mjs';
import { CONFIRMATION, fetchPinned, flash, selectImages, STEPS } from './mia3.js';
import { mia3 } from './mia3-manifest.js';

/** @template {HTMLElement} T @param {string} id @returns {T} */
function el(id) {
  const node = document.getElementById(id);
  if (!node) throw Error(`Missing interface element: ${id}`);
  return /** @type {T} */ (node);
}
const message = error => {
  const text = error instanceof Error ? error.message : String(error);
  // Another program (usually the PC's own fastboot/adb) holds the USB interface.
  return /in use|claim|access denied/i.test(text) ? `${text} Close other USB tools (adb kill-server, no running fastboot). On Linux you may need the android-udev rules.` : text;
};

const supported = 'usb' in navigator && window.isSecureContext;
const connectButton = /** @type {HTMLButtonElement} */ (el('connect'));
const flashButton = /** @type {HTMLButtonElement} */ (el('flash'));
const downloadButton = /** @type {HTMLButtonElement} */ (el('download'));
const picker = /** @type {HTMLInputElement} */ (el('files'));
const risk = /** @type {HTMLInputElement} */ (el('risk'));
const confirm = /** @type {HTMLInputElement} */ (el('confirm'));
const status = el('status');
const progress = /** @type {HTMLProgressElement} */ (el('progress'));
const log = el('log');
/** @type {{fb: FastbootDevice|null, lost: boolean, busy: boolean, finished: boolean, images: ReturnType<typeof selectImages>|null}} */
const st = { fb: null, lost: false, busy: false, finished: false, images: null };

const update = () => {
  connectButton.disabled = !supported || st.busy || st.fb !== null || st.lost;
  flashButton.disabled = !supported || !st.fb || st.lost || st.busy || st.finished || !st.images || !risk.checked || confirm.value !== CONFIRMATION;
};
/** @param {string} text @param {number} fraction */
const report = (text, fraction = 0) => {
  status.textContent = text;
  progress.value = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
  el('progress-value').textContent = `${progress.value}%`;
  if (log.lastElementChild?.textContent !== text) {
    const entry = document.createElement('li');
    entry.textContent = text;
    log.append(entry);
  }
};
const assertSession = () => {
  if (st.lost || !st.fb) throw Error('USB session lost. Reload the page before reconnecting; no automatic retry.');
};

if (!supported) report('Open this page over HTTPS in desktop Chrome or Edge for WebUSB.');
else navigator.usb.addEventListener('disconnect', event => {
  if (st.fb && event.device === st.fb.device && !st.finished) {
    st.lost = true;
    report('USB disconnected. The flash may be incomplete; reload before reconnecting.');
    update();
  }
});

connectButton.addEventListener('click', async () => {
  st.busy = true; update();
  try {
    const fb = new FastbootDevice();
    await fb.connect();
    st.fb = fb;
    report(`Connected (${await fb.getVariable('product') ?? 'unknown product'}). Select the images, confirm, and flash.`);
  } catch (error) {
    report(`${message(error)} Reload to try again.`);
  } finally { st.busy = false; update(); }
});

/** @param {File[]} files */
const pick = files => {
  const list = el('selected');
  try {
    st.images = selectImages(files);
    list.replaceChildren(...st.images.map(image => {
      const entry = document.createElement('li');
      entry.textContent = `${image.name} → ${image.partition} (${(image.size / 1024 / 1024).toFixed(1)} MiB)`;
      return entry;
    }));
  } catch (error) {
    st.images = null;
    const entry = document.createElement('li');
    entry.textContent = message(error);
    list.replaceChildren(entry);
  }
  update();
};
picker.addEventListener('change', () => pick([...picker.files ?? []]));
downloadButton.addEventListener('click', async () => {
  downloadButton.disabled = true;
  try {
    const files = [];
    for (const pin of mia3.images) files.push(await fetchPinned(pin, f => report(`Downloading ${pin.name}`, f)));
    pick(files);
    report('Downloaded and selected. The SHA-256 is checked before anything is written.', 1);
  } catch (error) {
    report(message(error), 0);
    downloadButton.disabled = false;
  }
});

flashButton.addEventListener('click', async () => {
  if (flashButton.disabled || !st.fb || !st.images) return;
  st.busy = true; update();
  try {
    await flash(st.fb, st.images, { consent: risk.checked, confirmation: confirm.value }, report, assertSession);
    st.finished = true;
  } catch (error) {
    st.lost = true;
    report(`STOPPED: ${message(error)} Do not retry blindly; reload the page first.`);
  } finally { st.busy = false; update(); }
});
risk.addEventListener('change', update);
confirm.addEventListener('input', update);
el('build-info').textContent = `Build ${mia3.release}. Wait until step ${STEPS} says Done.`;
update();
