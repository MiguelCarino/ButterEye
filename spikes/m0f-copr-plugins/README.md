# M0(f) spike — COPR plugin builds

Throwaway. Answers SCOPE.md M0(f): do `buttereye-vs-rife-ncnn` (against
Fedora's ncnn) and `buttereye-vs-mvtools` build in mock, network off, for
`fedora-44-x86_64` and `fedora-45-x86_64`, and do they interpolate in mpv?

| File | What |
|---|---|
| `repack-rife-ncnn.sh` | Blobless sparse clone of RIFE-ncnn-Vulkan `r9_mod_v33` (commit-pinned) → plugin tarball without `models/`/`subprojects/`, plus a 4-model tarball. Reproducible. |
| `specs/` | `buttereye-vs-rife-ncnn` (bundles the pinned ncnn 305837fd + glslang a9ac7d5, static; `--with system_ncnn` builds against Fedora's ncnn instead), `buttereye-rife-ncnn-models`, `buttereye-vs-mvtools` (v29_2). Downloaded sources are SHA-256-checked. Plugins install to `/usr/lib64/buttereye/vapoursynth/` (not autoloaded), models to `/usr/share/buttereye/rife-ncnn-models/`. |
| `patches/` | MVTools: VapourSynth headers via pkg-config, private install dir. RIFE (system-ncnn build only): drop the pack8 shader path removed in Fedora's ncnn 20250916. |
| `build.sh` | Repack → SRPMs → `mock --rebuild` per chroot → `results/<chroot>/<pkg>/`. |
| `test/interp.vpy` | 2× RIFE or MVTools; works in mpv (`video_in`) and vspipe (synthetic clip). |
| `test/run-test.sh` | Plugin load, vspipe throughput at 1080p/1440p/2160p, optional mpv playback. |

## Run

```bash
# once
sudo dnf install mock vulkan-tools
sudo usermod -aG mock $USER          # then log out and back in

# build (first mock run downloads the chroots; expect a while)
./build.sh                           # or: ./build.sh fedora-44-x86_64
SPECS=buttereye-vs-rife-ncnn ./build.sh fedora-44-x86_64            # one package
MOCK_OPTS='--with system_ncnn' SPECS=buttereye-vs-rife-ncnn ./build.sh  # Fedora-ncnn variant

# install the F44 results and test
sudo dnf install results/fedora-44-x86_64/*/*.x86_64.rpm results/fedora-44-x86_64/*/*.noarch.rpm
test/run-test.sh ~/Videos/some-24p-video.mkv
```

Remove afterwards with `sudo dnf remove buttereye-vs-rife-ncnn buttereye-rife-ncnn-models buttereye-vs-mvtools`.

## Record the result

Write a go/no-go in `docs/spikes/m0f.md`: build status per package × chroot
(from the `build.sh` summary), the throughput table from `results/run-test-*.log`,
and how mpv playback felt (Shift+I: output fps, dropped frames).
Against Fedora's ncnn, RIFE built but faulted the GPU at `gpu_thread` ≥ 2
(docs/spikes/m0f.md), so the spec now defaults to the SCOPE.md §9 fallback:
the bundled pinned ncnn (+ glslang).

## Known spike shortcuts

- Scene-change detection is off (`sc=False`); R72 has no `misc` plugin.
- The YUV↔RGB matrix is guessed from height; real templates read it over IPC.
- Plugin/model paths are hard-coded to Fedora x86_64 locations.
