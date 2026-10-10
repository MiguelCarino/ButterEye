<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(d): HDR10 through the live filter (SCOPE F7)

Date: 2026-10-10. Dev box: RTX 4090, mpv 0.41.0 (`--vo=null --ao=null --hwdec=nvdec-copy`),
VapourSynth R72, the -1 COPR plugins. Reproducer: `tools/perf/hdr_params_probe.py` with
`tools/perf/hdr_rife_probe.vpy` (the generated script's RIFE chain: RGBS via the BT.2020-ncl
matrix, RIFE v4.26 2×, back to YUV420P10). Regression gate:
`tests/integration/test_hdr_live_real.py` (ButterEye's own filter, MVTools, generated
10-bit HDR10 clip with mastering display and light levels).

## Question

F7: does mpv keep HDR signalling on the frames a VapourSynth filter returns, so an HDR10
video smoothed live is still shown as HDR10? Gate: `video-out-params` primaries, gamma and
max-cll identical with the filter on and off.

## Result: passes for HDR10 (PQ)

Source: a 3840×1616 HDR10 WEB-DL excerpt (HEVC Main 10, PQ, BT.2020, mastering P3-D65
0.05–1000 cd/m², light levels unknown).

| Filter | pixel format out | matrix / primaries / gamma / range | sig-peak | min/max luma | max-cll / max-fall | vf fps |
|---|---|---|---|---|---|---|
| none | p010 | bt.2020-ncl / bt.2020 / pq / limited | 4.926 | 0.05 / 1000 | 0 / 0 | 24 |
| `video_in` unchanged | yuv420p10 | bt.2020-ncl / bt.2020 / pq / limited | 4.926 | 0.05 / 1000 | 0 / 0 | 24 |
| RIFE 2× on PQ RGB | yuv420p10 | bt.2020-ncl / bt.2020 / pq / limited | 4.926 | 0.05 / 1000 | 0 / 0 | 48 |

mpv carries the source's colour parameters onto the filter's output; only the memory layout
changes (p010 → yuv420p10, the same 10-bit picture). The integration test confirms it
through ButterEye's filter with a clip that has non-zero light levels (1000 / 400).

Picture: RIFE runs on PQ-encoded RGB without linearisation (§13 Q20 stays open); the
in-between frames of the offline HDR10 render of the same film looked clean when tonemapped
(SCOPE §7.4 status).

## Not covered

- **HLG**: not tested; stays skipped.
- **HDR10+ and Dolby Vision**: per-frame dynamic metadata can't follow new frames; mpv
  shows the HDR10 picture (the DV base layer).
- Real-time speed at 4K is the usual live decision (speed test, smaller size, TensorRT);
  nothing HDR-specific.

## Applied

`hdr = "passthrough"` in a profile now smooths PQ video (F7, opt-in, experimental): the
simple window offers it on the row of an HDR10 video ("Smooth HDR" / "Play HDR as is"),
saved in the simple profile; the row says "HDR smoothing is experimental."
