// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
// butter.carino.systems. A Fedora button is a plain link to the app's RPM (the
// no-JS path) and also opens the install sheet, since the app's plugins are not
// in Fedora's repositories. The Windows button links the installer and opens its
// three steps (mpv from winget, SmartScreen, Microsoft's terms). The macOS button
// links the disk image and opens the steps past Gatekeeper (not notarised).
// Linux readers see both Fedora buttons marked (the browser doesn't say which
// Fedora), Windows and Mac readers their own. The links follow the latest
// GitHub release when the API answers.
'use strict';

const REPO = 'MiguelCarino/ButterEye';
const FALLBACK = {
  tag: 'v0.0.3',
  assets: [
    'buttereye-0.0.3-1.fc44.noarch.rpm', 'buttereye-0.0.3-1.fc45.noarch.rpm',
    'buttereye-vs-mvtools-29.2-1.fc44.x86_64.rpm', 'buttereye-vs-mvtools-29.2-1.fc45.x86_64.rpm',
    'buttereye-vs-rife-ncnn-9.33-2.fc44.x86_64.rpm', 'buttereye-vs-rife-ncnn-9.33-2.fc45.x86_64.rpm',
    'buttereye-rife-ncnn-models-9.33-1.fc44.noarch.rpm', 'buttereye-rife-ncnn-models-9.33-1.fc45.noarch.rpm',
    'buttereye-vs-mvgpu-0.0.1-2.fc44.x86_64.rpm', 'buttereye-vs-mvgpu-0.0.1-2.fc45.x86_64.rpm',
    'ButterEye-0.0.3-setup.exe', 'ButterEye-0.0.3-macos-arm64.dmg', 'SHA256SUMS',
  ].map((name) => ({ name, url: `https://github.com/${REPO}/releases/download/v0.0.3/${name}` })),
};
const SETUP = /^ButterEye-.*-setup\.exe$/;
const DMG = /^ButterEye-.*-macos-arm64\.dmg$/;
// install order in the command: the app first, then its plugins
const ORDER = ['buttereye-', 'buttereye-vs-mvtools', 'buttereye-vs-rife-ncnn', 'buttereye-rife-ncnn-models', 'buttereye-vs-mvgpu'];

let release = FALLBACK;
let platform = 'fc44';

const chips = Array.from(document.querySelectorAll('.dl-btn'));
const install = document.getElementById('installDialog');
const mac = document.getElementById('macDialog');
const windows = document.getElementById('windowsDialog');
const seg = Array.from(install.querySelectorAll('.seg button'));

// ---------------------------------------------------------------- which system is this?
function detectOS() {
  const s = ((navigator.userAgentData && navigator.userAgentData.platform)
    || navigator.platform || navigator.userAgent || '').toLowerCase();
  if (/android/i.test(navigator.userAgent || '') || s.includes('android')) return null;  // a phone: no build
  if (/^win|windows/.test(s)) return 'windows';  // Win32, "Windows"; never "darwin"
  if (s.includes('linux') || s.includes('x11')) return 'linux';
  // an iPad asks for desktop pages as "MacIntel" too; it is the one with a touch screen
  if (s.startsWith('mac') && !(navigator.maxTouchPoints > 1)) return 'macos';
  return null;
}
const os = detectOS();
for (const c of chips) {
  const p = c.dataset.platform;
  if ((os === 'linux' && p.startsWith('fc')) || (os === p)) {
    c.classList.add('recommended');
  }
}

// ---------------------------------------------------------------- the install sheet
function rank(name) {  // the app first, then the plugins in ORDER
  let best = ORDER.length;
  ORDER.forEach((p, k) => { if (name.startsWith(p) && (k > 0 || /^buttereye-\d/.test(name))) best = k; });
  return best;
}
function rpms(p) {
  return release.assets
    .filter((a) => a.name.includes(`.${p}.`) && a.name.endsWith('.rpm') && !a.name.includes('.src.'))
    .sort((a, b) => rank(a.name) - rank(b.name));
}
function fill() {
  seg.forEach((b) => b.setAttribute('aria-checked', String(b.dataset.platform === platform)));
  const list = rpms(platform);
  document.getElementById('cmd').textContent = ['sudo dnf install \\', ...list.map((a, k) => `    ${a.url}${k < list.length - 1 ? ' \\' : ''}`)].join('\n');
  const files = document.getElementById('files');
  files.replaceChildren(...list.map((a) => {
    const li = document.createElement('li');
    const link = document.createElement('a');
    link.href = a.url;
    link.textContent = a.name;
    li.append(link);
    return li;
  }));
  const sums = release.assets.find((a) => a.name === 'SHA256SUMS');
  if (sums) document.getElementById('sums').href = sums.url;
  document.getElementById('installTitle').textContent = `Install ButterEye ${release.tag.replace(/^v/, '')}`;
}
function openInstall(p) {
  if (p) platform = p;
  fill();
  if (!install.open) install.showModal();
}
seg.forEach((b) => b.addEventListener('click', () => { platform = b.dataset.platform; fill(); }));
seg.forEach((b, k) => b.addEventListener('keydown', (e) => {  // radiogroup: arrows move between the two
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  const next = seg[(k + 1) % seg.length];
  next.focus();
  next.click();
  e.preventDefault();
}));
document.getElementById('installBtn').addEventListener('click', () => openInstall());

// ---------------------------------------------------------------- the buttons
chips.forEach((c) => c.addEventListener('click', () => {
  const p = c.dataset.platform;
  if (p === 'windows') {
    // the link downloads the installer; the sheet says what else to do
    windows.showModal();
  } else if (p === 'macos') {
    // the link downloads the disk image; the sheet says how to open it the first time
    mac.showModal();
  } else {
    // the link downloads the app's RPM; the sheet shows the command that brings its plugins
    openInstall(p);
  }
}));

// close a sheet by clicking its backdrop
for (const d of [install, mac, windows]) {
  d.addEventListener('click', (e) => { if (e.target === d) d.close(); });
}

// ---------------------------------------------------------------- copy
async function copyFrom(button, source) {
  const text = document.getElementById(source).textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement('textarea');
    ta.value = text;
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  button.textContent = 'Copied';
  button.classList.add('done');
  setTimeout(() => { button.textContent = 'Copy'; button.classList.remove('done'); }, 1600);
}
document.getElementById('copy').addEventListener('click', (e) => copyFrom(e.currentTarget, 'cmd'));
document.getElementById('wincopy').addEventListener('click', (e) => copyFrom(e.currentTarget, 'wincmd'));
document.getElementById('maccopy').addEventListener('click', (e) => copyFrom(e.currentTarget, 'maccmd'));

// ---------------------------------------------------------------- latest release
fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: 'application/vnd.github+json' } })
  .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
  .then((data) => {
    if (!data || !Array.isArray(data.assets) || !data.assets.length) return;
    release = { tag: data.tag_name, assets: data.assets.map((a) => ({ name: a.name, url: a.browser_download_url })) };
    const v = release.tag.replace(/^v/, '');
    document.querySelector('#version strong').textContent = v;
    document.getElementById('foot-version').textContent = `ButterEye ${v}`;
    for (const c of chips) {
      if (c.tagName !== 'A') continue;
      if (c.dataset.platform === 'windows') {
        const setup = release.assets.find((a) => SETUP.test(a.name));
        if (setup) c.href = setup.url;
        continue;
      }
      if (c.dataset.platform === 'macos') {
        const dmg = release.assets.find((a) => DMG.test(a.name));
        if (dmg) c.href = dmg.url;
        continue;
      }
      const app = rpms(c.dataset.platform)[0];
      if (app && rank(app.name) === 0) c.href = app.url;
    }
    for (const id of ['winsums', 'macsums']) {
      document.getElementById(id).href = `https://github.com/${REPO}/releases/tag/${release.tag}`;
    }
    if (install.open) fill();
  })
  .catch(() => { /* offline or rate-limited: the built-in 0.0.3 list stays */ });

fill();
