<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Bundled mpv shaders

Third-party mpv user shaders ButterEye can add to the player (SCOPE §15.2, spike M0(o)).
They are data files mpv loads; ButterEye never runs them itself.

| File | Origin | Licence | SHA-256 |
|---|---|---|---|
| `FSRCNNX_x2_8-0-4-1.glsl` | igv/FSRCNN-TensorFlow, release 1.1 (https://github.com/igv/FSRCNN-TensorFlow/releases/tag/1.1) | LGPL-3.0-or-later (file header), unmodified | `e800dbc5c1c95185cc82216c597724533ff5f2880179f256eef600f03e8dc2ae` |
| `ravu-lite-ar-r3.hook` | bjin/mpv-prescalers at commit `b3f0a59d68f33b7162051ea5970a5169558f0ea2` (https://raw.githubusercontent.com/bjin/mpv-prescalers/b3f0a59d68f33b7162051ea5970a5169558f0ea2/ravu-lite-ar-r3.hook) | LGPL-3.0-or-later (file header and the repository's `LICENSE`), unmodified | `6225c2362567f818cf6e67e66cd7125a32ec0ea727285643b1e5a377387c5b5f` |

`LGPL-3.0.txt` and `GPL-3.0.txt` are the licence texts the LGPL-3.0 refers to. The
repository `LICENSE` of bjin/mpv-prescalers at that commit is the same LGPL-3.0 text
(it differs only in an `http://` link to the FSF where ours has `https://`).

Both shaders hook the luma plane only while mpv upscales: FSRCNNX above 1.3x, RAVU
above 1.414x (its `//!WHEN` lines). FSRCNNX runs a full 2x network on every output
frame, interpolated ones included, so ButterEye also turns it off where it buys
little (`mpvctl/shaders.sharper_allowed`); RAVU-lite is a small fixed-kernel pass.
