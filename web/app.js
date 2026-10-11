// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
// butter.carino.systems: slide the butter to a platform to get ButterEye for it.
// Pointer drag, keyboard (arrows, Home, Enter) and plain buttons all reach the same
// choose(). The download list comes from the latest GitHub release when the API
// answers, else from the built-in 0.0.1 list below.
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

const rail = document.getElementById('rail');
const butter = document.getElementById('butter');
const stops = Array.from(document.querySelectorAll('.stop'));
const result = document.getElementById('result');
const reduce = window.matchMedia('(prefers-reduced-motion: reduce)');

let index = 0;          // 0 = resting at the start, 1..4 = a stop
let x = 0;              // butter offset in px
let dragging = null;    // {dx} while dragging

function butterSize() { return butter.getBoundingClientRect().width || 64; }
function stopX(i) {     // butter offset that centres it over stop i (1-based)
  const r = rail.getBoundingClientRect();
  const s = stops[i - 1].getBoundingClientRect();
  return s.left + s.width / 2 - r.left - butterSize() / 2;
}
function maxX() { return rail.getBoundingClientRect().width - butterSize(); }

function place(px, settle) {
  x = Math.max(0, Math.min(px, maxX()));
  rail.classList.toggle('settle', Boolean(settle) && !reduce.matches);
  butter.classList.toggle('settle', Boolean(settle) && !reduce.matches);
  rail.style.setProperty('--x', `${x}px`);
}

function nearest(px) {  // the stop under the butter, if any
  const centre = px + butterSize() / 2;
  const r = rail.getBoundingClientRect();
  for (let i = 0; i < stops.length; i++) {
    const s = stops[i].getBoundingClientRect();
    if (centre >= s.left - r.left && centre <= s.right - r.left) return i + 1;
  }
  return 0;
}

function mark(i) {
  stops.forEach((s, k) => s.classList.toggle('near', k + 1 === i));
  butter.setAttribute('aria-valuenow', String(i));
  butter.setAttribute('aria-valuetext', i ? NAMES[stops[i - 1].dataset.platform] : 'Start: no platform yet');
}

function goTo(i, settle = true) {
  index = i;
  place(i ? stopX(i) : 0, settle);
  mark(i);
}

function choose(i) {
  goTo(i);
  stops.forEach((s, k) => s.classList.toggle('chosen', k + 1 === i));
  if (!i) { result.hidden = true; return; }
  show(stops[i - 1].dataset.platform);
}

// ---------------------------------------------------------------- pointer
butter.addEventListener('pointerdown', (e) => {
  dragging = { dx: e.clientX - butter.getBoundingClientRect().left };
  butter.setPointerCapture(e.pointerId);
  butter.classList.add('dragging');
  e.preventDefault();
});
butter.addEventListener('pointermove', (e) => {
  if (!dragging) return;
  const r = rail.getBoundingClientRect();
  place(e.clientX - r.left - dragging.dx, false);
  mark(nearest(x));
});
function drop() {
  if (!dragging) return;
  dragging = null;
  butter.classList.remove('dragging');
  const i = nearest(x);
  if (i) choose(i); else goTo(0);  // not over a platform: the butter slides back
}
butter.addEventListener('pointerup', drop);
butter.addEventListener('pointercancel', drop);

// ---------------------------------------------------------------- keyboard
butter.addEventListener('keydown', (e) => {
  const last = stops.length;
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') goTo(Math.min(index + 1, last));
  else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') goTo(Math.max(index - 1, 0));
  else if (e.key === 'Home') goTo(0);
  else if (e.key === 'End') goTo(last);
  else if (e.key === 'Enter' || e.key === ' ') { if (index) choose(index); }
  else return;
  e.preventDefault();
});

// ---------------------------------------------------------------- the stops themselves
stops.forEach((s, k) => s.addEventListener('click', () => choose(k + 1)));
window.addEventListener('resize', () => goTo(index, false));

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
    const v = release.tag.replace(/^v/, '');
    document.getElementById('version-chip').textContent = release.tag;
    document.getElementById('foot-version').textContent = `ButterEye ${v}`;
    const chosen = stops.findIndex((s) => s.classList.contains('chosen'));
    if (chosen >= 0) show(stops[chosen].dataset.platform);
  })
  .catch(() => { /* offline or rate-limited: the built-in list stays */ });

goTo(0, false);
