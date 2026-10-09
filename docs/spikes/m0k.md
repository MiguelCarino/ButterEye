<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(k): RIFE fp16 quality gate (performance track P2A, SCOPE §12 M2)

Date: 2026-10-09. Dev box as in M0(j) (RTX 4090, ORT 1.31 with TensorRT 10.16 in the dev venv).
Reproducer: `tools/perf/fp16_quality.py` (raw numbers: `m0k-results.json`, `m0k-conv16.json`;
table: `m0k-table.md`). Model: vs-mlrt `rife_v2/rife_v4.26.onnx`.

## Method

Eight frame triples (A, B, C consecutive) from two local videos: a 1920×804 film
(t = 600, 1800, 3600, 7200 s) and a 3840×2160 SDR music video (t = 30, 90, 150, 200 s).
RIFE rebuilds B from A and C (timestep 0.5). Each variant is compared with the fp32 CUDA
output (drift) and with the real B (absolute quality), next to a plain A/C blend. Frames are
decoded into memory only; no frame or crop of the videos is stored in the repository.

## Results

Drift from fp32 (PSNR dB / share of pixels off by more than 10 %):

| Size | t | TRT fp16 (no pins) | fp16, vsmlrt's TRT < 11 layer pins | fp16, pins + warp in fp32 |
|---|---|---|---|---|
| 1920×804 | 600 | 46.9 | 46.8 / 0 % | 47.4 / 0 % |
| 1920×804 | 1800 | 56.2 | — | — |
| 1920×804 | 3600 | 48.5 | 48.5 / 0.001 % | 48.7 / 0.001 % |
| 1920×804 | 7200 | 52.1 | — | — |
| 3840×2160 | 30 | 34.8 / 0.70 % | 35.9 / 0.59 % | 36.0 / 0.59 % |
| 3840×2160 | 90 | 47.1 | 48.3 / 0.006 % | 48.4 / 0.007 % |
| 3840×2160 | 150 | 52.2 | 50.9 / 0.014 % | 51.7 / 0.014 % |
| 3840×2160 | 200 | 37.6 | 39.4 / 0.28 % | 39.4 / 0.28 % |

Absolute quality, PSNR against the real middle frame (fp32 → TRT fp16): film 33.65 → 33.26,
45.44 → 44.53, 40.61 → 40.18, 33.28 → 33.13; 4K 23.61 → 23.45, 24.84 → 24.73, 13.62 → 13.61,
20.51 → 20.56. TRT fp32 matches CUDA fp32 exactly (≥ 70 dB everywhere).

## Findings

1. **1080p-class: fp16 passes.** Drift is 47–56 dB (SSIM ≥ 0.995) and costs at most 0.9 dB
   against the real frame. No visible difference expected.
2. **4K: fp16 changes fast motion.** On two of four frames, 0.3–0.7 % of pixels move by
   more than 10 % (worst 0.84); a crop of the worst spot (kept locally, not committed)
   shows a hair edge shifted and smeared with horizontal streaks, the pixel-shift pattern
   of vs-mlrt issue #66. Measured against the real frame, though, fp16 is within 0.16 dB
   of fp32 on all four 4K frames: a different motion estimate, not a clearly worse one.
3. **vsmlrt's fp32 layer pins barely help** (34.8 → 35.9 dB on the worst frame), and keeping
   the five warps (`GridSample`) and their grid arithmetic in fp32 adds nothing more. The
   difference comes from the flow network's convolutions in fp16; pinning those would undo
   the speed gain.
4. **vsmlrt's fp16 conversion breaks this model.** `onnxconverter_common.float16` with
   `keep_io_types` (what vsmlrt 3.23.2 runs before `trtexec` on TensorRT 11) retypes the
   outputs of the graph's ten `Cast` nodes but not their `to` attribute; ONNX Runtime refuses
   the result. Passing the `Cast` nodes in `node_block_list` fixes it. Whether TensorRT 11's
   parser accepts the broken file is for the `vstrt` build to show. Worth reporting upstream
   (not done; outward-facing).
5. **Not every scene suits RIFE.** On 4 of 8 frames (flashes, fades, cuts in the music video,
   one dark film shot) a plain blend scores better against the real frame than RIFE, whatever
   the precision. Scene-change and fade handling (SCOPE §5.5) matter more for quality than
   fp16 does.

## Proposed gate (for the owner)

- fp16 **on by default up to 1440p** (passes: drift ≥ 45 dB, ≤ 1 dB against real frames).
- fp16 **offered at 4K live**, labelled "faster, may soften fast motion": fp32 TensorRT
  reaches only ~21 new frames/s at 4K (M0(j)), below the 24 needed for 2×, so fp16 is the
  only real-time option there.
- fp32 **for offline renders at 4K**, where time is cheap and quality is the point.
- Re-run the gate on TensorRT 11 (strongly typed) through `vstrt`, and on more clips, before
  the fp16 default ships.

## Next phase: building vstrt (TensorRT 11)

`contrib/build-vstrt.sh` builds vs-mlrt's `vstrt` (pinned v16.3.test1) against Fedora's
VapourSynth headers inside a memory-capped scope and installs it under
`$XDG_DATA_HOME/buttereye/plugins/vstrt/<tag>/`. It needs the NVIDIA packages below, which
the owner installs. Both NVIDIA repositories are added **disabled**, so a normal
`dnf upgrade` never sees them and can't replace the RPM Fusion driver; installs name them
explicitly. Versions are pinned to SCOPE §5.2.

```sh
sudo dnf config-manager addrepo --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/rhel10/x86_64/cuda-rhel10.repo
sudo dnf config-manager addrepo --from-repofile=https://developer.download.nvidia.com/compute/cuda/repos/fedora44/x86_64/cuda-fedora44.repo
sudo dnf config-manager setopt cuda-rhel10-x86_64.enabled=0 cuda-fedora44-x86_64.enabled=0
sudo dnf install --enablerepo=cuda-rhel10-x86_64 libnvinfer-bin-11.3.0.99-1.cuda13.4 libnvinfer-devel-11.3.0.99-1.cuda13.4
sudo dnf install --enablerepo=cuda-fedora44-x86_64 cuda-nvcc-13-4 cuda-cudart-devel-13-4
contrib/build-vstrt.sh
```

Undo: `sudo dnf remove 'libnvinfer*' libnvonnxparsers11 cuda-nvcc-13-4 cuda-cudart-devel-13-4`
and delete `/etc/yum.repos.d/cuda-rhel10.repo` and `/etc/yum.repos.d/cuda-fedora44.repo`.
Never install the `cuda` or `cuda-toolkit` metapackages (SCOPE §5.2).
