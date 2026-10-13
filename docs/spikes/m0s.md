<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(s): the engine benchmark on an Apple GPU (macOS phase 5)

Date: 2026-10-13. Machine: **Apple M2, macOS 27.0.1, 8 threads** — not the dev box, and
every figure below is this machine's. Homebrew's mpv 0.41.0, VapourSynth R81 and `vspipe`,
MoltenVK 1.4.2, `vapoursynth-mvtools` 29; `libmvgpu.dylib` built by
`packaging/macos/build-plugins.sh` and `librife.dylib` by `packaging/macos/build-rife.sh`,
with the three packaged RIFE models. Tool: **`buttereye bench` itself** (SCOPE F14), not a
spike script. Raw numbers: `m0s-results.json`; tables: `m0s-table.md`.

## Question

Every performance figure in this repository is an RTX 4090's, and SCOPE F14 says ButterEye
does not quote published fps figures: an engine is trusted when it has been measured on the
machine that will play the video. On a Mac nothing had been measured. Phase 4's benchmark
run saw **one** engine, MVTools, because the two plugins ButterEye builds for macOS were in
a directory nothing searches. So: with RIFE-ncnn and mvgpu where ButterEye looks, what does
`bench` measure at 720p, 1080p, 1440p and 4K, what does the engine chooser then pick, and is
that the right pick?

## Getting the built plugins where ButterEye looks, without touching Homebrew

`build-plugins.sh` writes to `${XDG_CACHE_HOME:-$HOME/.cache}/buttereye/macos`, which is not
a place ButterEye searches. On macOS both plugin answers are derived from
`types.homebrew_prefix()`: `macos_plugin_dir()` is `<prefix>/lib/buttereye/vapoursynth` and
`_packaged_data_dir()` is `<prefix>/share/buttereye`. `BUTTEREYE_HOME` is no use here — it
moves `paths.install_dir()`, which only `_resolve_windows` reads. The one injection point
that already exists is **`HOMEBREW_PREFIX`**, which `homebrew_prefix()` honours first
because a brew shell exports it. So the prefix was shadowed, not modified: a directory of
symlinks to the real prefix, with ButterEye's own two directories added. Nothing was
installed, and `/opt/homebrew` was not written to.

```sh
shadow=$HOME/.cache/buttereye/macos-prefix   # a directory this user owns
built=$HOME/.cache/buttereye/macos           # build-plugins.sh / build-rife.sh output
real=/opt/homebrew
mkdir -p "$shadow/lib" "$shadow/share"
# every top-level entry of the real prefix, as a symlink, except the two we extend;
# the Cellar has to come along because the macOS install database is read off it
for e in "$real"/*; do case $(basename "$e") in lib|share) ;; *) ln -s "$e" "$shadow/";; esac; done
for sub in lib share; do for e in "$real/$sub"/*; do ln -s "$e" "$shadow/$sub/"; done; done
mkdir -p "$shadow/lib/buttereye" "$shadow/share/buttereye"
ln -s "$built/vapoursynth"              "$shadow/lib/buttereye/vapoursynth"
ln -s "$built/share/rife-ncnn-models"   "$shadow/share/buttereye/rife-ncnn-models"
HOMEBREW_PREFIX=$shadow buttereye bench --width 1920 --height 1080
```

With that, `doctor` finds `librife.dylib`, `libmvgpu.dylib` and the three models, and the
in-mpv probe loads RIFE-ncnn and MVTools inside mpv. It still measures only four engines,
though, because MVTools lives in Homebrew's autoload directory and the benchmark asks one
directory (finding 2), so every run below used a second shadow prefix with one more link:

```sh
ln -s "$real"/lib/python3.*/site-packages/vapoursynth/plugins/mvtools.dylib \
      "$shadow/lib/buttereye/vapoursynth/mvtools.dylib"
```

That is the only reason the tables have five rows. Loading MVTools from there costs nothing:
`_load` in the generated script skips a `LoadPlugin` for a namespace VapourSynth has already
autoloaded, so the copy in play is Homebrew's either way.

## Speed (`m0s-table.md` has every row; in-mpv fps, the column that decides)

Real time for a 2× of 23.976 fps is `REALTIME_HEADROOM` 1.15 × 47.95 = **55.14 fps in mpv**.

One run of each size (pass "a"); the ranges across every run are in `m0s-table.md`,
and 1440p was run eleven times because its verdict does not settle.

| size | best RIFE | mvgpu | MVTools | `bench` recommends |
|---|---|---|---|---|
| 1280×720 | 20.07 | **201.27** | 199.21 | mvgpu |
| 1920×1080 | 8.79 | **89.21** | 91.07 | mvgpu |
| 2560×1440 | 5.03 | 52.8–58.3 (96–106 % of bar) | 56.3–59.7 | mvtools 8 of 11, mvgpu 3 |
| 3840×2160 | not measurable | 32.20 | 28.58 | nothing |

- **RIFE-ncnn is offline-only on an M2, at every size.** The phase-3 spike measured 8.5–9.8
  fps at 1080p; `bench` agrees (8.79) and adds the shape of the curve: 20.1 at 720p, 5.0 at
  1440p, and at 4K it does not finish at all. The benchmark's own timeout is
  `30 s + frames/2` — 78 s for 96 frames, a floor of about **1.23 fps** — and all three
  models hit it, so 4K RIFE is recorded here as *not measured* rather than as a number.
  Even the fastest model at the smallest size reaches 36 % of the bar.
- **mvgpu and MVTools both play 1080p with headroom**, and at 720p both are 3.6–4× the
  bar. mvgpu's advantage over the CPU is small (89.21 against 91.07 at 1080p: MVTools is
  *faster*), which is nothing like the 4090, where mvgpu is ~7× MVTools. Unlike 1440p this
  verdict holds up: two more 1080p runs at mean load 3.2 and 3.7 gave mvgpu 96.10 and 97.01
  and MVTools 101.76 and 102.56 in mpv, 1.74–1.86× the bar, and one more 720p run gave
  205.03 and 218.34, 3.7–4.0×. Both would need to lose about 45 % at 1080p and 73 % at 720p
  to reach the bar, and the largest loss ever measured here is the 43 % of the overloaded
  1440p run — so 1080p is clear in ordinary use but not by more than that one overload.
- **1440p, which is SCOPE §1's v1 real-time target, is too close to call.** mvgpu sits
  within a few per cent of the 55.14 bar and which side it lands on depends on what else
  the machine is doing — and not only on that, since two runs at the same load landed on
  opposite sides. Eleven runs of the same command (`m0s-table.md` lists each one with its
  load average) put mvgpu at 52.77, 52.92, 53.39, 53.57, 53.9, 54.25, 54.57, 54.99, 55.39,
  56.92 and 58.31 in mpv — **96 % to 106 % of the bar, real time in three of the eleven**
  — and `bench` recommended mvtools in eight of them and mvgpu in three. MVTools came out
  at 56.33–59.69 and was real time in all ten runs that reported it, but **never by more
  than 8 %**, and the one overloaded run put it at 32.39. So the measured answer at 1440p
  is that both motion-vector engines are a few per cent from the bar, MVTools on the clear
  side whenever the machine is not overloaded and mvgpu on either side. This spike first
  reported the CPU engine as the only one that reaches the v1 target: three of these
  eleven runs contradict that, and it should never have been written as settled. The
  figures that decide v1 are therefore the engine's *and* the machine's, and nothing in
  `bench` tells the user that.
- **4K is out of reach for everything**, as SCOPE expects (4K is offered only when the in-mpv
  benchmark passes): 32.20 and 28.58 against 55.14, and `recommended` is correctly `None`.
  This is the one verdict load cannot touch, and that was checked rather than assumed: a
  second 4K run at mean load 13.9 (peak 40.7, against the first run's 3–12) gave mvgpu 32.14
  and MVTools 28.88 — within 1 % of the first — so at 4K the engines are not CPU-starved,
  and they would have to get 1.71× and 1.91× *faster* to reach the bar, where the largest
  gap between two runs of the same configuration on this machine is 1.61×.
- Spread, and a warning about it: at 1440p and 4K every measurement is repeatable (CoV
  < 0.05); at 720p and 1080p the CPU engine is not (CoV 0.157 and 0.081), and neither is
  mvgpu at 720p. The machine was shared with other work throughout (load average 3–12 on 8
  threads) — four 720p MVTools runs on the same day gave 258.8, 160.6, 203.1 and 199.2 in
  mpv — so **the CPU numbers are a floor, not a best case**. The later runs record the
  1-minute load average with each one, which is what makes the 1440p spread readable: over
  seven runs at mean load 3.19 to 6.08 the MVTools figure tracks the load closely (56.33 at
  6.08 up to 59.69 at 3.42, r = −0.93), while mvgpu's does not (r = −0.63) — its two
  quietest runs, mean load 3.42 and 3.19, came out at 58.31 and 54.25, a 1.07× swing that
  crosses the bar with the load average almost unchanged. So load is the largest single
  influence but not the whole story, and at 1440p mvgpu's run-to-run spread is on its own
  wider than its distance from the bar. One 1440p run had to be discarded because a second
  benchmark of this spike's was running beside it: MVTools came out at
  32.39 instead of 57.16 and every RIFE model timed out, which **flipped 1440p from
  "MVTools keeps up" to "nothing keeps up"**. `bench`'s verdict is a threshold, so a busy
  machine does not make it a little pessimistic, it changes the answer — and at 1440p, where
  the margins are 2–8 %, it changes it between two runs nobody would call overloaded.
  Nothing in SCOPE or the GUI tells a user not to run the benchmark while the machine is
  busy, and `bench` does not record what the machine was doing, so a stored result cannot be
  judged afterwards.

## What an Apple GPU costs, against the figures already in this repository

The second half of `m0s-table.md`; nothing was re-measured on the 4090, so these are ratios
against numbers this repository already carries, not a head-to-head.

At 1080p 2× in mpv, the M2 is **7.1–10× slower than the RTX 4090 on every GPU engine**
(RIFE v4.26 7.3×, v4.22-lite 7.1×, v4.18 10.0×, mvgpu 7.9×) and **1.12× slower on the CPU
engine** (91.07 against 102.3 — an 8-thread M2 against a 24-thread Ryzen 9 5900X). At 4K,
MVTools is 1.24–1.30× behind and RIFE at least 11× behind. That is the whole story of this
phase in one line: **on an Apple machine the GPU is the slow part and the CPU is the fast
part**, which is the opposite of the machine every other number here was taken on.

## What the engine chooser picks, and why it is right for the wrong reason

`buttereye setup` on this Mac prints "Set up with mvtools." `api.select_backend` (the path
that is handed the benchmark) says:

```
backend     mvtools        from_benchmark  True
ranked      ('mvtools',)   reason  Fastest real-time configuration in your benchmark (mvtools).
rife-ncnn   available=False :: The RIFE plugin is not installed
mvtools     available=True  :: MVTools on the CPU
mvgpu       available=True  :: GPU motion vectors on Apple M2
rife-trt    available=False :: TensorRT can't run on macOS: it needs CUDA and an NVIDIA GPU.
```

MVTools is the right engine on this evidence — it reached 1440p in every run that was not
overloaded, where mvgpu reached it in three of eleven — but it clears the bar by only
2–8 %, which is not a margin that makes the choice obvious. RIFE, meanwhile, never even entered the
ranking, and that has nothing to do with the measurement (finding 1). So the answer to
"does an M2 reach the full RIFE model" is **no, twice over**:

- `MODEL_HIGH` is gated on a benchmark recommendation, and RIFE can never earn one here: its
  best in-mpv figure is 36 % of the bar at the smallest size.
- **Phase 2's decision not to gate on `device_type` is vindicated by an exact number.**
  MoltenVK reports this M2's device-local heap as 17179869184 bytes — **16.0 GiB, exactly
  `select.HIGH_END_HEAP`**. `heap_bytes >= HIGH_END_HEAP` is therefore True, and the only
  thing keeping `_high_end_gpu` False is the `device_type == "discrete"` half. Had the
  threshold been the whole test, this machine would have been handed RIFE v4.26 — 8.3 fps at
  1080p — on a coincidence. And the heap it passed with is not graphics memory at all: it is
  byte for byte `sysctl hw.memsize`, the whole machine's RAM.

`bench --apply <label>` writes a profile named `Benchmark: <label> (<w>×<h>)` with the
engine, model and `concurrent_frames` of that measurement, plus one rule matching up to the
benchmark's own size. Applying the 1440p result's `mvtools` wrote `backend = "mvtools"`,
`target = "2x"` and `match = { fps_max = "24240/1001", width_max = 2560, height_max = 1440 }`
— correct, and the configuration this Mac should run. Which engine a 1440p result offers
to apply is not stable, though: in the three runs where mvgpu cleared the bar,
`recommended` was mvgpu.

## Findings (defects, reported rather than fixed: this spike's files are its own)

1. **The macOS install database cannot see ButterEye's own plugins, so RIFE is never
   available.** `checks_pkgs.brew_db` answers only for names in `BREW_FORMULAE`, and
   `PKG_RIFE`, `PKG_MODELS` and `PKG_MVGPU` are absent there on purpose, with the comment
   "Homebrew has no formula for any of them". Phase 4's tap *does* ship formulae with exactly
   those names (`packaging/macos/homebrew/buttereye-vs-rife-ncnn.rb`,
   `buttereye-rife-ncnn-models.rb`, `buttereye-vs-mvgpu.rb`), and nothing on macOS reads the
   `components.json` `build-plugins.sh` writes — `manifest_db` is Windows-only. Measured
   consequences with `librife.dylib` loading inside mpv and three models benchmarked:
   `packages.rife` is DEGRADED, the `packages.models` row is **not emitted at all** (it is
   inside `if rife_ok`), so `select.packaged_models()` is `()` and `_default_model()` is
   `None`, and `backends()` reports RIFE-ncnn unavailable. A real `brew install` of the tap
   would produce the same degraded row.
2. **The engine chooser and live play ask one plugin directory where the doctor asks a
   path.** `bench.runner.discover_candidates` and `decide.installed_backends` take
   `paths.rpm_plugin_dir` — `types.macos_plugin_dir()`, a single directory — while
   `checks_pkgs.plugin_dirs` returns both. On macOS the two directories hold disjoint sets by
   construction: a Homebrew formula cannot install into another formula's keg, which is the
   whole reason `types.macos_plugin_dirs` exists. Measured:

   | `HOMEBREW_PREFIX` | doctor searches | bench candidates |
   |---|---|---|
   | `/opt/homebrew` (no ButterEye directory) | both | `mvtools` |
   | shadow prefix with ButterEye's directory | both | `rife-v4.26`, `rife-v4.18`, `rife-v4.22-lite`, `mvgpu` |

   So the benchmark can never see all the engines at once, which is why phase 4 measured
   MVTools alone and why this spike needed a second prefix with `mvtools.dylib` symlinked
   into ButterEye's directory. Worse for a tap-installed Mac:
   `decide.installed_backends` then returns `{rife-ncnn, mvgpu}`, `resolve_backend(None, …)`
   picks **rife-ncnn** (8 fps at 1080p) for Automatic, and `BackendId.MVTOOLS in installed`
   is **False** — so `session.switch_to_cpu`, the one-shot CPU fallback after a stall or a
   GPU fault, has nothing to fall back to, on the single platform where MVTools is the only
   engine that reaches 1440p.
3. **A benchmark that recommends mvgpu is discarded whole.** `select._from_bench` looks only
   at `res.recommended` and mvgpu is never in `ranked` (§5.3, deliberately: live play picks
   it per file). Measured with only the 720p and 1080p results in the history, both of which
   recommend mvgpu: `from_benchmark` is **False** and the reason becomes "MVTools on the CPU:
   RIFE can't run here." Same engine, but the user is told no measurement exists when five
   configurations were measured. On a Mac this is the normal case, because mvgpu wins
   wherever RIFE cannot and MVTools is ranked below it in `_quality_key`.
4. **`bench --apply <label>` does not check the verdict it is saving.** `--apply rife-v4.26`
   against the 720p result wrote `backend = "rife-ncnn"` with a catch-all rule up to
   1280×720 for a configuration the same run measured at 20.07 fps against the 55.14 it
   demanded. `--apply mvgpu` against the 4K result — whose `recommended` is `None` because
   nothing was real time — wrote a profile covering everything up to 3840×2160.
5. **A candidate that could not be measured is stored and printed as `0.0`.** The runner
   emits a `Notice` ("rife-v4.26 could not be measured: vspipe did not finish within 78 s")
   and `cmd_bench` does not print it, so the CLI table reads `0.0 0.0 no` for RIFE at 4K with
   no reason and no fault marker. The stored measurement is `0.0` with `cov` infinity.
6. **Nothing warns that a busy machine changes the verdict, and `repeatable` does not catch
   it.** The discarded 1440p run above was 0.57× on the CPU engine and timed every RIFE model
   out, yet its surviving measurements all reported `repeatable` true: the coefficient of
   variation is computed over three runs that were all equally slowed, so a steady overload
   looks steady. It does not take an overload, either: across eleven 1440p runs mvgpu moved
   between 96 % and 106 % of the bar, `repeatable` true in every run that reported it, so
   the stored verdict for the size SCOPE §1 makes v1 depend on is decided by conditions the
   result does not record — the load average is not stored, and two runs at the same load
   came out on opposite sides of the bar anyway. The result is stored
   like any other and `select._from_bench` prefers the newest, so one benchmark run during a
   build can pin a worse engine until the next run.

## Not measured, and what each would take

- **GPU faults, either way.** macOS keeps its restart counters in IOReport and ButterEye does
  not read them, so every `faults` cell on this machine is `?`. Nothing can be said about
  whether RIFE or mvgpu faults an Apple GPU; the Linux rule that drops a repeatedly faulting
  model has no input here. It needs the IOReport API (or `powermetrics` as root).
- **4K RIFE as a number.** It needs a longer benchmark timeout than `30 s + frames/2`, which
  is a product decision, not a measurement: the current floor of ~1.23 fps is a deliberate
  "not worth waiting for".
- **A quiet machine.** Every run here shared the machine with other work: the later runs
  record their own 1-minute load average and the quietest of them averaged 3.1 on 8 threads,
  with peaks above 10 in most. An idle Mac was never available, so no figure here is this
  M2's best case — which matters most at 1440p, where the margins are 2–8 % and the two
  quietest runs of all landed on opposite sides of the bar. What is still unmeasured is
  the whole table on a machine doing nothing else.
- **Real footage.** `bench` measures generated clips only (`av://lavfi:testsrc2` in mpv, a
  moving pattern through `vspipe`), by design (F14, nothing downloaded). How these engines
  behave on a real 4K HDR film inside mpv on an M2 is still unmeasured.
- **Any other Mac.** One M2 with 16 GB, 8 threads and an 8-core GPU. An M-series Pro, Max or
  Ultra has several times the GPU and would change every GPU row; an Intel Mac has a
  different prefix (`/usr/local`) and a different GPU family, and none of it is measured.
