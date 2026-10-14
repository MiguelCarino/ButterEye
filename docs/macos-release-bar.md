<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
<!-- Copyright (C) 2026 The ButterEye contributors -->
# macOS: the release bar

This is the phase-5 document of the macOS port (`docs/PLATFORMS.md`, "macOS: the
realistic path"). It holds no new feature. It answers one question — **is there
enough measured evidence to call macOS supported, and if not, what exactly is
missing** — by listing every line of the bar Windows is held to, giving each a
verdict, and attaching to each verdict either a command and its output or an
explicit admission that nobody has run one.

**The decision is the owner's, not this document's.** SCOPE §2 lists macOS as a
v1 non-goal and §13 records the decisions the owner has taken; nothing here
edits either, and no sentence here is a support claim. The last section is
marked as a recommendation and is meant to be read as one.

Everything dated 2026-10-13 below was measured for this document on the dev Mac:
macOS 27.0.1 (build 26A434), Apple M2, arm64, 8 threads, Homebrew prefix
`/opt/homebrew`, mpv 0.41.0, VapourSynth R81, MoltenVK 1.4.2. Paths are written
as `$HOME`, `~` or `<prefix>`; no serial number, platform UUID, host name or
per-user temporary path appears in this file, by the same rule phase 2 adopted
for `docs/PLATFORMS.md`.

**Why this file is in `docs/`.** `docs/` holds the documents that outlive a
phase and that cut across the tree — `SCOPE.md`, `PLATFORMS.md`, `errors.md`;
`docs/spikes/` holds the numbered M0 measurement spikes, and
`packaging/macos/` holds the records that belong to one build
(`RIFE-SPIKE.md`, `SOURCES.md`). A release bar is neither a spike nor a
packaging note: it is the port-wide decision record `PLATFORMS.md`'s phase-5
row points at, so it sits beside it under `docs/`.

## The bar, and where it comes from

The bar is not invented here. `docs/PLATFORMS.md` says why Windows — four
completed phases, an installer, and a green CI job that installs it on Windows
Server 2025 — is still unreleased:

> It has **not yet been used on a real Windows desktop PC with a GPU**, so it is
> not released.

and the Windows phase-4 section lists what that means item by item, under "Still
not verified, needs a real Windows desktop PC with a GPU": the engines on a real
GPU, on-screen playback and its timing, the GUI window as a user sees it, the
installer's pages, the operating system's gatekeeping of an unsigned installer,
the runtime download and its permission prompt, a model in a non-ASCII folder,
the window with a prerequisite missing, fonts, the install route a user types,
and a non-administrator account. `.github/workflows/windows.yml` is the other
half of the same bar: it builds the distribution and then **installs it and runs
ButterEye's own tests from the installed copy** on a clean runner.

Each of those is a question, not a Windows detail, and every question has a
macOS form. The derivation:

| The Windows bar asks | The macOS form of the same question |
|---|---|
| RIFE and mvgpu on a real GPU | every engine run and benchmarked on the Apple GPU through MoltenVK, with the engine choice following the measurement |
| On-screen playback and its timing | mpv drawing on a real display at the display's rate, not `--vo=null` |
| The GUI window as a user sees it | the Qt window under the `cocoa` platform plugin on a real screen, at the system font scale |
| The installer's pages, SmartScreen, Defender | the tap installed for real, and what Gatekeeper does with what `brew` fetches |
| The Visual C++ download and its prompt | nothing: a Mac's prerequisites are Homebrew formulae the tap declares |
| A model in a non-ASCII folder | the same, plus macOS's own path normalisation, plus the 242-character model-path limit the port found |
| The window without a prerequisite | ButterEye on a Mac with no VapourSynth, no MoltenVK, no RIFE: does it say what is wrong |
| Fonts | the same question, on the Mac system fonts and a HiDPI screen |
| `winget install shinchiro.mpv` and `PATH` | `brew install mpv`, and the mpv a Mac user is likelier to already have (the mpv.io bundle) |
| A non-administrator account | a second account at all: the cross-UID socket refusal SCOPE §4.3 is actually about |
| CI installs the artefact and runs the tests from it | a CI job that installs the tap and runs the suite from the installed copy |
| Code signing before release | nothing to sign in a tap, measured — but a tagged release with real checksums |

Two things macOS adds that Windows has no peer for, both found by this port and
both on the bar because they can lose a user's session or their data: the
per-user temporary directory being swept on an age rule, and `flock` on a
network home. They are items 11 and 12 below.

## The verdicts at a glance

| # | Bar item | Verdict |
|---|---|---|
| 1 | Live playback with real smoothing, in mpv, on this hardware | **VERIFIED** |
| 2 | Offline render, end to end | **VERIFIED** |
| 3 | The doctor tells the truth on a Mac | **VERIFIED** |
| 4 | Every engine benchmarked on the real GPU, and the engine choice following it | **PARTLY VERIFIED** — every engine measured via a shadow prefix (`docs/spikes/m0s.md`), and the chooser now follows those measurements when they recommend mvgpu (fixed and tested against the real rows); never from an install, and the 1440p ordering does not reproduce |
| 5 | The GUI as a user sees it | **PARTLY VERIFIED** — drawn and captured on this Mac; never driven by a person |
| 6 | The tap actually installed | **NOT VERIFIED** |
| 7 | ButterEye's own tests run from the installed copy, in CI | **NOT VERIFIED** |
| 8 | On-screen playback and its timing | **NOT VERIFIED** |
| 9 | A clean machine: no venv, no repository, nothing exported | **NOT VERIFIED** |
| 10 | A cross-UID socket refusal (SCOPE §4.3's real threat) | **CANNOT BE VERIFIED HERE** (needs a second account) |
| 11 | `dirhelper` sweeping the runtime directory in a long session | **NOT VERIFIED** (the rule is verified; the consequence is not) |
| 12 | `flock` on an SMB/AFP network home | **CANNOT BE VERIFIED HERE** (needs a file server) |
| 13 | `$TMPDIR` inside a sandboxed bundle | **CANNOT BE VERIFIED HERE** (needs a bundle that will never exist) |
| 14 | Intel Macs | **CANNOT BE VERIFIED HERE** (needs Intel hardware) |
| 15 | The model-path patch on Fedora, which this port changed | **VERIFIED** (built for fc44 and fc45 in a Fedora container; before aborts at 242, after does not) |
| 16 | Unicode and long paths | **NOT VERIFIED** |
| 17 | Gatekeeper and code signing | **VERIFIED**, in the narrow form that applies to a tap |
| 18 | RIFE live on an M2 | **MEASURED FALSE** — five times too slow; the docs must say offline-only, and the code now never picks it live on a Mac without a benchmark or the user's choice |
| 19 | TensorRT on macOS | **WILL NEVER BE TRUE** |
| 20 | A second machine of any kind | **NOT VERIFIED** — every macOS number in this repository comes from one M2 |

Four items are green and two are half green; seven are open — several of them
only closable on a machine that is not this one — five cannot be closed on this
hardware at all, one is a measured negative the documentation has to respect,
and one is impossible forever. The shape matters more than the count, and the recommendation section
sorts the open ones into blocking and merely unknown.

`docs/PLATFORMS.md`, "Phase 5: the real-hardware gate", carries the same
measurements as a narrative, with the table of every macOS number in the
repository and the machine it came from. This file is the checklist form of the
same evidence: one line per question Windows is asked, with a verdict on it.

## The checklist in detail

### 1. Live playback with real smoothing — VERIFIED

```
$ env -u VSSCRIPT_PATH -u DYLD_LIBRARY_PATH -u DYLD_FALLBACK_LIBRARY_PATH \
      <venv>/bin/python -m pytest tests/integration/test_hdr_live_real.py -q
3 passed in 2.83s
```

(2026-10-13.) This is the one test that was red in phases 0 to 3 with BE-3003,
"the smoothing filter failed and was removed", and it passes with nothing in the
environment: ButterEye derives VSScript's path itself and hands it to each child
mpv (`osapi.vsscript_library`, `osapi.point_at_vsscript`; `docs/PLATFORMS.md`,
phase 4). Real HDR10, a real Homebrew mpv, a real VapourSynth filter inside
mpv's own process. Recorded in `docs/PLATFORMS.md` ("The headline: a Mac smooths
video live, in mpv") and covered by `tests/macos/test_runtime_macos.py`.

What this does **not** cover is item 8: the test runs mpv with no video output
on the screen.

One thing this item's mechanism did not cover either, found and fixed during
phase 5: the file `vsscript_library()` returns is `dlopen`-ed inside mpv, and the
search read `sys.path`, whose first entry under `python -m` is the **current
directory** — so running ButterEye from a directory holding
`./vapoursynth/libvsscript.dylib` handed that file to the player. Measured before
the fix, and closed by a cwd skip plus an ownership-and-mode check on every
candidate directory (`_darwin._vsscript_dirs`, `_darwin._trustworthy_dir`); both
attacks were re-run on 2026-10-13 and both now answer with Homebrew's real
library, and it now has tests at both levels. Against the real Homebrew copy
(macOS and `BUTTEREYE_TEST_BREW` only, so **not** in CI):
`tests/macos/test_runtime_macos.py::test_a_plant_in_the_working_directory_loses_to_the_library_homebrew_ships`
and `…::test_a_plant_in_a_directory_the_group_can_write_loses_to_homebrews`.
Both assert the *identity* of the answer rather than the absence of the plant —
a search that had simply stopped working would satisfy "not the plant" — and
each isolates its own half: the cwd test asserts `_trustworthy_dir` **accepts**
the plant before chdir-ing into it, and the mode test ends by loosening nothing
but the mode back to `0o750` and asserting the same plant **is** then returned,
which is what proves the search really does prefer `sys.path` to the keg and
that the earlier refusal was the mode and not something else. The half the macOS
CI job can run, because it needs no bottle, is in
`tests/core/test_windows_runtime_pure.py`:
`test_macos_vsscript_is_never_taken_from_the_directory_it_was_started_in`,
`…_skips_the_working_directory_under_either_spelling` (the `sys.path` entry and a
symlink to it — a Mac's per-user temporary directory is reachable under two
spellings, one a symlink to the other, which is why comparing the literal entry
is not enough), `…_is_never_taken_from_a_directory_others_can_write` (group- and
other-writable, each with the 0o750 control). The first three lead with their
control — the same plant, reached the same way, *is* returned before the attack
is arranged — so a search that stopped reading `sys.path` at all would fail the
first assertion instead of passing the test. The fourth,
`…_is_never_taken_from_a_directory_another_user_owns`, is the owner half, which
`tmp_path` cannot stage because this suite creates no users: it asks
`_trustworthy_dir` about `/usr/local`, root's and 0o755 on an arm64 Mac, and
skips itself rather than guessing if that directory is this user's (an Intel
Mac's Homebrew prefix) or group-writable, so it can only ever be the *owner*
rule that answers.
`docs/PLATFORMS.md`, phase 4's "What ButterEye does about it", carries
the detail and the general rule it implies, because ButterEye exports
`DYLD_FALLBACK_LIBRARY_PATH` to every child it spawns.

### 2. Offline render, end to end — VERIFIED

```
$ ffmpeg -f lavfi -i testsrc2=size=640x360:rate=24 -t 3 -c:v libx264 in.mkv
$ <venv>/bin/python -m buttereye.cli render -o out.mkv in.mkv
Saved out.mkv.                                              # 4.0 s wall
$ ffprobe -count_frames -select_streams v:0 \
      -show_entries stream=codec_name,r_frame_rate,nb_read_frames in.mkv out.mkv
in.mkv:  codec_name=h264  r_frame_rate=24/1  nb_read_frames=72
out.mkv: codec_name=hevc  r_frame_rate=48/1  nb_read_frames=144
```

(2026-10-13.) `vspipe` → `ffmpeg` (libx265) → `mkvmerge`, with MVTools as the
engine, with no VapourSynth variable set: `vspipe` imports VapourSynth as a
Python module and never goes near the bare-name `dlopen` that broke mpv. The
only gap is a test: all four of `tests/integration/test_render.py`'s tests are
`devbox`-marked and skip on a Mac for want of `rpm` and the `buttereye-*` RPMs,
so this is a measurement a human has to repeat, not one CI protects.

### 3. The doctor — VERIFIED

```
$ <venv>/bin/python -m buttereye.cli doctor
13 ok, 5 info, 1 degraded, 0 blocking (0.7 s)
```

(2026-10-13, exit 0.) No `dnf`, `rpm`, `sudo`, `copr` or `journalctl` word in
any finding — `tests/core/test_macos_doctor_pure.py` asserts that on every
platform, and `tests/macos/test_doctor_macos.py` runs the whole doctor against
this machine's own Homebrew, `vulkaninfo` and mpv behind `BUTTEREYE_TEST_BREW`.
The one degraded row is RIFE not being installed, which is true (item 4) and
says so without offering a command that cannot work.

Two strings have drifted behind the build, and `docs/PLATFORMS.md` phase 4 names
both. The mvgpu row still says ButterEye "doesn't build mvgpu for macOS yet",
which phase 3 made untrue and the tap makes installable; it appeared again in
today's run, quoted above. BE-1005's cause names only
`libvapoursynth-script.dylib` as the bare name mpv looks for
(`probe.MPV_VSSCRIPT_NAME`), where mpv asks for `libvsscript.dylib` first and
quotes only the second failure — so that one is accurate about mpv's error text
and incomplete about what mpv does, and it is **not** in today's output at all,
because the probe passes here and the finding is never emitted. Both are strings
in `buttereye/core/doctor/` rather than verdicts, and neither changes one.

### 4. Every engine benchmarked on the real GPU — PARTLY VERIFIED

**This item was written before `docs/spikes/m0s.md` landed, and the spike answers
the measurement half of it.** What remained open was the install route and the
chooser, so the verdict was downgraded from NOT VERIFIED rather than cleared. The
chooser half has since been fixed and tested against the spike's rows ("The
chooser half", below); the install route and an idle re-measurement are what
keep it from closing. The
paragraph and the one-row table below record what a *plain Homebrew install*
still measures, which is unchanged and is the thing item 6 unblocks.

```
$ <venv>/bin/python -m buttereye.cli bench --width 1280 --height 720
configuration                      vspipe   in mpv  real time  faults
mvtools                             135.2    203.1  yes        ?
recommended: mvtools
```

(2026-10-13.) One row. The benchmark SCOPE's fifth product principle and F14
rest on has never seen a second engine on a Mac, so the engine choice on a Mac
has no measured basis at all — it is MVTools by elimination.

Why, measured the same day:

```
$ <venv>/bin/python -c "from buttereye.core import types; print(types.macos_plugin_dirs())"
('/opt/homebrew/lib/python3.14/site-packages/vapoursynth/plugins',
 '/opt/homebrew/lib/buttereye/vapoursynth')
$ ls /opt/homebrew/lib/python3.14/site-packages/vapoursynth/plugins
libffms2.dylib   mvtools.dylib
$ ls /opt/homebrew/lib/buttereye
ls: /opt/homebrew/lib/buttereye: No such file or directory
$ ls ~/.cache/buttereye/macos/vapoursynth
libmvgpu.dylib (124 416 bytes)   librife.dylib (162 528 bytes)
```

Both engines are built and both have been shown to render correctly — mvgpu's
in-between frame is bit-exact against the geometric truth, RIFE's scores 0.0045
mean absolute error where a blend scores 0.0331 (`mvgpu/README.md`,
`packaging/macos/RIFE-SPIKE.md`) — but they sit in a build cache that nothing in
`buttereye/` reads, and there is no environment variable that would point it
there: `BUTTEREYE_HOME` is Windows-only (`paths.install_dir`), and the macOS
plugin directories are derived from the Homebrew prefix. So the benchmark can
only measure what a `brew install` would have put in
`<prefix>/lib/buttereye/vapoursynth`, which is item 6.

**What it would take**, concretely, and in the owner's hands rather than this
phase's, because both routes write into `/opt/homebrew`:

1. `brew install --HEAD <owner>/buttereye/buttereye-vs-mvgpu` (that formula has
   a `head` line; the RIFE one does not, so RIFE needs item 6's tagged release
   first), or the blunt equivalent of copying the two built dylibs into
   `<prefix>/lib/buttereye/vapoursynth` and the models into
   `<prefix>/share/buttereye/rife-ncnn-models`;
2. `buttereye bench --width 1280 --height 720` and again at 1920×1080, on an
   **idle** machine;
3. check that `backends/select.py` then promotes the model from a measurement
   rather than from a device name. The default on any Apple GPU today is
   `select.MODEL_MID`, `rife-v4.22_lite_ensembleFalse` — `select._high_end_gpu`
   requires
   `device_type == "discrete"`, and MoltenVK reports the M2 as
   `PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU` (verified 2026-10-13 with
   `vulkaninfo --summary`: `deviceName = Apple M2`, `driverName = MoltenVK`,
   `driverInfo = 1.4.2`). Phase 2 deliberately left the gate alone and made the
   benchmark the way out; nobody has yet walked through that way out on a Mac.

One caveat about any number produced today: this machine's load average was
**14.40 on 8 threads** while the run above was made, and the same command
answered 92.3 / 160.6 earlier in the same session, while an earlier run of the
same command on this machine — reported when phase 5 was set up, and written
down nowhere else in this tree — gave 314.8 / 258.8. A benchmark figure from a
loaded Mac is not a release number, and a figure nobody can point at a file for
is not evidence. The structural result — one row, MVTools by elimination — is
unaffected by either.

**What the spike then measured** (`docs/spikes/m0s.md`, `m0s-table.md`,
`m0s-results.json`), by pointing `HOMEBREW_PREFIX` at a shadow prefix of symlinks
so that nothing was installed and `/opt/homebrew` was not written to: all five
configurations, at four sizes. In mpv, against the 55.14 fps a real-time 2× of
23.976 needs:

| size | best RIFE | mvgpu | MVTools | `bench` recommends |
|---|---|---|---|---|
| 1280×720, 2 runs | 19.97–20.07 | 201.27, 205.03 | 199.21, 218.34 | mvgpu |
| 1920×1080, 3 runs | 8.79–9.30 | 89.21–97.01 | 91.07–102.56 | mvgpu |
| 2560×1440, 11 runs | 5.03–5.41 | 52.77–58.31 — **either side** | 56.33–59.69 — **holds** | mvtools 8, mvgpu 3 |
| 3840×2160, 2 runs | **not measured** (78 s timeout) | 32.20, 32.14 | 28.58, 28.88 | none |

Every figure above is a row of `m0s-results.json`, which now carries nineteen
runs: the spike re-measured every size after this item was first written, eleven
times at 1440p because that verdict does not settle, and once more at 4K under a
deliberately heavy load to check that the 4K verdict does. Two of the eleven
1440p rows are the phase-5 review's own reproductions rather than the spike's,
and the file marks them so with every figure it was not told left `null`. A
twelfth 1440p run is in the file as `pass x` and excluded from the range above:
the spike discarded it because a second benchmark of its own was running beside
it. An earlier draft of this item quoted a third 1440p run and some ranges that
were written down in no file at the time and struck them out for it; the spike
has since recorded the run as `review-1`, so the figures are evidence again, and
what the rule caught was the missing file rather than the measurement. The 4K
RIFE rows are *not measured* rather than zero: `vspipe` hit the benchmark's own
`30 s + frames/2` timeout (78 s for 96 frames, a floor near 1.23 fps) and all
three models stored 0.0.

**The 1440p verdict does not reproduce, and it is the claim this page leaned
hardest on.** A reviewer re-ran the spike's own command on a quieter machine and
measured mvgpu at **51.99 through `vspipe` and 56.92 in mpv — real time, clearing
the 55.14 bar** — with `bench` recommending `mvgpu` and not `mvtools`. The spike
then ran the same command another seven times, recording the 1-minute load
average beside each, and the answer got less settled rather than more: across
**eleven valid runs** mvgpu in mpv has read 52.77, 52.92, 53.39, 53.57, 53.9,
54.25, 54.57, 54.99, 55.39, 56.92 and 58.31 — **96 % to 106 % of the bar, real
time in three of the eleven** — and `bench` recommended `mvtools` in eight of
them and `mvgpu` in three. MVTools came out at 56.33–59.69, real time in all ten
runs that reported it but **never by more than 8 %**, and 32.39 in the one run
that was overloaded. The sentence this document and `docs/PLATFORMS.md` both
carried — "at 1440p MVTools is the only engine that reaches the v1 target" — is
therefore **withdrawn from both**: it is a measurement of one afternoon's machine
load, not of two engines. `docs/spikes/m0s.md` and `m0s-table.md` have since
landed the same correction and own the per-run table; the three files are meant
to be read as one statement, and if they ever differ the spike holds the rows.

What replaces it is narrower and more useful. At 1440p both motion-vector
engines sit *on* the threshold: MVTools a little above it in every run that was
not overloaded, mvgpu on either side of it. The 1440p engine ordering on an M2 is
**not settled**, no release note may state one, and the next person to measure it
should do so on an idle machine and record the load average beside the number.
Load is the largest single influence but not the whole of it: the spike's two
quietest runs, at mean load 3.42 and 3.19, came out at 58.31 and 54.25 — a 1.07×
swing straight across the bar with the load average if anything lower. `bench`'s
real-time verdict is a threshold and not a score, so extra load does not make it
pessimistic — it changes the answer, which is the same effect the spike already
recorded in the run it discarded (MVTools 32.39 instead of 57.16, every RIFE
model timed out, and the verdict flipped to "nothing keeps up").

That had a second consequence, for the chooser half of this item. When mvgpu
clears the bar at 1440p, `bench` recommends mvgpu *there* too — and
`select._from_bench` used to throw an mvgpu recommendation away whole. So the
defect described next was not a 720p-and-1080p curiosity: on a quiet machine it
reached SCOPE §1's v1 size as well. It is fixed, and the fix is tested against
the 1440p runs that recommended mvgpu as well as the 720p and 1080p ones.

The one result here that is **not** load-dependent: **RIFE-ncnn is offline-only
at every size**. It misses the bar by a multiple — 2.7× short at 720p, 6.3× at
1080p, 11× at 1440p — where mvgpu misses or clears it by a few per cent, so no
amount of idling changes that verdict. Item 18 already records it.

**The chooser half: what was wrong, and what landed (2026-10-13).**
`select._from_bench` looked only at `res.recommended`, and mvgpu is deliberately
absent from `ranked` — live play picks it per file and Setup never offers it
(`select`'s module docstring, step 3) — so a run that recommended mvgpu, the
normal case at 720p and 1080p on a Mac, was discarded whole. On this Mac today
that was only a wrong reason: RIFE is not in the install database, `ranked` is
`(mvtools,)`, and the fallback happened to land on MVTools. Once RIFE *is*
available `ranked` becomes `(rife-ncnn, mvtools)` and the fallback is
`ranked[0]`. Measured against `tests/core/test_macos_doctor_pure._ready_mac` —
the fixture for a Mac with both plugins installed and all three models packaged,
which is the state item 6 is meant to produce — fed the real rows of
`m0s-results.json`, before the fix:

```
no benchmark     -> rife-ncnn  rife-v4.22_lite_ensembleFalse  from_benchmark False
1280x720 a, b    -> rife-ncnn  rife-v4.22_lite_ensembleFalse  from_benchmark False
1920x1080 a,b,c  -> rife-ncnn  rife-v4.22_lite_ensembleFalse  from_benchmark False
2560x1440 f, h   -> rife-ncnn  rife-v4.22_lite_ensembleFalse  from_benchmark False
reason: "RIFE (Vulkan) on Apple M2; model chosen without a benchmark."
```

— in runs whose own RIFE rows were 20.07 fps in mpv at 720p and 8.33 at 1080p
with `realtime` False, against a 55.14 fps bar. The benchmark changed nothing it
was built to change.

The fix keeps mvgpu out of `ranked` and stops discarding the run. When the
recommendation is rankable it wins exactly as before. When it is not, the same
run is asked again restricted to the rankable engines, with
`bench.runner.recommend`'s own rule — quality order, a repeatable measurement
first, real time in mpv, no GPU fault — and the measurement it finds stands in.
So an engine a run measured below real time is never chosen while another
rankable engine in the same run held it. The same rows, after:

```
1280x720 a, b    -> mvtools  from_benchmark True
1920x1080 a,b,c  -> mvtools  from_benchmark True
2560x1440 f, h   -> mvtools  from_benchmark True
reason: "mvtools keeps up in real time in your benchmark. Its first choice,
         GPU motion vectors, is never a default: playback picks it per video."
2560x1440 a-e, g, i (recommended mvtools) -> mvtools, as before, reason unchanged
3840x2160 a, b (recommended None)         -> as with no benchmark (see below)
no benchmark at all                       -> mvtools since item 18's fix
```

Held by
`tests/core/test_macos_doctor_pure.py::test_a_benchmark_that_recommends_mvgpu_keeps_rife_off_live_playback`,
parametrised over every complete spike run that recommended mvgpu (seven: 720p
`a`, `b`; 1080p `a`, `b`, `c`; 1440p `f`, `h`), which reads `m0s-results.json`
itself rather than a transcription, asserts the shape the bug needs off the file
(mvgpu recommended, no RIFE row real time, MVTools real time) and that the
fixture with no benchmark does reach for RIFE, and then asserts `mvtools` with
`from_benchmark` True. Against the old `select.py` all seven fail with
`rife-ncnn is not mvtools`. The two `review-*` rows are left out because they
report one or two engines only, and pass `x` because the spike discarded it.

Off macOS the change is confined to the same shape: a run whose recommendation
cannot be ranked. On Linux that is a run recommending mvgpu where mvgpu is
installed, and there the stand-in is whatever the run measured best among
TensorRT, RIFE-ncnn and MVTools — RIFE with its measured model on a GPU where
RIFE held real time, which is the engine the old fallback (`ranked[0]`) picked
there too unless TensorRT was ranked first, now with the measured model and
`from_benchmark` True (`tests/core/test_select.py`, two new tests, which also
hold the repeatable-first rule). A run whose recommendation is rankable,
and a run with no recommendation, choose exactly what they chose before; the
existing selection tests pass unchanged.

**What this did not touch, and a claim this paragraph used to make that was
wrong.** `select.select` is not the per-file engine choice live play makes. It
feeds the Setup wizard's proposal (and so the engine its smoke test runs unless
the user picks another) and the Benchmark page's "Top candidates" line. An
earlier version of this paragraph said, from reading the code, that live play
on Automatic would not trust an unmeasured RIFE anyway. That was true of a
profile set to Automatic and **false of the profiles a fresh install actually
plays with**: the shipped Quality, Balanced and Fast profiles are pinned to
RIFE-ncnn, the shipped rules give a 24p 1080p film Quality, and a pinned engine
with no benchmark ran uncapped. It was then measured from the self-contained
app with no benchmark on file (item 18): RIFE v4.26 live at 1080p, 6–8 % of real
time. That is fixed too — item 18 has the change and the measurement.

**Still not verified, and why this item is not closed:**

- *Never from an install.* The shadow prefix reproduces the file layout a
  `brew install` would create, not the Cellar receipt the install database reads.
  Item 6 is untouched.
- *The chooser half is fixed; what it does not reach is stated here.*
  `setup.plan` calls `select.select` with **no** benchmark history at all
  (`setup.py`, `select(report, cfg, ())`). On a Mac that no longer means RIFE:
  with no benchmark showing RIFE in real time, the fallback proposes MVTools
  ("MVTools on the CPU: RIFE is too slow for live playback on this Mac unless a
  benchmark here shows otherwise. RIFE is still used to convert videos.") and
  Setup smoke-tests MVTools (item 18). A history in which **nothing rankable**
  held real time — both 4K runs, where `recommended` is None — is no evidence
  either way, so the chooser answers exactly as with no benchmark, which on a Mac
  is now that MVTools proposal (held by
  `test_a_run_where_nothing_rankable_keeps_up_is_no_evidence_either_way`). Such
  a run does show every rankable engine below the bar — the 4K RIFE rows are
  timeouts, under about 1.23 fps, and MVTools measured 28.58 and 28.88 — but it
  measures nothing about the smaller sizes a machine-wide default is mostly used
  for, and the code does not extrapolate one. Whether a Setup run after a
  benchmark should be handed the history is a question for the owner, not a
  defect this item found.
- *RIFE never enters the ranking at all*, because the macOS install database has
  no entry for ButterEye's own plugins, so `packaged_models()` is empty and
  `_default_model()` is None even with `librife.dylib` loading inside mpv. That
  is a doctor-contract question, not a benchmark one.
- *The model gate was not overruled, and did not need to be.* Measured on this
  M2: `device_type` `integrated`, `heap_bytes` 17179869184 — **exactly**
  `HIGH_END_HEAP`, and byte-for-byte `sysctl hw.memsize`, i.e. the whole machine's
  RAM rather than graphics memory. So the heap half of `_high_end_gpu` passes and
  only the `discrete` half refuses. Phase 2's decision to gate the high-quality
  model on a benchmark instead of on a heap size is vindicated by that
  coincidence: a heap threshold alone would have handed this M2
  `rife-v4.26_ensembleFalse`, which `m0s-results.json` measures at **8.33 fps in
  mpv at 1080p** — the 8.79 in the table above is `rife-v4.22_lite`, a different
  and faster model, and quoting it here understated the gate's value.

**A real 50p film, after 0.0.3 (`docs/PLATFORMS.md`, "Live smoothing on the M2:
where the time goes").** The benchmark's costing held on a 1080p50 AV1 film: in
its own 2×-rate units the film ran mvgpu at 112 and MVTools at 103 (1080p,
50 → 60, untimed) against benchmark rows of 106 and 115. What was wrong was the
target: "Double" sent 50 → 100 to a 60 Hz panel, mpv dropped 40 frames a second
by design, and live correction stored 0.54 for MVTools at 1080p and 720p from
those drops. Targets now stop at the display rate on every platform, older
live-limit records are ignored, and Automatic lets a GPU engine at a smaller size
win with the best rate the display allows. Measured live on this M2 afterwards
(the table there): mvgpu 1080p 50 → 60 holds real time (0 drops, 1.2 cores),
MVTools does at 720p and not at 1080p. mvgpu at 1080p is still not *chosen* on
Automatic for this film, because the live cap's 1.25 headroom asks for a 125 fps
benchmark row and the rows on file are 89–121; left for the owner.

### 5. The GUI as a user sees it — PARTLY VERIFIED

This item moved while this document was being written, and the move is real:
`tools/screenshots.py` is now three-platform, asks a Mac for the **real `cocoa`
Qt platform** before falling back to `offscreen` and prints which it got, and
`docs/design/screenshots/macos/` now holds the ButterEye window as it draws on
this machine — the simple window empty, on its silent first run, with a real
live MVTools session ("24 → 48 fps · MVTools on the CPU, full size 1280 × 720")
and its dark variant, all four tabs of the Details dialog, the setup wizard, and
all seven pages of the classic window with Sessions and System also in dark.
That is **18 files**, counted in the directory on 2026-10-13, which agrees with
the GUI phase's own record (`docs/PLATFORMS.md`, phase 5); an earlier draft of
this paragraph said fourteen were present and left the disagreement open, and
there is none. That record also answers the other question this paragraph used to
leave open — `screenshots._qt_platform` asks a child process to start
`QApplication` under `QT_QPA_PLATFORM=cocoa` and falls back only if the child
dies, and here it answered `cocoa`.

So the window is no longer unseen. What remains unverified is everything a
picture cannot answer: nobody has **driven** it on a Mac. What is measured
here directly:

```
$ QT_QPA_PLATFORM=cocoa <venv>/bin/python -c "..."
platform: cocoa
screen: 1920 x 1200 dpr 1.0 refresh 60.0
style: macos
PySide6 6.12.0 Qt 6.12.0; platform plugins: libqcocoa.dylib, libqminimal.dylib,
libqoffscreen.dylib
```

(2026-10-13.) So Qt's real Mac platform plugin loads in a logged-in session and
Qt picks its `macos` style — which is the thing that makes the untested surface
large rather than small, because every widget is drawn by a style no ButterEye
screenshot has ever used.

```
$ QT_QPA_PLATFORM=offscreen <venv>/bin/python -m pytest tests/gui -q \
      --deselect …test_alt_menu_letters_open_the_menu \
      --deselect …test_cancel_stops_the_run_and_keeps_old_results
1 failed, 557 passed, 2 skipped, 8 deselected in 216.30s
FAILED tests/gui/test_theme_scale.py::test_double_font_nothing_below_minimum_size
```

(2026-10-13, under the load described above.) That file passes 16 of 16 when it
runs alone, and a second full run of `tests/gui` an hour later was **558 passed,
2 skipped** with nothing failing, so this is an order- or load-dependent failure
rather than a flat break — one in two full runs, which is too often to dismiss.
It is **not** one of the intermittents `.github/workflows/macos.yml` names, and
it has no owner: whoever owns `tests/gui` should characterise it, including
whether a Linux machine does the same thing. A bar cannot be called green while
a GUI test fails for an unexplained reason.

**What it would take for the rest**, and it is half an hour with a person at the
machine rather than a build: open the window and use it. The menus live in the
system menu bar and answer to Cmd rather than Alt, which is why the seven
Alt+letter mnemonic cases are deselected rather than fixed and is the one known
behavioural difference; keyboard operation and visible focus (SCOPE §2's
accessibility goal) have been checked with Orca on GNOME and never with
VoiceOver; dark and light should follow the system setting rather than be forced
by a screenshot run; the system font scale is untried; and this display reports
a device pixel ratio of **1.0**, so nothing in this port has ever drawn at 2× on
a Retina panel. None of that is captured by an image.

### 6. The tap actually installed — NOT VERIFIED

No formula in `packaging/macos/homebrew/` has been installed, bottled,
`brew audit`ed or `brew style`d, here or anywhere. `ruby -c` passes on all four
and every formula name they depend on exists; that is the whole of it.

Why it cannot simply be run today, verified 2026-10-13: the newest tag in this
repository is `v0.0.2` (`git tag`), every formula's `url` names a `v0.0.3`
release that does not exist, and each formula's own `sha256` is 64 zeros — a
placeholder that fails a download rather than installing something unverified —
their `resource` blocks' checksums are real, so it is the four main archives that
are stubbed and nothing else. `v0.0.2` predates
the whole macOS port, so a formula built from that tag would install a ButterEye
with no macOS platform layer and an mvgpu that cannot create a Vulkan instance
on any Mac. `--HEAD` exists for `buttereye.rb` and `buttereye-vs-mvgpu.rb` only.

**What it would take:** tag a release, fill in the four checksums, then, on a
machine that is not this one, `brew tap`, `brew install buttereye`, the two
engine formulae and the models, `brew audit --strict` and `brew style`, and
`buttereye doctor`. Three things to watch, each a measured hazard rather than a
guess: that `licences.py` finds its texts where `buttereye.rb` symlinks them
into `site-packages`; that the Licences pane shows the macOS conveyance table
(`licences._macos_rows`) and not Fedora's; and that
`<prefix>/lib/buttereye/vapoursynth` existing does not hide the autoloaded
MVTools. The two named licence debts (`packaging/macos/SOURCES.md`) have to close
before anything is published: the RIFE formula's pinned licence texts as
`resource`s, and the repacked RIFE tarball its `url` names, which cannot even be
produced on a Mac because `repack-rife-ncnn.sh` needs GNU `tar --sort`.

**The third hazard was reported closed here before it was, and that is worth
writing down rather than quietly fixing.** This item used to say phase 4 had
measured the "ButterEye's own directory hides the autoloaded MVTools" regression
and fixed it with `types.macos_plugin_dirs()`, so that only an install was left
to confirm. The search path was real but it reached the **doctor only**. A
reviewer built a scratch prefix shaped like a tap install — `libmvgpu.dylib`,
`librife.dylib` and a model directory in `<prefix>/lib/buttereye/vapoursynth`,
`mvtools.dylib` where Homebrew autoloads it — pointed `HOMEBREW_PREFIX` at it,
and measured the doctor and the engine chooser disagreeing:

```
doctor installed_plugin(mvtools.dylib) -> <prefix>/lib/python3.14/site-packages/
                                            vapoursynth/plugins/mvtools.dylib
decide.installed_backends(paths.rpm_plugin_dir, …) -> [mvgpu, rife-ncnn]
MVTOOLS in installed: False
```

(2026-10-13.) `decide.installed_backends`, `session.can_switch_to_cpu` and the
benchmark's candidate discovery all took one directory, `paths.rpm_plugin_dir`,
which on a Mac with the tap is ButterEye's own — so installing the tap would have
made MVTools vanish from the engine chooser while the doctor went on reporting
it correctly. MVTools has cleared the real-time bar at every size where anything
does — 720p, 1080p and 1440p; nothing clears 4K — and it is the engine the
chooser currently picks, so the hazard was not cosmetic: the step this item
recommends would have broken the thing item 4 depends on.

**What landed, measured on the same scratch prefix on 2026-10-13.** The search
path moved out of the doctor and into `paths`, where the chooser can reach it:
`paths.plugin_dirs(paths)` is the load-ordered list — `(paths.rpm_plugin_dir,)`
off macOS, so Linux and Windows resolve exactly the directory they resolved
before — `paths.plugin_in(where, name)` takes one directory *or* a list, which is
what lets the tests written against the old signature keep passing a bare `Path`,
`paths.installed_plugin` and `paths.plugin_dir_for` are built on it, and
`checks_pkgs.plugin_dirs` now delegates there rather than owning the answer.
`decide.installed_backends` takes `Path | Sequence[Path]`; `session.py` passes
`_paths.plugin_dirs(paths)` at both call sites, `can_switch_to_cpu` included; and
`bench.runner`'s `Tools` carries `plugin_dirs` with `candidate_plugin_dir`
resolving a single directory per candidate for the generated `.vpy`. Re-run on
the same prefix:

```
paths.plugin_dirs          -> (<prefix>/lib/python3.14/site-packages/vapoursynth/
                               plugins, <prefix>/lib/buttereye/vapoursynth)
decide.installed_backends  -> [mvgpu, mvtools, rife-ncnn]
MVTOOLS in installed: True
plugin_dir_for(mvtools.dylib)  -> …/site-packages/vapoursynth/plugins
plugin_dir_for(libmvgpu.dylib) -> <prefix>/lib/buttereye/vapoursynth
```

So the hazard is closed for the doctor, the engine chooser, live play and the
benchmark — and, since the paragraph below, offline render — and the last line matters as much as the third: the doctor and the
generated script now resolve each plugin to the same copy, which is how
"installed but not loading" used to happen.

**One call site was missed, and it was a second hazard of the same shape.**
`render/jobs.py` asked one directory in two places — `_installed` passed
`p.rpm_plugin_dir` to `decide.installed_backends`, and `_constants` built
`ScriptConstants(ctx.paths.rpm_plugin_dir, …)`. Measured on the same scratch
prefix, 2026-10-13, before the fix:

```
render jobs._installed      -> [mvgpu, rife-ncnn]      (MVTOOLS: False)
render _constants PLUGIN_DIR -> <prefix>/lib/buttereye/vapoursynth
mvtools really lives in      -> …/site-packages/vapoursynth/plugins
```

So **offline render — item 2 — lost MVTools the moment the tap was installed**,
exactly as live play did. Live play happened to be the louder half and got fixed
first; `buttereye render` is the half SCOPE §1 calls the one that always works.

**Fixed, with the API above and nothing new** (2026-10-13): `_installed` passes
`paths.plugin_dirs(p)`, and `_constants` takes the job's engine and names
`paths.plugin_dir_for(ctx.paths, decide.BACKEND_PLUGIN.get(engine))` — the
directory that really holds that engine's plugin, which is the directory the
doctor reports it in, as live play's script already did. TensorRT, which has no
entry in `BACKEND_PLUGIN` and loads vstrt from its own install, still gets the
packaged directory. Off macOS `plugin_dirs` is `(paths.rpm_plugin_dir,)`, so
Fedora and Windows resolve the one directory they always did. Re-run through the
render provider's own functions on the same prefix shape:

```
render jobs._installed               -> [mvgpu, mvtools, rife-ncnn]
render _engine(profile "cpu")        -> mvtools
render _constants(mvtools) PLUGIN_DIR -> <prefix>/lib/python3.14/site-packages/
                                         vapoursynth/plugins
render _constants(mvgpu)   PLUGIN_DIR -> <prefix>/lib/buttereye/vapoursynth
```

Held by
`tests/core/test_macos_doctor_pure.py::test_offline_render_asks_every_plugin_directory_a_mac_has`,
which asserts first that the single directory render used to ask has no MVTools
in it, so it is not satisfied by the plant alone, and fails against the old
`jobs.py` (`_installed` missing MVTools); its sibling
`…::test_offline_render_keeps_its_one_directory_off_macos` holds the Linux
answer to exactly `rpm_plugin_dir` for every engine. No real render was run on a
tap-shaped prefix for this: the end-to-end render of item 2 used Homebrew's
layout without ButterEye's own directory, and a render through the fixed code on
an installed tap is part of item 6's run.

**The test that holds it** is
`tests/core/test_macos_doctor_pure.py::test_the_engine_chooser_asks_every_plugin_directory_a_mac_has`,
and it is worth saying why it is not a tautology: it plants the two directories
and then asserts the *old* answer first —
`decide.installed_backends(paths.rpm_plugin_dir, model_dir)` is
`{mvgpu, rife-ncnn}` — before asserting the new one, so a revert that puts the
single directory back fails on the second assertion rather than passing both.
It then checks the three call sites separately rather than re-running the search
once: `_Session.can_switch_to_cpu` (live play's MVTools fallback),
`runner.find_tools` → `discover_candidates` (the benchmark's candidate list) and
`plugin_dir_for` against `installed_plugin` for both plugins, with
`plugin_dir_for(mvtools.dylib) != paths.rpm_plugin_dir` as the assertion that the
script and the doctor cannot name different copies. The sibling test
`…::test_the_plugin_search_asks_homebrews_autoload_directory_first` covers the
doctor half and the load order.

What an install still has to confirm is that a real `brew install` produces that
layout — the scratch prefix reproduces the file layout, not the Cellar receipt —
so this item stays NOT VERIFIED, but it is no longer carrying a hazard that
argued against running it.

Nothing in this phase ran `brew install`: it writes into the owner's Homebrew
prefix, and that is the owner's to run.

### 7. The tests run from the installed copy, in CI — NOT VERIFIED

This is the line `.github/workflows/windows.yml` clears and
`.github/workflows/macos.yml` does not. The Windows job installs the artefact on
a clean runner and then runs `tests/windows` **with the installed ButterEye's own
Python**. The macOS job installs no Homebrew package at all, so on that runner
there is no mpv, no VapourSynth, no Vulkan loader, no MoltenVK and no plugin:
nothing in `tests/integration` executes, `tests/macos/test_doctor_macos.py` and
`tests/macos/test_plugins_macos.py` skip themselves, and a green tick covers the
pure-Python and Qt layers only. The workflow's own header says this at length,
and proposes a second, opt-in workflow as the shape that would fix it — a
proposal, not a decision.

**What it would take:** a workflow that `brew install`s the bottles (and, once
item 6 is real, the tap), then runs the gate with `BUTTEREYE_TEST_BREW=1` and
`tests/integration` on a GitHub Apple Silicon runner. Those runners have a real
Metal GPU, which is the one way they beat the GPU-less Windows ones, so unlike
Windows this is a bar line CI could genuinely close.

### 8. On-screen playback and its timing — NOT VERIFIED

`buttereye play` has been run on this Mac and reports
"24 → 60.002 fps with mvtools (active, ok)" with the target taken from the 60 Hz
display (`docs/PLATFORMS.md`, phase 4), and phase 1 saw mpv draw with
`gpu-next` and `videotoolbox-copy` hardware decoding. What nobody has watched is
a real film on the screen for long enough to judge it: dropped and repeated
frames, `video-sync=display-resample` against a 60 Hz panel, seek behaviour,
what happens on a display change or a wake from sleep, and whether the picture
is actually smooth to a human eye. Windows is in exactly the same position, and
the Windows bar counts it as blocking.

### 9. A clean machine — NOT VERIFIED

Every macOS measurement in this repository was taken from a developer's
repository checkout with a hand-made virtualenv, on a Mac that has the Command
Line Tools, meson, ninja, glslang and a Cellar that earlier phases filled in
order to build things — 172 kegs on it today, counted 2026-10-13, where the ten
formulae `checks_pkgs.BREW_FORMULAE` asks about are what a user would need.
Nothing has ever run on a Mac that merely installed ButterEye. Item 6 is the
prerequisite; this is the test that closes it.

### 10. A cross-UID socket refusal — CANNOT BE VERIFIED HERE

SCOPE §4.3's threat is another user's process answering on ButterEye's socket.
The macOS peer check is real code and is covered by tests
(`_darwin.peer_cred`: the uid from `LOCAL_PEERCRED`, trusted only when
`cr_version` is 0, the pid from `LOCAL_PEERPID`, failing closed on a short
answer, an unexpected version or any errno but `ENOTCONN`) — but **every peer
test so far had both ends at the same uid**, which is the case that cannot fail
for the interesting reason.

Why not here: this machine has exactly one non-system login account
(`dscl . -list /Users`, 2026-10-13). **What it would take:** a second account
(`sysadminctl -addUser`, an administrator action and a change to the owner's
machine), then a socket served by that user's process and a ButterEye at this
uid connecting to it — the refusal must come from the uid comparison, with the
finding and the error code recorded. It is a half-hour test on a machine that
has two accounts, and it has never been done on any platform in this port.

### 11. `dirhelper` sweeping the runtime directory — NOT VERIFIED

The rule is verified, today:

```
$ plutil -p /System/Library/LaunchDaemons/com.apple.bsd.dirhelper.plist
  "EnvironmentVariables" => { "CLEAN_FILES_OLDER_THAN_DAYS" => "3" }
  "ProgramArguments" => [ 0 => "/usr/libexec/dirhelper" ]
  "StartCalendarInterval" => { "Hour" => 3, "Minute" => 35 }
```

So a `launchd` daemon walks the per-user temporary directory daily at 03:35 and
deletes what is older than three days, and phase 1 put ButterEye's runtime
directory — the mpv socket, the generated `.vpy` — inside it
(`_darwin.user_temp_dir`, `paths._resolve_macos`). A session attached to one mpv
for longer than three days could therefore have its socket unlinked underneath
it. **Nobody has observed that**, and nothing in the code prevents it; it is
written down in `paths._resolve_macos` and in the storage overview the user sees
("cleared once three days old").

**What it would take:** the natural reproduction is a four-day session, which is
why it has not been done. The consequence is reproducible in minutes without
waiting: unlink the socket under a live attached session and see what ButterEye
says. That is the test to write, and the question it answers is the one that
matters — whether the user gets a diagnosable error or a silent dead session.

### 12. `flock` on an SMB/AFP network home — CANNOT BE VERIFIED HERE

`osapi.lock` is `fcntl.flock(fd, LOCK_EX)` with no `except`, and
`osapi.locked_dir` takes it on a **directory** descriptor opened with
`O_DIRECTORY` — which is the shape most likely to be unsupported on a network
filesystem. `profiles.config` saves every profile under that lock, so an
`OSError` there surfaces as a failed save rather than as a finding. Verified by
reading the code, 2026-10-13; `mount` on this machine lists no `smbfs` or
`afpfs` filesystem and `/` is local sealed APFS, so there is nothing here to
measure against.

**What it would take:** a folder shared over SMB from another machine (or macOS
File Sharing enabled and `smb://localhost` mounted — a change to the owner's
machine), `HOME` pointed at it, and one `save_config`. If it raises, the fix is
a guarded fallback, and the decision of what ButterEye should do on a filesystem
that cannot lock is the owner's.

### 13. `$TMPDIR` inside a sandboxed bundle — CANNOT BE VERIFIED HERE

The numbers that are measured, 2026-10-13:

```
per-user temp dir:  48 bytes
session socket:     80 bytes   (<temp dir>/buttereye/mpv-<12 hex>.sock)
macOS bind limit:  103 bytes
```

23 bytes spare in the shape ButterEye actually uses. The "96 bytes, +7
headroom" figure for a sandboxed bundle comes from the port plan and is
reproduced nowhere in this tree — `grep` finds no such measurement — so it must
be read as a calculation, not a measurement.

It is also close to moot, and that is worth saying rather than leaving as a
to-do: phase 4 decided there will be **no `.app` bundle, ever**, for a reason
about money (notarisation needs a paid Developer ID) rather than engineering, so
ButterEye is not the thing that would be sandboxed. The case survives only if a
user starts ButterEye from inside somebody else's sandboxed container, where
`confstr(_CS_DARWIN_USER_TEMP_DIR)` answers a container path. **What it would
take:** a minimal ad-hoc-signed `.app` with the App Sandbox entitlement that
prints `confstr(_CS_DARWIN_USER_TEMP_DIR)` and `$TMPDIR`, launched once. An
hour's work, and worth doing only if the no-bundle decision is ever revisited.

### 14. Intel Macs — CANNOT BE VERIFIED HERE

Nothing is measured. `uname -m` here is `arm64` and no Intel Mac is available,
so this is hardware, not effort. Two concrete reasons it is not a formality:

- Homebrew's prefix on Intel is `/usr/local`, which sits inside the protected
  `/usr` root `buttereye/core/storage.py` refuses to clean, where `/opt/homebrew`
  does not. The code already carries that comment (`storage.py`, the `rpm_roots`
  tuple) and nobody has watched what the storage page does about it.
- `buttereye.rb` depends on `:macos` with `arch: :arm64`, stated rather than
  discovered, so an Intel Mac cannot install the tap today even by accident.
  That is the right default while nothing is measured.

### 15. The model-path patch on Fedora — VERIFIED

This is the one item where the macOS port changed a **supported** platform.
`packaging/plugins/buttereye-vs-rife-ncnn-model-path.patch` rewrites RIFE's two
`char[256]` `sprintf` stack buffers as `std::string`, and it is applied
unconditionally in both ncnn variants of the spec, so it is in the build Fedora
ships. The defect it fixes is real and reachable on Fedora — measured at exactly
242 characters of `model_path`, where the process `SIGABRT`s and takes `vspipe`
or mpv with it (`packaging/macos/RIFE-SPIKE.md`, "The model-path abort").

Measured 2026-10-13, without a Linux machine: `packaging/build-in-container.sh`
runs `packaging/build.sh` unchanged in a pinned `fedora:44` amd64 container
(qemu-user in the Mac's podman machine, mock with `isolation=simple`), and all
five packages built for both chroots, the bundled-ncnn
`buttereye-vs-rife-ncnn-9.33-2` among them (36 minutes per chroot, %check
passing). The repro then ran in a fresh `fedora:44` and a fresh `fedora:45`
amd64 container: the RIFE RPM installed, `mesa-vulkan-drivers` for a Vulkan
device (llvmpipe; the plugin wants a device before it loads a model), the
`rife-v4.26_ensembleFalse` model copied to a `model_path` of exactly N
characters, and `core.rife.RIFE(..., model_path=...)` asked for one
interpolated frame of a 64×64 grey clip. *Before* is v0.0.2's published
`buttereye-vs-rife-ncnn-9.33-1` (checked against that release's `SHA256SUMS`);
*after* is the new build:

```
                 len=200   241    242    245    260    300
fc44 before 9.33-1  ok      ok    134    134    134    134
fc44 after  9.33-2  ok      ok     ok     ok     ok     ok
fc45 before 9.33-1  ok      ok    134    134    134    134
fc45 after  9.33-2  ok      ok     ok     ok     ok     ok
```

Every `ok` is exit 0 with the same frame (min/max 0.5039/0.5039, before and
after alike); every 134 is `SIGABRT`. The threshold is the same 242 the Mac
measured, but on Fedora the abort is cleaner than the Mac's: the RPM is built
with `_FORTIFY_SOURCE`, so `sprintf` becomes `__sprintf_chk` and glibc stops
the process at the overflowing write — stderr reads `*** buffer overflow
detected ***: terminated` — before `parampath` can run over into `modelpath`.
Either way the whole process dies, so the patch is the fix on Fedora too, and
no regression showed: the new build loads and runs at every length, the short
ones included. Not covered: a real GPU (llvmpipe only), and the
`--with system_ncnn` variant, which Fedora does not ship.

### 16. Unicode and long paths — NOT VERIFIED

The Windows port treats this as a bar line and cleared it (installed into
`Butter Eye ñ`, with RIFE loading its model from there). On macOS nothing has
been tried: no non-ASCII media filename, no non-ASCII profile name, and nothing
about APFS's path normalisation, which decomposes accented characters where
Linux does not — a round-trip of a composed name through a profile file or a
generated `.vpy` is exactly the kind of thing that works until a user types one.
**What it would take:** a render and a live session from a folder named like
Windows' `Butter Eye ñ`, plus a model directory long enough to come near the
242-character limit of item 15.

### 17. Gatekeeper and code signing — VERIFIED, in the form that applies

Windows still owes code signing before release; macOS owes nothing, and that is
measured rather than assumed. `brew` fetches over curl and marks nothing with
`com.apple.quarantine` — re-verified 2026-10-13, `xattr -l` on the newest cached
bottle tarball in `~/Library/Caches/Homebrew/downloads/` prints nothing at all —
so Gatekeeper is never asked and nothing in the tap needs to be signed or
notarised. The contrast is also measured: an ad-hoc-signed binary with the
quarantine attribute set is `rejected` by `spctl -a -t exec` (exit 3), which is
why there is no `.app`.

The honest limit of this verdict: it is measured on bottles this Mac happened to
download, not on a ButterEye tap nobody has installed. Item 6 confirms it.

### 18. RIFE live on an M2 — MEASURED FALSE

8.5 fps at 1080p against Homebrew's ncnn and 9.8 with the bundled one, against
the ≈48 fps a live 2× 24p needs before SCOPE F2's 1.15× headroom: about five
times too slow (`packaging/macos/RIFE-SPIKE.md`). `gpu_thread` must be 1,
because MoltenVK exposes one compute queue, where ButterEye's default is 4.

So this is not an unmeasured item but a measured negative, and the bar's
requirement is that no user-facing string claim otherwise: `buttereye render`
uses RIFE on a Mac, live playback should not.

**The code did not respect it, and on a fresh install it chose live RIFE.**
Measured from the self-contained `ButterEye.app` with no benchmark on file (the
`.app` work, `docs/PLATFORMS.md`, "The .app"): first-run Setup proposed RIFE and
smoke-tested it at 19.1 fps on a 720p clip ("Test run passed"); then the shipped
Quality profile — pinned to RIFE-ncnn, and the shipped rules' pick for a 24p
1080p film — ran RIFE v4.26 live, uncapped because nothing was measured: at
1080p mpv slowed to 6–8 % of real time (silently, "active, ok", 0 drops,
because mpv slows the clock rather than dropping when there is no audio), and
with audio it stalled, fell back to MVTools at the 60 Hz display target,
stalled again and rolled back. 1440p ran at 4.8 % of real time; 4K stalled.
Every RIFE script also carried `gpu_thread=4`, and ncnn printed its warning.

**What changed (2026-10-13).** One rule, `decide.rife_live_needs_proof()`, True
on macOS only: there, RIFE-ncnn runs live only on a benchmark that measured it
(Automatic then weighs its in-mpv cap like any other engine's), or because the
user chose it. It is applied at each place that could reach live RIFE without a
measurement:

- `session.wanted_backend`: a shipped Quality/Balanced/Fast profile still on its
  shipped RIFE, with no Setup choice, asks for **Automatic** instead of pinning
  RIFE. A Setup choice of RIFE (`backend_override`), a profile the user made or
  moved to another engine, and the CPU profile all keep their engine.
- `decide.pick_engine` on Automatic: an unmeasured GPU engine was trusted when it
  was all there was; an unmeasured RIFE-ncnn on a Mac is now skipped for the
  next engine (mvgpu, if installed) or gives "too demanding to smooth in real
  time" when it is the only one, and "smooth anyway" on Automatic does not pick
  it either.
- `select.select`'s no-benchmark fallback, and so Setup's proposal and smoke
  test: MVTools instead of RIFE when MVTools is available. Choosing RIFE in
  Setup anyway is stored as an explicit `backend_override`, and is honoured.
- `gpu_thread` is 1 on macOS (`scriptgen.rife_gpu_thread`, used by live play,
  render, the benchmark's RIFE rows and the smoke test), from the spike's own
  measurement: 18.81 / 19.66 / 17.62 fps at gt 1 / 2 / 4 on 720p — nothing
  gained above 1, and a warning per load.

Why MVTools and not mvgpu as the no-benchmark default: Automatic's existing rule
is that an unmeasured GPU engine is never trusted with a live target, and
nothing in the measurements argues for an exception — at 720p and 1080p the two
were level in mpv (201.27 / 199.21 and 89.21 / 91.07), and at 1440p MVTools
held real time in every run that was not overloaded where mvgpu held it in three
of eleven (item 4). Once a benchmark is on file, mvgpu competes on its measured
cap as before. RIFE stays available for `buttereye render` (render picks its
engine from its own profile, untouched) and for a user who asks for it. Linux
and Windows are unchanged: the rule is False there, because an RTX 4090 runs
RIFE v4.26 live at 1080p and the shipped Quality profile is that machine's
correct default before any benchmark.

Held by tests on both sides of the rule: `tests/test_live_decisions.py`
(`test_a_mac_never_runs_an_unmeasured_rife_live_on_automatic`,
`…measured_rife_is_weighed_on_a_mac_like_anywhere_else`,
`…mac_plays_the_shipped_quality_profile_with_mvtools_until_measured` against
`…linux_plays_the_shipped_quality_profile_with_rife`,
`…mac_still_runs_rife_live_when_the_user_chose_it`,
`…mac_with_only_rife_does_not_smooth_live_without_a_benchmark`,
`…linux_keeps_an_unmeasured_rife_when_it_is_all_there_is`),
`tests/test_live_review_fixes.py::test_setup_choice_applies_to_shipped_profiles`,
`tests/core/test_setup_cancel.py::test_a_mac_setup_proposes_mvtools_and_smoke_tests_it`
and `tests/test_scriptgen.py::test_rife_gpu_thread_is_one_on_a_mac`. The
existing Linux-shaped selection, setup and live tests now pin the Linux answer
explicitly, because the suite also runs on a Mac.

**Re-measured from a rebuilt disk image**, a fresh copy of the app taken out of
it, `env -i` with an empty `HOME` and the system `PATH`, no benchmark on file —
on a machine that was **not idle**: a virtual machine was using about four of
the eight cores, and the 1-minute load average read 15.3–18.6 across these runs.

```
setup                       -> "Set up with mvtools."  smoke test: mvtools,
                               720p, 160.26 fps; no backend_override written
play, 1080p 23.976 + audio  -> 23.976 → 60.002 fps with mvtools
                               dropping 47–81 % over 10 s, then stalled and
                               rolled back to unsmoothed (real time, unsmoothed)
play --profile cpu, 1080p   -> 23.976 → 47.952 fps with mvtools; 150 frames
                               dropped in 10.2 s, recovered to "ok"
play, 2160p 23.976 + audio  -> 23.976 → 60.002 fps with mvtools; stalled and
                               rolled back to unsmoothed
```

So the engine choice is now the measured one — RIFE is not chosen at any size —
but **this run does not show live smoothing holding real time**, and it would be
wrong to read it as doing so: on this loaded machine even MVTools' plain 2×
dropped frames at 1080p, where the app's earlier, quieter run measured MVTools
2× at 1080p in real time with one drop. Two things remain open and are not
fixed here: whether MVTools at the display's 60 Hz (2.5× from 24p, where every
m0s figure is 2×) holds 1080p on an idle M2 (since measured, once, on a quieter
machine — 1-minute load 5–6 with the VM stopped: 30 s of a 1080p film cut to
23.976 fps and re-encoded as H.264, MVTools FlowFPS **23.976 → 60 at 1080p ran
1.48× real time untimed and held real time live, full screen, 0 drops in 25 s, at
4.1 cores**. A 1080p **50** fps film is harder than its inferred-frame count
suggests, because MVTools analyses every source frame: 50 → 60 ran 1.03× untimed
and dropped about 17 % live — `docs/PLATFORMS.md`, "Live smoothing on the M2"), and the 4K behaviour, where nothing
holds real time on this Mac (item 4) and an unmeasured MVTools is tried, stalls
once and rolls back — one stall now, where RIFE-then-MVTools was two. Both want
an idle machine; the first also wants a benchmark at the display rate. The RIFE formula's `caveats` says
so. It is "measured false on this hardware" rather than "will never be true",
because it is a throughput number on one GPU generation and a future Apple GPU
could change it; nothing about the architecture forbids it.

### 19. TensorRT on macOS — WILL NEVER BE TRUE

There is no CUDA and no TensorRT for macOS, and no Apple Silicon Mac has an
NVIDIA GPU. Not pending, not deferred: impossible. The code already says so in
three places — `osapi.HAS_VSTRT` is False (phase 1), the generated script refuses
the TensorRT branch before it looks for an engine, and the doctor emits one INFO
row that says *can't* rather than *not set up yet*
(`checks_trt.trt_macos_finding`, `capabilities._TRT_IMPOSSIBLE`). Verified again
in today's doctor run: "Experimental NVIDIA TensorRT can't run on macOS".

### 20. A second machine of any kind — NOT VERIFIED

Every macOS number in this repository — this document included — comes from one
Apple M2 running macOS 27.0.1. Not a second M2, not an M1, not an M3 or M4, not
a different macOS version, not a Mac with an external display or a Retina panel.
That is the single largest unknown on the list, and it is the same unknown
Windows has: the difference is that Windows has never run on real target
hardware at all, where macOS has run on exactly one.

## The .app route (0.0.3) — added for the experimental disk image

This section was added with `packaging/macos/build-app.sh`; it does not change any
verdict above, which are about the Homebrew route. `docs/PLATFORMS.md`, "The .app",
has every command and number; this is the checklist form, for one machine (the dev
M2, macOS 27.0.1) and a copy taken out of the mounted 0.0.3 image, run with `env -i`,
a fresh `HOME` and the system `PATH`.

| Question | Verdict |
|---|---|
| The image builds from pinned inputs, and the build refuses a bundle that names anything outside it | **VERIFIED** — `appstage.py check` (92 Mach-O files, arm64, no foreign load command) and `appstage.py scan` (no home folder, user or host name in the app or the sources tar) both gate the build |
| The app needs nothing installed | **VERIFIED on this Mac** — `DYLD_PRINT_LIBRARIES` and `lsof` show nothing loaded from `/opt/homebrew` or `/usr/local` by ffmpeg, vulkaninfo, vspipe or mpv running a VapourSynth filter. **Not verified on a Mac without Homebrew**: this one has it, which is the harder case for the claim but not the same machine as a user's |
| The doctor from the app | **VERIFIED** — 15 ok, 3 info, 1 degraded (mkvmerge, not carried), 0 blocking |
| Live smoothing through the app's mpv | **VERIFIED** — MVTools 2× at 1080p in real time (15.08 s of video in 15 s); mvgpu after a benchmark, real time, 0 dropped frames |
| Offline render through the app | **VERIFIED** — RIFE and MVTools, 1080p → 47.952 fps HEVC |
| Every engine from the app | **VERIFIED** — RIFE (three models), mvgpu and MVTools all ran in `buttereye bench`; RIFE far below real time, as item 18 says |
| Automatic live play on a fresh install | **MEASURED FALSE for real time** — before any benchmark it picks RIFE, which plays at 5-8 % of real time at 1080p and 1440p and stalls at 4K; reported to the engine chooser's owner, not changed here |
| The window | **PARTLY VERIFIED** — opened with `open`, first-run setup ran, the app's Qt drew every simple-window view through `cocoa`; never driven by a person |
| Gatekeeper: what a downloaded copy does | **VERIFIED** — refused (`spctl` rejected, `syspolicy_check`: ad hoc, no notarisation ticket), the app and each of its programs held for a prompt; `xattr -dr com.apple.quarantine` makes everything run |
| Gatekeeper: System Settings ▸ Open Anyway | **NOT VERIFIED** — needs a person to click; whether it covers the programs inside the app is unknown, so the documented route is the Terminal command and the doctor flags a quarantined program |
| `/Applications`, App Translocation, a second account, macOS older than 27, a second Mac | **NOT VERIFIED** |
| CI builds the app | **NOT DONE** — the build needs Homebrew bottles, which `macos.yml` does not install |

## The regression contract, re-measured today

Phase 5 was planned as a measuring phase that changed no code, and it did not
stay one: the measurements found two defects that had to be fixed rather than
filed — the plugin search path that reached the doctor only (item 6) and the
VSScript search that read the working directory (item 1). So the contract below
is a real regression check and not a formality. All of it re-run on 2026-10-13
against the tree as it now stands, each command with the port's own virtualenv
Python:

| Check | Command | Result |
|---|---|---|
| Three-platform type check | `for p in linux win32 darwin; do python -m mypy --platform $p buttereye; done` | `Success: no issues found in 112 source files` × 3 |
| Gate, Homebrew tests opted in | `BUTTEREYE_TEST_BREW=1 python -m pytest tests/core tests/boundary tests/macos -q` | 899 passed, 18 skipped (864 / 12 before release-bar step 1; the difference is step 1's and item 18's new tests and the `.app` work's) |
| Gate, nothing opted in | `python -m pytest tests/core tests/boundary tests/macos -q` | 871 passed, 46 skipped (836 / 40 before step 1) |
| Live smoothing, clean environment | `env -u VSSCRIPT_PATH -u DYLD_LIBRARY_PATH -u DYLD_FALLBACK_LIBRARY_PATH python -m pytest tests/integration/test_hdr_live_real.py -q` | 3 passed |

Both gates were clean in these runs. An earlier run of the second one failed
once before passing, on
`tests/core/test_ops_shutdown.py::test_kill_owned_groups_is_scoped_to_loop` —
the intermittent `.github/workflows/macos.yml` already documents (its macOS
helper shells out to `ps -ax` and scans the whole process table). It passed 6 of
6 when the file ran alone, with a load average of 14.40 on 8 threads, which is
the condition that intermittent is described as needing. **The counts are not
the contract**: other phase-5 work adds tests, so treat "green" as the check and
re-measure the numbers rather than trusting these two.

Linux and Windows behaviour is untouched except in the one case named at the
end of this paragraph: this document adds no code, and the code changes phase 5
did make leave both platforms answering what they answered before. `paths.plugin_dirs` is `(paths.rpm_plugin_dir,)` off macOS — a
one-element search path holding the single directory those callers already
asked about — and `paths.plugin_in` accepts that one `Path` directly as well,
which is what the tests that were written against the old signature still pass.
The VSScript search is inside `_darwin` and reachable from neither. The
render fix of item 6 uses the same `plugin_dirs` and `plugin_dir_for`, so
Fedora and Windows still name `paths.rpm_plugin_dir` for every engine
(`test_offline_render_keeps_its_one_directory_off_macos`). The one change that
can reach Linux is the chooser fix of item 4, and only in the shape it was
written for: a stored benchmark whose recommendation the chooser cannot rank —
on Linux, a run recommending mvgpu where mvgpu is installed. Such a run used to
be ignored and is now read for its best real-time rankable row; every other run,
and every machine with no benchmark, chooses what it chose before, and the
pre-existing `tests/core/test_select.py` passes unchanged. The three-platform
type check above is the gate on all of it.

## Recommendation

**This section is a recommendation, not a decision.** macOS being a supported
platform is a SCOPE §2 non-goal and §13 is where the owner records decisions;
nothing above or below edits either.

**The owner's decision, 2026-10-13:** macOS ships in 0.0.3 as an *experimental*
download — the ad-hoc-signed disk image described under "The .app" — on the same
footing as the Windows installer in 0.0.2. It is still not a supported platform:
SCOPE §2 is unchanged. The items below that this section calls blocking are
blocking for *supported*, and the release notes carry the warnings listed here.

### Can macOS be described as supported?

**Recommended: no — and for a different reason than Windows.** Windows is
unreleased because nothing has run on target hardware. macOS has run on target
hardware and does the job there: live smoothing in mpv, offline render end to
end, a doctor with no blocking finding, an engine whose frame is bit-exact. What
macOS lacks is not evidence that the code works; it is evidence that anybody
other than its author can get it. **Nothing has ever been installed.** The tap
points at a release that does not exist, no formula has been audited, and the
engines are in a build cache the application cannot see — so the benchmark that
SCOPE's own product principle rests on still measures **one engine** on a Mac
that merely installed ButterEye. It has measured all five, but only through a
shadow prefix nobody but this port will ever build (item 4). A platform where
the install route has never been executed once cannot be called supported,
however well it runs for the person who built it.

The three items that make "supported" unsayable in the strongest sense are 6
(the tap), 4 (the engine choice) and 9 (a clean machine) — and they are one item
in practice, because the first unlocks the other two. What changed during review
is the *order*: item 4's chooser half had to be closed before item 6 is run, not
after, because installing the tap is what lets the chooser reach live RIFE. It
now is (step 1 below).

### If not supported, what should the docs call it?

**Recommended wording: "ported and measured on one Mac; not released, not
supported."** That is what is true, it is sayable in one line, and it does not
read as a support claim. The README already says something close to it and
should not be strengthened; `docs/PLATFORMS.md` keeps the measurements and the
gaps. Neither file needs a new adjective.

**The shortest honest path to a release**, in the order that unblocks the most.
**The order changed while this document was being reviewed**, and the reason is
in item 4: installing the tap is what makes RIFE available to the engine
chooser, and the chooser used to throw away the measurement that would keep RIFE
off live playback. So the install is not step 1. If you disagree with
that reordering, the measurement to argue with is the one in item 4's "The
chooser half" paragraph, not this list.

1. **Teach `select._from_bench` what to do with an mvgpu-recommended result**
   (item 4) — **done, 2026-10-13.** It had to come *first*, because every later
   step installs the tap, and a Mac with the tap installed and a 720p or 1080p
   benchmark on file was measured to recommend live `rife-ncnn` — the engine
   item 18 records as five times too slow — on the strength of a run whose own
   RIFE rows were 20.07 and 8.33 fps. The same fixture fed every complete m0s run
   that recommended mvgpu now chooses `mvtools` with `from_benchmark` True, and
   the regression test reads `m0s-results.json` itself and fails against the old
   code. The sibling hazard, one plugin directory hiding the other, is now closed
   for offline render too (`render/jobs.py` on `paths.plugin_dirs`, item 6), so it
   is closed everywhere a plugin is looked up. A second path to live RIFE, which
   step 1 as first written missed — the shipped RIFE profiles running unmeasured,
   measured from the app at 6–8 % of real time — is closed as well, with
   `gpu_thread` 1 on a Mac (item 18).
2. **Tag a release and fill in the four checksums** (item 6). Everything else
   waits on this: `brew install` cannot be tried, the engines cannot reach
   `<prefix>/lib/buttereye/vapoursynth`, and the benchmark cannot see them.
3. **Install the tap on a Mac that is not the dev machine, and run the doctor
   and the gate from the installed copy** (items 6, 9). This is where the two
   licence debts in `packaging/macos/SOURCES.md` have to close.
4. **Benchmark every engine on the real GPU, idle, and check the model gate
   follows the measurement** (item 4) — and settle the 1440p ordering while the
   machine is idle, because the verdict this document used to state as fact does
   not reproduce and no release note may assert one until it does.
5. **Use the window, and watch a real film** (items 5, 8) — the screenshots are
   taken, so what is left is a person with a keyboard, VoiceOver and a HiDPI
   screen — and give the `tests/gui/test_theme_scale.py` failure an owner.
6. **Exercise the cross-UID refusal** (item 10) on a machine with two accounts,
   and **unlink the socket under a live session** for item 11's consequence.
7. ~~**Build the Fedora RPM with the model-path patch in and run the 242-character
   repro** (item 15)~~ — done 2026-10-13, fc44 and fc45, see item 15.

Blocking, in the recommender's view: items 4, 6, 8, 9, the half of item 5
that is a person using the window, and item 10 if SCOPE §4.3 is to mean what it
says. Merely unknown, and releasable with a sentence in the notes: items 11, 12,
13, 14, 16, 20. Item 7 (a CI job that
installs the tap) is not blocking for a first release but is what keeps the
other items from going stale, and it is cheaper on macOS than on Windows because
GitHub's Apple Silicon runners have a real GPU.

### What should the first macOS release notes claim, and what should they warn?

**Claim, because each is measured:** ButterEye smooths video live in mpv on
Apple Silicon; MVTools (CPU) and mvgpu (Vulkan through MoltenVK) interpolate,
and mvgpu's in-between frame is bit-exact against the geometric truth; offline
render runs end to end and keeps the source's other streams; `buttereye doctor`
answers in Homebrew's vocabulary and names the real cause when something is
missing; the install is a Homebrew tap and needs no Gatekeeper exception,
because `brew` marks nothing it fetches.

**Warn, and none of these may be softened:**

- Apple Silicon only. Intel Macs are untested and the tap refuses them.
- RIFE is for `buttereye render` only: 8.5–9.8 fps at 1080p on an M2 against
  the ≈48 fps live playback needs.
- A macOS RIFE build is **not** the same pixels as the Linux one: it links
  Homebrew's `ncnn`, and 21.3 % of output samples differ from the pinned build.
- **1440p, SCOPE §1's v1 size, is on the threshold rather than inside it.** Both
  motion-vector engines measure within a few per cent of the 55.14 fps a
  real-time 2× of 23.976 needs, and which of them clears it changes between runs:
  over eleven runs of the same command mvgpu measured 52.8–58.3 in mpv, real time
  in three of them, and MVTools cleared the bar in every run that was not
  overloaded but by only 2–8 %. No note may name "the" 1440p engine, and none may
  promise 1440p on a busy machine. (4K reaches real time with nothing.)
- Everything was measured on one M2 running macOS 27.0.1. No other Mac, no other
  macOS version, no second account, no network home.
- A session left attached to one mpv for more than three days can lose its
  socket to the system's daily sweep of the per-user temporary directory.
- The GUI has been drawn and captured on a Mac but never driven by a person on
  one: the menus answer to Cmd rather than Alt, VoiceOver has never been tried,
  and nothing has been seen on a HiDPI screen.
- TensorRT is impossible on macOS and will not appear.

## How to re-run this bar

```sh
for p in linux win32 darwin; do <venv>/bin/python -m mypy --platform $p buttereye; done
BUTTEREYE_TEST_BREW=1 <venv>/bin/python -m pytest tests/core tests/boundary tests/macos -q
<venv>/bin/python -m pytest tests/core tests/boundary tests/macos -q
env -u VSSCRIPT_PATH -u DYLD_LIBRARY_PATH -u DYLD_FALLBACK_LIBRARY_PATH \
    <venv>/bin/python -m pytest tests/integration/test_hdr_live_real.py -q
<venv>/bin/python -m buttereye.cli doctor
<venv>/bin/python -m buttereye.cli render -o /tmp/out.mkv <a real clip>
<venv>/bin/python -m buttereye.cli bench --width 1280 --height 720   # idle machine
tools/macos-tests.sh                                                 # the full gate
```

Run them on an idle machine, and record the load average beside any benchmark
number. A figure measured while another suite runs is not a release number — the
same `bench` command answered 92.3, 135.2 and 314.8 frames a second on this one
machine depending on what else was running at the time.
