// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
// butter.carino.systems: put the butter in a pan (one pan per platform) to get
// ButterEye for it. Pointer drag anywhere in the kitchen, keyboard (arrows hover the
// butter over a pan, Enter drops it, Home/Escape take it back) and the pans as
// buttons all reach choose(). The download list comes from the latest GitHub
// release when the API answers, else from the built-in 0.0.1 list.
'use strict';

const REPO = 'MiguelCarino/ButterEye';
const FALLBACK = {
  tag: 'v0.0.1',
  assets: [
    'buttereye-0.0.1-1.fc44.noarch.rpm', 'buttereye-0.0.1-1.fc45.noarch.rpm',
    'buttereye-vs-mvtools-29.2-1.fc44.x86_64.rpm', 'buttereye-vs-mvtools-29.2-1.fc45.x86_64.rpm',
    'buttereye-vs-rife-ncnn-9.33-1.fc44.x86_64.rpm', 'buttereye-vs-rife-ncnn-9.33-1.fc45.x86_64.rpm',
    'buttereye-rife-ncnn-models-9.33-1.fc44.noarch.rpm', 'buttereye-rife-ncnn-models-9.33-1.fc45.noarch.rpm',
    'buttereye-vs-mvgpu-0.0.1-1.fc44.x86_64.rpm', 'buttereye-vs-mvgpu-0.0.1-1.fc45.x86_64.rpm',
    'SHA256SUMS',
  ].map((name) => ({ name, url: `https://github.com/${REPO}/releases/download/v0.0.1/${name}` })),
};
// install order in the command: the app first, then its plugins
const ORDER = ['buttereye-', 'buttereye-vs-mvtools', 'buttereye-vs-rife-ncnn', 'buttereye-rife-ncnn-models', 'buttereye-vs-mvgpu'];
const NAMES = { fc44: 'Fedora 44', fc45: 'Fedora 45', macos: 'macOS', windows: 'Windows' };
const SOON = {
  macos: 'ButterEye drives mpv over Unix sockets and loads Linux builds of its VapourSynth plugins; '
    + 'macOS also lacks native Vulkan for the GPU engines. Not in this release.',
  windows: 'ButterEye drives mpv over Unix sockets, checks a Fedora system and loads Linux builds of '
    + 'its VapourSynth plugins. A Windows port is possible but not in this release.',
};

let release = FALLBACK;

const kitchen = document.getElementById('kitchen');
const butter = document.getElementById('butter');
const pans = Array.from(document.querySelectorAll('.pan'));
const result = document.getElementById('result');
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

let hover = 0;          // pan the butter is over (1-based), 0 = none
let pos = { x: 0, y: 0 };
let dragging = null;    // {dx, dy} while dragging
let timer = 0;

function size() { return butter.getBoundingClientRect().width || 64; }
let chosen = 0;         // pan that has butter in it (1-based), 0 = none yet
function home() {       // resting place: above the pans, or on the chosen pan's handle
  const k = kitchen.getBoundingClientRect();
  if (!chosen) return { x: (k.width - size()) / 2, y: 6 };
  const s = pans[chosen - 1].querySelector('svg').getBoundingClientRect();
  return { x: s.left + s.width * 0.86 - k.left - size() / 2, y: s.top + s.height / 2 - k.top - size() * 0.62 };
}
function panCentre(i) { // butter position that centres it over pan i's cooking surface
  const k = kitchen.getBoundingClientRect();
  const s = pans[i - 1].querySelector('svg').getBoundingClientRect();
  // the pan body sits in the left 72 % of the drawing (the handle is the rest)
  return { x: s.left + s.width * 0.36 - k.left - size() / 2, y: s.top + s.height / 2 - k.top - size() / 2 };
}

function place(p, { settle = false, scale = 1 } = {}) {
  pos = p;
  butter.classList.toggle('settle', settle && !reduce.matches);
  butter.style.setProperty('--bx', `${p.x}px`);
  butter.style.setProperty('--by', `${p.y}px`);
  butter.style.setProperty('--bs', String(scale));
}

function under(p) {     // the pan under the butter's centre
  const k = kitchen.getBoundingClientRect();
  const cx = k.left + p.x + size() / 2;
  const cy = k.top + p.y + size() / 2;
  for (let i = 0; i < pans.length; i++) {
    const r = pans[i].getBoundingClientRect();
    if (cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom) return i + 1;
  }
  return 0;
}

function mark(i) {
  hover = i;
  pans.forEach((p, k) => p.classList.toggle('near', k + 1 === i));
  butter.setAttribute('aria-valuenow', String(i));
  butter.setAttribute('aria-valuetext', i ? `Over the ${NAMES[pans[i - 1].dataset.platform]} pan` : 'Not in a pan yet');
}

function back() {       // a fresh pat of butter at home
  clearTimeout(timer);
  butter.classList.remove('melted');
  place(home(), { settle: true });
  mark(0);
}

function choose(i) {
  clearTimeout(timer);
  mark(i);
  place(panCentre(i), { settle: true, scale: 0.7 });
  butter.classList.add('melted');
  chosen = i;
  kitchen.classList.add('cooking');  // the butter no longer needs room above the pans
  pans.forEach((p, k) => p.classList.toggle('chosen', k + 1 === i));
  const platform = pans[i - 1].dataset.platform;
  chips.forEach((c) => c.classList.toggle('recommended', c.dataset.platform === platform));
  show(platform);
  // once it has melted, a new pat waits at home for another choice
  timer = setTimeout(() => {
    butter.classList.remove('melted', 'settle');
    place(home());
    mark(0);
  }, reduce.matches ? 0 : 900);
}

// ---------------------------------------------------------------- pointer
butter.addEventListener('pointerdown', (e) => {
  clearTimeout(timer);
  butter.classList.remove('melted');
  const b = butter.getBoundingClientRect();
  dragging = { dx: e.clientX - b.left, dy: e.clientY - b.top };
  butter.setPointerCapture(e.pointerId);
  butter.classList.add('dragging');
  e.preventDefault();
});
butter.addEventListener('pointermove', (e) => {
  if (!dragging) return;
  const k = kitchen.getBoundingClientRect();
  place({ x: e.clientX - k.left - dragging.dx, y: e.clientY - k.top - dragging.dy });
  mark(under(pos));
});
function drop() {
  if (!dragging) return;
  dragging = null;
  butter.classList.remove('dragging');
  const i = under(pos);
  if (i) choose(i); else back();  // not over a pan: the butter slides back
}
butter.addEventListener('pointerup', drop);
butter.addEventListener('pointercancel', drop);

// ---------------------------------------------------------------- keyboard
function hoverOver(i) {
  mark(i);
  if (i) place({ x: panCentre(i).x, y: panCentre(i).y - 40 }, { settle: true });
  else place(home(), { settle: true });
}
butter.addEventListener('keydown', (e) => {
  const last = pans.length;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') hoverOver(Math.min(hover + 1, last));
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') hoverOver(Math.max(hover - 1, 0));
  else if (e.key === 'Home' || e.key === 'Escape') back();
  else if (e.key === 'End') hoverOver(last);
  else if (e.key === 'Enter' || e.key === ' ') { if (hover) choose(hover); }
  else return;
  e.preventDefault();
});

// ---------------------------------------------------------------- classic buttons
// A Fedora chip is a real link (the browser downloads the app's RPM) and also
// drops the butter in that pan, so the panel shows the command with the plugins.
const chips = Array.from(document.querySelectorAll('.dl-btn'));
chips.forEach((c) => c.addEventListener('click', () => {
  choose(pans.findIndex((p) => p.dataset.platform === c.dataset.platform) + 1);
}));
function linkChips() {
  for (const c of chips) {
    if (c.tagName !== 'A') continue;
    const app = release.assets.find((a) => /^buttereye-\d/.test(a.name) && a.name.includes(`.${c.dataset.platform}.`) && a.name.endsWith('.rpm'));
    if (app) c.href = app.url;
  }
}

// ---------------------------------------------------------------- the pans themselves
pans.forEach((p, k) => p.addEventListener('click', () => choose(k + 1)));
window.addEventListener('resize', () => { if (!dragging) place(hover ? panCentre(hover) : home()); });
// the kitchen shrinks when a pan is chosen: put the waiting pat back on its handle after
kitchen.addEventListener('transitionend', (e) => { if (e.target === kitchen && !dragging && !hover) place(home()); });

// ---------------------------------------------------------------- result
function show(platform) {
  result.replaceChildren();
  if (platform === 'macos' || platform === 'windows') {
    const t = document.getElementById('tpl-soon').content.cloneNode(true);
    t.querySelector('.result-title').textContent = `${NAMES[platform]}: not yet`;
    t.querySelector('.soon-text').textContent = SOON[platform];
    result.append(t);
  } else {
    const t = document.getElementById('tpl-fedora').content.cloneNode(true);
    const rpms = release.assets
      .filter((a) => a.name.includes(`.${platform}.`) && a.name.endsWith('.rpm') && !a.name.includes('.src.'))
      .sort((a, b) => rank(a.name) - rank(b.name));
    t.querySelector('.result-title').textContent = `${NAMES[platform]} · x86_64 · ${release.tag}`;
    t.querySelector('.cmd').textContent = ['sudo dnf install \\', ...rpms.map((a, k) => `    ${a.url}${k < rpms.length - 1 ? ' \\' : ''}`)].join('\n');
    const list = t.querySelector('.files');
    for (const a of rpms) {
      const li = document.createElement('li');
      const link = document.createElement('a');
      link.href = a.url;
      link.textContent = a.name;
      li.append(link);
      list.append(li);
    }
    const sums = release.assets.find((a) => a.name === 'SHA256SUMS');
    if (sums) t.querySelector('.sums').href = sums.url;
    const copy = t.querySelector('.copy');
    copy.addEventListener('click', () => copyText(result.querySelector('.cmd').textContent, copy));
    result.append(t);
  }
  result.hidden = false;
}

function rank(name) {  // the app first, then the plugins in ORDER
  let best = ORDER.length;
  ORDER.forEach((p, k) => { if (name.startsWith(p) && (k > 0 || /^buttereye-\d/.test(name))) best = k; });
  return best;
}

async function copyText(text, button) {
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

// ---------------------------------------------------------------- latest release
fetch(`https://api.github.com/repos/${REPO}/releases/latest`, { headers: { Accept: 'application/vnd.github+json' } })
  .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
  .then((data) => {
    if (!data || !Array.isArray(data.assets) || !data.assets.length) return;
    release = {
      tag: data.tag_name,
      assets: data.assets.map((a) => ({ name: a.name, url: a.browser_download_url })),
    };
    document.getElementById('version-chip').textContent = release.tag;
    document.getElementById('foot-version').textContent = `ButterEye ${release.tag.replace(/^v/, '')}`;
    linkChips();
    const chosen = pans.findIndex((p) => p.classList.contains('chosen'));
    if (chosen >= 0) show(pans[chosen].dataset.platform);
  })
  .catch(() => { /* offline or rate-limited: the built-in list stays */ });

place(home());
mark(0);
