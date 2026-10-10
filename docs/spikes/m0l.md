<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(l): vstrt (TensorRT 11) built and measured (performance track P2A)

Date: 2026-10-09. Dev box: Fedora 44, RTX 4090 (driver 615.71.09), VapourSynth R72,
mpv 0.41.0. NVIDIA packages from the disabled-by-default repositories (`docs/spikes/m0k.md`):
TensorRT 11.3.0.99 (`libnvinfer-bin`, `libnvinfer-devel`), CUDA 13.4.92 (`cuda-nvcc-13-4`,
`cuda-cudart-devel-13-4`).
Reproducers: `contrib/build-vstrt.sh`; `tools/perf/breakdown.py --trt --mpv` (raw:
`m0l-results.json`, `m0l-extra.json`; table: `m0l-table.md`); `tools/perf/trt11_quality.py`
(raw: `m0l-quality.json`; table: `m0l-quality.md`). Shared setup: `tools/perf/trt_common.py`.

## Build

`contrib/build-vstrt.sh` built vs-mlrt v16.3.test1's `vstrt` in seconds (5 jobs inside the
8 GiB scope) and installed `libvstrt.so`, `vsmlrt.py` (3.23.2) and the licence under
`~/.local/share/buttereye/plugins/vstrt/v16.3.test1/`. `core.trt.Version()` reports
TensorRT 110300 and CUDA 13040.

Three integration findings:

1. **vsmlrt needs a loaded plugin at import time.** Load `libvstrt.so` before
   `import vsmlrt`, or it raises "cannot load any filters".
2. **vsmlrt picks RIFE's v1 graph by default,** which needs frame sizes divisible by 64
   (1080 isn't). `_implementation=2` selects the v2 graph, which pads internally.
3. **vsmlrt's fp16 conversion is broken for RIFE on TensorRT 11.** TensorRT rejects the
   converted model: "/Mul: ElementWiseOperation PROD must have same input types. But they
   are of types Float and Half". It is the Cast-node bug of M0(k). The perf scripts replace
   `vsmlrt.convert_model` at run time with the same conversion plus
   `node_block_list=<Cast nodes>` (`tools/perf/trt_common.py`); the installed `vsmlrt.py`
   is not edited. Engines then build in ~30 s (1080p) to ~1–2 min (4K), cached per size.
   Worth reporting upstream (not done: outward-facing).

## Speed (new frames per second; machine quiet, no Xid faults)

A first run overlapped a Steam shader pre-compile using all 24 threads and read 15–30 % low;
it was discarded and rerun. "fp16-h" = fp16 engine with half-precision (RGBH) frames;
"fp16-s" = fp16 engine with fp32 (RGBS) frames. All rows include the YUV ↔ RGB conversion
and source pass-through, as live playback would.

| Case | Streams | Out fps | New fps | Needed (out) | vs RIFE-ncnn |
|---|---|---|---|---|---|
| vspipe 1080p 2×, fp16-h | 1 | 132.6 | 66.3 | 48 | 1.9× |
| vspipe 1080p 2×, fp16-h | 2 | 221.9 | 111.0 | 48 | 3.1× |
| vspipe 1080p 2×, fp16-h | 4 | 231.3 | 115.7 | 48 | 3.3× |
| vspipe 1080p 2×, fp16-s | 2 | 129.0 | 64.5 | 48 | 1.8× |
| vspipe 1080p 2×, fp32 | 2 | 122.9 | 61.4 | 48 | 1.7× |
| vspipe 1080p 2×, v4.22-lite fp16-h | 2 | 233.5 | 116.7 | 48 | — |
| vspipe 1080p → 60, fp16-h | 2 | 109.6 | 109.6 | 60 | 2.7× |
| **mpv 1080p 2×, fp16-h** | 2 | **262.2** | 131.1 | 48 | 4.0× (mpv ncnn: 65) |
| **mpv 1080p → 60, fp16-h** | 2 | **101.3** | 101.3 | 60 | — |
| vspipe 2160p 2×, fp16-h | 1 | 29.1 | 14.5 | 48 | 2.0× |
| vspipe 2160p 2×, fp16-h | 2 | 43.5 | 21.7 | 48 | 3.1× |
| vspipe 2160p 2×, fp16-h | 4 | 51.5 | 25.8 | 48 | 3.6× |
| vspipe 2160p 2×, fp16-s | 2 | 22.1 | 11.1 | 48 | 1.6× |
| vspipe 2160p 2×, fp32 | 2 | 21.1 | 10.6 | 48 | 1.5× |
| mpv 2160p 2×, fp16-h | 2 | 47.8 | 23.9 | 48 | — |
| **mpv 2160p 2×, fp16-h** | 4 | **48.8** | 24.4 | 48 | — |

Findings:

1. **1080p is solved on NVIDIA.** In mpv, 2× runs at 262 fps (5.5× headroom, 4× the ncnn
   path) and 23.976 → 60 at 101 fps (1.7× headroom). The SCOPE §15.1 target "1080p → 60 with
   ≥ 2× headroom" is nearly met: 1.8× in vspipe (109.6 vs 60), 1.7× in mpv.
2. **Half-precision frames matter as much as the fp16 engine.** fp16 with fp32 frames is
   1.7× slower than with RGBH frames (64.5 vs 111.0 at 1080p; 11.1 vs 21.7 at 4K): the
   transfers, not the inference, were the limit (M0(j) finding 2).
3. **Two streams are the sweet spot at 1080p** (GPU ~99 % busy; four add 4 %). At 4K four
   streams help (+19 %).
4. **4K 2× is exactly at real time, with no headroom** (mpv: 48.8 out fps against 48). Usable
   only with nothing else on the GPU; the speed test should keep treating 4K as "play
   smaller" (1440p) unless it measures ≥ 1.25× headroom (`LIVE_HEADROOM`).
5. **The lite model gains only 5 %** at 1080p on TensorRT.
6. **vstrt reaches ~75 % of the M0(j) inference ceiling** at 1080p (111 of 148 new
   frames/s) and ~77 % at 4K with four streams (25.8 of 33.5).

## fp16 quality on TensorRT 11 (the real path)

Same eight frame triples as M0(k), through vspipe and vstrt (and the RIFE-ncnn plugin for
comparison), against ONNX Runtime CUDA fp32. Harness note: vspipe writes planar RGB as
G, B, R; the first run read it as R, G, B and was discarded.

| Clip | t | TRT 11 fp16-h vs fp32 (dB / px > 10 %) | TRT 11 fp32 | **RIFE-ncnn (today)** | vs real: fp32 → fp16 |
|---|---|---|---|---|---|
| film 1920×804 | 600 | 46.4 / 0.002 % | 56.6 | 51.7 / 0.001 % | 33.65 → 33.25 |
| film | 1800 | 55.9 / 0 % | 66.9 | 53.7 / 0 % | 45.44 → 44.53 |
| film | 3600 | 48.4 / 0.001 % | 73.8 | 54.1 / 0 % | 40.61 → 40.18 |
| film | 7200 | 51.9 / 0.001 % | 65.3 | 53.4 / 0 % | 33.28 → 33.13 |
| 4K music video | 30 | 34.8 / 0.68 % | 58.8 | 49.5 / 0.012 % | 23.61 → 23.45 |
| 4K | 90 | 46.2 / 0.018 % | 53.0 | **37.5 / 0.52 %** | 24.84 → 24.74 |
| 4K | 150 | 52.4 / 0.006 % | 69.3 | **35.3 / 0.24 %** | 13.62 → 13.61 |
| 4K | 200 | 37.7 / 0.36 % | 60.0 | 52.7 / 0.002 % | 20.51 → 20.56 |

- TensorRT 11 fp16 behaves as TensorRT 10 fp16 did in M0(k): close up to 1080p, a different
  motion estimate in fast 4K motion on two of four frames.
- **The RIFE-ncnn plugin ButterEye plays with today deviates as much at 4K**, on two other
  frames. Against the real frames all three stay within 0.9 dB of fp32.

## Decision (proposed; owner to confirm)

- **TensorRT fp16 with RGBH frames and 2 streams (4 at ≥ 1440p) is the NVIDIA default**
  once ButterEye drives it. Its quality is on par with the current ncnn path, so the M0(k)
  idea of fp32 for 4K renders becomes optional: offer "Best quality (slower)" for renders
  rather than make it the default.
- **4K live stays "play smaller" by default** on a 4090 (no headroom at 2×).
- ButterEye's generated script carries the three integration fixes above; the conversion fix
  is also reported upstream when the owner agrees.
- Next: wire TensorRT into the core (`scriptgen` template backend, engine build out of
  process before mpv, F12 engine cache, doctor's TensorRT section, bench), still behind the
  experimental opt-in (SCOPE §5.2).
