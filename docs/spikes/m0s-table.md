| size | engine | vspipe fps | in mpv fps | real time (bar 55.14) | repeatable | faults |
|---|---|---|---|---|---|---|
| 1280×720 | rife-v4.26 | 20.47 | 20.07 | no | yes | not checked |
| 1280×720 | rife-v4.18 | 16.96 | 13.82 | no | no | not checked |
| 1280×720 | rife-v4.22-lite | 16.35 | 17.63 | no | no | not checked |
| 1280×720 | **mvgpu** | 193.94 | **201.27** | **yes** | yes | not checked |
| 1280×720 | mvtools | 228.57 | 199.21 | yes | no | not checked |
| 1920×1080 | rife-v4.26 | 8.13 | 8.33 | no | no | not checked |
| 1920×1080 | rife-v4.18 | 6.39 | 5.96 | no | yes | not checked |
| 1920×1080 | rife-v4.22-lite | 8.92 | 8.79 | no | yes | not checked |
| 1920×1080 | **mvgpu** | 83.84 | **89.21** | **yes** | yes | not checked |
| 1920×1080 | mvtools | 98.46 | 91.07 | yes | no | not checked |
| 2560×1440 | rife-v4.26 | 4.43 | 4.64 | no | yes | not checked |
| 2560×1440 | rife-v4.18 | 3.63 | 3.63 | no | yes | not checked |
| 2560×1440 | rife-v4.22-lite | 5.15 | 5.03 | no | yes | not checked |
| 2560×1440 | mvgpu | 49.66 | 52.92 | no here (96 %; 96–106 % over 11) | yes | not checked |
| 2560×1440 | **mvtools** | 58.78 | **57.16** | **yes** (104 %; 102–108 %) | yes | not checked |
| 3840×2160 | rife-v4.26 | — | — | not measured (78 s timeout) | — | not checked |
| 3840×2160 | rife-v4.18 | — | — | not measured (78 s timeout) | — | not checked |
| 3840×2160 | rife-v4.22-lite | — | — | not measured (78 s timeout) | — | not checked |
| 3840×2160 | mvgpu | 32.11 | 32.20 | no | yes | not checked |
| 3840×2160 | mvtools | 28.57 | 28.58 | no | yes | not checked |

Recommended by `bench`: mvgpu (720p), mvgpu (1080p), nothing (4K). At 1440p the
recommendation is not stable: mvtools in eight of the eleven runs below, mvgpu in three,
and nothing in the overloaded one.
Bar = `REALTIME_HEADROOM` 1.15 × the 2× target of 23.976 fps = 55.14 fps in mpv.
Machine: Apple M2, macOS 27.0.1, 8 threads. Faults: macOS keeps its GPU restart
counters in IOReport, which ButterEye does not read, so no run on this machine can
report a fault either way.

### 2560×1440, eleven runs: mvgpu straddles the bar, and load is only part of it

The 1440p verdict is the one SCOPE §1 makes v1 depend on, so it was run until it
stopped changing — and it did not stop changing. Every run below is the same command
(`buttereye bench --width 2560 --height 1440` against the shadow prefix); the load
average is the 1-minute figure from `sysctl -n vm.loadavg`, sampled every 15 s for the
length of the run on 8 threads.

| run | 1-min load average | mvgpu vspipe / in mpv | MVTools vspipe / in mpv | `bench` recommends |
|---|---|---|---|---|
| a | not per run (3–12 that day) | 49.66 / 52.92 — no | 58.78 / 57.16 — yes | mvtools |
| b | not recorded per run | 49.15 / 53.39 — no | 59.50 / 58.84 — yes | mvtools |
| review 1 | 2.6–3.7, as reported | — / 53.9 — no | — / 58.1 — yes | mvtools (implied) |
| review 2 | not recorded | 51.99 / **56.92** — **yes** | not reported | **mvgpu** |
| c | mean 4.45 (2.33–11.14) | 49.32 / 53.57 — no | 54.34 / 57.76 — yes | mvtools |
| d | mean 6.08 (3.67–11.13) | 50.17 / 52.77 — no | 59.50 / 56.33 — yes | mvtools |
| e | mean 5.10 (1.94–13.33) | 50.53 / 54.57 — no | 59.26 / 57.56 — yes | mvtools |
| f | mean 3.71 (2.12–10.12) | 50.88 / **55.39** — **yes** | 60.00 / 58.66 — yes | **mvgpu** |
| g | mean 3.69 (2.22–10.67) | 49.48 / 54.99 — no | 59.50 / 58.75 — yes | mvtools |
| h | mean 3.42 (1.79–9.10) | 55.60 / **58.31** — **yes** | 61.02 / 59.69 — yes | **mvgpu** |
| i | mean 3.19 (1.89–10.05) | 49.83 / 54.25 — no | 60.00 / 58.62 — yes | mvtools |
| x | not recorded; two benchmarks at once | 41.74 / 42.71 — no | 23.92 / 32.39 — no | none |

Runs a, b and x are this spike's own and are in `m0s-results.json`; c–i were measured on
2026-10-13 and are there too, each with its load average. "review 1" and "review 2" are
two reproductions reported by the phase-5 review and **not** re-measured here, kept
because they are two of the eleven and one of them is a run that clears the bar; the
cells nobody reported are left empty rather than filled in.

Reading the mvgpu column: 52.77, 52.92, 53.39, 53.57, 53.9, 54.25, 54.57, 54.99, 55.39,
56.92, 58.31 in mpv — **96 % to 106 % of the 55.14 bar, real time in three runs of
eleven**. MVTools: 56.33 to 59.69, real time in all ten runs that reported it, but never
by more than 8 %, and the one overloaded run (x) put it at 32.39.

Load is the largest single influence and it is not the whole story. Over c–i the MVTools
figure tracks the mean load closely (r = −0.93: 56.33 at load 6.08, 59.69 at 3.42), which
is what you would expect of a CPU engine on 8 shared threads. mvgpu tracks it loosely
(r = −0.63), and the two quietest runs of the whole set make the point better than the
correlation does: run h at mean load 3.42 gave 58.31 and run i at mean load 3.19 gave
54.25 — a 1.07× swing straight across the bar with the load average if anything lower.
Whatever else is moving (thermal state, the GPU's own scheduling, what the other 8 threads
were doing in the seconds that matter) is not recorded by `bench` and was not recorded
here either.

So at 1440p neither "MVTools is the only engine that makes it" nor "mvgpu makes it" is a
measurement. What is measured is that both motion-vector engines sit within a few per cent
of the bar, MVTools on the clear side in every run that was not overloaded and mvgpu on
either side, and that `bench`'s recommendation flips with them. RIFE is nowhere near:
3.6–5.4 in mpv across all eleven.

### Which verdicts can be leaned on, and which are not settled

A verdict flips when a run differs from the figures above by the factor in the last
column. The largest load-driven change actually seen on this machine is 0.57× (MVTools
at 1440p, run x) downwards and 1.61× (MVTools at 720p, 160.6 → 258.8 on one day)
upwards, both on the CPU engine; the GPU engines moved less (mvgpu's worst was 0.81×,
also run x).

| size | engine | in mpv | × the bar | needs this much change to flip | verdict |
|---|---|---|---|---|---|
| 1280×720 | mvgpu | 201–205 | 3.6–3.7× | 0.27× | robust |
| 1280×720 | mvtools | 161–259 | 2.9–4.7× | 0.21–0.34× | robust |
| 1280×720 | best RIFE | 17.6–20.1 | 0.32–0.36× | 2.7× | robust |
| 1920×1080 | mvgpu | 89–97 | 1.62–1.76× | 0.57–0.62× | robust, just clear of the one overload |
| 1920×1080 | mvtools | 91–103 | 1.65–1.86× | 0.54–0.61× | the same: run x did 0.57× here |
| 1920×1080 | best RIFE | 8.8–9.3 | 0.16–0.17× | 5.9× | robust |
| 2560×1440 | mvgpu | 52.8–58.3 | 0.96–1.06× | none: the bar is in the range | **not settled** |
| 2560×1440 | mvtools | 56.3–59.7 | 1.02–1.08× | 0.92–0.98× | **marginal**: 10 of 10, by 2–8 % |
| 2560×1440 | best RIFE | 4.7–5.4 | 0.09–0.10× | 10× | robust |
| 3840×2160 | mvgpu | 32.1–32.2 | 0.58× | 1.71× | robust (see below) |
| 3840×2160 | mvtools | 28.6–28.9 | 0.52× | 1.91× | robust (see below) |
| 3840×2160 | RIFE | not measured | — | — | no verdict: the 78 s timeout, not a number |

The two 4K rows were checked rather than assumed, in the only direction that matters:
load can only cost throughput, so 4K can flip only if a quiet machine turns out 1.71×
faster. A second 4K run (pass b) was therefore made deliberately *loaded* — mean 1-minute
load 13.9, peaks to 40.7, against pass a's 3–12 — and it came out at 32.14 and 28.88 in
mpv, within 1 % of pass a's 32.20 and 28.58. At this size the engines are not fighting
for the CPU, the spread between two very differently loaded runs is under 1 %, and 1.61×
is the largest gap ever seen between two runs of the same configuration on this machine
— at 720p, on the engine with the widest spread, where the CoV is 0.157 against
0.008–0.022 at 4K. RIFE at 4K has no verdict at all: every model hit the 78 s timeout, so
the 0.0 in `m0s-results.json` means not measured.

### Against the RTX 4090 figures already in this repository (1080p, 2×, in mpv)

| engine | RTX 4090 (dev box) | Apple M2 | M2 is slower by | the 4090 figure comes from |
|---|---|---|---|---|
| rife-v4.26 | 61.2 | 8.33 | 7.3× | GUI.md §11.9 field report (bench `mpv_fps`) |
| rife-v4.22-lite | 62.6 | 8.79 | 7.1× | GUI.md §11.9 field report |
| rife-v4.18 | 59.4 | 5.96 | 10.0× | GUI.md §11.9 field report |
| mvgpu | ~705 | 89.21 | 7.9× | SCOPE §5.3 (background 1080p speed test) |
| mvtools | 102.3 | 91.07 | **1.12×** | GUI.md §11.9 field report |

| engine | RTX 4090 at 2160p 2× | Apple M2 at 2160p 2× | M2 is slower by |
|---|---|---|---|
| rife-v4.26 | 14.1 out fps (`m0i.md`, profiler) | below 1.23 (timed out) | ≥ 11× |
| mvtools | 35.3 (`m0p.md`, vspipe) / 37.1 (`m0i.md`) | 28.57 (vspipe) | 1.24–1.30× |

Nothing in the two tables above was re-measured on the 4090: the macOS column is
this M2 through `buttereye bench`, the other column is the figure this repository
already carries, and the comparison is a ratio against a number, not a head-to-head.
