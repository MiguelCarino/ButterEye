# Spike M0(f): COPR plugin builds (Fedora ncnn vs bundled pinned ncnn)

Date: 2026-10-07. Dev box: Fedora 44, RTX 4090, NVIDIA open module 615.71.09, mpv 0.41.0, VapourSynth R72.
Spike files: `spikes/m0f-copr-plugins/` (specs, patches, `build.sh`, `test/`).

## Verdict

| Question | Result |
|---|---|
| All three packages build in mock, network off, F44 + F45 | **Go** |
| `buttereye-vs-mvtools` works at runtime | **Go**: 108 fps output at 1080p 2x headless |
| `buttereye-vs-rife-ncnn` against **Fedora's ncnn** works at runtime | **No-go.** Intermittent GPU fault whenever `gpu_thread` ≥ 2 (12 of 20 soak runs); stable at `gpu_thread=1`, but only ~32 fps |
| `buttereye-vs-rife-ncnn` with the **bundled pinned ncnn** works at runtime | **Go (headless), with a caveat.** No Xid 13 in 42 runs, 20 of them interleaved A/B against the Fedora-ncnn build; ~72 fps at `gpu_thread=4`. Later the same day, 2 × Xid 109 (`CTX SWITCH TIMEOUT`) in vspipe with rife-v4.25-lite (see "Bundled-build faults") |

**Decision:** take the SCOPE fallback (§8.2 ncnn row, §9 item 2). `buttereye-vs-rife-ncnn` bundles Tencent/ncnn 305837fd (2025-05-03, the commit r9_mod_v33 pins) and its glslang submodule nihui/glslang a9ac7d5, statically linked. The spec keeps the Fedora-ncnn variant behind `--with system_ncnn`, so it can be retried when the plugin catches up with newer ncnn. Since a plugin built for the newer ncnn has not yet been tested, the fault is attributed to "r9_mod_v33 on ncnn 20250916", not to ncnn alone. mpv playback with the bundled build: see "mpv playback, bundled build" below.

## Build (mock, `fedora-44-x86_64` and `fedora-45-x86_64`)

| Package | F44 | F45 |
|---|---|---|
| buttereye-vs-rife-ncnn | OK | OK |
| buttereye-rife-ncnn-models | OK | OK |
| buttereye-vs-mvtools | OK | OK |

- First run: RIFE failed on both chroots, `RIFE/warp.cpp:62: 'const class ncnn::Option' has no member named 'use_shader_pack8'`. Fedora's ncnn 20250916 (F44 -2, F45 -5) removed Vulkan pack8.
- Fix: `patches/buttereye-vs-rife-ncnn-no-pack8.patch` never builds the pack8 warp pipeline. Fedora's `libncnn` contains no pack8 shaders (checked with `strings`), so `elempack == 8` cannot reach the custom layer. The `%check` (`librife.so` links `libncnn.so`) passes.
- Summary: `spikes/m0f-copr-plugins/results/build-summary.txt`.
- Bundled variant (`9.33-0.2.spike`, now the spec default): OK on F44 and F45 on the first try. Sources are the pristine GitHub archives `ncnn-305837fd….tar.gz` (sha256 `d0dd1cbe…`) and `glslang-a9ac7d5f….tar.gz` (sha256 `0aa8276b…`), checked in `build.sh`, extracted into `subprojects/ncnn` and built through upstream's own meson CMake subproject (`-Duse_system_ncnn=false`). Results: `librife.so` 7.5 MB, no `libncnn` dependency (enforced in `%check`), `Provides: bundled(ncnn) = 0^20250503git305837fd`, `bundled(glslang) = 0^gita9ac7d5f`, `%license` ships the ncnn and glslang `LICENSE.txt`. The pack8 patch is applied only to the system-ncnn variant (the pinned ncnn still has pack8).
- Caveat: GitHub archive tarballs are not guaranteed byte-stable. The real COPR SRPM should carry these tarballs with their checksums recorded in the spec (or `sources`), not re-download them.

## Runtime

### Headless throughput (vspipe, synthetic 1080p 23.976 source, 2x, RGBS)

| Backend / model | gpu_thread | Output fps | Stable |
|---|---|---|---|
| MVTools (CPU) | – | 108 | yes |
| RIFE v4.25-lite | 1 | ~32 | yes (10/10 runs) |
| RIFE v4.26 | 2 (default) | 49–54 | intermittent GPU fault |
| RIFE v4.26 / v4.22-lite | 4 | ~75 | intermittent GPU fault |
| RIFE v4.25-lite | 4 | ~74 | **crashed in 1 of 8 runs, with the dlssnr layer blocked** |

Real time at 2x needs ≥ 48 fps for 24p (SCOPE F2 asks for ≥ 1.15× headroom, i.e. ≥ 55). Only `gpu_thread` ≥ 2 reaches that, and only `gpu_thread` = 1 is stable.

### A/B soak (gpu_thread=4, v4.26, 1080p, dlssnr layer blocked, interleaved)

| Build | Clean | GPU fault (Xid 13) | Output fps |
|---|---|---|---|
| Fedora ncnn 20250916 (`0.1.spike`) | 8 / 20 | 12 / 20 (10 faulted but finished, 2 hung) | ~75 |
| Bundled ncnn 305837fd (`0.2.spike`) | 20 / 20 | 0 / 20 | ~72 |

The bundled build was also clean in an earlier 22-run sweep (v4.25-lite ×8 and v4.26 ×8 at gt=4, v4.26 ×3 at gt=2 ≈ 52 fps, v4.26 ×3 at gt=1 ≈ 32 fps). Throughput is the same on both ncnn versions, so the bundled build costs no speed.

Note: some Fedora-ncnn runs *finished with correct fps and still logged an Xid*, so "finished" is not "healthy". The soak counts kernel Xids per run, and `doctor` should too.

### The GPU fault

```
NVRM: Xid 13, Graphics Exception: SKEDCHECK36_DEPENDENCE_COUNTER_UNDERFLOW failed
NVRM: Xid 13, pid=…, name=vspipe|vo, Graphics Exception: channel …, Class 0000c9c0 (compute)
```

- After the fault, ncnn logs `vkQueueSubmit failed -4` / `vkWaitForFences failed -4` (VK_ERROR_DEVICE_LOST) for every frame. The desktop and the rest of the GPU survive (channel-level fault).
- Reproduced in both mpv and vspipe, so mpv and the display path are ruled out.
- Ruled out the `VK_LAYER_NV_dlssnr` implicit layer (package `dlssnr`): it still crashes with `VK_LOADER_LAYERS_DISABLE=VK_LAYER_NV_dlssnr`. `DLSSNR_DISABLE=1` does **not** stop that layer from loading.
- Not content-specific: it hits a uniform synthetic clip too. The probability rises with the number of parallel GPU queues.
- Likely cause: r9_mod_v33 was written for ncnn 2025-05-03. Its custom warp layer and its multi-queue use do not hold up on ncnn 20250916. Not yet confirmed: the pinned-ncnn rebuild is what tells this apart from a plugin or driver bug.

### mpv playback, Fedora-ncnn build (`--hwdec=auto-copy --video-sync=display-resample`, vapoursynth filter, `concurrent-frames=4`)

- 【MMD】V.I.P. (VP9 1080p 24.003): RIFE faulted within ~1 s, then glitching, A-V desync and hundreds of dropped frames.
- A later run of the same file played audio with no video: the filter could not keep up (gpu_thread=2 ≈ 49–54 fps, on top of the decode and RGBS conversion load).
- Cyberpunk: Edgerunners ED (VP9 1080p 23.976): played; Shift+I numbers not recorded.
- MVTools in mpv: not yet tested.

### mpv playback, bundled build (9.33-0.2.spike), 2026-10-07

Through ButterEye's own live play (U10), `concurrent-frames=4` (`gpu_thread=4`).

- 720p 24 fps played at 60 fps with rife-v4.25-lite (2.5×) and reached ACTIVE.
- 1080p: with `concurrent-frames=4` RIFE reaches about 44–46 fps in mpv, short of the ≈48 fps real time for 2× 23.976. Measured 2026-10-07 (v4.26, `mpv --untimed --vo=null`, 1100 output frames, startup included, 3 runs each): `concurrent-frames=4`/`gpu_thread=4` 45–47 fps; **8/4 56–58 fps**; 8/8 50–57 fps (no faster, less steady); 0 new Xid in all 9 runs. Owner decision: default `concurrent-frames=8` with `gpu_thread` kept at 4 (SCOPE §4.4).
- MVTools in mpv at 720p reached 48 fps.
- With the user's mpvSockets script installed, mpv moves its IPC socket to `/tmp/mpvSockets/<pid>` as the script loads, and the socket ButterEye asked for refuses connections. Live play now adopts `/tmp/mpvSockets/<pid>` after the socket and `SO_PEERCRED` checks (SCOPE §4.3).

### Bundled-build faults

No Xid 13 since the bundled RPM was installed at 03:43 (all 16 Xid 13 lines of this boot, 03:01–03:39, came from the Fedora-ncnn build during the A/B soak). Two faults of a different class did hit the bundled build:

| Time | Xid | Process | What ran |
|---|---|---|---|
| 06:35:40 | 109 `CTX SWITCH TIMEOUT` | pid 326356, `name=vspipe` | GUI Benchmark, rife-v4.25-lite, 1280×720, 23.976 → 47.952, vspipe pass |
| 07:22:16 | 109 `CTX SWITCH TIMEOUT` | pid 423051, `name=vspipe` | a test run during the review fixes; the real-bench tests use rife-v4.25-lite at 640×360 (not confirmed which run) |

- The cause (the model, desktop preemption under Hyprland, or ncnn) is unknown. Both hits were in vspipe with v4.25-lite.
- The bench keeps a model out of recommendations once it has faulted in 2 of the last 10 benchmarks on the same GPU.
- **Owner decision 2026-10-07: rife-v4.25-lite is dropped** from `buttereye-rife-ncnn-models` (9.33-0.2.spike, built OK in mock for F44 and F45; models now v4.26, v4.22-lite, v4.18). The Balanced profile and the mid-range selection default now use v4.22-lite. Whether the Xid 109 is specific to that model is untested; any new Xid 109 with the remaining models reopens this.

## Side findings for the real product

- `doctor` should flag any NVIDIA Xid (13, 109, …) in `journalctl -k` after a failed RIFE session, and say "GPU fault in RIFE-ncnn" rather than leaving the user with a frozen picture.
- The live template must detect a stalled or failed filter (no frames while audio runs) and fall back or disable interpolation, rather than playing audio over a frozen picture.
- Even on a 4090, RIFE-ncnn at 1080p reaches only about 75 fps at best. The RGBS (fp32) conversion and `use_fp16_arithmetic=false` are candidates to profile before judging the SCOPE §1 "up to 1440p" goal (spike M0(a)).
- Installing a local RPM with dnf cancels a pending GNOME Software offline update.
