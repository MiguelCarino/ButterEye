# ButterEye v1 Scope

Status: revised draft, **owner decisions applied 2026-10-07** · Date: 2026-10-07 · Target file: `docs/SCOPE.md`

The owner's decisions of 2026-10-07 are folded into the text: DCO, the AGPL §7 output permission, no NVIDIA code in-process, an **experimental** TensorRT 11 path, Fedora 44 + 45 only, HDR10 offline in v1, and a ButterEye COPR that ships ButterEye plus the FOSS plugins it needs. They are recorded in §3, §13 ("Decided") and Appendix B. No git repository exists yet. Nothing in this document assumes one exists today; git tags and CI checks apply once it is created.

Every claim in this document was researched and then checked by an independent verifier. Where the verifier corrected a researcher, the correction is used here. Claims the verifier refuted have been dropped. Anything that could not be verified is either an open question (§13), a feasibility spike (§12, M0), or marked **unverified** in the text. Changes from review critiques are folded into the text. Critiques that were rejected or modified are listed with reasons in Appendix A.

---

## 1. Summary

ButterEye is a free/open-source smooth-motion tool for Linux. It adds real-time motion interpolation to video playing in the user's own mpv. It also has an offline mode that renders an interpolated file.

ButterEye has no player of its own in v1. It does four things:

- detects the machine's GPU and the interpolation backends that are installed;
- writes VapourSynth scripts (`.vpy`) and an mpv config profile;
- launches or attaches to mpv and controls it over mpv's JSON IPC;
- renders interpolated files through `vspipe` → `ffmpeg`.

**Who it is for.** The main audience is Linux desktop users who watch local video in mpv and want smooth motion interpolation without a proprietary tool. A secondary audience is people who want scriptable batch interpolation through the CLI.

**Hardware expectations.** The v1 real-time target is **≤1440p**.

- 4K real-time is offered only when the in-mpv benchmark (F14) passes on the user's machine. Expect it only on RTX 4080/4090-class GPUs with the experimental TensorRT path (§5.2).
- Real-time RIFE on non-NVIDIA GPUs is expected only on discrete GPUs of roughly RX 7800 / Arc A770 class at ≤1080p. This is **unverified** until spike M0(a).
- Integrated Intel and AMD GPUs are expected to get MVTools only.

At 4K, RIFE's RGB-float frames and the CPU-side YUV↔RGB conversion make memory bandwidth the bottleneck. vs-mlrt's v13 release notes say so.

**Installation.** The cross-vendor (RIFE-ncnn-Vulkan) and CPU (MVTools) paths install from a single `dnf copr enable <owner>/buttereye` plus `dnf install buttereye`, with no run-time downloads (§9). The only network fetch left is the optional ONNX models for the experimental TensorRT path (§5.4).

**Licence.** AGPL-3.0-or-later, with an adopted §7 additional permission that covers generated output (§8.4).

**Governance.** A standalone community project. It is not affiliated with or endorsed by any company or other interpolation product.

---

## 2. Goals and non-goals (v1)

### Goals

1. Real-time RIFE interpolation up to 1440p in a stock distro mpv on Linux. This must work on any discrete Vulkan GPU (AMD, Intel, NVIDIA) through RIFE-ncnn-Vulkan. On NVIDIA Turing or newer, TensorRT 11 is offered as an **experimental**, opt-in path that is not a release gate (§5.2). MVTools is the lightweight fallback and the only option when no GPU is available.
2. ButterEye never edits the user's `mpv.conf`. Every mpv setting arrives through an include file, a profile, or IPC.
3. One profile drives both live playback and offline render, using the same `.vpy` graph.
4. Offline render keeps every non-video stream (audio, subtitles, chapters, attachments, tags) and handles colour and HDR10 signalling explicitly. HDR10 offline (x265/SVT-AV1) is confirmed in v1 scope; HLG, Dolby Vision and NVENC/VAAPI HDR stay deferred (§7.4).
5. Backend selection is hardware-aware and backed by a built-in benchmark that measures inside mpv. We do not quote published fps figures.
6. Plugins and models are provisioned on Linux, where upstream publishes no Linux release binaries (§5). A **ButterEye COPR** for `fedora-44-x86_64` and `fedora-45-x86_64` ships ButterEye plus the FOSS VapourSynth plugins it needs (RIFE-ncnn-Vulkan, a curated MIT RIFE ncnn model set, MVTools, `vsmlrt.py`), all built from source with complete SRPMs (§8.1, §9).
7. A GUI-free core library and a CLI that can be tested without a display. The PySide6 GUI sits on top.
8. Licence hygiene: proprietary NVIDIA components stay optional, are installed by the user, and never load into ButterEye's own processes.
9. Translatable and accessible:
   - All user-facing strings go through Qt `tr()` (GUI) or `gettext` (CLI, `doctor` messages, and the Lua OSD via a generated string table). English is the source locale. The string catalogue is extracted in CI from M4 on, with `tools/extract_ts.py` (stdlib `ast` → Qt `.ts`) until `lupdate` is usable (Fedora's PySide6 `lupdate`/`lrelease` links point at missing files; `.qm` compilation waits for `qt6-linguist`). Translations land after v1 through a Weblate/.po workflow.
   - The GUI is fully keyboard-operable with visible focus. Every control has an accessible name, verified with Orca via AT-SPI on GNOME. Status is never shown by colour alone. The GUI respects the system font scale and dark/light theme.
   - CLI output supports `--json` and `NO_COLOR`.

**Supported minimums:** mpv 0.41.0, VapourSynth R72, Python 3.14. **Supported distributions: Fedora 44 and Fedora 45 only.** Other distributions, including Arch, are not supported targets. Wayland **and** X11 sessions are supported; Wayland is primary and X11 is covered by one test pass.

### Non-goals (v1)

- Training models.
- Browser, DRM or streaming-service playback.
- Injecting into arbitrary players (VLC, Celluloid), and Flatpak or Snap mpv (§9).
- Windows and macOS.
- Linux distributions other than Fedora 44 and 45, including Arch. Distro-installed plugins are still detected generically (§4.4 guard), with no support commitment (§9).
- An embedded player. That would be strategy B, considered after v1 at the earliest.
- Upscaling or denoising. The architecture does not rule them out; the post-v1 plan is §15.2.
- Any network-reachable interface: web UI, phone remote, or HTTP/TCP control. AGPL §13 puts a duty on anyone who **modifies** ButterEye and lets users interact with it remotely: they must offer those users the Corresponding Source. Through §13 of the AGPL and GPLv3, that duty also covers any GPL-3.0 code combined in-process. Local Unix-socket IPC, D-Bus portals and pipes are not "remote interaction through a computer network".
- Telemetry, analytics, crash upload or update checks. ButterEye makes **no network connections** except downloads the user starts (§4.8). Logs, benchmark results (including GPU UUIDs) and crash traces stay local. `doctor --report` produces a text bundle the user can attach to a bug report themselves. Home paths and media filenames are redacted by default, and `engines/` is always excluded.
- Loading, reading or redistributing any component, profile or asset of a proprietary interpolation tool.
- Deinterlacing. Interlaced sources skip interpolation (§4.11).

### Deferred to after v1 (explicitly out of v1 acceptance)

- Global shortcuts via the portal (was F19). The tray icon.
- Offline pause, "stop and keep", and chunked resume (§7.6).
- Preserve-VFR offline mode (§7.3). The MP4 container. HLG and Dolby Vision offline, and offline HDR through NVENC/VAAPI (§7.4).
- The full benchmark matrix across resolution × backend × model × scale. v1 benchmarks the current source on demand (F14).
- Promoting the TensorRT path out of "experimental". It depends on vs-mlrt v16.x pre-releases (§5.2).
- An in-tree vstrt build helper. v1 ships a documented build script in `contrib/` instead (F11).
- Interpolation of HDR in live mode, unless spike M0(d) passes. It is then opt-in and experimental (F7).

---

## 3. Decisions taken

| Decision | One-line rationale |
|---|---|
| **Player strategy A**: ButterEye controls the user's own mpv through a generated `.vpy`, config and JSON IPC | mpv 0.41 already ships a VapourSynth bridge ([mpv manual: vapoursynth filter](https://mpv.io/manual/stable/#video-filters-vapoursynth)) and a JSON IPC ([mpv manual: JSON IPC](https://mpv.io/manual/stable/#json-ipc)), so the user's own player can be driven from outside. This avoids writing a player and keeps mpv (GPL-2.0-or-later) in a separate process. |
| **Linux only for v1** | The gap ButterEye fills is Linux-specific. The dev box is Fedora 44 + Wayland + NVIDIA, and more platforms would multiply the packaging work in §9. |
| **Offline render in scope for v1** | The mpv manual itself recommends `vspipe` for reliable, full-featured VapourSynth use ([vf.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/vf.rst)), and offline render reuses the same script graph. |
| **Name "ButterEye"** | No hard conflict was found in software or video (§10). The GitHub org and PyPI names are free. |
| **Licence AGPL-3.0-or-later** | Every required dependency is compatible with it. Each one is either used as a separate process, or imported in-process and AGPL-compatible (PySide6 under LGPL-3.0). The design keeps proprietary NVIDIA code, and any code that might be GPL-2.0-only, out of ButterEye's own process (§8.1). "Or later" keeps the FSF upgrade path, as long as no GPL-3.0-only or AGPL-3.0-only code (such as a vendored vsmlrt.py or code from RVE) is combined in. |
| **Python + PySide6 over a GUI-free asyncio core, plus a CLI** | PySide6 6.11.2 and VapourSynth both work on Python 3.14 today and are packaged by Fedora. A GUI-free core stays testable. |
| **DCO (`Signed-off-by:`, CI-enforced), no CLA** (decided 2026-10-07) | Provenance without a relicensing asymmetry (§8.5). CI enforcement starts once a repository exists. |
| **AGPL §7 additional permission for generated output** (decided 2026-10-07) | Covers `.vpy`, `buttereye.conf`, `input.conf` snippets and job manifests written from ButterEye's templates; text in §8.4. Adopted before any outside contribution, so no contributor consent is needed later. |
| **No NVIDIA code in ButterEye's process** (confirmed 2026-10-07) | ButterEye never loads TensorRT, CUDA or NVML in-process, so no NVIDIA §7 exception is needed (§8.1). |
| **TensorRT path: experimental, vs-mlrt v16.x / TensorRT 11** (decided 2026-10-07) | Targets the line NVIDIA ships as RHEL10 RPMs (`trtexec` from `libnvinfer-bin`). Every v16 tag is still a GitHub pre-release ([releases](https://github.com/AmusementClub/vs-mlrt/releases)), so the path is opt-in, labelled experimental and never a release gate (§5.2). |
| **Fedora 44 and Fedora 45 only** (decided 2026-10-07) | Arch is not a supported target. Distro-installed plugins are detected generically, with no commitment (§9). |
| **Project COPR in v1** (decided 2026-10-07) | A ButterEye COPR ships ButterEye and the FOSS plugins it needs, built from source for F44 and F45. The project thereby becomes a GPL/MIT distributor of those plugins, with full source duties (§8.1, §9). `vstrt` is never packaged. |
| **Offline HDR10 in v1** (decided 2026-10-07) | HDR10 via x265/SVT-AV1 only. HLG, Dolby Vision and NVENC/VAAPI HDR stay deferred, with no silent widening (§7.4). |

**Correction to the brief.** The brief planned "RIFE via vs-mlrt ncnn-Vulkan on any GPU". That cannot work:

- vs-mlrt's NCNN_VK backend cannot run RIFE, because ncnn lacks the GridSample op ([issue #80](https://github.com/AmusementClub/vs-mlrt/issues/80), [v12 notes](https://github.com/AmusementClub/vs-mlrt/releases/tag/v12)).
- vs-mlrt's MIGX backend cannot run RIFE either ([issue #154](https://github.com/AmusementClub/vs-mlrt/issues/154)).

The non-NVIDIA RIFE path is therefore [styler00dollar/VapourSynth-RIFE-ncnn-Vulkan](https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan) (MIT). §5 reflects this.

---

## 4. Architecture

### 4.1 Components

```
buttereye/
  core/          GUI-free asyncio library (no Qt import)
    hw/          GPU, VRAM, driver, Vulkan, display refresh detection
    backends/    backend registry + probes (TRT, RIFE-ncnn, MVTools)
    plugins/     plugin + model manager (manifest, download.py, verify, install, licences)
    scriptgen/   .vpy generator (static templates, two entry points)
    mpvctl/      mpv launcher + asyncio JSON-IPC client + command allowlist + include writer
    render/      offline render pipeline (probe, vspipe|ffmpeg, remux, jobs)
    bench/       benchmark runner (vspipe and in-mpv subprocesses)
    profiles/    profiles + rules + config schema/migration
    doctor/      environment checks (incl. in-mpv probe), error catalogue
  cli/           `buttereye` command
  gui/           PySide6 app (thin layer over core)
  data/          manifest.toml, config.schema.json, mpv/buttereye.lua, mpv/buttereye.conf template
contrib/         build-vstrt.sh (experimental TRT 11 path; documented, version-pinned; not v1 acceptance)
packaging/       RPM specs (MIT) + SRPM build helpers for the ButterEye COPR (§9)
```

- **Core engine library.** All logic lives here, and it has no Qt dependency.
  - The core is asyncio-based. It runs on a stock `asyncio` event loop in a dedicated non-daemon thread, and Qt signals (queued connections) are the only bridge to the GUI (`gui/bridge.py`). `PySide6.QtAsyncio` cannot drive it: in 6.11.2 its loop raises `NotImplementedError` for subprocesses, Unix sockets, fd readers, pipes and signal handlers, all of which the core needs (spike M0(h), `docs/spikes/m0h.md`). The bridge is one class, so a QtAsyncio bridge can replace it if a later PySide6 passes the M0(h) reproducer.
  - No Qt types cross into `core/`.
- **CLI.** A thin wrapper over the core. Every GUI action has a CLI equivalent.
  - Subcommands: `setup`, `play`, `attach`, `detach`, `render`, `bench`, `doctor`, `models`, `plugins`, `clean`, `profiles`, `licence`.
  - Flags that give GUI actions a CLI equivalent (F17): `attach --socket <s> --enable|--disable` (toggle interpolation), `attach --socket <s> --profile <id>` (switch profile), `detach --orphans` (remove a leftover `@buttereye` filter), `bench --apply <label>` (save a result as the benchmark profile), `clean --dry-run`, `profiles list`, `profiles explain --fps <n/d> --height <h>` (rule trace), `setup --trt-experimental` (TRT opt-in). The full id → argv table is `core/commands.py` (`docs/design/GUI.md` §2.6).
  - Exit codes: 0 ok, 1 runtime failure, 2 usage or config error, 3 blocking `doctor` issue, 4 dependency missing, 130 cancelled.
  - Every command accepts `--json` (stable, versioned output) and `-v`/`-q`.
  - Logs go to `$XDG_STATE_HOME/buttereye/logs/`, rotated at 5 × 5 MB.
- **PySide6 GUI.** Main window, profile editor, render queue, benchmark view and a Storage page.
  - It depends on **PySide6-Essentials** only.
  - It must not import `vapoursynth`. VapourSynth evaluation always happens in mpv or in a `vspipe`/Python subprocess, which isolates crashes and keeps licence scope clean (§8).
- **mpv integration layer.**
  - An include file, `~/.config/buttereye/mpv/buttereye.conf`, with a `[buttereye]` profile.
  - A Lua helper, `buttereye.lua`. It is loaded with `--script=` at launch, or with `load-script` over IPC when attaching. It handles in-window keybindings (toggle, cycle preset, stats), an OSD status line, and `script-message-to buttereye` commands. It runs inside mpv, so it keeps working after the GUI closes. Client names may only use alphanumerics and `_` ([input.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/input.rst)).
  - An asyncio IPC client with one persistent connection per mpv instance. Property observation lasts only as long as the connection ([ipc.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/ipc.rst)).
- **Offline render pipeline.** `ffprobe` → `.vpy` (source-filter entry point) → `vspipe -c y4m` → `ffmpeg` encode → `mkvmerge` remux (§7).

### 4.2 Data flow (live playback)

1. The core reads the hardware (§4.5) and which backends are available, then picks a profile through the rules (§4.9).
2. The core starts mpv, or attaches to it (§4.3). **No `vf` is set at launch.** The core waits until the observed `display-fps` is > 0 and `video-params` is set. `display-fps` can be 0 before the window is mapped, and the script's own `display_fps` is frozen when the script initialises.
3. The core reads these properties over IPC: `video-params` (colormatrix, primaries, gamma, levels, max-cll, interlacing), `container-fps`, `display-fps`, `hwdec-current` and `path`. It then applies the behaviour matrix (§4.11). If the source is VFR (§4.4), it is probed with `ffprobe` on `path`.
4. The core decides the target rate and multiplier (§5.6). It writes the `.vpy` **atomically** (temp file with mode 0600, then rename) into `$XDG_RUNTIME_DIR/buttereye/`. mpv re-reads the file on every seek, so a half-written file would break playback.
5. The core issues `vf add @buttereye:vapoursynth=file=%<len>%<path>:buffered-frames=<n>:concurrent-frames=<m>:user-data=%<len>%<json>`.
   - `%<bytelen>%` is mpv's length-prefixed quoting. JSON and paths contain `:`, `,`, `=` and `"`, which mpv's option parser would otherwise split on.
   - The `user-data` JSON carries the target fps, colour parameters, backend settings, any data paths, and a generation counter `gen=N`.
   - An identical `vf add` does nothing, because filters with equal arguments are reused. Every hot reload therefore changes `gen` ([f_output_chain.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/filters/f_output_chain.c)).
6. The core checks the `request_id` reply and the `vf` property. On failure it rolls back with `vf remove @buttereye`. A failed reinit can leave a `vf` value that makes the next file fail ([input.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/input.rst)).
7. The core observes `frame-drop-count`, `decoder-frame-drop-count`, `vo-delayed-frame-count`, `mistimed-frame-count`, `display-sync-active` and `display-fps` for the local stats display, which is never transmitted.
   - `estimated-vf-fps` is **not** used as a performance signal. It averages timestamps, so it reports the nominal rate even while frames are being dropped.
   - A change in `display-fps` (for example, the window moving between the dev box's 60, 144 and 180 Hz outputs) triggers a recompute and a hot reload, debounced to 2 s.

### 4.3 How mpv is launched and controlled

**Launch from the CLI** (`buttereye play FILE`), a foreground process that owns the connection for mpv's lifetime:

`mpv --include=~/.config/buttereye/mpv/buttereye.conf --profile=buttereye --script=<pkgdata>/buttereye.lua --input-ipc-client=fd://N FILE`

- `--input-ipc-client=fd://N` uses a socketpair, so there is no socket file. mpv quits when ButterEye's end closes. The FD must not be CLOEXEC ([options.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/options.rst)).
- The ordering of `--include` and `--profile` on the command line is **untested** and is covered by an integration test.

**Launch from the GUI** uses `--input-ipc-server=$XDG_RUNTIME_DIR/buttereye/mpv-<id>.sock` instead, so mpv outlives the GUI window (§4.10).

- If the user runs mpvSockets.lua (doctor detects it in the user's mpv `scripts/` directory), that script overwrites `input-ipc-server` when it loads.
- ButterEye knows the PID of the mpv it launched. It adopts `/tmp/mpvSockets/<pid>` after applying the socket checks below.

**Attach** (`buttereye attach`):

- Discover sockets in `$XDG_RUNTIME_DIR/buttereye/*` and in `/tmp/mpvSockets/*`. The latter is the convention of the [mpvSockets](https://github.com/wis/mpvSockets) script.
- Refuse the instance if it fails F1's mpv checks: version, the `vapoursynth` filter, and being a host binary. The refusal uses the same message as F1, and ButterEye never sends `vf add` to such an instance.
- Never change the instance's socket path or the user's per-PID scheme.
- On attach, set these over IPC, recording the previous values so they can be restored on detach:
  - `hwdec` to a copy mode;
  - `hr-seek-framedrop=no`;
  - `interpolation=no`;
  - `change-list watch-later-options remove vf`.
- Then `load-script` the helper. Only `vf` entries labelled `@buttereye` are ever added, changed or removed. Entries ButterEye did not create are never touched.

**Profile contents** (`[buttereye]`):

- `hwdec=nvdec-copy,auto-copy` and `hwdec-codecs=all`. NVDEC comes first because mpv 0.41's `auto-copy` settles on `vulkan-copy` on NVIDIA, which costs about 1.6-2× the CPU for the same decode; on other GPUs `nvdec-copy` fails to start and mpv falls through to `auto-copy`. The bridge takes only system-memory frames (it maps only regular software image formats to VapourSynth; see `mp_from_vs` in [vf_vapoursynth.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/video/filter/vf_vapoursynth.c)).
- `hr-seek=default` and `hr-seek-framedrop=no`. Relative (arrow-key) seeks snap to keyframes, so the keyframe pre-roll is not run through the interpolation filter only to be thrown away; absolute and exact seeks stay precise. With the default `hr-seek-framedrop=yes`, filters that add frames can make those precise seeks skip the target. (mpv still rebuilds the script on every seek.)
- `interpolation=no`, so mpv's tscale does not blend over RIFE frames.
- `video-sync=display-resample`.
- `watch-later-options-remove=vf`. Without it, `save-position-on-quit` stores the `@buttereye` filter, because `vf` is in mpv's default `--watch-later-options`, and plain mpv later tries to load a stale script.
- **No `vf=` line.** The filter is added only after IPC has supplied the display and colour parameters (§4.2 step 2).

**Crash and stale state.**

- If ButterEye dies while attached, the `@buttereye` filter stays in mpv, pointing at a script in `$XDG_RUNTIME_DIR`. Seeks keep working until logout or reboot.
- On its next start, ButterEye scans the instances it can reach and offers to remove any orphaned `@buttereye` filters.
- Attached instances get the `watch-later-options` change described above. `doctor` also warns when the user's config has `save-position-on-quit` without that removal.

**IPC security.** JSON IPC has no authentication and exposes `run` ([ipc.rst](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/DOCS/man/ipc.rst)).

- Sockets go only in `$XDG_RUNTIME_DIR/buttereye/`, created with `mkdir(mode=0o700)`. If the directory already exists with other permissions or another owner, ButterEye fails rather than chmod-ing it. mpv's `fchmod` does not set the mode of the bound socket file, so the directory is the real protection.
- Never use `@`-prefixed abstract sockets.
- mpv unlinks any existing file at the socket path before binding ([ipc-unix.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/input/ipc-unix.c)). A second instance on the same path therefore silently steals the socket instead of failing, so every instance needs a unique path.
- Before connecting to any discovered socket, `lstat` it and require all of the following:
  - `S_ISSOCK`;
  - `st_uid == getuid()`;
  - a parent directory owned by the user.

  `/tmp/mpvSockets` lives in world-writable `/tmp`, where another local user could plant a socket. After connecting, check `SO_PEERCRED` (peer uid == own uid). On any failure, refuse and log the reason.
- The IPC client never sends `run` or `subprocess`, never sends `load-script` with a path outside ButterEye's package, and never sends `loadfile` with input that has not been validated. Every command ButterEye can send is on an allowlist in one module, which is unit-tested.

### 4.4 Generated script contract

There is one static template with two entry points, which branch on whether `video_in` is present in `globals()`:

- **mpv**: `video_in` plus `user_data`.
- **vspipe**: a source filter (§7).

Templates are written from scratch and contain no third-party code (§8.4).

Rules every generated script follows:

- **No untrusted data in code.**
  - File paths, titles and every parameter reach the script only through `user_data` (JSON), parsed with `json.loads`. In the offline path they come through `vspipe -a`.
  - The template is static. Where a constant must appear in the script, it is emitted with `repr()`.
  - A test feeds filenames containing quotes, newlines, `"""`, `%`, `:` and `,`. It checks that the generated script is byte-identical apart from the data block, and that mpv's quoting round-trips.
- **Plugin loading.**
  - Guard every load with `if not hasattr(core, '<ns>')`, then `core.std.LoadPlugin(<absolute path>)` from the COPR's private, non-autoload plugin directory (`%{_libdir}/buttereye/vapoursynth/`, §9), or from a user-built `vstrt` path. With av-rpm or any other distro copy, a plugin may already be autoloaded (av-rpm installs `%{_libdir}/vapoursynth/libmvtools.so`). Loading a second copy into the same namespace fails, and `doctor` reports which copy is active.
  - Do not rely on autoload directories. They changed in R74 ([VS releases](https://github.com/vapoursynth/vapoursynth/releases)) and differ between the two targets: F44 (R72) uses `%{_libdir}/vapoursynth`, F45 (R79) uses `site-packages/vapoursynth/plugins` (`rpm -ql vapoursynth-libs` on F44; `dnf repoquery -l python3-vapoursynth` for F45).
  - Load vs-mlrt plugins **before** `import vsmlrt`; otherwise the import raises "cannot load any filters".
  - Scripts insert the directory holding the COPR-packaged `vsmlrt.py` (`buttereye-vsmlrt-py`, §9) into `sys.path` explicitly.
- **Frame rate.**
  - `video_in` has fps 0 and a length of INT_MAX/16.
  - For CFR sources, the script calls `AssumeFPS` with the rate supplied in `user_data`, falling back to `container_fps`.
  - For VFR sources in live mode, `AssumeFPS` would overwrite mpv's per-frame durations and the output would drift until the next seek. Instead the script uses an integer multiplier and sets each output frame's duration to the source duration ÷ multi (via `_DurationNum`/`_DurationDen`). A source counts as VFR when the `ffprobe` probe says so or when `container-fps` is unstable. Fractional, display-matched rates are used only for CFR sources. How each backend behaves on VFR is confirmed in spike M0(a).
  - Inside mpv, vs-mlrt RIFE must use `video_player=True`. Its fractional-multi path builds per-frame lists, which on `video_in` would hang or exhaust memory.
- **Display rate.** Inside the script, `display_fps` ignores `--display-fps-override`, is the nominal mode rate on Wayland, and is frozen at init. The IPC `display-fps` property does honour the override. The core therefore decides the rate and passes it in ([f_output_chain.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/filters/f_output_chain.c), [vo.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/video/out/vo.c)).
- **Colour conversion.**
  - Convert YUV → RGBS/RGBH with an explicit `matrix_in` and range taken from `user_data`. mpv exports only the non-standard `_ColorSpace` and never `_Matrix`, `_Primaries` or `_Transfer`. Never hard-code 709.
- **Output format.**
  - Convert back to planar YUV with the same matrix and transfer. The bridge accepts only YUV in and out.
  - Output is `YUV420P10` for 10-bit or HDR sources, and `YUV420P8` for 8-bit SDR, with no dither for 10-bit output, ordered dither for 8-bit live playback and error-diffusion dither for 8-bit offline renders. MVTools searches at `pel=1` (full-pixel) from 720p up during live playback (about 20-30 % less CPU; vectors are already fine-grained at that size) and at `pel=2` below 720p and in every offline render. These live defaults have no profile key yet. At an integer multiplier the frames that land on a source frame are taken from the source untouched; only the others go through RIFE and the float round trip.
  - 4:2:2 and 4:4:4 sources keep their subsampling.
- **Alignment (vs-mlrt only).**
  - Pad to the per-model alignment, then crop back. Never scale to align.
  - The multiple is 32/scale for RIFE models before v4.25, 64 for v4.25_heavy and v4.26+, and 128 for v4.25_lite ([vsmlrt.py](https://raw.githubusercontent.com/AmusementClub/vs-mlrt/master/scripts/vsmlrt.py)).
  - RIFE-ncnn is expected to pad internally. This is upstream behaviour, confirmed in spike M0(a).
- **Scene detection** always runs before RIFE (§5.5).
- **TensorRT.** mpv scripts never build engines. If the expected engine file is missing, they fail fast (§5.2).
- **Frame properties** are preserved, so mpv's `_MP_IMAGE` property survives. It holds the original image parameters, including HDR signalling. Whether interpolated frames keep `_MP_IMAGE` is **unverified** and is spike M0(d).
- **Buffer sizes.** mpv multiplies `buffered-frames` by `concurrent-frames`. The defaults on a 24-thread box give 96 frames, about 2.4 GB at 4K 10-bit. ButterEye sets explicit values instead:

  | Backend | `concurrent-frames` | `buffered-frames` |
  |---|---|---|
  | RIFE-ncnn | 8, set independently of the plugin's `gpu_thread` (default 4: the soak-tested value; 2 gives only ~52 fps at 1080p 2x on the 4090, M0(f)). In mpv at 1080p 2x on the 4090, 4 gives ~45–47 fps (below real time) and 8 gives ~56–58 fps; `gpu_thread` 8 was no faster and less steady | 4 |
  | vs-mlrt TRT | `num_streams` (2) | 4 |
  | MVTools | min(nproc, 16) (was 8; spike M0(m)) | 4 |

  The benchmark (F14) tries ±1 around each default.

Every seek or format change destroys and recreates the VSScript **and the core** (`createScript`/`freeScript` in [vf_vapoursynth.c](https://raw.githubusercontent.com/mpv-player/mpv/v0.41.0/video/filter/vf_vapoursynth.c)). That means plugin loading, Vulkan device and ncnn model creation, and TRT engine deserialisation all repeat on every seek. Seek latency is therefore a first-class metric for **every** backend (F2, F12).

### 4.5 Hardware and display detection

All detection is read-only, except plugin load tests, which run plugin code in a throwaway subprocess. Nothing proprietary from NVIDIA is loaded into a ButterEye process (§8.1).

| What | Primary | Fallback / extra |
|---|---|---|
| GPU vendor, model, driver | `/sys/class/drm/card*/device/{vendor,device,uevent}` + hwdata `pci.ids` | `lspci` |
| Vulkan devices | `vulkaninfo -j`, **dropping `PHYSICAL_DEVICE_TYPE_CPU` (llvmpipe)**; check the Vulkan loader and at least one ICD | — |
| VRAM | Vulkan memory heaps/budget, read at run time | amdgpu `mem_info_vram_*` ([kernel docs](https://docs.kernel.org/gpu/amdgpu/driver-misc.html)); `nvidia-smi` **subprocess** (optional). NVIDIA exposes no VRAM in sysfs. |
| TensorRT (experimental) | `ldconfig -p` for `libnvinfer.so.11`; `trtexec` on `PATH` (default `/usr/bin/trtexec` from `libnvinfer-bin`) or at a configured path; compute capability and driver via `nvidia-smi --query-gpu=compute_cap,driver_version,uuid --format=csv,noheader` (**subprocess**) | — |
| Plugins | Installed `buttereye-vs-*` RPMs (private plugin directory), a user-built `vstrt`, and distro autoload dirs; load test via the in-mpv probe (F1) | vspipe subprocess |
| Refresh rate | IPC `display-fps` of the actual mpv window | `QScreen.refreshRate()` (correct under Hyprland Wayland on the dev box), `wlr-randr --json`, `kscreen-doctor -j`. Do not use Mutter DisplayConfig, which is an internal API. |

**Multiple GPUs** (PRIME/Optimus laptops, iGPU + dGPU):

- With more than one non-CPU Vulkan device, default to the discrete device with the largest heap.
- Expose `gpu` (Vulkan device index or UUID) in the config. Pass it as `gpu_id` to RIFE-ncnn and as `device_id` to vstrt. Both parameter names are to be confirmed against the pinned plugin versions.
- `doctor` reports which GPU mpv decodes on (`hwdec-current`) and which GPU interpolates. It warns when they differ, because of the PCIe copy cost.

### 4.6 File and config locations (XDG)

| Path | Contents |
|---|---|
| `$XDG_CONFIG_HOME/buttereye/config.toml` | settings, profiles, rules (§4.9) |
| `$XDG_CONFIG_HOME/buttereye/mpv/buttereye.conf` | generated mpv include (`[buttereye]` profile) |
| `%{_libdir}/buttereye/vapoursynth/`, `%{_datadir}/buttereye/…` (RPM-owned, read-only) | COPR-packaged plugin `.so` files, the curated RIFE ncnn models and `vsmlrt.py`, with `%license` files (§9). Not a user path; listed for orientation. |
| `$XDG_DATA_HOME/buttereye/plugins/vstrt/<version>/` | the user's locally built `vstrt` (`contrib/build-vstrt.sh`), if any |
| `$XDG_DATA_HOME/buttereye/models/<model-name>/` | **Only** user-fetched ONNX models for the experimental TRT path, and optional extra ncnn models outside the packaged set ("extra"), each with licence files + SHA-256 manifest. Keyed by **model name**, not by the vsmlrt enum integer, which has no forward-compatibility guarantee. |
| `$XDG_CACHE_HOME/buttereye/engines/` | TensorRT engines keyed by (GPU UUID, driver, TRT version, model, **exact padded W×H, multi, scale**, precision path such as `fp32` or `fp16-onnxconverter-<ver>`) |
| `$XDG_CACHE_HOME/buttereye/downloads/` | in-progress and verified downloads (§4.8) |
| `$XDG_STATE_HOME/buttereye/jobs/<id>/` | offline render manifests, scripts, logs |
| `$XDG_STATE_HOME/buttereye/logs/` | rotated logs |
| `$XDG_STATE_HOME/buttereye/bench.json` | benchmark results |
| `$XDG_RUNTIME_DIR/buttereye/` (0700) | live `.vpy` scripts (0600), IPC sockets, probe output |

- ButterEye never writes to `~/.config/mpv`. It may *offer* to add a single `include=` line to the user's `mpv.conf`, with explicit consent.
- TensorRT engines are NVIDIA-generated artefacts. No export, sharing or "profile bundle" feature may include them, and bug-report bundles exclude `engines/`.
- **Cleanup.**
  - `buttereye clean [--engines|--models|--plugins|--all]` deletes cached and user-installed data (never RPM-owned files), and the GUI Storage page shows the size of each directory.
  - Engine caches are pruned when their TRT version or driver no longer matches.
  - Uninstalling the RPM leaves user data in place. The documentation lists the directories to delete.

### 4.7 First run

1. Starting `buttereye` (CLI or GUI) with no `config.toml` begins the **setup flow**. It runs `doctor` and shows the results grouped as *blocking*, *degraded* or *ok*.
2. If nothing is blocking, it proposes a backend (§5.3). Missing `buttereye-vs-*` packages are reported with the exact `dnf copr enable <owner>/buttereye` and `dnf install …` lines; ButterEye never runs dnf itself. Only if the user opts in to the experimental TRT path does it list the ONNX downloads needed, with the URL, size, licence and SHA-256 of each. Nothing is downloaded until the user confirms (§5.4).
3. It downloads, verifies and installs any confirmed ONNX models (§4.8), then runs a 10-second smoke test: a `vspipe` run plus the in-mpv probe on a generated clip.
4. It writes `config.toml` with `schema_version`, the selected backend and the default profiles.
5. It offers, but never forces, the benchmark and the one-line `include=` for the user's `mpv.conf`.
6. If the user cancels at any step, nothing has been written except logs. The flow can be rerun with `buttereye setup`.

### 4.8 Plugin and model install transaction

Since the 2026-10-07 COPR decision, plugins (RIFE-ncnn `.so`, MVTools, `vsmlrt.py`) and the curated RIFE ncnn models arrive as RPMs from the ButterEye COPR (§9), and dnf handles their installation. The download transaction below applies **only** to vs-mlrt ONNX models for the experimental TRT path and to optional extra ncnn models outside the packaged set (shown as "extra").

- **Transport.** HTTPS only. Redirects are followed only to allowlisted hosts: `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` and `codeload.github.com`. (`files.pythonhosted.org` is no longer needed: MVTools comes from the COPR, not a PyPI wheel.) ButterEye itself never downloads from NVIDIA (§8.3).
- **Download.** Files go to `$XDG_CACHE_HOME/buttereye/downloads/<sha256>.part`, resuming with HTTP Range when possible. `HTTPS_PROXY`/`NO_PROXY` are respected.
- **Verification.** The SHA-256 is checked against the pinned manifest **before** extraction. A mismatch deletes the file and fails with "hash mismatch (expected X, got Y); not installed". There is no override flag in v1.
- **Extraction.**
  - Files are extracted into a temp directory with path-traversal protection: absolute paths, `..`, symlinks and hardlinks are rejected. A size cap of 2× the unpacked size declared in the manifest applies.
  - The result is then moved into place with an atomic `rename()` into `models/<name>/`.
  - `.tar.gz` files are extracted with the Python standard library. `.7z` files (vs-mlrt ONNX model archives, TRT path only) are listed first, then extracted with an external `7z` binary that ButterEye detects. Fedora ships `7zip` 26.03 on F44 (updates) and F45 (`dnf list --available`).
- **Failures.**
  - On network failure, ButterEye makes 3 retries with exponential backoff, then fails with the URL and the reason. Previously installed versions stay usable.
  - Before downloading, it refuses if free space is below the manifest size + 10%.
- **Offline install.** `buttereye models install --from <file>` accepts an archive the user downloaded manually. It is still hash-checked against the manifest. A file with no manifest entry needs a user-supplied hash and is shown as "unpinned".
- **Network code location.** All network code lives in `core/plugins/download.py` (F20).

### 4.9 Config schema

The config is TOML, with `schema_version = 1` at the top level.

- `[general]`: backend override, `gpu` selection, language.
- `[[profiles]]`: `id`, `name`, `backend`, `model`, `scale`, `target` (`"display"` | `"2x"` | `"fps:<n/d>"`), `sc_threshold`, `buffered_frames`, `concurrent_frames`, and `hdr` (`"skip"` (default) | `"passthrough"`, where passthrough is experimental and gated by F7).
- `[[rules]]`: an ordered list; the first match wins. Each rule has a `match` and a `profile` (the profile's id). `match` can use any of `fps_min`, `fps_max`, `width_max`, `height_max`, `hdr_class`, `display_hz_min`, `interlaced` and `path_glob`.
- `[render]`: default encoder per vendor, container, audio.

Behaviour:

- The schema is published as JSON Schema in `data/config.schema.json`.
- Unknown keys produce a warning and are preserved on save.
- A newer `schema_version` causes a read-only load and a warning. Older versions are migrated, with a `.bak` written first.
- An invalid config never crashes ButterEye. It loads the defaults and reports the line and column.
- Shipped default profiles: `quality`, `balanced`, `fast`, `cpu`.

### 4.10 Process model

- `buttereye play` is a foreground process. It owns a socketpair connection for the life of mpv, and mpv exits when it exits.
- The GUI holds connections only for instances it launched or attached to.
  - GUI-launched instances use an `--input-ipc-server` socket in the runtime directory (§4.3), so they outlive the GUI window.
  - Closing the GUI **detaches**. It restores the settings changed on attach and leaves `@buttereye` in place unless the user chooses "disable and detach".
  - The Lua helper keeps working, because it runs inside mpv.
- `buttereye detach` and `buttereye attach` can reconnect to any instance later.

### 4.11 Behaviour matrix (live)

| Situation | Behaviour |
|---|---|
| Source fps ≥ target rate (e.g. a 60 fps source on a 60 Hz display) | No filter is added. The OSD says "already at display rate". |
| Interlaced source (`_FieldBased` ≠ 0 or `video-params` reports interlaced) | Interpolation is skipped, with a warning. |
| HDR source (PQ/HLG) | Follows the profile's `hdr` setting, which defaults to `"skip"`. The OSD shows a notice. |
| Window moves to a monitor with a different refresh rate | Recompute and hot-reload (F4), debounced to 2 s. |
| Rotated or anamorphic sources | Interpolation runs on the coded frame. mpv handles display. |
| Unsupported input format (RGB, gray, or 4:4:4 with odd dimensions) | Skipped, with a warning. |
| Audio-only or image files | No-op. |
| Backend can't keep up (drops > 1% of output frames over 10 s) | OSD warning plus a one-click "step down" (§5.3 step 5). Never switches automatically mid-playback without notice. |
| Video stalls, the GPU device is lost or the GPU faults (Xid) while RIFE-ncnn runs | That session switches to MVTools once, with a notice ("The GPU couldn't keep up, so ButterEye switched this video to CPU smoothing."), and never goes back to RIFE by itself. If MVTools also stalls, interpolation turns off (GUI.md §11.9). |
| No engine can sustain the target at this size (benchmark cap with live headroom) | No filter is added; the video plays without smoothing, with a plain notice (GUI.md §11.9). |
| TRT engine not built yet | Playback uses RIFE-ncnn (or MVTools) while the engine builds in the background. ButterEye switches with a notice once the engine is ready (on the next hot reload). |
| GPU out of memory, or a script evaluation error | Roll back (F4), then show the error. |
| Multiple mpv instances | Each has its own profile and connection. A VRAM warning appears when the summed estimates exceed the budget. |
| Offline render running on the same GPU during playback | Allowed, with a warning; renders always run at lower CPU priority (`nice`). There is no automatic GPU arbitration in v1. |

### 4.12 Security model (summary)

The rules are defined in the sections listed below; this is a summary.

- **IPC** (§4.3): 0700 runtime directory or a socketpair, no abstract sockets, ownership and peer-credential checks on discovered sockets, and a command allowlist.
- **Generated scripts** (§4.4): static templates, data passed only as JSON through `user_data`, mpv length-prefixed quoting, files written with mode 0600 and replaced atomically.
- **Packages and downloads** (§9, §4.8): plugins from COPR-signed, source-built RPMs; ONNX downloads over HTTPS with a host allowlist, pinned SHA-256 checked before extraction, safe extraction, and a manifest that ships inside the package (§5.4).
- **Privacy** (§2): no telemetry and no unsolicited network access. Reports are redacted.

---

## 5. Backends

### 5.1 Matrix

| Backend | GPUs | What it is | Linux availability | Status in v1 |
|---|---|---|---|---|
| **RIFE · TensorRT** (vs-mlrt `vstrt`) | NVIDIA Turing+ (SM ≥7.5) | GPL-3.0 VS plugin (VapourSynth **API3**); links `libnvinfer` dynamically and cudart statically ([CMakeLists](https://raw.githubusercontent.com/AmusementClub/vs-mlrt/master/vstrt/CMakeLists.txt)) | **No Linux release binaries.** Upstream Linux CI currently builds only against TRT 11.1/CUDA 13.3, and its artifacts are CI-only ([linux-trt.yml](https://raw.githubusercontent.com/AmusementClub/vs-mlrt/master/.github/workflows/linux-trt.yml)). Must be built locally by the user (`contrib/build-vstrt.sh`). ButterEye never depends on prebuilt `vstrt`/`mlrt-trt` packages (§8.3), but detects one the user already has. | **Experimental**, opt-in: NVIDIA SM ≥7.5, driver ≥580, user-installed TensorRT 11 from NVIDIA's rhel10 repo, VapourSynth ≤R79 |
| **RIFE · ncnn-Vulkan** ([styler00dollar plugin](https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan)) | Any non-CPU Vulkan GPU (AMD, Intel, NVIDIA) | MIT. API4 plugin (r9_mod_v33, 2025-09-06); dlopens `libvulkan.so.1`; needs libgomp | **ButterEye COPR**, built from source per chroot with the plugin's pinned ncnn (+ glslang) bundled and statically linked (`-Duse_system_ncnn=false`; on Fedora's ncnn the plugin faults the GPU, M0(f); §8.2, §9) | **Default cross-vendor backend** |
| **MVTools** (`mv.FlowFPS`/`BlockFPS`) | CPU | GPL-2.0-or-later per [meson.build](https://github.com/dubhatervapoursynth/vapoursynth-mvtools/blob/master/meson.build) and most headers, with caveats (§8.2). Now at `dubhatervapoursynth/vapoursynth-mvtools`. | **ButterEye COPR**, built from v29.x source per chroot against Fedora `fftw-devel`, patched for R72 (§9) | Lightweight fallback; the only no-GPU option |
| vs-rife (PyTorch / Torch-TRT) | NVIDIA | MIT; multi-GB torch; must be importable from the system Python that mpv embeds | PyPI | **Not in v1** (candidate for later) |
| vs-mlrt `vsncnn` / `MIGX` | — | cannot run RIFE | — | **Excluded** for RIFE |

### 5.2 TensorRT specifics and constraints

**Status: EXPERIMENTAL** (owner decision 2026-10-07). The path targets the vs-mlrt v16.x / TensorRT 11 line, which upstream still publishes only as pre-releases.

- **What "experimental" means.**
  - UI: the backend card and profile picker say "RIFE · TensorRT (experimental)". Docs carry an "Experimental" badge and a pinned-versions table.
  - The path is never auto-selected on first run. F13 ranks TRT only after the user opts in (`trt.experimental = true`). The opt-in dialog states that it depends on vs-mlrt pre-release v16.x and NVIDIA's TRT 11 RPMs, and that it may break on Fedora, TRT or VapourSynth updates.
  - Bug-report bundles tag such runs as experimental.
  - TRT is excluded from the v1 release gates. A TRT failure never fails the v1 release; the ncnn and MVTools paths are the gates (§12 M2).
- **Pinned versions.**
  - Target: vs-mlrt **v16.3.test1** (`vsmlrt.py` 3.23.2), **TensorRT 11.3.0.99-1.cuda13.4**, **CUDA 13.4.2**.
  - Fallback pair: TRT 11.1.0.106 + CUDA 13.3, which is what upstream's Linux CI actually builds ([linux-trt.yml](https://raw.githubusercontent.com/AmusementClub/vs-mlrt/master/.github/workflows/linux-trt.yml)).
  - v15.16 / TRT 10.16 (the last stable vs-mlrt) is **not a target**. A user who already has it may be detected, but it is unsupported.
- **vs-mlrt versions.** Every v16 tag is a GitHub pre-release, there is no non-test v16 as of 2026-10-07, and none ships Linux binaries (assets are Windows builds plus `scripts`/`models`/`contrib-models` `.7z`) ([releases](https://github.com/AmusementClub/vs-mlrt/releases)):

  | Tag | Date | TensorRT | CUDA |
  |---|---|---|---|
  | v16.test1 | 2026-06-06 | 11.0 | 13.2.1 |
  | v16.1.test1 | 2026-06-25 | 11.1 | 13.3.0 |
  | v16.2.test1 | 2026-08-05 | 11.2(.1) | 13.3.1 |
  | v16.3.test1 | 2026-09-23 | 11.3 | 13.4.2 |

- **TensorRT on Fedora 44/45.**
  - NVIDIA's `fedora44` CUDA repo carries no TensorRT, and there is no `fedora45` CUDA repo yet (HTTP 404). The TRT support matrix lists RHEL 8/9/10 and Ubuntu, not Fedora ([support matrix](https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/support-matrix.html)). Fedora use is therefore unsupported by NVIDIA, which is one reason the path is experimental.
  - **Runtime** (from NVIDIA's `rhel10` repo): `libnvinfer-bin`. It pulls `libnvinfer11`, `libnvonnxparsers11` and the plugin/lean/dispatch/vc-plugin libraries. `trtexec` installs at `/usr/bin/trtexec` and the libraries in `/usr/lib64`, so no ldconfig or `LD_LIBRARY_PATH` step is needed. About 1.9 GB download (`libnvinfer11` alone is ~1.88 GB).
  - **Build-only** (for `contrib/build-vstrt.sh`): `libnvinfer-devel libnvinfer-headers-devel` from rhel10, and `cuda-cudart-devel-13-4 cuda-nvcc-13-4` from the `fedora44` CUDA repo. Neither CUDA package depends on an NVIDIA driver package, so they coexist with the RPM Fusion driver. **Never install the `cuda` or `cuda-toolkit` metapackages.** On F45 the fedora44 or rhel10 CUDA repo is used, unsupported by NVIDIA.
  - The TRT RPMs declare no dependency on CUDA or driver packages. `trtexec` links cudart statically.
  - The pip wheels on pypi.nvidia.com include **no `trtexec`** ([install guide](https://docs.nvidia.com/deeplearning/tensorrt/latest/installing-tensorrt/installing.html)).
- **Hardware and driver.** TRT 11 ships builder resources only for SM 7.5 and newer, so Pascal and Volta cannot use this path. Every CUDA 13.x runtime needs driver ≥580; R615+ is recommended for CUDA 13.4 builds ([CUDA release notes](https://docs.nvidia.com/cuda/cuda-toolkit-release-notes/index.html)). The dev box (RTX 4090, SM 8.9, RPM Fusion driver 615.71.09) satisfies both 11.3/13.4 and 11.1/13.3.
- **VapourSynth API3 cliff.** `vstrt` is an **API3** plugin (`VapourSynth.h`, `VapourSynthPluginInit`). VapourSynth R80 dropped API3 ([ChangeLog](https://raw.githubusercontent.com/vapoursynth/vapoursynth/master/ChangeLog)), and upstream closed the port request as not planned ([issue #171](https://github.com/AmusementClub/vs-mlrt/issues/171)). It loads on F44 (R72) and F45 (R79) only. A third-party API4 port exists ([RyougiKukoc/vs-mlrt-api4](https://github.com/RyougiKukoc/vs-mlrt-api4)) but is not adopted.
- **Precision under TRT 11.** TRT 11 removed the weakly-typed APIs, static libraries, and `trtexec --fp16/--int8` ([TRT 11.0 notes](https://docs.nvidia.com/deeplearning/tensorrt/latest/getting-started/release-notes-11/11.0.0.html)).
  - Options are **fp32**, or **fp16 via ONNX conversion**. On fp16, `vsmlrt.py` 3.23.2 converts the model with `onnxconverter_common.float16` before calling `trtexec` (vsmlrt itself falls back to `modelopt`, which ButterEye does not support; see next point).
  - ButterEye supports **`onnxconverter-common` only**. `nvidia-modelopt` 0.47.0 requires Python <3.15 and torch ≥2.8, which rules out F45 ([PyPI](https://pypi.org/pypi/nvidia-modelopt/json)).
  - The converter must be importable from the system Python embedded by vspipe for the engine-build subprocess (from the private path of `buttereye-onnxconverter-common`, which the engine-build script adds to `sys.path`, §9). mpv does not need it, because mpv scripts fail fast when an engine is missing.
  - `onnxconverter-common` 1.16.0 (MIT) is not in Fedora. Its `protobuf>=3.20.2` requirement is unsatisfiable on F44 (python3-protobuf 3.19.6) and must be relaxed in packaging; whether it works there is tested in spike M0(c). F45 (protobuf 6.33.5) satisfies it.
  - **Never pass `force_fp16` or `bf16`** on TRT ≥11: vsmlrt raises an error.
  - `python3-onnx` (distro) is required on the TRT path, in mpv too, whenever RIFE `scale≠1` is used (the v4.6 scale=0.5 4K default), because vsmlrt rewrites Resize constants on every script evaluation.
  - **Quality risk.** On TRT 11, vsmlrt skips the fp32 layer-precision pins it used on TRT <11 to stop fp16 RIFE aliasing and pixel shifting ([issue #66](https://github.com/AmusementClub/vs-mlrt/issues/66)). The fp16-vs-fp32 quality gate in §12 M2 decides whether fp16 is offered.
- **Integration fixes found in spike M0(l)** (2026-10-09), carried by the generated script:
  - `vsmlrt` looks for a loaded plugin at import time: the script loads `libvstrt.so` before `import vsmlrt`.
  - RIFE is called with `_implementation=2` (the `rife_v2` graph pads internally; the v1 graph needs sizes divisible by 64, which 1080 is not).
  - `vsmlrt.convert_model` (3.23.2) breaks RIFE's Cast nodes during fp16 conversion and TRT 11 rejects the result; the script replaces it at run time with the same conversion plus `node_block_list=<Cast nodes>` (reference: `tools/perf/trt_common.py`). The packaged `vsmlrt.py` is still never edited.
  - Use fp16 engines with half-precision frames (`output_format=1`, RGBH clips): fp32 frames cost 1.7× (M0(l)). Two streams at 1080p, four from 1440p up.
- **Engines are built with `trtexec` only**, from `libnvinfer-bin` (`/usr/bin/trtexec`).
  - ButterEye does not use the TensorRT Python bindings (`python3-libnvinfer` is built for Python 3.12 and unusable on Fedora). The Python version therefore does not matter for TRT bindings; Python 3.14/3.15 matters only for the fp16 converter and `python3-onnx` (see Precision above). ButterEye never loads TRT into its own process (§8.1).
- **As built (2026-10-09, `buttereye/core/backends/trt.py`).** Live play, the speed test and offline renders use `rife-trt` when `[general] trt_experimental = true` and `backends.trt.find_install` finds everything; otherwise the path is invisible.
  - User-installed pieces: `vstrt` + `vsmlrt.py` in `$XDG_DATA_HOME/buttereye/plugins/vstrt/<tag>/`; `onnx`, `onnxconverter-common` and `protobuf` in `$XDG_DATA_HOME/buttereye/python` (or the packaged `/usr/share/buttereye/python`), put **first** on `sys.path` in the script because Fedora's `python3-protobuf` 3.19 is too old for `onnx`; vs-mlrt's RIFE ONNX models in `$XDG_DATA_HOME/buttereye/models/vsmlrt/rife_v2/`. `contrib/build-vstrt.sh --with-python-deps --with-models` installs the last two.
  - Engines: `$XDG_CACHE_HOME/buttereye/engines/<model>-<WxH>-<precision>-<hash>/`, keyed by NVIDIA GPU UUID, driver, vstrt build, TensorRT version, model, exact frame size and precision (not streams). A folder is usable once `ready.json` is written after a successful build; the script fails fast without it (§5.2 below).
  - Model names stay ButterEye's (`rife-v4.26_ensembleFalse`), mapped to `vsmlrt.RIFEModel` members (`backends.trt.MODELS`).
  - Live: Automatic ranks TensorRT first but trusts it only with in-mpv speed-test data (like RIFE-ncnn); a pinned TensorRT that can't keep up falls back to RIFE-ncnn, then MVTools. When the engine for the chosen size is missing, the session plays with the next engine, shows "Preparing TensorRT for W × H…", builds it once per core in the background and hot-reloads onto TensorRT when it is ready. A failed build is not retried in that run.
  - Speed test: one TensorRT candidate per installed model, ranked first, built for the measured size, then timed through the playback template itself.
  - Renders: Automatic prefers TensorRT; a pinned engine that isn't installed falls back (never to an unrequested TensorRT); the engine is built before vspipe starts.
- **Engine building runs only out of process.**
  - ButterEye runs the identical graph via `vspipe --end 0` in a subprocess (vsmlrt.py then calls `trtexec`) **before** TRT is enabled in mpv.
  - `vsmlrt.trtexec_path` (still a module global in 3.23.2) is overridden at run time in the generated script to point at the detected `trtexec` (PATH lookup, default `/usr/bin/trtexec`). The packaged file is never edited, and nothing is written into the plugin directory.
  - Engines are static-shape only and keyed as in §4.6, so a first play at a new padded resolution triggers a build.
  - While an engine is missing, live playback uses RIFE-ncnn and the UI shows build progress.
  - Generated mpv scripts check that the expected engine file exists and fail fast if it is absent, rather than building it inside mpv, which would freeze playback for minutes. ButterEye's build subprocess determines the engine path and passes it in `user_data`. The exact vsmlrt cache-path mechanics are confirmed in spike M0(c).
- **Build script.** `contrib/build-vstrt.sh` is modelled on vs-mlrt's [linux-trt.yml](https://raw.githubusercontent.com/AmusementClub/vs-mlrt/master/.github/workflows/linux-trt.yml). (An earlier draft said that workflow builds an "API 4.0" plugin; that was wrong, `vstrt` is API3, see above.)
  - It builds against Fedora's own `/usr/include/vapoursynth` headers (`vapoursynth-devel`) rather than downloading R57 headers.
  - Needs: cmake ≥3.20, a C++20 g++ (Fedora GCC 16.2.1 is inside CUDA 13.4's supported 6.x–16.x range), ninja or make, and `git`. vstrt's CMake runs `find_package(Git REQUIRED)` and `git describe`, so the script clones **upstream** vs-mlrt at the pinned tag (`v16.3.test1`). This is upstream's repository, not a ButterEye one, so it does not conflict with ButterEye having no repository yet.
  - Flags: `-DCMAKE_BUILD_TYPE=Release -DVAPOURSYNTH_INCLUDE_DIRECTORY=/usr/include/vapoursynth -DCUDAToolkit_ROOT=/usr/local/cuda-13.4`. Never `USE_NVINFER_PLUGIN_STATIC` (TRT 11 removed the static libraries). `-march=x86-64-v3` only if the user opts in.
  - It installs into `$XDG_DATA_HOME/buttereye/plugins/vstrt/<version>/`.
  - The user installs the NVIDIA components themselves (§8.3).
  - A local build for one's own use is not distribution, so the GPLv3/TensorRT conflict does not arise. `vstrt` is never packaged in the COPR.

### 5.3 Selection logic

1. Run `doctor` and list the backends whose plugin loads inside mpv (the F1 probe) and whose runtime is present.
2. **No usable GPU** means there is no Vulkan device other than `PHYSICAL_DEVICE_TYPE_CPU`, or the Vulkan loader is missing. In that case only MVTools is offered.
   - The UI says "CPU-only: MVTools interpolation, RIFE unavailable" and gives the reason (no Vulkan driver, or llvmpipe only).
   - If MVTools cannot hold real time in the benchmark, live mode is disabled at that resolution. Offline render stays available.
3. Rank the remaining backends: TensorRT (only if the user opted in to the experimental path, and NVIDIA SM ≥7.5 + driver ≥580 + TRT 11 + loadable `vstrt` on VapourSynth ≤R79 + built engine), then RIFE-ncnn-Vulkan (any non-CPU Vulkan device), then MVTools. Without the opt-in, TRT is never ranked, so a first run never selects it.
4. Pick a model and scale from the in-mpv benchmark results for the actual source resolution and target fps. Without a benchmark, use these conservative defaults:

   | Situation | Default |
   |---|---|
   | TRT (experimental) at 4K | RIFE 4.6 with scale=0.5 (`scale≠1` is rejected for vs-mlrt models ≥4.7; needs `python3-onnx`, §5.2), or a lite model ≥4.7 at scale=1 if the benchmark allows |
   | RIFE-ncnn at 4K | `uhd=True` is passed, but the plugin reads it only for v1-v3 models; the packaged v4.x models ignore it and compute flow at full size (about 4× the 1080p cost, ~17 GB VRAM). What lowers 4K cost is a smaller processing size: live through the benchmark caps (GUI.md §11.9), offline through the render size choice (§7.8) |
   | ≤1440p on mid-range GPUs | RIFE 4.6 or 4.22-lite (4.25-lite not shipped, Appendix B) |
   | 1080p on high-end GPUs | 4.25/4.26. Upstream recommends 4.25 for most scenes ([Practical-RIFE](https://github.com/hzwer/Practical-RIFE)). |

5. If the benchmark shows real time cannot be held, step down: lite model (skipped when this machine's benchmark shows it no more than 5% faster than the full model), then lower multiplier, then MVTools. A custom GPU profile running above 2× steps to RIFE at 2× ("fast") before MVTools. Never drop frames silently without telling the user.
   - Live, the engine is chosen **after** the target (GUI.md §11.9): each engine's cap is its in-mpv benchmark rate ÷ 1.25 (live headroom: the bench runs untimed with `--vo=null`), scaled by pixel count from the closest measured size. The cap is costed in interpolated frames, not output frames: only the output frames that do not land on a source frame are inferred (half at 2×, 4 in 5 at 24 → 60, almost all at 23.976 → 60), so a target's cost is 2 × its interpolated frames per second compared with the 2× benchmark rate. Automatic uses RIFE-ncnn only when it is benchmarked and at least doubles the rate (or matches the best engine), otherwise MVTools; a RIFE-ncnn chosen explicitly that can't keep up uses MVTools for that file with a notice.
6. The user can always override the choice.

Published speed figures are community or vendor estimates and are used only as a sanity check. The VSGAN-tensorrt-docker numbers are vs-rife/Torch-TRT 2x with sc=False and are flagged "no verified output". No primary Linux 4K numbers exist, so ButterEye's in-mpv benchmark is the source of truth.

### 5.4 Model handling

- **Sources.**
  - **RIFE ncnn models: the ButterEye COPR.** `buttereye-rife-ncnn-models` (noarch, MIT) ships a curated subset (v4.26, v4.22-lite and v4.18; v4.25-lite was dropped after GPU faults, Appendix B), repacked from the RIFE-ncnn-Vulkan `r9_mod_v33` tag with the repo LICENSE and a provenance note. Shipping all 74 model directories (~1.49 GB, `gh api …/git/trees/r9_mod_v33?recursive=1`) is impractical. The models live inside the MIT-licensed RIFE-ncnn-Vulkan repo and derive from MIT weights (hzwer/Practical-RIFE, hzwer/ECCV2022-RIFE, nihui/rife-ncnn-vulkan), so they meet COPR's allowed-licence rule. Extra ncnn models outside the subset can still be fetched by the plugin manager from the pinned tag tarball (about 1.3 GB, whole repository), SHA-256-pinned in the manifest and shown as "extra".
  - **`vsmlrt.py`: the ButterEye COPR.** `buttereye-vsmlrt-py` (noarch, GPL-3.0) ships it unmodified, pinned to the same v16.x tag as `contrib/build-vstrt.sh`. The `scripts.*.7z` fetch is no longer needed.
  - **ONNX models for TRT (experimental path only): upstream URLs only**, downloaded on user action and never mirrored or put in the COPR (§8.1). They come from vs-mlrt's [external-models release](https://github.com/AmusementClub/vs-mlrt/releases/tag/external-models) (rife_v4.7 through v4.26, including lite/heavy variants) and from [model-20220923/rife_v8.7z](https://github.com/AmusementClub/vs-mlrt/releases/download/model-20220923/rife_v8.7z) (v4.0–v4.6). The ONNX models are unchanged for TRT 11; engines are rebuilt per TRT version.
  - ButterEye never fetches `models.*.7z` or `contrib-models.*.7z`, which bundle non-RIFE models under other licences.
  - The ncnn plugin loads models through `rife.RIFE(model_path=<absolute model dir>)`. The plugin README says `model_path` "supersedes `model` parameter if specified". The integer `model` index is never used.
- **Manifest.** `data/manifest.toml` records name, version, URL, sha256, size, unpacked size, licence and minimum VS API for each **downloadable** item (ONNX models and extra ncnn models).
  - It ships inside the ButterEye package and changes only with a ButterEye release.
  - ButterEye never fetches a remote manifest in v1, so no signing infrastructure is needed.
  - Users who want newer models wait for a release, or use `--from <file>` with a hash they supply, shown as "unpinned".
- **Consent.** Downloads happen only on user action. Before the first download, the confirmation dialog shows each component's licence (MIT, GPL-3.0, and so on) and its upstream URL.
- **Licences.**
  - The weights are MIT: hzwer/Practical-RIFE (© hzwer) and hzwer/ECCV2022-RIFE (© Megvii Inc.); the ncnn conversions are MIT (nihui/rife-ncnn-vulkan, styler00dollar repo).
  - vs-mlrt states no licence for its ONNX conversions, which may also fall under the repo's GPL-3.0. That unknown licence is why they stay out of the COPR: COPR bars material "governed in whole or in part by a license not contained in the list of acceptable licenses for Fedora" ([COPR docs](https://docs.copr.fedorainfracloud.org/user_documentation.html), [Fedora allowed licences](https://docs.fedoraproject.org/en-US/legal/allowed-licenses/)).
  - Downloaded ONNX models keep both MIT notices plus a pointer to vs-mlrt's GPL-3.0 LICENSE next to the files, and in the About → Licences pane (§13 Q16). Packaged ncnn models carry the four MIT texts listed in §9 as `%license`.
- **Input formats.** RIFE-ncnn takes RGBS only. vs-mlrt takes RGBH/RGBS.

### 5.5 Scene-change handling

- The VapourSynth R72 core has no `misc.SCDetect`. Its only namespaces are `resize`, `std` and `text`, and vs-miscfilters-obsolete is archived.
- The default detector is a `std.PlaneStats`-difference detector that sets `_SceneChangeNext`, **written from scratch**. It needs no extra plugin and works on R72.
  - vs-rife's MIT `sc_detect` is a behaviour reference only. It is not copied into templates, because MIT would then require its notice in every generated script.
- Optional detectors: `mv.SCDetection` (MVTools), and akarin-based expressions only when `buttereye-vs-akarin` is installed (§5.6, §9; API3, VapourSynth ≤R79).
- Both vs-mlrt RIFE and RIFE-ncnn (`sc=True`) consume `_SceneChangeNext` and repeat the frame instead of interpolating across the cut.
- The threshold is exposed per profile (`sc_threshold`).

### 5.6 Multipliers and target rate

- **Target rule.** The display target (`"display"`, the shipped default) is the **lowest** refresh/k (k a positive integer) that at least doubles the source (≥ 1.98×) and that the benchmark says the engine sustains (live: in-mpv benchmark fps ÷ 1.25, scaled to the video's pixel count, GUI.md §11.9). For example, 23.976 → 60 on 180 Hz, → 48 on 144 Hz, → 59.94 on 119.88 Hz (60 on 120 Hz). Every output frame costs GPU inference, transfers and colour conversion, so a higher rate than doubling buys little smoothness for up to twice the work.
  - If no such refresh/k fits, plain 2× is used when it fits below the display rate (23.976 → 47.952 on 180 Hz with a 1080p RIFE-ncnn at ~61 fps in the bench), then the highest refresh/k that fits (50 fps on 60 Hz → 60).
  - Cost is counted in interpolated frames, not output frames (§5.3 step 5): 23.976 → 60 infers almost every output frame and costs about as much as 2× at 120.
  - `"display-max"` (Profiles: "Maximize smoothness (highest display rate)"): the **highest** refresh/k ≥ 2× that fits, multiplier ≤ 5× (23.976 → 119.88 on 119.88 Hz, but 60 on 120 Hz, where 120 would be just over 5×; on 180 Hz, 23.976 → 90, refresh/2, ≈3.75×, rather than 7.5×), with the same fallbacks.
  - On VRR outputs, cap the target at the benchmark-sustainable rate and do not chase the mode maximum.
  - The `target` profile key can force `"2x"` or a fixed `"fps:n/d"`.
- **Live.**
  - The benchmark rule matches sources up to the measured rate + 1 % (`fps_max` = 24000/1001 × 101/100), so files tagged 24.0031 or 24 fps still use it.
  - RIFE-ncnn supports fractional or target-fps interpolation natively (`factor_num/den`, `fps_num/den`).
  - vs-mlrt uses `video_player=True`.
  - VFR sources use integer multipliers only (§4.4).
- **Offline.**
  - Integer multipliers work on all backends.
  - Fractional multipliers on vs-mlrt need the akarin plugin (LGPL-3.0, v1.5.0). It is optional in v1, and fractional TRT offline (experimental path) is not guaranteed. If needed, it ships as the optional COPR package `buttereye-vs-akarin` (§9); otherwise it is left out. The AkarinVS plugin is API3, so it shares `vstrt`'s R80 cliff (§5.2).
  - Never pass a VFR clip to vs-mlrt with a fractional multiplier: it treats VFR as 1 fps.

---

## 6. v1 feature list with acceptance criteria

"Dev box" means Fedora 44, mpv 0.41.0-5, VS R72 and an RTX 4090. "Cross-vendor box" means a Fedora 44 or 45 machine with an AMD or Intel discrete Vulkan GPU; having one is a **precondition of M0** (§12). Criteria for the TensorRT path (F11, F12, and the TRT parts of F13/F14) are recorded on the dev box but are **not v1 release gates**, because that path is experimental (§5.2). Each criterion names the box it runs on.

| # | Feature | Acceptance criteria |
|---|---|---|
| F0 | First run | On a clean user account, `buttereye setup` reaches a playable state using only the COPR packages plus any downloads the user confirmed (none are needed for the ncnn and MVTools paths). Cancelling at any step leaves no files outside `$XDG_STATE_HOME/buttereye/logs`. Re-running is idempotent. |
| F1 | `buttereye doctor` | **mpv checks:** version ≥ 0.41.0; `vapoursynth` filter present (`mpv --vf=help`); mpv is a host binary, not `flatpak`/`snap` (detected via the binary path or `/proc/<pid>/root`). Each unsupported case is blocking with a fixed message: filter missing → "your mpv was built without VapourSynth; install the distro mpv package"; Flatpak/Snap → "unsupported in v1, see §9"; version too old. **In-mpv probe:** runs `mpv --no-config --vo=null --frames=1 --vf=vapoursynth=file=probe.vpy av://lavfi:testsrc2,format=yuv420p`, which writes `core.version()`, `sys.version`, `sys.path`, each plugin's load result and whether `import vsmlrt` resolves to a JSON file in `$XDG_RUNTIME_DIR/buttereye/`. Doctor compares this with `vspipe --version` and the python3 module, and reports which copy of each plugin is active (distro or ButterEye). **Packages:** missing `buttereye-vs-*` / `buttereye-rife-ncnn-models` packages are reported with the exact `dnf copr enable <owner>/buttereye` and `dnf install …` lines (doctor never runs dnf), and installed RPM `%license` files are checked to be present. **TensorRT (experimental) section**, shown only after opt-in and never blocking the ncnn or MVTools paths: (a) `nvidia-smi` subprocess reports compute_cap ≥7.5, else "TRT 11 does not support this GPU"; (b) driver major ≥580, R615+ recommended; (c) `libnvinfer.so.11` found by `ldconfig -p` and equal to the pinned 11.x, else a warning that engines will rebuild and `vstrt` was built against a different minor; (d) `/usr/bin/trtexec` present; (e) `vstrt` loads in the in-mpv probe and `core.trt.Version()` matches libnvinfer; (f) VapourSynth core ≤R79, else blocking for TRT: "vstrt and akarin are API3 plugins and cannot load on R80+"; (g) `onnxconverter_common` importable in the vspipe engine-build Python (including the `buttereye-onnxconverter-common` private path), else only fp32 is offered; `onnx` importable in the Python that mpv and vspipe embed, else RIFE `scale≠1` profiles (v4.6 scale=0.5) are disabled. **Also reports:** Vulkan loader present, ≥1 ICD, and `vulkaninfo` returns the selected device, with CPU devices filtered out (a missing `vulkan-loader` or Mesa/NVIDIA ICD is named together with the dnf package to install); conflicting user mpv settings (non-copy `hwdec`, `interpolation=yes`, `save-position-on-quit` without `watch-later-options-remove=vf`); mpvSockets in use; NVIDIA Xid errors in `journalctl -k` since the last RIFE-ncnn session started, reported as "GPU fault in RIFE-ncnn" (a session can finish and still have faulted, M0(f)). Exit code 3 on a blocking issue. Runs in under 10 s with no network access. |
| F2 | `buttereye play FILE` | Launches mpv with the include, profile, Lua helper and socketpair IPC. On a 1080p 23.976 fps sample at 2x with RIFE-ncnn (dev box and cross-vendor box), over 5 minutes of playback, `frame-drop-count`, `decoder-frame-drop-count`, `vo-delayed-frame-count` and `mistimed-frame-count` each grow by ≤ 0.1% of output frames. Separately, mpv with `--untimed --vo=null` sustains ≥ 1.15× the target output rate. Seek-to-first-interpolated-frame is ≤ 1.0 s at 1080p with RIFE-ncnn, measured from the `seek` reply to the next `playback-restart` event (this threshold is validated in spike M0(b)). `~/.config/mpv` is byte-identical before and after. |
| F3 | Attach to a running mpv | Discovers instances in `$XDG_RUNTIME_DIR/buttereye/` and `/tmp/mpvSockets/`, applying the socket checks in §4.3. Refuses sockets owned by another uid, and instances that fail F1's mpv checks. Applies the attach settings and restores them on detach. Enables and disables interpolation without restarting playback. Never changes the instance's socket path or any `vf` entry other than `@buttereye`. |
| F4 | Hot reload and rollback | Changing a profile parameter during playback applies within one reload, verified through `user-data` `gen` in the `vf` property. A deliberately broken script results in `@buttereye` being removed and playback continuing without interpolation, with an error shown. |
| F5 | Atomic script writes | A stress test of 100 seeks while profiles change continuously produces no "failed to evaluate script" errors. |
| F6 | Colour-correct conversion (SDR) | For BT.709, BT.601 and BT.2020 non-constant-luminance SDR samples: on frames passed through unchanged (even output indices at 2x), grabs taken with the filter on and off (`screenshot-to-file … video`) have mean ΔE2000 < 0.5 and max < 2.0, computed in the source's colour space. The template never contains a hard-coded matrix. |
| F7 | HDR live handling | By default, HDR sources (PQ/HLG) bypass interpolation with an OSD notice. `hdr="passthrough"` is opt-in and experimental, and is enabled only if spike M0(d) passes. In that case mpv's `video-out-params` primaries, gamma and max-cll on an HDR10 sample are identical with the filter on and off. |
| F8 | Lua helper | Keybindings toggle the filter, cycle presets and show stats. The OSD shows backend, model, target rate, and the drop and delay counters. Works after the GUI window is closed, for both launched and attached instances. Shows a one-line licence notice on its stats page (F18). |
| F9 | Hardware detection | Correct vendor, model, driver, VRAM and Vulkan device list on the dev box and the cross-vendor box. llvmpipe is never selected as a backend device. On a two-GPU fixture, the selection is deterministic and can be overridden. No NVIDIA library is loaded in-process (§8.1 test). |
| F10 | Plugin and model manager | The COPR (dnf) is the primary and only source of the RIFE-ncnn `.so`, the curated RIFE ncnn models, MVTools and `vsmlrt.py`. On a clean F44 and F45 install from the COPR, every `buttereye-*` package's `%license` files are present (RIFE-ncnn-Vulkan MIT plus the bundled ncnn and glslang `LICENSE.txt`; MVTools GPL-2.0 text; models MIT + provenance note; vsmlrt.py GPL-3.0), and the plugins load from the private directory via the §4.4 guard. The download manager (§4.8) applies **only** to ONNX models (and "extra" ncnn models): it uses pinned SHA-256 verification and refuses a mismatching hash; `buttereye models list/add/remove` works offline for models already downloaded; killing the process mid-download or mid-extract leaves no partial install, and the next run resumes or cleans up; a simulated 404, TLS failure, truncated file and wrong hash each produce the documented message and a non-zero exit. |
| F11 | vstrt build script (`contrib/`) | Documented and pinned to vs-mlrt v16.3.test1 / TRT 11.3 / CUDA 13.4 (§5.2). **Experimental; not covered by v1 acceptance.** Tested on the dev box in spike M0(c) and before each release. It never installs NVIDIA components itself: for the rhel10 and fedora44 NVIDIA repos it prints the exact `dnf` command (`libnvinfer-bin libnvinfer-devel libnvinfer-headers-devel cuda-cudart-devel-13-4 cuda-nvcc-13-4`, never `cuda`/`cuda-toolkit`) and NVIDIA's SLA link, and the user runs it. It may print, never run, `dnf versionlock add 'libnvinfer*'`. |
| F12 | TRT engine cache (experimental) | Engines are built only out of process (§5.2). The second launch with the same key performs no engine build. A first play at a new resolution uses the ncnn fallback during the build. Engines are static-shape. Seek-to-first-frame with a cached engine is ≤ 1.0 s at 1080p (threshold validated in spike M0(b)). If the threshold is missed, the profile falls back to RIFE-ncnn (or MVTools) or disables interpolation while scrubbing. |
| F13 | Backend auto-selection | Picks the backend and model per §5.3, deterministically for identical inputs (unit-tested with fixtures). With a fixture that has no Vulkan devices, or only llvmpipe, selection returns MVTools or "none" with a reason. It never returns RIFE-ncnn in that case. Without `trt.experimental = true`, it never returns TensorRT, even on a fixture with TRT fully present. |
| F14 | Benchmark | `buttereye bench` benchmarks the current source's resolution for the top 2 candidate configurations on demand. `--full` runs the 720p/1080p/1440p/2160p matrix, which is optional and not part of acceptance. Each configuration is measured twice: (a) `vspipe` throughput, and (b) in-mpv throughput via `mpv --no-config --untimed --vo=null --vf=vapoursynth=file=<vpy>:… av://lavfi:testsrc2=size=WxH:rate=R,format=<nv12\|p010le> --frames=N`. The semi-planar input makes mpv's autoconvert and `mp_image_new_copy` costs show up, as they do with copy-back hwdec. A run on the user's actual file is optional. Selection (§5.3) uses (b). It also measures startup/reload latency and VRAM. Only **generated** clips are used (`testsrc2`/`mandelbrot`, or VapourSynth `BlankClip`/`Expr`). Results go to `bench.json`. Repeatable means a coefficient of variation < 5% over 3 runs after 1 warm-up run. |
| F15 | Profiles and rules | Rules match on source fps, resolution, HDR class, display refresh, interlacing and path glob, and select a profile by first match (§4.9). Covered by unit tests with fixtures. Round-tripping a config through load and save preserves its content (comments are not preserved). Migration from v0 fixtures is tested. Invalid TOML gives a line-numbered error and exit code 2. |
| F16 | Offline render | See the §7.7 acceptance list. |
| F17 | GUI | Every CLI operation is reachable from the GUI. Works without a system tray. The GUI process imports only `PySide6.QtCore`, `PySide6.QtGui` and `PySide6.QtWidgets` from PySide6. It never imports `vapoursynth`, `QtNetwork`, `QtDBus`, `QtCharts`, `QtGraphs`, `QtDataVisualization` or any other PySide6-Addons module, enforced by a test (any exception needs an owner decision). Fully keyboard-operable with visible focus. Every control has an accessible name, checked with Orca on GNOME. No status is shown by colour alone. Follows the system font scale and theme. |
| F18 | Licence surfacing | **Third-party:** the About → Licences pane lists detected components (informational) and the full MIT texts of the RIFE weights. `THIRD-PARTY` and `NOTICE` ship in the package. **ButterEye's own Appropriate Legal Notices:** the About dialog, `buttereye --version` and `buttereye licence` (listed in `--help`) show the copyright notice, the no-warranty statement, the licence name, a way to view the full AGPL text (shipped in the package, not only as a URL), the §7 output permission (§8.4), and a link to the source (the public repository once it exists; until then the COPR SRPM, §9). |
| F20 | No network by default | Running play, attach, render, bench and doctor inside a network namespace with no route (`unshare -n`) succeeds, given the COPR packages are installed. A CI check fails on network calls outside `core/plugins/download.py`. |
| F21 | Actionable errors | Every blocking `doctor` finding and every install, render or attach failure has a stable error code (`BE-xxxx`), a one-line cause and a one-line fix, documented in `docs/errors.md`. Tests assert the codes, not the message text, so translations do not break them. |

(F19, global shortcuts, is deferred to after v1. The fallback, in-mpv keybindings via the Lua helper, is F8.)

### 6.1 Testing strategy

- **Unit tests** (no GPU, no mpv): rules, selection (using fixtures of doctor and bench output), config migration, mpv option quoting, the IPC command allowlist, and IPC framing against a fake mpv socket server that replays recorded transcripts.
- **Golden tests.** Generated `.vpy` files are snapshot-compared for a matrix of {backend × colour class × model alignment × entry point}. Each is also executed by `vspipe --info` with stub plugins, or with MVTools on CPU. The injection test from §4.4 runs here.
- **CI integration** (hosted CI once a repository exists; until then the same suite runs locally in Fedora 44 **and** Fedora 45 containers/toolboxes; no GPU; from M1 onward):
  - Real mpv with `--vo=null --ao=null` plus MVTools installed from the COPR, covering launch, attach, socket checks, hot reload, rollback, the atomic-write stress test (F5), `watch_later` behaviour and the `--include`/`--profile` ordering.
  - Offline render end-to-end with MVTools plus libsvtav1 or ffv1.
  - The no-network test (F20).
- **GPU tests** on a self-hosted runner: the dev box and the cross-vendor box, run manually or nightly. Covers F2, F6, F7, F9, F12, F14 and the spike scripts. Results are attached to the release checklist.
- **Test media.** Media is generated by script (`ffmpeg -f lavfi testsrc2`, `smptehdbars`, plus HDR10 and VFR variants), or is CC0, or is CC-BY with attribution recorded in `tests/media/README` (for example Blender open movies). Never commit vendor HDR or Dolby Vision demo clips or other copyrighted samples. Tests that need such clips read their path from an environment variable.
- **Quality gates:** ruff, mypy `--strict` on `core/`, REUSE lint, the GUI-import test (F17), the in-process NVIDIA-library test (§8.1), and the no-network test.
- **Coverage target:** 80% of lines in `core/`, excluding `bench/`.

---

## 7. Offline render mode

### 7.1 Pipeline (default; works on R72 and R79+)

```
ffprobe source                       -> colour / HDR / VFR / stream inventory
generate job.vpy (source entry point; parameters via vspipe -a)
vspipe -c y4m -p -r <N> job.vpy -    |  ffmpeg -f yuv4mpegpipe -i - \
     -color_primaries P -color_trc T -colorspace M -color_range R \
     <encoder + HDR params> -an -progress pipe:3 -nostats video.mkv
mkvmerge -o out.mkv [--color-* / mastering flags] [--sync] video.mkv -D source.ext
```

- **`-r <N>`** bounds the frames in flight: 8 for RIFE-ncnn (at least `gpu_thread` + 2), which runs only `gpu_thread` frames at once. vspipe's default (one per CPU thread) only holds float RGB frames waiting for the GPU: a 4K 23.976 → 60 render used 18.9 GB at the default and 14.5 GB with `-r 8`, at the same speed. MVTools (CPU-bound) keeps the default.
- **Use vspipe, not `ffmpeg -f vapoursynth`.** The ffmpeg VapourSynth demuxer requests frames synchronously, one at a time. It reads the deprecated `_ColorRange`, carries no mastering data, and **leaves pts unset for VFR scripts** ([libavformat/vapoursynth.c](https://raw.githubusercontent.com/FFmpeg/FFmpeg/release/8.1/libavformat/vapoursynth.c)).
- **The y4m header carries no colour or range metadata** ([vspipe.cpp R72](https://raw.githubusercontent.com/vapoursynth/vapoursynth/R72/src/vspipe/vspipe.cpp)). Colour flags are therefore always passed explicitly, from the `ffprobe` results.
- **vspipe `-c mkv`** exists from R79, which ships in Fedora 45. It carries only a ColourSpace FourCC (no HDR elements) and refuses `--start/--end` with more than one track. v1 keeps y4m as the single code path and feature-detects mkv for later use.
- **Source filter (hard dependency).**
  - On R72 the only workable source filter is Fedora's `ffms2`, loaded with `LoadPlugin('/usr/lib64/libffms2.so.5')`. It is not in the autoload directory, and it is **untested on R72** (spike M0(e)).
  - ffms2 decodes whatever the system libavcodec decodes. With `ffmpeg-free` that excludes H.264 and HEVC; AV1, VP9, MPEG-2 and ProRes are included. Decoding H.264/HEVC needs RPM Fusion's `libavcodec-freeworld` (or `ffmpeg-libs`).
  - `doctor` checks, per codec, that ffms2 can decode by indexing a 1-second generated sample. Render is disabled, with an explanation, for codecs that fail the check. Without ffms2 at all, render is disabled.
  - The PyPI `bestsource` wheel (needs VS ≥80) does not run on R72 or R79. L-SMASH-Works `lsmas` (needs VS ≥74) could run on F45's R79; spike M0(e) evaluates it, and if adopted it ships as a source-built COPR package (`buttereye-vs-lsmas`), never as a PyPI wheel.
  - If bestsource is used later, pass `variableformat=0` (RIFE needs a constant format) and be aware of its default auto-rotation.

### 7.2 Stream preservation

- **Container: MKV only in v1.** MP4 is deferred.
- **Remux** with `mkvmerge … -D source`, which takes everything except video: audio, subtitles, chapters, attachments and tags. mkvtoolnix 99.0 is in Fedora updates and is a recommended dependency.
- **Fallback when mkvtoolnix is absent:** one-pass ffmpeg mapping (`-map 1:a? -map 1:s? -map 1:t? -map_chapters 1 -map_metadata 1 -c copy`).
- **Start offset.** Carry over the source's video start time or audio delay (`--sync` / `-itsoffset`), because the y4m stream always starts at 0.
- **mkvmerge flag spelling.** Flags use American spelling: `--color-primaries`, `--color-transfer-characteristics`, `--color-matrix-coefficients`, `--color-range`, `--white-color-coordinates` ([mkvmerge docs](https://mkvtoolnix.download/doc/mkvmerge.html)).

### 7.3 VFR

- Detect VFR when `r_frame_rate ≠ avg_frame_rate`, then confirm by finding more than one distinct frame duration.
- **v1 behaviour:** convert to CFR at the source filter (fpsnum/fpsden) and warn the user. vs-mlrt RIFE keeps the original per-frame durations on VFR input, which would desync A/V by about the multiplier factor.
- **Preserve-VFR mode is deferred to after v1.** It needs `_DurationNum/_DurationDen` rewritten after RIFE, then `vspipe -t` v2 timecodes fed to `mkvmerge --timestamps`. On R72 the timecodes file must be rounded first, because mkvmerge rejects R72's excess decimals; R80 fixed that.

### 7.4 HDR

- **Classify the source** as SDR, HDR10, HLG, HDR10+ or DV from `ffprobe -show_frames -read_intervals %+#1` side data.
- **Scope (owner decision 2026-10-07):** HDR10 offline via x265/SVT-AV1 is in v1. HLG, Dolby Vision and NVENC/VAAPI HDR are explicitly deferred. Scope is not to be widened silently; any widening needs a new owner decision.
- **HDR10 (the only HDR class supported offline in v1):**
  - Encode with `libx265` (`-x265-params hdr10=1:master-display=…:max-cll=…`) or `libsvtav1` (`-svtav1-params mastering-display=…:content-light=…`). These are the only encoders that accept explicit mastering data on the CLI.
  - Also write container-level colour metadata with mkvmerge.
- **NVENC and VAAPI are offered for SDR only.** They take mastering data only from frame side data, which the y4m pipe does not carry.
- **HDR10+ and Dolby Vision dynamic metadata** cannot be mapped onto interpolated frames. HDR10+ sources, and Dolby Vision sources with an HDR10-compatible base layer, are rendered as plain HDR10 with a warning that the dynamic metadata (HDR10+ / DV RPU) is dropped. This is HDR10 output, not DV support.
- **Refused with an explanation in v1:**
  - Dolby Vision profile 5, whose behaviour is unverified.
  - Other DV profiles that have no HDR10 base layer.
  - HLG (deferred to after v1).
- RIFE runs on PQ-encoded RGB without linearisation. The quality impact is untested (§13 Q20).

### 7.5 Encoders

- Probe `ffmpeg -hide_banner -encoders` and offer only the encoders present.
- Fedora `ffmpeg-free` has NVENC, VAAPI, QSV, AMF, libsvtav1, libaom, librav1e, ffv1, prores_ks and libopenh264, but **no libx264 or libx265**. RPM Fusion's ffmpeg adds x264 and x265 ([enable_encoders](https://src.fedoraproject.org/rpms/ffmpeg/raw/f44/f/enable_encoders)).
- Fedora 45 moves `ffmpeg-free` to 9.0.2, so probing must handle FFmpeg 9.
- **Default video encoders:**

  | Case | Default |
  |---|---|
  | SDR, NVIDIA | `hevc_nvenc` |
  | SDR, AMD/Intel | `hevc_vaapi` |
  | SDR, software | `libsvtav1` |
  | HDR10 | `libx265` if present, else `libsvtav1` |

- **Encoder settings** (`render/pipeline.py`): `hevc_nvenc` p5/hq CQ 20, `av1_nvenc` p5/hq CQ 28, `libx264` medium CRF 18, and the software HEVC/AV1 encoders **one preset lighter than their defaults**: `libx265` `fast` CRF 20 (not `medium`) and `libsvtav1` preset 8 CRF 28 (not 6). At the doubled frame rate `medium`/6 kept about 9/8 CPU cores busy; `fast`/8 are about 1.5-2× lighter, at the cost of files a few % larger (lower quality per bit) at the same CRF. There is no setting to go back to `medium`/6 yet; a hardware encoder avoids the trade-off.
- **Default audio:** `-c:a copy` (via the remux). If re-encoding is needed, use the native `aac` encoder. Never default to `libfdk_aac`.

### 7.6 Progress and cancel

- **Progress:**
  - vspipe writes `Frame: N/T (X fps)` lines terminated by `\r`, so stderr is split on `\r`. Errors match `Error: Failed to retrieve frame N…`.
  - ffmpeg's `-progress pipe:3` writes key=value lines.
  - The ETA is computed from the slower of the two.
- **Cancel:**
  - The pipeline runs in its own process group (`start_new_session`).
  - Cancelling sends SIGTERM to the group and then deletes partial files. R72 vspipe has no Ctrl-C handling on non-Windows systems; R76 fixed that.
- **Free-space check.** Before starting, estimate the output size as encoder bitrate × duration, plus temporary files. Refuse to start if the space is not available.
- **Priority.** Renders always run at lower CPU priority (`nice` 10 on the vspipe/ffmpeg process group; the remux under `nice` and best-effort `ionice -c2 -n7`), so playback, the GUI and the desktop keep the CPU whether they start before or after the render (§4.11). This does not arbitrate the GPU.
- **Deferred to after v1:**
  - Pause (SIGSTOP/SIGCONT; GPU memory would stay allocated).
  - "Stop and keep" (kill only vspipe so ffmpeg finalizes; untested).
  - Chunked resume (`vspipe -s/-e` plus a manifest plus mkvmerge append). Seamless joins are unverified, and R72 treats negative frame requests as fatal.

### 7.7 Offline acceptance criteria

1. **SDR 1080p H.264 → 2x HEVC** (RPM Fusion libavcodec present). The output frame count is 2 × the input (±1). Audio, subtitles, chapters and attachments are present and identical (stream hash). A/V drift is below 1 frame at the end of a 30-minute file.
2. **HDR10 source → libx265 or libsvtav1.** `ffprobe` on the output shows matching primaries, transfer, matrix, range, mastering display and max-cll.
3. **VFR source.** The output plays in sync in mpv, and the UI reported the CFR conversion.
4. **Cancel** leaves no partial files.
5. **Progress** is monotonic and ends at 100%. The ETA is within ±20% once 10% is complete.
6. **Encoder list** matches `ffmpeg -encoders` on both the ffmpeg-free and the RPM Fusion builds, on F44 (FFmpeg 8.1) and F45 (FFmpeg 9.0).
7. **ffmpeg-free only.** H.264/HEVC sources are refused with the documented error code. AV1 and VP9 sources render.

Test media follows the rules in §6.1.

### 7.8 Output size and the simple window (amendment 2026-10-08)

- **Smaller output size (owner decision 2026-10-08).** A render may be made at the source size (default) or at a smaller size: 1440p, 1080p or 720p below the source height, same shape, even sides. The script shrinks with Spline36 before interpolating (the same `size` step live playback uses, GUI.md §11.9). Upscaling stays a non-goal. Reason: on the dev box (RTX 4090) RIFE v4.26 converts 4K at ~10 output fps — about 7–8 hours for a 97-minute film at 2× — and 1080p at ~47 fps (~1.5–2 hours).
- **Estimated time per size.** The add-job dialog shows an estimated time for each size, from the benchmark's vspipe rate scaled by pixel count plus the measured cost of decoding and shrinking the source (`decide.shrink_cap`).
- **Target rate offline.** `Double (2×)` or a fixed fps from the profile; a display target ("Match your display") has no meaning for a file and renders as 2×.
- **Engine offline.** The profile's engine; Automatic means RIFE-ncnn when it is installed with a model (time, not real-time speed, is the cost offline), else MVTools.
- **First cut: SDR only (owner decision 2026-10-08).** SDR/CFR conversion ships first, then HDR10 (§7.4) as before. Until then an HDR10 source is refused with a plain explanation (BE-4003), not converted wrongly.
- **Simple window.** Each Now playing row has "Save smooth copy…", and the window has a second drop zone, "save a smooth copy", beside the play zone (amended 2026-10-09; its button is "Convert a video…"). One small dialog (size with estimated times, target, encoder, output path); a progress row with Cancel. Details are in GUI.md §12.7.

---

## 8. Licensing and compliance

### 8.1 Principles

- **Three relationships, kept apart** (GPL FAQ: [MereAggregation](https://www.gnu.org/licenses/gpl-faq.html#MereAggregation), [GPLPlugins](https://www.gnu.org/licenses/gpl-faq.html#GPLPlugins)):
  1. Python imports in ButterEye's own process combine code into one work. The only such third-party import planned is PySide6-Essentials (plus the Python standard library).
  2. mpv, vspipe, ffmpeg, ffprobe, mkvmerge, `7z`, `trtexec`, `vulkaninfo` and `nvidia-smi` are separate programs, connected by pipes, sockets or arguments.
  3. VapourSynth plugins, `vsmlrt.py`, TensorRT and the ncnn plugin load inside mpv or vspipe, not inside ButterEye. This holds **only** while ButterEye never evaluates scripts in its own process. Every probe, benchmark and engine build therefore runs in a subprocess.
- **No proprietary libraries in ButterEye's processes.**
  - ButterEye never imports `tensorrt` or `pynvml`, and never uses ctypes to load `libnvinfer`, `libnvidia-ml` or `libcuda`. A test enforces this.
  - VRAM on NVIDIA comes from an `nvidia-smi` subprocess. Engines are built by `trtexec`, run by vspipe.
  - Because of this design, no NVIDIA linking exception is needed. The owner confirmed on 2026-10-07 that ButterEye will never load TensorRT, CUDA or NVML in-process, so no NVIDIA §7 exception is added. The GPU driver's GL/Vulkan libraries that Qt itself may load for rendering are treated as System Libraries (§13 Q18).
- **Combining with GPL-3.0 is allowed.** AGPL-3.0 §13 permits combining with GPL-3.0 code ([AGPL text](https://www.gnu.org/licenses/agpl-3.0.html)). GPLv2-*only* code would be the hard incompatibility.
  - MVTools declares GPL-2.0-or-later in `meson.build` and in most file headers. However, its readme says only "GPL 2", and several files (MVAnalyse.cpp, MVSuper.cpp, EntryPoint.cpp) have no header.
  - ButterEye's compliance therefore does not depend on "or later". MVTools stays a separately loaded plugin in mpv or vspipe. It is never imported into or combined with ButterEye's process, and never vendored into ButterEye's source tree. In the COPR it is a separate package (`buttereye-vs-mvtools`) built from its own SRPM, so ButterEye and MVTools are conveyed as an aggregate.
- **Package, don't fetch.** ButterEye itself is pure-Python AGPL code. The FOSS native plugins it needs are built from source and shipped through the project COPR (§9). The pinned ncnn and glslang are bundled into `buttereye-vs-rife-ncnn` (§8.2). Everything else native (mpv, VapourSynth, ffmpeg, ffms2, fftw) comes from Fedora, and NVIDIA components are installed by the user.
- **The COPR is the one distribution channel for third-party code.** Since 2026-10-07 the project COPR conveys the FOSS plugins (RIFE-ncnn-Vulkan with its bundled ncnn and glslang, MVTools, `vsmlrt.py`, optionally akarin and onnxconverter-common) and the curated MIT RIFE ncnn models. The project is therefore a GPL/MIT/LGPL distributor of those, with full licence texts and source duties. All other project infrastructure (GitHub releases, website, CI artifacts, any download mirror) still never hosts third-party binaries or models. Never conveyed anywhere: a `vstrt` binary, TensorRT, CUDA, TensorRT engines, ffmpeg, and the vs-mlrt ONNX conversions (no stated licence, so COPR's allowed-licence rule bars them; [COPR docs](https://docs.copr.fedorainfracloud.org/user_documentation.html)). (The `buttereye-vsmlrt-py` SRPM carries the unmodified vs-mlrt tag tarball, which includes GPL-3.0 `vstrt` source and no NVIDIA files; nothing from it is compiled.)
- **SRPM source duty (concrete obligations).**
  - Each SRPM contains the spec, patches and upstream source tarballs. A tarball is pristine unless it contains material the COPR must not convey; such tarballs are repacked to remove it, with the repack script and the upstream URL + SHA-256 of the original shipped inside the SRPM. Repacks: (a) RIFE-ncnn-Vulkan for `buttereye-vs-rife-ncnn`, with `models/` removed (the tag tarball carries all 74 model directories, ~1.3–1.5 GB, several with no recorded provenance) and the `subprojects/ncnn` submodule left out (the pinned ncnn and glslang ship as their own pristine tarballs); (b) the curated MIT model subset for `buttereye-rife-ncnn-models`; (c) akarin with `ngx/` and `vfx/` removed, if packaged (§9). MIT/LGPL/GPL impose no duty to convey removed, unused files.
  - The bundled ncnn and glslang are pristine GitHub archive tarballs with their SHA-256 recorded in the spec. GitHub archives are not guaranteed byte-stable, so the SRPM carries the tarballs and builds never re-download them.
  - The COPR project is created with internet access during builds **off** (`copr-cli create … --enable-net off`; the CLI/API default is on, [bz 1335236](https://bugzilla.redhat.com/show_bug.cgi?id=1335236)), so each SRPM provably carries the complete corresponding source.
  - Under COPR's Pulp storage backend, the SRPM is published inside each chroot repository as an `arch=src` package, so source travels in the same repo as the binary (checked against `flawlessmedia/av-rpm`'s F43 repodata).
  - Retention on Pulp is the 5 most recent successful builds per RPM Name; `auto-prune` and `persistent` are admin-only. Subpackage names therefore stay stable, every subpackage is built from the same SRPM in every build, and a subpackage is never dropped without bumping and rebuilding the whole SRPM, so no binary outlives its SRPM.
  - When a Fedora release goes EOL, its chroot's RPMs and SRPMs are deleted together after 180 days, which a project admin can extend ([outdated chroots policy](https://docs.copr.fedorainfracloud.org/copr_outdated_chroots_removal_policy)).
  - Never delete an SRPM-bearing build while its binaries remain published (GPLv3 §6(d); GPLv2 §3 "equivalent access").
  - The docs carry a "Source for every binary" page linking the SRPMs in each chroot.

### 8.2 Dependency table

| Component | Licence | Relationship | Bundled / detected | Conveyed by ButterEye? |
|---|---|---|---|---|
| ButterEye package, `buttereye.lua`, templates | AGPL-3.0-or-later (+ adopted §7 output permission, §8.4) | — | COPR `buttereye` (noarch, built per chroot) | **yes (COPR)** |
| FSRCNNX x2 8-0-4-1 mpv shader (igv, release 1.1) | LGPL-3.0-or-later (file header; LGPL-3.0 and GPL-3.0 texts shipped next to it) | data file loaded by mpv (`--glsl-shaders-append`) | bundled in `buttereye/data/shaders/`, unmodified, SHA-256 recorded | **yes (COPR, sdist)** |
| PySide6-Essentials / Qt 6.11 | LGPL-3.0-only OR GPL-2.0/3.0 ([PyPI](https://pypi.org/project/PySide6/)); Fedora: LGPL-3.0-only OR GPL-3.0-only WITH Qt-GPL-exception-1.0 | in-process import | distro package or PyPI `PySide6-Essentials`, **not** the `PySide6` meta-package (which pulls Addons) | no |
| Qt Charts / Graphs / other Addons | GPL-3.0 only in the open-source offering ([doc.qt.io](https://doc.qt.io/qt-6/qtcharts-index.html)) | would be in-process | **avoided in v1**; benchmark plots are drawn with QPainter; enforced by a test (F17) | no |
| mpv | GPL-2.0-or-later ([Copyright](https://raw.githubusercontent.com/mpv-player/mpv/master/Copyright)) | separate process (IPC) | detected | no |
| VapourSynth | LGPL-2.1-or-later upstream; Fedora metadata says LGPL-2.1-only (cite both) | separate process (vspipe, mpv) | detected | no |
| zimg (inside VS) | WTFPL | inside VS | detected | no |
| `vstrt` (vs-mlrt) | GPL-3.0, no exception, "or later" not stated ([LICENSE](https://github.com/AmusementClub/vs-mlrt/blob/master/LICENSE)) | loaded by mpv/vspipe (experimental TRT path) | built locally by the user (`contrib/build-vstrt.sh`) | **no, never** |
| `vsmlrt.py` (vs-mlrt) | GPL-3.0 (as above); packaged as `GPL-3.0-only`, conservatively (§13 Q16) | imported by the generated script inside mpv/vspipe | COPR `buttereye-vsmlrt-py` (noarch), unmodified, pinned to the v16.x tag; separate package, not vendored into ButterEye's source | **yes (COPR)** |
| RIFE-ncnn-Vulkan plugin | MIT (HolyWu 2021-2022; derived from nihui/rife-ncnn-vulkan, MIT, © 2020 nihui) AND WTFPL (`VSHelper4.h`) AND LGPL-2.1-or-later (`VapourSynth4.h`, declarations) AND BSD-3-Clause (ncnn inline headers) | loaded by mpv/vspipe | COPR `buttereye-vs-rife-ncnn`, built from the r9_mod_v33 tarball | **yes (COPR)** |
| ncnn + glslang | ncnn: BSD-3-Clause AND BSD-2-Clause AND Zlib. glslang: BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2 (per the pinned `LICENSE.txt`) | statically linked into the plugin | bundled in `buttereye-vs-rife-ncnn` (M0(f), 2026-10-07): Tencent/ncnn 305837fd (2025-05-03, the r9_mod_v33 `subprojects/ncnn` pin) and its glslang submodule nihui/glslang a9ac7d5f, as separate pristine SRPM tarballs, built through upstream's meson CMake subproject (`-Duse_system_ncnn=false`); `%license` ships ncnn `LICENSE.txt` and glslang `LICENSE.txt` verbatim; `Provides: bundled(ncnn) = 0^20250503git305837fd`, `bundled(glslang) = 0^gita9ac7d5f`. Fedora's `ncnn` is not used (GPU faults, §9) | **yes (COPR)** |
| MVTools | GPL-2.0-or-later per meson.build and headers; some files have no header (treat as possibly GPL-2.0-only); `x86inc.asm` ISC (x264 project) | loaded by mpv/vspipe | COPR `buttereye-vs-mvtools`, built from v29.x source per chroot | **yes (COPR)** |
| fftw3f | GPL-2.0-or-later AND MIT AND BSD-2-Clause (Fedora) | shared library used by MVTools | Fedora `fftw` | **not conveyed** (Fedora's) |
| RIFE ncnn models (curated subset) | MIT (hzwer/Practical-RIFE © hzwer; hzwer/ECCV2022-RIFE © Megvii Inc.; nihui/rife-ncnn-vulkan © nihui; RIFE-ncnn-Vulkan repo LICENSE) | data | COPR `buttereye-rife-ncnn-models` (noarch), with LICENSE + provenance note | **yes (COPR)** |
| RIFE ONNX conversions (vs-mlrt) | weights MIT; conversions unlicensed and possibly GPL-3.0 | data | downloaded from vs-mlrt URLs on user action (experimental TRT path); never mirrored, never in the COPR; notices kept | no |
| akarin (optional) | LGPL-3.0-only AND Apache-2.0 AND BSD-2-Clause-Patent AND MIT AND OFL-1.1 (bundled libvmaf/Reactor/fmt/inja/json/Terminus); NGX/VFX sources stripped | loaded by vspipe | COPR `buttereye-vs-akarin` only if the experimental fractional TRT path needs it | **yes (COPR)** if packaged, otherwise no |
| onnxconverter-common (optional) | MIT | imported by `vsmlrt.py` inside the vspipe engine-build subprocess | COPR `buttereye-onnxconverter-common` may ship it (protobuf requirement relaxed on F44, patch documented); ButterEye has at most `Suggests:` | **yes (COPR)** if packaged |
| ffms2 (offline source) | MIT source; binary is GPL when linked to a GPL FFmpeg | loaded by vspipe | Fedora package, detected | no |
| FFmpeg | as built by the distro: `ffmpeg-free` GPL-3.0-or-later; RPM Fusion GPLv3+ | separate process | detected, never bundled | no |
| mkvtoolnix | GPL-2.0-or-later AND LGPL-2.1-or-later | separate process | detected, optional | no |
| TensorRT, CUDA runtime, `trtexec` | proprietary NVIDIA SLA / EULA | loaded by `vstrt` in mpv/vspipe; `trtexec` run by vspipe | **user-installed from NVIDIA; never bundled, re-hosted, or loaded in ButterEye's process** | no |
| Proprietary or closed-source interpolation plugins (including plugins shipped only inside a proprietary product) | proprietary / closed source | — | **never loaded or redistributed** | no |

For every row marked "yes (COPR)", the RPM ships the full licence text as `%license`, `THIRD-PARTY` lists it, and the complete corresponding source is the SRPM published in the same chroot repository (§8.1). Every spec's `License:` is a full SPDX expression covering all files compiled or installed, checked with `license-validate` before each upload. All of these licences are on Fedora's allowed list, as COPR requires (raw LICENSE/meson.build upstream; `dnf repoquery --qf %{license}` for Fedora's fftw; the pinned `LICENSE.txt` for the bundled ncnn and glslang).

### 8.3 Keeping proprietary NVIDIA pieces optional

- **NVIDIA's terms.** The TensorRT SLA allows redistributing the runtime `.so` files only inside an application with "material additional functionality". §2.5 forbids using the SDK in any way that subjects it to an open-source licence ([SLA](https://docs.nvidia.com/deeplearning/tensorrt/latest/reference/sla.html)). The CUDA EULA has equivalent terms ([EULA](https://docs.nvidia.com/cuda/eula/index.html)).
- **Where the real conflict lies.** Shipping TensorRT next to AGPL software as an aggregate would not by itself relicense it. The real conflict is distributing a GPLv3 `vstrt` binary linked against `libnvinfer`. ButterEye therefore never distributes a prebuilt `vstrt`.
- **No routing to prebuilt `vstrt`.** ButterEye's packages, including everything in the project COPR, never ship, `Requires:` or `Recommends:` any prebuilt `vstrt`/`mlrt-trt` package (av-rpm, AUR `-bin`, CI artifacts). TensorRT and CUDA are proprietary, so `vstrt` could not be built in COPR in any case. Neither `doctor` nor the docs automate installing one. A copy the user already has is detected and supported. The recommended path is a local build with `contrib/build-vstrt.sh`, because a local build for one's own use is not distribution.
- **NVIDIA licence acceptance belongs to the user.** ButterEye never downloads NVIDIA components itself (F11). The user accepts NVIDIA's terms directly.
- **Upstream practice is not a precedent.** vs-mlrt's own Windows releases bundle NVIDIA DLLs next to GPLv3 binaries, and COPR av-rpm builds `mlrt-trt` against a user-installed TRT. ButterEye does not copy these arrangements on the assumption that they are legal.
- **Everything works without NVIDIA software.** All v1 release-gated features run on the all-FOSS path: RIFE-ncnn-Vulkan + MVTools from the COPR, plus distro ffmpeg. The TensorRT path is experimental and optional (§5.2).

### 8.4 Other compliance items

- **§7 output permission: adopted (owner decision 2026-10-07).** An additional permission covers the files ButterEye writes at run time from its templates: `.vpy` scripts, `buttereye.conf`, `input.conf` snippets and job manifests. It is in place from the first public release and the first public commit, before any external contribution, so no contributor consent is needed.
  - It does **not** cover shipped program files (`buttereye.lua`, the Python package, the template files themselves).
  - It can only cover code the ButterEye licensors own. Templates therefore contain no third-party code: no copied vsmlrt.py logic and no copied vs-rife `sc_detect`. The PlaneStats scene detector is written from scratch.
  - Rendered videos are not covered by ButterEye's licence in any case ([GPL FAQ #GPLOutput](https://www.gnu.org/licenses/gpl-faq.html#GPLOutput)).
  - Placement: the text below goes in `LICENSES/AdditionRef-ButterEye-generated-output.txt`, is referenced from `COPYING`/README and the SPDX header of each template (`AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output`; SPDX requires `WITH` to be followed by an exception ID or an `AdditionRef-`, not a `LicenseRef-`; a REUSE `LicenseRef` entry is the fallback if the lint does not accept the `WITH` form), and is shown by `buttereye licence` (F18). The `buttereye` RPM ships `COPYING` and the permission text as `%license`.
  - **Draft text** (for a final wording check before the first public release):

    > **Additional permission under GNU AGPL version 3 section 7 (generated output)**
    >
    > As an additional permission under section 7 of the GNU Affero General Public License, version 3, the copyright holders of ButterEye grant you permission to use, copy, modify, distribute and license any Generated File under terms of your choice, without the conditions of the GNU Affero General Public License applying to that Generated File solely because it contains or is derived from material that ButterEye inserted into it when generating it (from its templates or from code fragments emitted by its script generator).
    >
    > A "Generated File" is a file that ButterEye writes at run time by filling one of its templates with data, namely: VapourSynth scripts (`.vpy`), mpv configuration include files (`buttereye.conf`), mpv `input.conf` snippets, and offline render job manifests.
    >
    > This permission does not apply to ButterEye itself as distributed (its Python package, `buttereye.lua` and the template files as files), and it does not apply to any material in a Generated File whose copyright is not held by the ButterEye copyright holders, including third-party code imported or loaded by a Generated File (such as `vsmlrt.py` or VapourSynth plugins).
    >
    > If you modify ButterEye, you may extend this permission to your version, but you are not obliged to do so. If you do not wish to do so, delete this permission statement from your version, as section 7 allows.
- **AGPL §13 network clause.** Not triggered in v1, which uses local IPC only (§2). The GUI and CLI still ship a "Source code" link (F18), so modified versions that add a network interface start out compliant.
- **No code from GPL-2.0-only or unlicensed sources.** The following are design references only, and no code is copied from them:
  - president-not-sure/mpv-interpolation (file header "Licensed under GPLv2", plain GPLv2 LICENSE, so GPL-2.0-only);
  - the archlinuxcn forum script;
  - forum and wiki snippets from other interpolation tools;
  - any other post without a licence.

  Templates and the Lua helper are written from scratch. Each PR that touches `scriptgen/` or `data/mpv/` states its provenance.
- **No copying from RVE.** REAL Video Enhancer is AGPL-3.0-**only** on Flathub. Copying its code would force those parts to "only", so features are reimplemented instead.
- Shipping `vsmlrt.py` unmodified as its own COPR package (`buttereye-vsmlrt-py`, with its own GPL-3.0 `License:` and `%license`) is aggregation, not vendoring; ButterEye's `License:` stays `AGPL-3.0-or-later`.
- **If `vsmlrt.py` is ever vendored into ButterEye's own source** (not in v1):
  - SPDX `GPL-3.0-only`, conservatively, since upstream states no "or later";
  - ship `LICENSES/GPL-3.0-only.txt`;
  - set the package and RPM `License:` to `AGPL-3.0-or-later AND GPL-3.0-only`;
  - never edit the file in place, and override settings such as `trtexec_path` at run time;
  - any edit needs a dated modification notice under GPLv3 §5(a).
- **fdk-aac wording.** Do not call Fedora's ffmpeg builds unredistributable. Upstream FFmpeg classifies fdk-aac as nonfree, but Fedora ships `fdk-aac-free` and treats it as GPL-compatible (Fedora spec Patch2, RH bz 1501522#c112). ButterEye simply never defaults to `libfdk_aac`.
- **Copyright and SPDX.**
  - The copyright line is `Copyright (C) 2026 The ButterEye contributors`.
  - Program files (including `buttereye.lua`) carry `SPDX-License-Identifier: AGPL-3.0-or-later`; template files carry `AGPL-3.0-or-later WITH AdditionRef-ButterEye-generated-output` (or the REUSE fallback above), so DCO sign-offs on templates certify contributions under the permission.
  - The source tree (and later the repository) has a REUSE-compliant `LICENSES/` directory, including the §7 permission text.
  - `THIRD-PARTY` lists the components ButterEye conveys, including everything in the COPR (§8.2), with full licence texts; this is an obligation.
  - COPR spec files are ButterEye's own work, licensed MIT (matching Fedora dist-git's default for spec files). av-rpm's specs are **not** copied: [adworacz/av-rpm](https://github.com/adworacz/av-rpm) has no LICENSE file, so its specs are all-rights-reserved by default. The About → Licences pane additionally lists detected components for information.
- **ButterEye's own Corresponding Source.**
  - Every release publishes an sdist (on PyPI alongside the wheel, and inside the COPR SRPM). Once a repository exists, releases are built from a signed git tag; until then the sdist and SRPM are the authoritative source.
  - The sdist and the repository include the sources of every generated asset: Qt `.ts` translations (not only `.qm`), `.qrc` and `.ui` sources (not only `pyside6-rcc` output), icon SVGs (not only PNGs), and the generator for the plugin and model manifest.
- **Proprietary tools.** ButterEye never reads, imports, copies or converts profiles, generated scripts, preset values or UI assets from proprietary interpolation tools, and uses no third-party marks.
- **Re-check at release.** The TRT 11 SLA, the vs-mlrt licence, and the VapourSynth and FFmpeg versions all move quickly.

### 8.5 Contributor policy: DCO, not a CLA

**Adopted (owner decision 2026-10-07): the DCO**, with `Signed-off-by:` on every commit and no CLA. A CI check enforces it once a repository exists; until then there are no outside contributions to check. The reasons:

- **Contributor trust.** A CLA that grants one party relicensing rights over an AGPL community project creates an asymmetry: the copyright holder could ship proprietary versions while contributors are bound by the AGPL. That is a known deterrent, and it undercuts a project whose selling point is being fully open.
- **Low friction.** The DCO certifies provenance, which is the legal protection a community project actually needs, with no paperwork.
- **Licence evolution is already covered.** "Or later" lets the project move to future AGPL versions without a CLA.
- **Provenance.** The DCO sign-off also covers code copied from elsewhere.
  - The PR template asks "Does this include code from another project? Which licence?". GPL-2.0-only or unlicensed sources are rejected (§8.4).
  - AI-assisted contributions are accepted under the same certification: the contributor confirms that no verbatim third-party code is present.
- **Trade-off.** With a DCO, relicensing later (for example to a permissive licence, or dual licensing) needs every contributor's consent. The same applies to adding any further §7 permission later. That is why the §7 output permission (§8.4) is adopted now, before the first external contribution.

---

## 9. Packaging and distribution (v1)

**Primary channel: the ButterEye COPR** (owner decision 2026-10-07), shipping ButterEye **and** the FOSS VapourSynth plugins it needs, built from source.

**COPR project.**

- **Name: `<owner>/buttereye`.** A COPR owner is a FAS user or a FAS group (`@group`), and COPR cannot create groups ([COPR docs](https://docs.copr.fedorainfracloud.org/user_documentation.html)). `buttereye/buttereye` would need a FAS user called `buttereye`. The realistic options are `<owner's FAS user>/buttereye` now, or `@buttereye/buttereye` after a Fedora infrastructure ticket for a FAS group. This depends on §13 Q5.
- **Chroots:** `fedora-44-x86_64` and `fedora-45-x86_64` (both offered by COPR today, `/api_3/mock-chroots/list`).
- **Network off during builds:** create the project with `copr-cli create … --chroot fedora-44-x86_64 --chroot fedora-45-x86_64 --enable-net off`, because the CLI/API default is on ([bz 1335236](https://bugzilla.redhat.com/show_bug.cgi?id=1335236)). This proves each SRPM carries complete corresponding source (§8.1). Builds are never submitted with `--enable-net on` (which overrides the project setting); the release checklist (M5) verifies the build log of each published build shows network disabled.
- **Build submission without git:** build SRPMs locally (`rpmbuild -bs`) and upload them (`copr-cli build <owner>/buttereye foo.src.rpm`). No repository exists yet, so SCM, Packit and webhook builds are not used now; they can come later.
- **Signing:** COPR signs every package with an auto-generated per-project key; projects cannot bring their own. The generated `.repo` has `gpgcheck=1` and `gpgkey=https://download.copr.fedorainfracloud.org/results/<owner>/buttereye/pubkey.gpg`. Comparable projects show rsa2048 keys with a 5-year expiry. The README and docs publish the key fingerprint. No project signing infrastructure is needed.
- **Source and retention** follow the obligations in §8.1 (SRPM per chroot as `arch=src`, stable subpackage names, 180-day EOL window). The COPR project Description and Install instructions state each package's licence and that the SRPM for every binary is in the same chroot repository (`dnf download --source <pkg>`, or the "Source for every binary" page URL).

**Packages** (names prefixed `buttereye-` so nothing overrides or `Conflicts:` with av-rpm's `vapoursynth-plugin-*` packages or a current or future Fedora package):

1. **`buttereye`** (noarch, AGPL-3.0-or-later). Built per chroot anyway, because Python site-packages paths and bytecode differ (3.14 on F44, 3.15 on F45).
   - `Requires: python3-pyside6, python3-vapoursynth, vapoursynth-tools, mpv, /usr/bin/ffmpeg, vulkan-loader, buttereye-vs-mvtools`. MVTools is the only no-GPU path.
     - The ffmpeg requirement is a file dependency, because Fedora's `ffmpeg-free` does **not** Provide `ffmpeg`, so `Requires: ffmpeg` would force RPM Fusion.
     - The GUI uses only Essentials modules (F17).
   - `Recommends: buttereye-vs-rife-ncnn, buttereye-rife-ncnn-models, mkvtoolnix, ffms2, vulkan-tools`.
   - `Suggests: buttereye-vsmlrt-py, buttereye-onnxconverter-common, python3-onnx, 7zip` (experimental TRT path only; `7zip` only for ONNX model archives).
2. **`buttereye-vs-rife-ncnn`** (`License: MIT AND WTFPL AND LGPL-2.1-or-later AND BSD-3-Clause AND BSD-2-Clause AND Zlib AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2`; `%license`: repo LICENSE, nihui/rife-ncnn-vulkan LICENSE, ncnn `LICENSE.txt`, glslang `LICENSE.txt`, VapourSynth header licence notes). Built from the `r9_mod_v33` tag tarball, repacked without `models/` and `subprojects/ncnn` (§8.1), plus the pinned ncnn (Tencent/ncnn 305837fd, 2025-05-03, the commit the tag's submodule pins) and glslang (nihui/glslang a9ac7d5f) tarballs, linked statically with meson `-Duse_system_ncnn=false` (§8.2). Spike M0(f) chose this over Fedora's `ncnn-devel` (F44 20250916-2, F45 20250916-5): built against it (with a patch for the removed pack8 option), the plugin faulted the GPU (NVIDIA Xid 13, compute class c9c0) in 12 of 20 soak runs at `gpu_thread=4` and was stable only at `gpu_thread=1` (~32 fps at 1080p 2x); the bundled build had no Xid 13 in 42 runs at the same ~72 fps (later, 2 × Xid 109 in vspipe with rife-v4.25-lite; m0f.md). The Fedora-ncnn variant stays behind `rpmbuild --with system_ncnn` for a retry once the plugin catches up with newer ncnn. The shaders ship as `*.comp.hex.h` byte arrays that decode to plain GLSL text, so they are source, not blobs. Needs `BuildRequires: cmake` and libgomp, and an explicit `Requires: vulkan-loader`, because ncnn dlopens Vulkan (`NCNN_SIMPLEVK`) and produces no automatic dependency. `%check` fails if `librife.so` links any `libncnn`.
3. **`buttereye-rife-ncnn-models`** (noarch, MIT). The curated subset of §5.4, repacked from the same tag; the repack script ships in the SRPM. `%license` ships the RIFE-ncnn-Vulkan LICENSE (© HolyWu), hzwer/Practical-RIFE LICENSE (© 2021 hzwer), hzwer/ECCV2022-RIFE LICENSE (© Megvii Inc.) and nihui/rife-ncnn-vulkan LICENSE (© 2020 nihui), plus a provenance note naming, per packaged model directory, the upstream weight release and the ncnn converter. Only models whose provenance is recorded are packaged.
4. **`buttereye-vs-mvtools`** (`License: GPL-2.0-or-later AND ISC`, with the §8.2 caveat; `%license` ships `LICENSE` plus the `x86inc.asm` ISC notice). v29.x from `dubhatervapoursynth/vapoursynth-mvtools`, patched so meson takes VapourSynth headers from `pkg-config vapoursynth` instead of `vs.get_include()` (absent on F44's R72) and installs to the private directory. `BuildRequires: fftw-devel, nasm, meson`. Building per chroot sets `VAPOURSYNTH_API_VERSION` correctly for R72 and R79.
5. **`buttereye-vs-akarin`** (`License: LGPL-3.0-only AND Apache-2.0 AND BSD-2-Clause-Patent AND MIT AND OFL-1.1`, optional; verify against the pinned tag). The SRPM tarball is repacked without `ngx/` and `vfx/` (Windows-only, and `ngx/cuda.h` has no stated licence) (§8.1). `%license` ships `COPYING.LESSER`, the GPL-3.0 text (required by LGPL-3.0), and the bundled components' licence texts. Only if the experimental fractional TRT path needs it; otherwise left out. Needs patches (upstream meson has `install: false` and uses `vs.get_include()`), `-Dstatic-llvm=false`, and `BuildRequires: llvm22-devel` on F45 (akarin needs LLVM ≥20 <23; F45's default is 23). It is API3, so it carries `Requires: vapoursynth-libs < 80` (or an equivalent conflict).
6. **`buttereye-vsmlrt-py`** (noarch, `License: GPL-3.0-only`, conservatively; §13 Q16). `vsmlrt.py` unmodified, pinned to the same v16.x tag as `contrib/build-vstrt.sh`.
7. **`buttereye-onnxconverter-common`** (noarch, MIT, optional; installed to a private path such as `%{_datadir}/buttereye/python/` and added to `sys.path` by the engine-build script, like `vsmlrt.py`, so it never shadows a current or future Fedora `python3-onnxconverter-common`). For fp16 engine builds on the experimental TRT path. Its `protobuf>=3.20.2` requirement is relaxed on F44 (python3-protobuf 3.19.6), with that patch documented.

**Never in the COPR:** a `vstrt` binary (the `buttereye-vsmlrt-py` SRPM contains its GPL-3.0 source as part of the unmodified vs-mlrt tarball, never compiled), TensorRT, CUDA, TensorRT engines, the vs-mlrt ONNX conversions (no stated licence), `ffms2` (already in Fedora: F44 5.0-8, F45 5.0-10), and standalone ncnn/glslang packages (they exist only statically inside `buttereye-vs-rife-ncnn`).

**Install layout.** Every COPR plugin installs into a private, non-autoload directory, `%{_libdir}/buttereye/vapoursynth/`, and is loaded by absolute path through the §4.4 guard. This avoids the autoload-directory skew between F44 and F45, and a namespace clash with av-rpm's or a distro's `%{_libdir}/vapoursynth/libmvtools.so`. Upstream meson `install_dir` values are overridden in the specs.

**Plugin manager behaviour.** dnf is the primary and only source for the RIFE-ncnn `.so`, the curated RIFE ncnn models, MVTools and `vsmlrt.py`. The plugin manager no longer downloads the upstream prebuilt RIFE `.so`, unpacks PyPI MVTools wheels, or fetches the model tarball. Its pinned-SHA-256 downloader remains only for vs-mlrt ONNX models (experimental TRT path) and optional extra ncnn models ("extra") (§4.8, §5.4).

- Docs explain that offline render of H.264/HEVC sources needs RPM Fusion's `libavcodec-freeworld` (§7.1).
- Fedora rebuilds its Python stack for Python 3.15 in F45, and each chroot's build always matches the host VapourSynth that mpv embeds.

**Coordination with av-rpm.** [COPR flawlessmedia/av-rpm](https://github.com/adworacz/av-rpm) now has chroots EPEL 9/10, Fedora 43 and Fedora 44, but no Fedora 45. Its F44 repo ships `vapoursynth-plugin-mvtools` 25-1.fc44 (installed into the autoload directory) and mlrt-openvino, but no akarin, no RIFE-ncnn and no mlrt-ncnn/trt. ButterEye's packages coexist with it (different names, private install path). Its repository has no LICENSE, so ButterEye writes its own specs and does not copy av-rpm's (§8.4). Detection of an av-rpm copy stays generic (§4.4).

**`vstrt`** is never packaged by ButterEye (§8.3).

**Secondary channel:** `pipx install --system-site-packages buttereye`, for developers. It reuses the distro PySide6 and VapourSynth. Do not depend on PyPI PySide6: 6.11.2 declares `requires_python <3.15`, and F45 moves to Python 3.15. If a PyPI Qt dependency is ever declared, it is `PySide6-Essentials`.

**Not in v1:**

- **Flatpak.** A sandboxed app needs `flatpak-spawn --host` to drive the host mpv, which is a reviewed exception on Flathub. Flathub's `io.mpv.Mpv` bundles its own VapourSynth (R73, with a TODO to move to ≥R74), has no plugin extension point, and reads its config from `~/.var/app/io.mpv.Mpv/config/mpv`. The NVIDIA GL extension carries no CUDA or TensorRT. Revisit only with strategy B.
- **AppImage.** It cannot control the host VapourSynth ABI that mpv embeds.
- **Arch/AUR, and any distribution other than Fedora 44/45.** Not a supported target (owner decision 2026-10-07). ButterEye's §4.4 guard still detects distro-installed plugins generically, but there is no Arch commitment.

**Version skew to support at the same time:**

| | Fedora 44 | Fedora 45 (in final freeze; release date uncertain) |
|---|---|---|
| VapourSynth | R72 (autoload `%{_libdir}/vapoursynth`) | R79 (autoload `site-packages/vapoursynth/plugins`) |
| Python | 3.14 | 3.15 (3.15.0~rc2 today) |
| `ffmpeg-free` | 8.1.3 | 9.0.2 |
| `ncnn` | 20250916-2 | 20250916-5 |
| LLVM (for akarin) | 22 | 23.1.2 default; `llvm22-devel` available |
| meson | 1.10/1.11 | 1.12.1 |

`mpv-libs` hard-requires `libvapoursynth-script.so.0`. A soname-breaking VapourSynth update (R74+ renamed the library to `libvsscript`) therefore arrives together with an mpv rebuild, or dnf holds it back. `doctor` compares the exact VS versions in use via the in-mpv probe (F1). CI covers both releases from M1 onward (§6.1). A future Fedora update to VapourSynth R80+ would stop the API3 plugins (`vstrt`, akarin) from loading (§5.2, §11); the COPR plugins are rebuilt per chroot after any VapourSynth soname change. Fedora's `ncnn` row is informational only: the bundled ncnn does not track it.

---

## 10. Prior art, differentiators, name check

### 10.1 Prior art

| Project | Licence | Linux | Real-time | Notes |
|---|---|---|---|---|
| [REAL Video Enhancer](https://github.com/TNTwise/REAL-Video-Enhancer) | AGPL-3.0 (AGPL-3.0-only on Flathub) | Flatpak | no (offline only) | PySide6 GUI + separate backend process; architecture similar to ButterEye's; code not copied (§8.4) |
| [Flowframes](https://github.com/n00mkrad/flowframes) | GPL-3.0 | no | Patreon builds only | C#/.NET |
| [Enhancr](https://github.com/mafiosnik777/enhancr) | GPL-3.0 | promised, never shipped | yes (Windows) | dormant since 2024-01 |
| [president-not-sure/mpv-interpolation](https://github.com/president-not-sure/mpv-interpolation) | GPL-2.0-only (file header: "Licensed under GPLv2") | yes | yes | closest technical precedent: mpv + `.vpy` + MVTools/RIFE-ncnn, built in Podman; 4 stars; no GUI; **design reference only, no code copied** |
| [video2x](https://github.com/k4yt3x/video2x), [TheAnimeScripter](https://github.com/NevermindNilas/TheAnimeScripter), [VideoJaNai](https://github.com/the-database/VideoJaNai), [FluidFrames](https://github.com/Djdefrag/FluidFrames) | AGPL / AGPL / GPL-3.0 / MIT | mixed | no | offline only (VideoJaNai is a Windows GUI on vs-mlrt TRT) |
| [Lafourkad/mpv-RIFE](https://github.com/Lafourkad/mpv-RIFE) and similar mpv bundles | MIT etc. | no | yes | Windows configs |
| [PancakeTAS/lsfg-vk](https://github.com/PancakeTAS/lsfg-vk) | none declared | yes | yes | Vulkan layer that needs the proprietary Lossless Scaling DLL; not a playback interpolator |
| AUR vs-mlrt packages; COPR flawlessmedia/av-rpm | GPL-3.0 etc. | Arch; Fedora/EPEL | — | prior art for Linux provisioning (av-rpm specs are not reused, §8.4) |
| mpv `--interpolation` | GPL | yes | yes | frame **blending** for display timing, not motion interpolation |
| ffmpeg `minterpolate` | LGPL/GPL | yes | no | zero-plugin offline motion interpolation; a possible fallback |

### 10.2 Differentiators (honest)

1. Fully open (AGPL-3.0-or-later), Linux-first real-time interpolation. The polished existing options are proprietary.
2. Works with the distro's own mpv and Python, and never edits `mpv.conf`.
3. Solves the Linux provisioning gap on Fedora. vs-mlrt has no Linux release binaries, and Fedora packages no interpolation plugins. One `dnf copr enable` plus `dnf install buttereye` gives the cross-vendor and CPU paths with no run-time downloads. (Other distributions are not targets; on Arch the AUR covers part of this gap independently.)
4. One profile and one script graph for both live and offline use. Existing open tools do one or the other.
5. Backend and model choice driven by a local in-mpv benchmark, plus an independently designed rules engine.
6. A GUI-free core and CLI. The GUI/backend split itself is not new; RVE does it.

### 10.3 Name check: "ButterEye" (as of 2026-10-07)

**Free:**

- GitHub user/org `buttereye` (404).
- PyPI `buttereye`, `butter-eye`, `buttereyes` (404).
- No Flathub app.
- `buttereye.org` and `buttereye.io` return "Domain not found" in whois. Whois is not authoritative, so confirm with a registrar.

**Taken or unknown:**

- `buttereye.com` is registered (2013, NameBright) and appears parked.
- `.app` and `.dev` are unknown.

**Soft collisions:**

- `Sergey0066/ButterEyes` is a 1-star PC-data and webcam-snapshot tool: a near-identical and unflattering match.
- Researcher-reported but not re-checked: a Tuxemon monster, the Butterfleye camera brand (Ooma), and a Butteraugli extension in `fish2000/instakit`.
- The older `dthpham/butterflow` interpolation tool has been renamed `dthpham/sminterpolate`.

**Trademarks:** Trademarkia showed none (not re-checked). **USPTO, EUIPO and IMPI were not searched**, so this is not clearance.

**Suggested app id:** `io.github.buttereye.ButterEye`.

---

## 11. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Real-time targets are unachievable on mid-range and cross-vendor hardware | Core promise fails | ≤1440p target (§1); spikes M0(a)/(b) before building product features; in-mpv benchmark drives selection |
| No Linux release binaries for vs-mlrt; CI artifacts are CI-only and built against TRT 11.1 only | NVIDIA path depends on local builds | `contrib/` build script pinned to TRT 11.3/CUDA 13.4 with an 11.1/13.3 fallback; the ncnn-Vulkan path works without it |
| Experimental TRT 11 path depends on vs-mlrt pre-releases (v16.x.test); Fedora is not an NVIDIA-supported TRT platform | Build, load or engine breakage on vs-mlrt, TRT, CUDA or Fedora updates | Labelled experimental, opt-in, not a release gate (§5.2); pin v16.3.test1 + TRT 11.3.0.99; docs suggest `dnf versionlock` for `libnvinfer*` (printed command only, never run by ButterEye); engine cache keyed by TRT version and precision path; `doctor` detects mismatches |
| TRT 11 fp16 RIFE quality regression (vsmlrt drops the fp32 layer pins that fixed aliasing on TRT <11, [issue #66](https://github.com/AmusementClub/vs-mlrt/issues/66)) | Jaggies or sub-pixel shifts, especially at 4K | fp16-vs-fp32-vs-ncnn quality gate in M2; on failure fp16 is disabled and the profile defaults to fp32 TRT |
| VapourSynth R80 API3 cliff | `vstrt` (TRT) and akarin stop loading if Fedora moves to R80+ | `doctor` blocks the TRT path with a clear message; ncnn and MVTools are built per chroot against the shipped VS and are unaffected (to be confirmed for the ncnn build); `Requires: vapoursynth-libs < 80` on akarin; watch upstream and the third-party API4 port [RyougiKukoc/vs-mlrt-api4](https://github.com/RyougiKukoc/vs-mlrt-api4) (not adopted) |
| TRT engine build inside mpv would freeze playback | Minutes-long stall | Builds run only out of process; mpv scripts fail fast; ncnn fallback during builds (§5.2) |
| RIFE-ncnn-Vulkan plugin bus factor (last release 2025-09); it faults the GPU on Fedora's newer ncnn (M0(f)) | Cross-vendor path stalls or fails | The COPR source build is the primary path, with the plugin's pinned ncnn bundled statically (§9); pin the version; retry `--with system_ncnn` when upstream catches up; keep the MVTools fallback |
| Bundled ncnn + glslang maintenance: security and bug fixes are now ours to pick up, and the pin is held to the plugin's `subprojects/ncnn` submodule (2025-05-03) | Unpatched ncnn/glslang bugs in a shipped binary; no Fedora updates flow in | Track upstream ncnn and glslang advisories; bump only together with the plugin's submodule pin, or backport a fix as a spec patch; re-run the M0(f) soak (Xid count in `journalctl -k`) after any bump; `Provides: bundled(…)` keeps the copies discoverable |
| COPR maintenance and source duty: the project is now a GPL/MIT/LGPL distributor of plugins it does not develop | Broken repo after Fedora updates (VapourSynth, Python 3.15); a binary outliving its SRPM would breach GPL source duties | Network-off builds; pristine tarballs in every SRPM; stable subpackage names and whole-SRPM rebuilds (Pulp keeps 5 builds per RPM Name); never delete SRPM-bearing builds; renew EOL chroot retention when needed; rebuild on VS updates (F44+F45 CI against the COPR); "Source for every binary" doc page (§8.1, §9) |
| VapourSynth skew (F44 R72, F45 R79, upstream R81); plugins built against other API headers may not load | Plugins fail to load | COPR plugins are built per chroot against that chroot's VapourSynth; load by absolute path from a private directory with an already-loaded guard; in-mpv probe; F44+F45 CI |
| Clash with av-rpm or distro copies of the same plugin | Double-load errors, wrong plugin version | `buttereye-` package names, private non-autoload install path, already-loaded guard, `doctor` reports the active copy |
| Per-seek reload of the whole core (all backends) | Multi-second seek stalls | Static-shape cached engines; seek-latency thresholds in F2/F12; fallback while scrubbing |
| mpv bridge CPU conversions (NV12/P010 → planar) and copies | Live performance below vspipe numbers | In-mpv benchmark with semi-planar input (F14) |
| `user-data` JSON breaking mpv option parsing; injection through filenames | Broken or hijacked scripts | `%len%` quoting; data only through JSON; static templates; golden and injection tests |
| A failed filter reinit breaks the next file | User-visible breakage | Verify via `request_id` + `vf` property; automatic rollback |
| Colour and HDR mishandling (`_ColorSpace` only; `_MP_IMAGE` survival unverified) | Wrong colours, washed-out HDR | Explicit matrix and range from IPC; HDR bypass by default (F7); spike M0(d) |
| User mpv config conflicts (non-copy hwdec, watch-later, mpvSockets); attached instances keep user settings | Stale filters, broken sockets, failed filter | Profile overrides; IPC settings on attach with restore on detach; stale-filter cleanup; `doctor` warnings |
| IPC socket allows code execution; planted sockets in `/tmp` | Local code execution | 0700 directory or socketpair; `lstat`/owner/`SO_PEERCRED` checks; command allowlist |
| Supply chain through downloads and the COPR | Malicious plugin or model | Plugins come from source-built, COPR-signed RPMs (`gpgcheck=1`, published key fingerprint); ONNX downloads use pinned SHA-256 shipped in the package, a host allowlist, safe extraction and no override flag |
| Offline HDR metadata loss via y4m | Incorrect HDR files | HDR10 only via x265/SVT-AV1 with explicit parameters (decided 2026-10-07); NVENC/VAAPI SDR only; HLG/DV deferred, no silent widening |
| VFR desync (live and offline) | Broken playback or renders | Live: integer multi with duration rewriting; offline: CFR conversion |
| `ffmpeg-free` lacks x264/x265 and H.264/HEVC decode | Missing presets or sources | Encoder probing; per-codec ffms2 decode check; RPM Fusion documented as needed for H.264/HEVC |
| Python 3.15 vs PyPI PySide6 | pipx installs break on F45 | RPM is the primary channel; pipx only with `--system-site-packages` |
| Licence: GPL-2.0-only code copied in; proprietary libraries in-process; incompatible contributions | AGPL violation | Provenance rules (§8.4, §8.5); in-process import tests; DCO |
| Licence drift (TRT 11 SLA, vs-mlrt, COPR-packaged plugins) | Compliance gaps | Re-check at every release, including the licence of every COPR package against Fedora's allowed list; REUSE lint (locally now, in CI once a repository exists) |
| Dev box is Hyprland + NVIDIA only | GNOME/KDE/AMD/Intel/X11 regressions | Cross-vendor box is an M0 precondition; GNOME 48+, KDE Plasma 6 and X11 test pass before v1.0 |
| Volunteer capacity | Release slips | Scope cuts listed under "Deferred" in §2; milestones ordered by risk (§12) |
| Name confusion (ButterEyes, Butterfleye); COPR owner name not yet fixed | Search and reputation; a COPR move later breaks users' `dnf copr enable` lines | Register the org, PyPI name, a domain and the COPR owner (FAS user or `@buttereye` group) early (§13 Q5); trademark search before 1.0 |

---

## 12. Milestones

Each milestone after M0 is shippable and versioned (a COPR build with a version number; git tags once a repository exists — none exists today, and no step below assumes one). **Precondition for M0:** access to a cross-vendor box (an AMD or Intel discrete Vulkan GPU).

- **M0: Feasibility spikes** (time-boxed, throwaway scripts). Each spike records a go/no-go in `docs/spikes/`:
  - (a) RIFE-ncnn in-mpv throughput at 1080p, 1440p and 4K, measured as in F14(b), on the 4090 **and** on the cross-vendor box. Also confirms internal padding and VFR duration handling.
  - (b) Seek latency with ncnn and TRT, which validates the F2 and F12 thresholds.
  - (c) **Experimental TRT 11**: build `vstrt` from vs-mlrt v16.3.test1 on F44 and in an F45 toolbox, against rhel10 TRT 11.3.0.99 plus fedora44-repo CUDA 13.4.2 with GCC 16.2.1 and Fedora's VapourSynth headers. Then: one engine build via `vspipe` + `/usr/bin/trtexec` with onnxconverter-common fp16 on F44 (protobuf 3.19.6) and on F45 (protobuf 6.33); the RIFE v4.6 scale=0.5 path with `python3-onnx`; the fp16-vs-fp32 quality comparison; akarin loading on R72/R79. Also confirms the vsmlrt engine-path mechanics and the `trtexec_path` override. A no-go narrows or drops the experimental path; it does not block v1.
  - (d) HDR10 `video-out-params` and `_MP_IMAGE` survival with the filter on and off.
  - (e) Fedora `ffms2` on R72 with H.264 and HEVC, using ffmpeg-free and using libavcodec-freeworld. `lsmas` (source build) on F45's R79. **Recorded 2026-10-08** (`docs/spikes/m0e.md`): go with libavcodec-freeworld for H.264, HEVC 8/10-bit, AV1 and VP9 (frame-exact, 4K HEVC decodes at 117 fps); full vspipe → NVENC → remux pipeline works; ffmpeg-free-only and mkvmerge left for the real install.
  - (f) **COPR plugin builds**: `buttereye-vs-mvtools` (patched for R72) and `buttereye-vs-rife-ncnn` with `-Duse_system_ncnn=true` against Fedora ncnn 20250916, built in mock (network off) for `fedora-44-x86_64` and `fedora-45-x86_64`, then loaded from the private directory and run in mpv. If system ncnn fails, fall back to the bundled ncnn submodule tarball. Per-chroot builds replace the earlier wheel-ABI question. **Recorded 2026-10-07** (`docs/spikes/m0f.md`): all three packages build on both chroots; Fedora ncnn is a no-go (GPU faults at `gpu_thread` ≥ 2), the bundled pinned ncnn is a go (no Xid 13 in 42 headless runs, ~72 fps at 1080p 2x), adopted (§9). mpv playback with the bundled build and MVTools in mpv were played later the same day (m0f.md, "mpv playback, bundled build"); the bundled build then showed 2 × Xid 109 in vspipe with rife-v4.25-lite (m0f.md, "Bundled-build faults").
  - (g) Non-copy hwdec through mpv's autoconvert path.
  - (h) PySide6.QtAsyncio driving the core loop. **Recorded 2026-10-07** (`docs/spikes/m0h.md`): no-go on PySide6 6.11.2 (no subprocess, Unix-socket, fd-reader, pipe or signal-handler support); the core loop runs in a dedicated thread with a Qt-signal bridge (§4.1).
  - (i) **Performance breakdown** (performance track P0, §15.1; added 2026-10-09). Split the cost of live and offline smoothing into colour conversion, RIFE inference, transfers, the mpv filter path and Vulkan-layer overhead, with `tools/perf/breakdown.py`. Recorded in `docs/spikes/m0i.md`. It decides between the plugin fixes (P2C) and a faster runtime (P2A/P2B). It is not a v1 gate.
  - (j) **RIFE inference ceiling on faster runtimes** (P2, §15.1; added 2026-10-09). vs-mlrt's RIFE ONNX models under ONNX Runtime's CUDA and TensorRT providers, fp32 and fp16, 1080p and 4K, with frames kept on the GPU and with copies, via `tools/perf/ort_ceiling.py` in a dev-box venv. **Recorded 2026-10-09** (`docs/spikes/m0j.md`): TensorRT fp16 is 4.2–4.9× the ncnn plugin; unoverlapped copies halve it; `scale=0.5` is unavailable for v4.7+. Not a v1 gate.
  - (k) **RIFE fp16 quality gate, first pass** (P2A, §12 M2; added 2026-10-09). fp16 vs fp32 on real 1080p-class and 4K frame triples, with `tools/perf/fp16_quality.py`. **Recorded 2026-10-09** (`docs/spikes/m0k.md`): fp16 passes up to 1080p (drift ≥ 47 dB); at 4K it changes fast motion (0.3–0.7 % of pixels on 2 of 4 frames) while staying within 0.16 dB of fp32 against the real frame; vsmlrt's fp32 layer pins barely help; vsmlrt's fp16 conversion breaks the model's `Cast` nodes unless they are blocked. Proposed gate: fp16 default ≤ 1440p, offered at 4K live, fp32 for 4K renders; repeat on TensorRT 11 via `vstrt`.
  - (l) **vstrt built and measured on TensorRT 11** (P2A; added 2026-10-09). `contrib/build-vstrt.sh` against TRT 11.3.0.99 / CUDA 13.4 on F44; speed in vspipe and mpv with `tools/perf/breakdown.py --trt`; fp16 quality through vstrt with `tools/perf/trt11_quality.py`. **Recorded 2026-10-09** (`docs/spikes/m0l.md`): builds and loads; three integration fixes needed (load vstrt before `import vsmlrt`; `_implementation=2`; a Cast-safe replacement for `vsmlrt.convert_model`, without which TRT 11 rejects every fp16 RIFE engine). fp16 with RGBH frames and 2 streams: 262 fps for 1080p 2× and 101 fps for 1080p → 60 in mpv (4× the ncnn path); 4K 2× reaches 48.8 fps in mpv with 4 streams, i.e. no headroom. fp16 quality on par with the RIFE-ncnn plugin.
  - (m) **Quick wins inside mpv** (P1; 2026-10-10, `docs/spikes/m0m.md`): MVTools concurrent-frames 8 → 16; RIFE-ncnn gpu_thread stays 4.
  - (n) **ML video upscaling on TensorRT** (§15.2; 2026-10-10, `docs/spikes/m0n.md`): speed and fidelity of vs-mlrt's Real-ESRGAN-family models, alone and after RIFE. No model is both shippable and good; see §15.2.
  - (o) **mpv scalers and shader upscalers** (§15.2; 2026-10-10, `docs/spikes/m0o.md`): libplacebo's built-in scalers and FSRCNNX, RAVU, ArtCNN and Anime4K shaders, fidelity and GPU time, against M0(n)'s ML models. Shaders win on both.
  - (p) **MVTools at 4K** (P3; 2026-10-10, `docs/spikes/m0p.md`): cheaper MVTools settings, speed and fidelity; BlockFPS becomes the live fallback when FlowFPS can't keep up.
  - **Exit:** every spike has a recorded result, and the scope is adjusted where a spike failed.
- **M1: ButterEye COPR (plugins first), CLI play and live control.** Play needs the plugins, so COPR work starts here.
  - COPR scope: create the FAS account (or request the `@buttereye` FAS group, §13 Q5); create the project for `fedora-44-x86_64` and `fedora-45-x86_64` with `--enable-net off`; write ButterEye's own MIT-licensed specs; build SRPMs locally with `rpmbuild -bs` and upload them with `copr-cli build` for `buttereye-vs-mvtools`, `buttereye-vs-rife-ncnn`, `buttereye-rife-ncnn-models` and a pre-release `buttereye`; publish the signing-key fingerprint and the "Source for every binary" page. Every M1 upload already ships `COPYING`, the §7 permission text, `THIRD-PARTY`/`NOTICE` and all `%license` files, and `buttereye --version`/`buttereye licence` print the AGPL legal notices. No git-based build steps.
  - Product scope: `play` with RIFE-ncnn and MVTools; include/profile; socketpair IPC; attach (F3); hot reload and rollback (F4); atomic writes (F5); quoting and the injection test; SDR colour (F6); HDR bypass (F7 default); Lua helper (F8); hardware detection (F9); COPR-based plugin provisioning and the ONNX-only download transaction (F10); scene detection; full `doctor` with the in-mpv probe (F1); first run (F0); no-network test (F20); error codes (F21); F44 and F45 CI (local containers until a repository exists).
  - **Exit:** F0–F6, F7 (default bypass), F8–F10, F20 and F21 pass, with plugins installed from the COPR. F2 passes on the dev box and the cross-vendor box. Unit, golden and integration suites are green on F44 and F45.
- **M2: NVIDIA TensorRT path (EXPERIMENTAL, vs-mlrt v16.x / TRT 11).**
  - Scope: `contrib/build-vstrt.sh` (F11) pinned to v16.3.test1 / TRT 11.3 / CUDA 13.4; the opt-in dialog and "experimental" labels (§5.2); the TRT section of `doctor`; out-of-process engine builds and the cache (F12); the on-demand benchmark (F14); auto-selection with the opt-in rule (F13); `buttereye-vsmlrt-py` (and, if needed, `buttereye-onnxconverter-common` and `buttereye-vs-akarin`) in the COPR.
  - **Quality gate:** TRT fp16 RIFE vs TRT fp32 vs RIFE-ncnn on a fixed 1080p and a fixed 4K clip, with PSNR/SSIM thresholds recorded and a visual check for jaggies and pixel shifts. On failure, fp16 is disabled and the profile defaults to fp32 TRT.
  - **Exit:** F12 and the TRT parts of F13/F14 pass on the dev box (RTX 4090, driver 615.71.09, F44) with TRT 11.3/CUDA 13.4 — **recorded, not release-blocking**. F11 documented and run once. F13 and F14 for the ncnn and MVTools paths, including "never selects TRT without opt-in", pass on the dev box and the cross-vendor box and are release-blocking.
- **M3: Offline render (SDR/CFR, then HDR10).**
  - Scope: `render` with the y4m pipeline; ffms2 per-codec checks; encoder probing; mkvmerge/ffmpeg remux; progress; cancel; free-space check; VFR → CFR conversion; HDR10 via x265/SVT-AV1.
  - **Exit:** acceptance tests 7.7 #1–#7.
- **M4: GUI.**
  - Scope: PySide6 front-end over the core (F17); profile and rules editor (F15); render queue; benchmark view; Storage page; licence pane and notices (F18); string catalogue extraction (`tools/extract_ts.py`, goal 9).
  - **Exit:** F15, F17 and F18, including the accessibility checks.
- **M5: v1.0 release.**
  - Scope: final ButterEye and plugin COPR builds for F44/F45 (all packages rebuilt from SRPMs, `%license` and THIRD-PARTY checked, SRPM availability verified in both chroots); sdist; re-verify NOTICE/THIRD-PARTY/REUSE compliance and the §7 text; verify network-disabled build logs; DCO enforcement in CI once a repository exists (release `git` tags likewise only once it exists); GNOME 48+, KDE Plasma 6 and X11 test pass; cross-vendor regression pass; trademark search recorded.
  - **Release gates** are the ncnn and MVTools paths. The experimental TRT path never blocks v1.0.

---

## 13. Open questions for the owner

Question IDs are stable identifiers. Decided IDs are not reused, so references elsewhere in this document (for example §13 Q16) stay valid.

**Decided (2026-10-07):**

1. (C1) **DCO vs CLA:** DCO (`Signed-off-by:`, CI-enforced once a repository exists), no CLA (§8.5).
2. (C2) **§7 output permission:** adopted, scoped to generated `.vpy`, `buttereye.conf`, `input.conf` snippets and job manifests; draft text in §8.4.
3. (C3) **NVIDIA in-process exception:** not needed. Confirmed that ButterEye never loads TensorRT, CUDA or NVML in-process (§8.1).
4. (Q1) **TensorRT line:** EXPERIMENTAL vs-mlrt v16.x / TensorRT 11 (RHEL10 NVIDIA RPMs, `trtexec` from `libnvinfer-bin`). Pinned to v16.3.test1 / TRT 11.3.0.99 / CUDA 13.4.2, fallback TRT 11.1.0.106 + CUDA 13.3. fp16 via `onnxconverter-common` only. v15.16 / TRT 10 is not a target. Not a v1 release gate (§5.2, §12 M2).
5. (Q2) **Plugin provisioning:** a ButterEye COPR in v1, shipping ButterEye and the FOSS plugins (RIFE-ncnn-Vulkan, curated MIT RIFE ncnn models, MVTools, `vsmlrt.py`; optionally akarin and onnxconverter-common), built from source for F44 and F45, with full source duties. `vstrt` and the unlicensed vs-mlrt ONNX conversions are never packaged (§8.1, §8.2, §9).
6. (Q3) **Supported distros:** Fedora 44 and Fedora 45 only. Arch is not a supported target; distro plugins are detected generically, with no commitment (§2, §9).
7. (Q6) **Offline HDR scope:** HDR10 offline via x265/SVT-AV1 is in v1. HLG, Dolby Vision and NVENC/VAAPI HDR are deferred, with no silent widening (§7.4).

**Still open (the recommended default applies until the owner answers):**

1. (Q4) **Flathub `io.mpv.Mpv` users:** explicitly unsupported in v1? Recommended: yes. (Default in this document: unsupported.)
2. (Q5) **Branding and names:** register the GitHub org `buttereye`, PyPI `buttereye` and a domain (`.org` or `.io`) now? Commission a USPTO/EUIPO/IMPI search before 1.0? The **COPR owner name** depends on this: `<owner's FAS user>/buttereye` now, or `@buttereye/buttereye` after a Fedora infrastructure ticket for a FAS group (`buttereye/buttereye` only works if a FAS user named `buttereye` exists). Recommended: decide before the first public COPR build, because moving later breaks users' `dnf copr enable` lines.
3. (Q25) **Curated RIFE ncnn model subset** for `buttereye-rife-ncnn-models`. Recommended default: v4.26, v4.22-lite and v4.18 (§5.4); v4.25-lite dropped 2026-10-07.

**To settle by spike (M0) — not repeated here:** HDR passthrough, ffms2 on R72, mpv playback of the COPR plugins (bundled-ncnn RIFE, MVTools) on F44/F45, non-copy hwdec, the real-time capability of RIFE-ncnn on AMD/Intel, seek latency, the experimental TRT 11 `vstrt` build (including onnxconverter-common on F44's protobuf 3.19.6, and fp16 quality).

**To settle by test or upstream contact:**

1. (Q16) Upstream vs-mlrt: is the GPL-3.0 "or later"? Would they add a TensorRT/CUDA linking exception? Do the ONNX conversions have a licence?
2. (Q17) Is there a revised TRT 11 SLA? The "latest" page still shows the 2021 text. The release check reads the SLA text shipped in the 11.3 RPMs (`/usr/share/doc/tensorrt-11.3.0.99`, and the LICENSE in the Python dist-info).
3. (Q18) Legal review: is it right to treat GPU driver GL/Vulkan libraries that Qt loads in the GUI process as System Libraries?
4. (Q19) How does `video-sync=display-resample` behave on VRR at ~119.88 fps output?
5. (Q20) How good is RIFE on PQ/HLG-encoded RGB without linearisation (relevant once HDR interpolation is enabled)?
6. (Q21) Do frames synthesized by RIFE preserve Dolby Vision RPU or film-grain side data? (Relevant after v1.)
7. (Q22) How does the GlobalShortcuts portal behave for unsandboxed apps on GNOME 48+ and KDE? (Relevant after v1.)
8. (Q23) When will Fedora 45 ship (the 2026-10-20 target may slip), and when will PyPI PySide6 support Python 3.15?
9. (Q24) Are `buttereye.app` and `buttereye.dev` available? (The `7z` part is answered: Fedora ships `7zip` 26.03 on F44 and F45.)
10. (Q26) Will Fedora move to VapourSynth R80+ during F44/F45's life, and will upstream vs-mlrt or akarin port to API4? Either decides how long the experimental TRT path keeps loading (§5.2, §11).

Resolved and removed from the previous draft:

- mpv lifetime is decided by the process model (§4.10).
- The offline source filter question became spike M0(e).
- The ctypes `RTLD_GLOBAL` preload was dropped: engines are built with `trtexec` only and no in-process TRT is used.
- Qt Charts is decided: avoided.
- Test hardware became an M0 precondition.
- PyPI wheel loading on R72/R79 (old spike M0(f)) is replaced by per-chroot COPR builds.

---

## 14. Contributing

No git repository exists yet (owner decision 2026-10-07), so there are no outside contributions today. This section describes what `CONTRIBUTING.md` will say once a repository is created; until then, the source tree is distributed as the sdist inside the COPR SRPM.

- **`CONTRIBUTING.md`** covers developer setup on Fedora 44 or 45:
  1. `sudo dnf install mpv vapoursynth-tools python3-vapoursynth python3-pyside6 mkvtoolnix vulkan-tools`, plus either `ffmpeg-free` or RPM Fusion `ffmpeg` (do not swap one for the other on a system that already has one);
  2. `sudo dnf copr enable <owner>/buttereye && sudo dnf install buttereye-vs-mvtools buttereye-vs-rife-ncnn buttereye-rife-ncnn-models` for the plugins (§9);
  3. `python3 -m venv --system-site-packages .venv`;
  4. `pip install -e .[dev]`.
- **Packaging contributions:** COPR specs are MIT-licensed, written for ButterEye, never copied from av-rpm (§8.4). Builds are submitted as locally built SRPMs (`rpmbuild -bs`, `copr-cli build`).
- **The Python floor for development** is 3.14. CI also runs on F45 (Python 3.15).
- **Pre-commit hooks:** ruff, mypy (on `core/`), REUSE lint and the SPDX header check. They can run locally today.
- **DCO** (adopted): every commit carries `Signed-off-by:`, and a CI check enforces it once a repository and CI exist. The PR template includes the provenance questions from §8.5.
- **Code of Conduct:** Contributor Covenant 2.1.
- **Issue templates** ask for `buttereye doctor --report` output (redacted by default) and error codes (`BE-xxxx`).
- **Spike results** live in `docs/spikes/`. Error codes are documented in `docs/errors.md`.

## 15. After v1: performance track and filter chain (added 2026-10-09)

Nothing in this section is a v1 gate. It records the owner-approved direction so that v1 work does not close it off.

### 15.1 Performance track (P0–P4)

**Evidence (dev box, 2026-10-08/09).** At 1080p 23.976 → 2×, RIFE v4.26, v4.22-lite and v4.18 measure the same: ~69–70 fps in vspipe and ~62 fps in mpv (MVTools: 139 and 118). Models of clearly different cost running at the same rate mean inference is not the limit. A 4K 23.976 → 60 render ran at 8.25 output fps (14,774 frames in 1,792 s), almost exactly a quarter of the 1080p rate, so the limit grows with pixel count: CPU YUV↔RGBS conversion, float transfers to and from the GPU, or the plugin's per-frame handling (see also §1 on 4K bandwidth). A 4K video shrunk to 720p still dropped 14–19 % of frames at start. An implicit Vulkan layer from another tool (`VK_LAYER_NV_dlssnr`) loads into every RIFE process.

- **P0, measure (spike M0(i)).** `tools/perf/breakdown.py` runs vspipe and mpv stages one at a time: source only; source + YUV→RGBS→YUV; RIFE per model and per `gpu_thread` (1/2/4/8); MVTools; each at 1080p and 2160p; with and without implicit Vulkan layers (`VK_LOADER_LAYERS_DISABLE`); while sampling `nvidia-smi dmon` (SM %, PCIe rx/tx). Optional: a Nsight Systems trace and the standalone `rife-ncnn-vulkan` image-pair rate as the inference ceiling. Reference numbers from other tools are kept outside the repository. **Decision rule:** GPU busy < ~60 % → overhead-bound → P2C first; ~100 % → inference-bound → P2A/P2B.
- **P1, cheap wins in ButterEye.** Status 2026-10-10 (spike M0(m)): MVTools concurrent-frames raised to 16 (+16 % at 1080p, +26 % at 4K in mpv); RIFE-ncnn `gpu_thread` stays 4 (8 gains ≤ 6 %, within noise); the implicit Vulkan layer had no cost (M0(i)). The per-frame Python scene-change callback costs ≤ 0.5 % even at TensorRT speeds (200–485 fps), so it stays (no native filter, no new plugin dependency). **P1 closed.** Keep implicit Vulkan layers out of the mpv and vspipe environment if P0 shows a cost. Prefer integer multipliers for demanding sources (at 2× the source frames pass through, §4.4). Have the benchmark choose `gpu_thread` and `concurrent-frames` per resolution. Replace the Python per-frame scene-change callback with a native filter.
- **P2, faster RIFE.** Status 2026-10-09: wired into live play, the speed test and renders (§5.2 "As built"); spike M0(l): `vstrt` on TensorRT 11 plays 1080p 2× at 262 fps and 1080p → 60 at 101 fps in mpv on the RTX 4090 (4× the ncnn path); 4K 2× reaches real time with no headroom. Measured in spike M0(j) (2026-10-09): TensorRT fp16 runs RIFE v4.26 at 148 new frames/s at 1080p and 33.5 at 4K on the RTX 4090, 4.2–4.9× the ncnn plugin; copying fp32 frames without overlap halves that. (A) **TensorRT is the NVIDIA path**: make it the default once the user has installed it, after `vstrt` reproduces these numbers in mpv with overlapped transfers and fp16 passes the quality gate (still user-built and out of process, §5.2, §8.3). (B) vs-mlrt's other runtimes: on NVIDIA they add nothing over (A); for AMD and Intel, vs-mlrt's ncnn and MIGraphX backends are still to be measured on the cross-vendor box. (C) Transfers, on every runtime: overlap upload, compute and download (several streams) and move half-precision frames; patching the ncnn plugin's pipelining alone cannot reach TensorRT. **Half-size motion (`scale=0.5`) is not an option for current models:** vs-mlrt supports it only for RIFE v4.0–v4.6, so 4K on GPUs slower than the 4090 still means smoothing at a smaller size (GUI.md §11.9).
- **P3, CPU and lightweight GPU path.** Status 2026-10-10 (spike M0(p)): MVTools' motion search isn't the bottleneck (block size, overlap and luma-only gain ≤ 6 %); FlowFPS is. Live play now falls back to **BlockFPS at full size** when FlowFPS can't keep up (2.2–2.3× faster in mpv, 72 fps at 4K 2×, same fidelity, blocky only in very fast motion); a GPU engine at a smaller size still wins. No `ScaleVect` in this MVTools, so downscaled analysis is out. Originally: MVTools: analyse a downscaled clip, larger blocks at 4K, `BlockFPS` when `FlowFPS` cannot keep up. Research: `VK_NV_optical_flow` (the hardware optical-flow engine through the Vulkan driver, which counts as a system library, §8.1) as a free GPU motion engine.
- **P4, mpv filter path** (~10 %). The vapoursynth filter takes system-memory frames only, so copy-back decode stays; changing that is upstream mpv work and not planned.
- **Targets (dev box).** 1080p → 60 fps with ≥ 2× headroom; 4K at 2× in real time with RIFE; 4K → 60 offline at ≥ 30 fps. M0(j) puts the first two within reach of TensorRT fp16 (148 and 33.5 new frames/s against 60 and 24 needed); the third needs ~2× more than measured.

### 15.2 Filter chain and upscaling

- **Two slots.** (1) The generated script, before mpv renders: denoise, deband, deinterlace, ML restoration and ML upscaling. Cleaning stages run before interpolation (noise and banding mislead motion search). (2) mpv GPU shaders (`--glsl-shaders`) after the filter, at almost no cost: CAS sharpening, FSR 1, FSRCNNX/RAVU, Anime4K-style shaders, deband. ButterEye passes these at launch and over IPC; it still never edits `mpv.conf` (§4.3).
- **Order and cost.** Upscaling before interpolation makes RIFE work at the larger size; after it, there are 2–2.5× as many frames to upscale. The default is interpolate at the source size, then upscale with a shader.
- **Upscaling — as built (2026-10-10):** `[general] upscaling = "standard" | "sharper"` (simple window: Upscaling). "Sharper" appends the bundled FSRCNNX x2 8 shader at launch (`--glsl-shaders-append`, after the user's own shaders) and adds or removes it while playing with `change-list glsl-shaders append|remove`; the IPC allowlist accepts only the bundled shader's exact path (`mpvctl/shaders.py`). The shader works only when mpv upscales by more than 1.3×. - **Shader upscaling — measured in spike M0(o) (2026-10-10):** through libplacebo (mpv's renderer), FSRCNNX x2 8 (GPL-3.0) beats Lanczos by +0.5 dB on anime and +1.25 dB on live action for ~1.7 ms per 4K frame; ArtCNN C4F16 (MIT) +1.75 dB on live action for ~2.9 ms; all candidates are shippable. Proposed: the upscaling stage uses mpv shaders, with FSRCNNX x2 8 as an off-by-default "sharper upscaling" option. - **ML upscaling — measured in spike M0(n) (2026-10-10):** the only shippable (BSD) models, Real-ESRGAN's anime-video ones, repaint the picture and lose 9–15 dB; the faithful, fast ones (AnimeJaNai V3 HD, Ani4K v2) are non-commercial, so ButterEye can't ship them; their gain over Lanczos is crisper anime line art only, real time up to 1440p output on a 4090. Proposed: no bundled ML upscaler; post-v1 "bring your own model" (user-installed, anime, behind the TensorRT opt-in); mpv's scaler stays the default. Originally: ML upscaling (Real-ESRGAN compact, SPAN and similar) runs on the same runtimes as RIFE (P2), so P2 is built as a general model runtime, not a RIFE-only one. Light models are expected in real time on high-end GPUs; heavy ones are offline only. Each model's licence is checked before it is packaged, as for the RIFE models (§5.4).
- **Vendor video upscalers.** NVIDIA's video super-resolution SDKs are proprietary; if one is usable on Linux, it follows the TensorRT pattern: installed by the user, loaded in mpv or vspipe, never in ButterEye's process (§8.1), labelled experimental.
- **Game upscalers and frame generation (DLSS and similar)** need engine inputs (motion vectors, depth, jitter) that decoded video lacks. They are not a planned path. A vendor video mode, if one appears, would follow the pattern above.
- **Needed in the core.** The script contract (§4.4) holds an ordered list of stages, each declaring its slot; the benchmark and the "play smaller" decision (GUI.md §11.9) budget the whole chain, not interpolation alone.

---

## Appendix A. Review notes (rejected or modified critiques)

| Critique | Disposition | Reason |
|---|---|---|
| Completeness 4: allow NVIDIA domains in the download allowlist | Modified: no NVIDIA hosts | Licence 15 has the user download or install NVIDIA components themselves, so ButterEye never fetches from NVIDIA. |
| Completeness 5: option to mirror ncnn models under ButterEye's own release | Rejected | Licence 6 and 7 forbid re-hosting third-party models and binaries on GitHub releases or mirrors. **Superseded 2026-10-07 (Q2):** a curated MIT ncnn model subset now ships in the project COPR with its SRPM (§5.4, §9); GitHub releases still never host models. |
| Completeness 5: `.7z` via `py7zr` in-process or external `7z` | Chose external `7z` | Keeps archive parsing out of ButterEye's process, consistent with §8.1. `.tar.gz` uses the standard library. Only the experimental TRT path needs `7z` (for ONNX model archives). Fedora's package is `7zip`. |
| Completeness 12: `buttereye-agent` subprocess for GUI-launched mpv | Modified: used the critic's stated alternative | GUI launches use `--input-ipc-server` in the 0700 runtime directory and adopt `/tmp/mpvSockets/<pid>` when mpvSockets overrides it. This is simpler than a resident agent and still resolves the F8 contradiction. |
| Completeness 17: optional CC-BY clip downloaded for the benchmark | Rejected | Licence 13 calls for generated clips for the benchmark. A generated clip also avoids a network dependency. Optional runs on the user's own file cover realism. |
| Feasibility 17: replace F15 (rules engine) with auto-selection only | Rejected; kept a minimal first-match rules engine | The owner's brief lists "rules" in the core library among decisions not to be re-litigated. The other scope cuts in this item were accepted. |
| Feasibility 17: shrink the benchmark | Accepted with an optional `--full` matrix | The benchmark itself is in the owner's brief. Only the acceptance scope was reduced. |
| Feasibility 2: benchmark source `format=yuv420p10le` | Modified to `nv12`/`p010le` | Planar input would skip the autoconvert cost that the critique itself identifies. Semi-planar input reproduces copy-back hwdec output. `--hwdec` has no effect on a lavfi source. |
| Feasibility 4: "RIFE-ncnn pads internally" | Kept as unverified | This is not in the verified evidence. It is confirmed in spike M0(a). |
| Feasibility 18: disable offline render entirely without libavcodec-freeworld | Modified to a per-codec check | Evidence shows `ffmpeg-free`'s decoders include AV1, VP9, MPEG-2 and ProRes. Only H.264/HEVC need RPM Fusion. `lsmas` (source build, COPR if adopted) on F45 (R79) is added to spike M0(e). |
| Feasibility 21: RX 7800 / Arc A770 class expectation | Kept, marked unverified | No primary data exists. Spike M0(a) settles it. |
| Feasibility 14 (vsmlrt dir on `sys.path`) vs licence 5 (do not vendor) | Reconciled | The directory holds an unmodified upstream `vsmlrt.py`, not vendored into ButterEye's source. Since 2026-10-07 (Q2) it is conveyed as the separate COPR package `buttereye-vsmlrt-py` under its own GPL-3.0 licence (§8.4). |
| Licence 3: choose (a) or (b) | Chose (a) | It matches feasibility 12 (`trtexec`-only), needs no new licence permission, and is enforced by a test. NVML was replaced by an `nvidia-smi` subprocess. |
| Licence 15: symlink `trtexec` into `<plugin dir>/vsmlrt-cuda/` | Replaced | Licence 5 has `trtexec_path` overridden at run time in the generated script, which avoids writing into plugin directories at all. |
| Licence 5: SPDX `GPL-3.0-only` for a vendored vsmlrt.py | Kept as a conservative choice, not vendored in v1 | GPLv3 §14 arguably lets the recipient choose any version when none is stated. The question goes to upstream (§13 Q16). |
| Licence 8: glslang licence composition | Accepted with a verification note | The claim is critic-reported and not in the verified evidence. It is checked against the pinned tag. **Checked 2026-10-07:** nihui/glslang a9ac7d5f `LICENSE.txt` lists BSD-3-Clause, BSD-2-Clause, MIT, Apache-2.0 and GPL-3.0 with the Bison exception (§8.2). |
| Licence 1: mpv-interpolation is GPL-2.0-only | Accepted | The evidence recorded "GPL-2.0" from GitHub's API field. The critic's check of the file header and LICENSE is more specific. |
| Feasibility 17: "v1 supports HDR10 only" offline | Applied to HLG as well (deferred) | This follows the critique's wording. HLG could be revisited cheaply after v1, since it needs no static metadata. |
| Completeness 3: X11 in or out | In scope (Wayland primary) | QScreen works on X11 through xrandr, and nothing in the design is Wayland-specific. One X11 test pass was added to M5. |
---

## Appendix B. Decision log

### 2026-10-07: owner decisions applied

| Item | Decision | Where applied |
|---|---|---|
| C1 Contributor policy | DCO (`Signed-off-by:`, CI-enforced once a repository exists), no CLA | §3, §8.5, §13, §14 |
| C2 §7 output permission | Adopted for generated `.vpy`, `buttereye.conf`, `input.conf` snippets and job manifests; draft text written | §3, §8.4, §13, F18 |
| C3 NVIDIA in-process | Confirmed never; no NVIDIA §7 exception | §3, §8.1, §13 |
| Q1 TensorRT line | EXPERIMENTAL vs-mlrt v16.x / TRT 11 (pinned v16.3.test1, TRT 11.3.0.99, CUDA 13.4.2; fallback 11.1/13.3); v15.16 / TRT 10 dropped as target; opt-in, never a release gate | §1, §2, §3, §4.5, §5.1–5.3, F1, F10–F13, §11, §12 M0(c)/M2 |
| Q2 Plugin provisioning | ButterEye COPR in v1 (ButterEye + FOSS plugins + curated MIT ncnn models, source-built for F44/F45, network-off builds, SRPM duties); `vstrt` and ONNX conversions never packaged | §2, §3, §4.4–4.8, §5.4, §8.1–8.2, §9, §11, §12 M0(f)/M1/M5 |
| Q3 Distros | Fedora 44 and 45 only; Arch not supported (generic detection only) | §2, §3, §9, §10.2 |
| Q6 Offline HDR | HDR10 via x265/SVT-AV1 in v1; HLG, DV, NVENC/VAAPI HDR deferred | §2, §3, §7.4, §11 |
| Q4, Q5 | Unanswered; recommended defaults kept, still open. Q5 now also determines the COPR owner name | §13 |
| Git repository | Not created yet; no step assumes one exists. Git tags and CI checks apply once it does | header, §6.1, §8.4, §8.5, §9, §11, §12, §14 |

Corrections made while applying these decisions: `vstrt` is a VapourSynth **API3** plugin (not API 4.0 as an earlier draft said) and cannot load on R80+; the MVTools source moved from a PyPI wheel to a per-chroot COPR build; fftw, ncnn and glslang are now Fedora's and are not conveyed (ncnn and glslang superseded by the bundled-ncnn decision below).

### 2026-10-07: bundled ncnn (after spike M0(f))

| Item | Decision | Where applied |
|---|---|---|
| ncnn for `buttereye-vs-rife-ncnn` | Bundle the plugin's pinned Tencent/ncnn 305837fd (2025-05-03) and nihui/glslang a9ac7d5f, statically linked (`-Duse_system_ncnn=false`), conveyed with their `LICENSE.txt` and `Provides: bundled(…)`. On Fedora ncnn 20250916 the plugin faulted the GPU (Xid 13) in 12/20 runs at `gpu_thread=4`; the bundled build had no Xid 13 in 42 runs at the same speed (later 2 × Xid 109 with rife-v4.25-lite, m0f.md). Fedora-ncnn variant kept behind `--with system_ncnn` | §5.1, F10, §8.1, §8.2, §9, §11, §12 M0(f), §13, Appendix A |
| RIFE-ncnn `gpu_thread` default | 4 (2 gives ~52 fps at 1080p 2x, below F2's ≥ 1.15× headroom of ~55 fps) | §4.4 |
| Doctor GPU-fault check | Report NVIDIA Xid errors in `journalctl -k` after a RIFE-ncnn session | F1 |

### 2026-10-07: reliable live decisions (docs/design/GUI.md §11.9)

| Item | Decision | Where applied |
|---|---|---|
| Live stalls on a 4090 at 180 Hz (BE-3004) | Live cap = in-mpv bench fps ÷ 1.25 (`LIVE_HEADROOM`; the bench's 1.15 real-time verdict unchanged), scaled by pixel count from any measured size; engine chosen after the target (Automatic: benchmarked RIFE-ncnn only if it at least doubles, else MVTools; pinned RIFE-ncnn that can't → MVTools for the file, with a notice); nothing keeps up → plain notice, no filter; benchmark rule `fps_max` +1 %; a RIFE-ncnn stall, device loss or Xid switches that session to MVTools once, never back | §4.11, §5.3, §5.6, GUI.md §11.9 |

### 2026-10-09: performance track, filter chain, two drop zones

| Item | Decision | Where applied |
|---|---|---|
| Performance | Measure first (spike M0(i), P0), then fix the plugin or move to a faster runtime per the P0 decision rule | §12 M0(i), §15.1 |
| Filters and upscaling | A post-v1 filter chain with two slots (script, mpv shaders); ML upscaling on the P2 runtime; vendor upscalers only out of process; game upscalers/frame generation not planned | §2 non-goals, §15.2 |
| Simple window | Two drop zones (play, save a smooth copy); no scrolling | §7.8, GUI.md §12, §12.5, §12.7 |
| P0 result (M0(i)) | RIFE-ncnn is capped near 35–41 new frames/s at 1080p and ~7 at 4K on the 4090; not the model, not colour conversion | `docs/spikes/m0i.md` |
| fp16 gate, first pass (M0(k)) | fp16 fine to 1080p; at 4K it alters fast motion; proposed: default ≤ 1440p, offered at 4K live, fp32 for 4K renders (owner to confirm) | `docs/spikes/m0k.md` |
| MVTools BlockFPS fallback (M0(p), 2026-10-10) | Live MVTools uses BlockFPS at full size when FlowFPS can't keep up, before smoothing smaller; GPU at a smaller size still wins; renders keep FlowFPS | §15.1 P3, `docs/spikes/m0p.md` |
| Sharper upscaling built (2026-10-10) | `[general] upscaling`; bundled FSRCNNX x2 8 (LGPL-3.0) added to mpv by exact path, user shaders kept; off by default | §8.2, §15.2 |
| Shader upscaling (M0(o), 2026-10-10) | Proposed: upscaling uses mpv shaders, not ML; ship FSRCNNX x2 8 (GPL-3.0) as an off-by-default "sharper upscaling" option; mpv's scaler stays the default (owner to confirm) | §15.2, `docs/spikes/m0o.md` |
| ML upscaling (M0(n), 2026-10-10) | Proposed: no bundled ML upscaler (shippable models repaint the image; good ones are CC BY-NC); post-v1 "bring your own model" for anime, ≤ 1440p live; mpv's scaler stays the default (owner to confirm) | §15.2, `docs/spikes/m0n.md` |
| MVTools concurrency (M0(m), 2026-10-10) | concurrent-frames up to 16 (CPU-count capped) in live play and the speed test; RIFE-ncnn gpu_thread stays 4 | §15.1 P1, `docs/spikes/m0m.md` |
| TensorRT wired in (2026-10-09) | Live play, speed test and renders use `rife-trt` when opted in and set up; engines built in the background with a RIFE-ncnn fallback meanwhile; ButterEye's Python folder goes first on `sys.path` (Fedora's protobuf is too old for onnx) | §5.2 "As built" |
| vstrt on TRT 11 (M0(l)) | Builds and runs with three script-side fixes; fp16 + RGBH + 2–4 streams is the NVIDIA setting (proposed default, owner to confirm); fp16 quality on par with today's ncnn path, so fp32 becomes an optional "best quality" for renders; 4K live stays "play smaller" by default | §5.2, `docs/spikes/m0l.md` |
| P2 result (M0(j)) | TensorRT fp16 is the NVIDIA path (4.2–4.9×); transfers must overlap; no `scale=0.5` for v4.7+ models | §15.1, `docs/spikes/m0j.md` |

### 2026-10-08: converting videos (offline render in the simple window)

| Item | Decision | Where applied |
|---|---|---|
| Output size | Offline render may shrink to 1440p/1080p/720p (source size stays the default); upscaling remains a non-goal | §7.8, GUI.md §12.7 |
| Entry point | "Save smooth copy…" per Now playing row and "Convert a video…" in the simple window | §7.8, GUI.md §12.7 |
| Order | SDR/CFR first; HDR10 next (§7.4 unchanged); HDR10 refused with an explanation until then | §7.8, §12 M3 |
| Prerequisite | Spike M0(e) (ffms2 on R72) runs before the render engine is relied on | §12 M0(e) |

### 2026-10-07: GUI design amendments (docs/design/GUI.md §10)

| Item | Decision | Where applied |
|---|---|---|
| Core/GUI bridge | QtAsyncio no-go (M0(h)); stock asyncio loop in a dedicated thread, Qt-signal bridge | §4.1, §12 M0(h), §13 |
| CLI parity | Added `profiles` subcommand and the `attach --enable/--disable/--profile`, `detach --orphans`, `bench --apply`, `clean --dry-run`, `setup --trt-experimental` flags | §4.1 |
| GUI Qt modules | Only QtCore, QtGui, QtWidgets in-process; QtNetwork and QtDBus banned with the Addons | F17 |
| String catalogue | `tools/extract_ts.py` until `lupdate` is usable | §2 goal 9, §12 M4 |
| RIFE model set | Drop rife-v4.25-lite from `buttereye-rife-ncnn-models` (both Xid 109 faults on the bundled build came from it in vspipe, m0f.md); the Balanced profile and the mid-range selection default move to v4.22-lite, with v4.18 as the fallback | §5.3, §5.4, §13 Q25 |
| RIFE-ncnn `concurrent-frames` | 8 in mpv, independent of `gpu_thread` (stays 4); measured ~56–58 fps vs ~45–47 at 1080p 2x on the 4090, no Xid in 9 runs | §4.4 |
