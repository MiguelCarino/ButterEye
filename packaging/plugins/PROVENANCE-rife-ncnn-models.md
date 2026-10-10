<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Provenance of buttereye-rife-ncnn-models (SCOPE §5.4, §9 package 3; §13 Q25)

Every packaged model directory is taken unmodified from
<https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan> tag `r9_mod_v33`
(commit `c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f`, MIT, © HolyWu and contributors),
by `repack-rife-ncnn.sh`. Those directories are ncnn conversions of the RIFE v4.x
weights published by hzwer in Practical-RIFE (MIT, © 2021 hzwer), the continuation of
ECCV2022-RIFE (MIT, © Megvii Inc.); the ncnn implementation follows nihui's
rife-ncnn-vulkan (MIT, © 2020 nihui). Their licence texts ship as `%license`.

| Directory | RIFE version | Upstream weights |
|---|---|---|
| `rife-v4.26_ensembleFalse` | 4.26 | Practical-RIFE v4.26 |
| `rife-v4.22_lite_ensembleFalse` | 4.22 lite | Practical-RIFE v4.22.lite |
| `rife-v4.18_ensembleFalse` | 4.18 | Practical-RIFE v4.18 |

v4.25-lite was dropped on 2026-10-07 (two Xid 109 GPU faults on the bundled ncnn build,
`docs/spikes/m0f.md`).

## SHA-256 of the packaged files

```
05858eb12ddef4441ec744e547ba80576c4acc7f014615cb4a68ebb0c18ee27c  rife-v4.18_ensembleFalse/flownet.bin
52fd5c75db42331d9195239c40c4fd88e681f61b6b6f5ff0590d8af6b0ebfa72  rife-v4.18_ensembleFalse/flownet.param
792da5d62937199886bf8bb781ab8e0eddf663854d57e4a43cd7b32e4e81b29b  rife-v4.22_lite_ensembleFalse/flownet.bin
091119e19a5891a5477de8f90b3c9e979baece5ee57c27d4e4138a6cc2552cc3  rife-v4.22_lite_ensembleFalse/flownet.param
94d58e30b75d7c7609cfa6f3bdad524deddd14f5f75e85c36d2f827ef5c64731  rife-v4.26_ensembleFalse/flownet.bin
79f16c28903f93f8308f0c4c947f8c7e0c17d99a57b85473e5f298dd578d137b  rife-v4.26_ensembleFalse/flownet.param
```
