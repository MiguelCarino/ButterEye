<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(j): RIFE inference ceiling on faster runtimes (performance track P2)

Date: 2026-10-09. Dev box: Fedora 44, RTX 4090 (driver 615.71.09, CUDA 13), Python 3.14.
Reproducer: `tools/perf/ort_ceiling.py` (raw numbers: `m0j-results.json`, table: `m0j-table.md`).
Follows M0(i) (`m0i.md`), which found RIFE-ncnn capped at ~35–41 new frames/s at 1080p and
~7 at 4K.

## Setup

- Models: vs-mlrt's `external-models` release, `rife_v2/rife_v4.26.onnx` and
  `rife_v2/rife_v4.22_lite.onnx` (input: img0, img1 and a timestep plane, fp32; internal
  padding). Kept outside the repository (`~/.cache/buttereye-dev/p2/models`); the
  conversions have no stated licence (SCOPE §8.1).
- Runtime: ONNX Runtime 1.31.0 (`onnxruntime-gpu[cuda,cudnn]`) with the CUDA and TensorRT
  execution providers. ORT 1.31 links TensorRT **10**, so the venv carries
  `tensorrt-cu13-libs==10.16.1.11` (libraries only: TRT 10 has no Python 3.14 bindings).
  This is a dev-box venv at `~/.cache/buttereye-dev/p2/venv` (~7 GB), never loaded by
  ButterEye (SCOPE §8.1).
- Two numbers per case: **gpu** (IO binding, frames stay on the GPU: the ceiling a
  pipelined plugin can approach) and **host** (fp32 frames copied in and out each call, no
  overlap: what a naive plugin gets). One inference = one new frame.

## Results (new frames per second)

| Model | Size | RIFE-ncnn plugin (M0(i)) | ORT CUDA fp32 gpu / host | TRT fp32 gpu / host | **TRT fp16 gpu** / host |
|---|---|---|---|---|---|
| v4.26 | 1080p | 35.5 | 55.5 / 38.8 | 92.8 / 54.6 | **148.3** / 69.6 |
| v4.22-lite | 1080p | 37.3 | 61.6 / 41.9 | 103.5 / 58.0 | **174.0** / 75.2 |
| v4.26 | 2160p | 7.1 | 12.6 / 9.0 | 21.2 / 12.5 | **33.5** / 16.2 |
| v4.22-lite | 2160p | — | 14.3 / 9.8 | 23.6 / 13.4 | **38.8** / 17.2 |

Real-time needs (23.976 fps source): 2× = 24 new frames/s; 23.976 → 60 = 60 new frames/s.
TensorRT engine builds took 5–52 s per model and size (cached afterwards, 96 MB for all).
No Xid faults during the run.

## Findings

1. **TensorRT fp16 is the step change: 4.2–4.9× the ncnn plugin** at both sizes. At 1080p
   it makes 23.976 → 60 real time with ~2.5× headroom (148 against 60); at 4K, 2× becomes
   possible with ~40 % headroom (33.5 against 24). 4K → 60 stays out of reach on a 4090.
2. **Copies to and from system memory halve it.** Without overlap, TRT fp16 drops to
   70–75 new frames/s at 1080p and 16–17 at 4K (below real time for 4K 2×). The plugin
   has to overlap transfers with compute (several streams, as `vstrt`'s `num_streams`) and
   should move half-precision frames (RGBH) instead of fp32. P2C's transfer work matters on
   every runtime, not just ncnn.
3. **The ncnn plugin is about as fast as plain fp32 with copies** (35.5 vs ORT CUDA's 38.8
   host). Its ceiling is the absence of tensor-core fp16 kernels and layer fusion, not a
   bug; patching its pipelining alone cannot reach TRT.
4. **The lite model is only 10–18 % faster** than v4.26 on every runtime.
5. **Half-size motion (`scale=0.5`) is not available for current models.** vs-mlrt raises
   "not supported" for RIFE v4.7 and later at `scale != 1.0` (vsmlrt.py, `RIFEMerge`); only
   v4.0–v4.6 accept it. SCOPE §15.1 assumed it for 4K; corrected. 4K on slower GPUs still
   means smoothing at a smaller size (`decide.pick_size`) or the older v4.6 at scale 0.5.
6. **fp16 accuracy is not checked yet.** vs-mlrt pins several RIFE v2 layers to fp32 under
   TensorRT < 11 (`--layerPrecisions`); ORT's TRT provider here ran them all in fp16, so
   these fp16 numbers may be slightly optimistic and need the M2 quality gate (PSNR/SSIM
   against fp32).

## Decision

- **P2A (TensorRT) is the NVIDIA path** and should leave "experimental" once the real
  plugin (`vstrt`, TensorRT 11) reproduces these numbers inside mpv with overlapped
  transfers. ORT's TensorRT provider (`vsort`) is a fallback route to the same engines.
- **P2B on NVIDIA adds nothing over P2A** (ORT CUDA is NVIDIA-only and 2.7× slower than TRT
  fp16). For AMD/Intel, P2B is still open: vs-mlrt's ncnn and MIGraphX backends need the
  cross-vendor box.
- **P2C is narrowed to transfers:** overlap and fp16 frames, whatever the runtime.

## Next

- [ ] Build `vstrt` against TensorRT 11 (SCOPE §5.2, F11) and repeat in vspipe and mpv with
      `num_streams` 1/2/4 and RGBH vs RGBS input.
- [ ] fp16 quality gate: v4.26 fp16 vs fp32 on fixed 1080p and 4K clips (PSNR/SSIM).
- [ ] TRT 11 (strongly typed) vs TRT 10 speed and accuracy.
- [ ] Cross-vendor box: vs-mlrt ncnn / MIGraphX vs the RIFE-ncnn plugin.
