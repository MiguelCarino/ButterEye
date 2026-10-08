<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(e): Fedora ffms2 on VapourSynth R72

Date: 2026-10-08. Dev box: Fedora 44, VapourSynth R72 (API 4.1), Python 3.14.7, RTX 4090 (driver 615.71.09), FFmpeg 8.1.3 from RPM Fusion (`ffmpeg-libs`, `libavcodec-freeworld`).
ffms2: Fedora `ffms2-5.0-8.fc44.x86_64`, unpacked from `dnf download` into a scratch directory and loaded with `core.std.LoadPlugin(<path>/libffms2.so.5)` (not installed system-wide during the spike).
Reproducer: `spikes/m0e-ffms2/probe.py` (generated clips only; the index goes to a cache directory, never beside the video).

## Verdict

**Go** for H.264, HEVC (8- and 10-bit), AV1 and VP9 with RPM Fusion's libavcodec.

| Clip (generated) | Format out | Frames | Index | Decode | Random access |
|---|---|---|---|---|---|
| H.264 1080p 23.976, yuv420p | YUV420P8, 24000/1001 | 479 (20 s) | 0.12 s | 784 fps | frame-exact |
| HEVC 4K 23.976, yuv420p10le | YUV420P10, 24000/1001 | 480 (20 s) | 0.36 s | 117 fps | frame-exact |
| AV1 720p 30 (SVT-AV1) | YUV420P8, 30 | 300 | 0.05 s | 1071 fps | frame-exact |
| VP9 720p 30 | YUV420P8, 30 | 300 | 0.05 s | 918 fps | frame-exact |

Frame props `_Matrix`, `_Primaries`, `_Transfer`, `_ColorRange` are set (2/2/2/1 for these untagged clips).

## Full pipeline (§7.1)

`job.vpy` (the live template, `source.kind = "file"`) → RIFE-ncnn v4.26 2× → `vspipe -c y4m -p` → `ffmpeg -f yuv4mpegpipe … -c:v hevc_nvenc` → `ffmpeg` remux (`-map 1:a? -map 1:s? -map 1:t? -map_chapters 1 -map_metadata 1 -c copy`):

| Source | Size out | Frames in → out | Speed | Wall time (20 s clip) |
|---|---|---|---|---|
| H.264 1080p | 1920×1080 | 479 → 958 | 45 fps | 24 s |
| HEVC 10-bit 4K, `size` 1920×1080 | 1920×1080 10-bit | 480 → 960 | 36 fps | 29 s |

No Xid in the kernel log. Scene-change marking costs nothing measurable; NVENC encoding adds ~10 %. Offline RIFE runs at about 0.8 × the in-mpv benchmark rate (61.2 fps at 1080p), which is the factor the render estimates use.

## Not covered

- `ffmpeg-free` without `libavcodec-freeworld` (H.264/HEVC must then be refused, §7.7 #7): the dev box has RPM Fusion; the probe's decode check covers it at run time.
- `mkvmerge`: the unpacked binary needs `libmatroska` from the system; the remux was tested with the ffmpeg fallback. Re-check after `dnf install mkvtoolnix`.
- `lsmas` on F45 R79: not needed while ffms2 works.

## Decision

Render reads files through ffms2, loaded from `/usr/lib64/libffms2.so.5` (`ScriptConstants.ffms2_lib`, written into the script's data block), with the index in ButterEye's cache (`source.cache`) and VFR → CFR done by ffms2 (`source.cfr`, `fpsnum/fpsden`).
