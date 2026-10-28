<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Changelog

All notable changes to ButterEye are listed here, newest first. Versions follow
[semantic versioning](https://semver.org); while the version starts with `0.`, any
release may change behaviour or settings.

## 0.0.7 — 2026-10-28

A fix for smooth copies of files with names that are not plain English letters.

### Fixed

- **Smooth copies of files with "⧸", "｜", accents or Japanese in their name
  failed at the end** with BE-4007: mkvmerge was given the name cut off at the
  first such character. YouTube titles often have them.

## 0.0.6 — 2026-10-28

Hand-drawn 2D animation now plays smoothly instead of still-still-still-jump.
Scene cuts in dark or similar-looking shots are caught, colours no longer shift
between frames, and seeking with RIFE is about a second faster.

On a Mac, and on a fanless MacBook Air in particular, smooth copies keep the
video's full size and finish sooner, the speed test measures what playback can
hold once the machine is warm, and heat or Low Power Mode no longer teaches
ButterEye that an engine is slow.

### New: 2D animation is treated differently from live action and 3D

- **Held drawings are re-timed.** Animation is usually drawn "on twos" or "on
  threes": each drawing is shown for 2 or 3 frames. Every engine smoothed that as
  if each frame were new motion, so at 2× the picture stood still for three frames
  and then jumped (half of all output steps did not move at all). ButterEye now
  finds drawings held 2–3 frames and replaces the repeats with in-between frames
  before smoothing, so the motion is even. On test clips the motion steps went from
  very uneven to even (step variation 1.00 → 0.05 on twos with RIFE, 1.00 → 0.01
  with MVTools). Live action, motion on ones, long deliberate holds and scene cuts
  are left as they were: output for those is byte-identical.
- **A "Content" setting per profile:** Automatic, Live action and 3D, or 2D
  animation. Automatic re-times held drawings only when the speed test shows room
  for the extra work (up to about 1.7× at 2×). 2D animation always does, and the
  automatic engine choice prefers RIFE, then MVTools, then GPU motion vectors.
  Offline renders re-time unless the profile says live action. If frames drop
  badly while re-timing, it is switched off for the rest of that video. Not on the
  TensorRT engine yet.

### New: picture controls

- **Colour controls** in Details → Settings: brightness, contrast, saturation,
  gamma and hue, applied by mpv after smoothing and saved in `[general.picture]`.
- **Colour LUTs.** Import a `.cube` 3D LUT once (it is copied into ButterEye's own
  folder) and it applies to playback and to smooth copies. It is left out of HDR10
  copies, where it would misread the colours.
- **Debanding strength and grain:** Light (mpv's own) or Strong, and how much
  grain hides what is left.
- **Sharp edges (RAVU),** a third upscaler next to Standard and Sharper: crisp
  edges for much less GPU time than FSRCNNX.
- **A/B view:** the original on the left half, smoothed on the right. Turn it on
  in Details → Settings, or press Alt+C in mpv for the video playing.
- **Anti-aliasing before smoothing** (a profile setting, off by default): stair-
  stepped edges are smoothed with znedi3 before the engine sees them. It runs on
  the CPU and needs the znedi3 plugin; without it the setting does nothing.
- **Even motion** in Details → Settings, both off by default. 2× of a 24 fps film is
  48 fps, which a 60 Hz screen shows as frames held 1-1-1-2, a slight regular
  judder. ButterEye now says so (in mpv's on-screen line, the window and the
  session details). "Blend frames the display can't show evenly" lets mpv blend
  the two frames around each in-between refresh, and "Match my display: even
  motion before size" smooths at a smaller size when that paces every frame
  evenly, instead of doubling at the full size.
- **Battery and heat rules.** A rule can match on battery power and on how hot the
  Mac is (`on_battery`, `thermal_min`, `thermal_max`), and a new built-in Battery
  profile smooths with GPU motion vectors at 2×. Details → Settings → Power offers
  "Use the Battery profile on battery" and "…or when the Mac runs hot". The rule
  tester uses the power state of the moment. The id `battery` is now a built-in
  one: a hand-written `[[profiles]]` entry with `id = "battery"` becomes an
  override of the shipped profile (keys it leaves out take the shipped values,
  and it can be reset but not deleted). ButterEye itself never wrote that id;
  rename such a profile to keep it as your own.
- **Rule frame rates written as decimals** (`fps_max = 23.976`, 29.97, 59.94) now
  mean the NTSC rate, in `config.toml` and in the rule editor; they used to miss
  NTSC films by a hair. `24000/1001` still means exactly that.

### New: smooth copies

- **The engine is chosen by time, at the video's own size.** A copy used to take
  RIFE and, when that was too slow, quietly make the file smaller (a 1080p film
  on an M2 became a 720p file after about 6.5 hours). Each size now shows the
  engine it would use and how long it takes; the best picture that finishes within
  twice the video's length is taken, else the fastest, and GPU motion vectors at
  full size come before RIFE at a smaller one.
- **A finished encode survives a failed copy of the other streams.** The video is
  kept and "Finish the copy" completes it without converting again. MP4 text
  subtitles (`mov_text`) are converted to SRT, which Matroska can hold, so that
  failure is also rarer.
- **10-bit colour** for 8-bit videos (less banding in gradients) with HEVC.
- **Copies pause while a video plays or the speed test runs,** and continue after,
  instead of sharing the GPU and spoiling both.
- **The colour LUT** applies to copies too.

### Improved

- **Scene cuts found by contrast with their surroundings,** not only by a fixed
  threshold. Cuts in dark scenes and between shots on the same backdrop (which
  showed a frame with the character missing) are now found. Fades, flashes and
  drawings on twos/threes are not marked. MVTools now honours cuts too.
- **GPU motion vectors (mvgpu) on flat colours and line art:** motion of 1, 3 or 5
  pixels a frame can now be followed (only even counts before), with a sub-pixel
  step, sharper Catmull-Rom sampling, a median that no longer erases the vectors of
  thin edges, and better handling of uncovered areas. Flat shapes went from about
  25–45 dB to 60–99 dB, and the three real test clips got slightly better too. The
  cost is about 10–35 % more GPU time per frame, depending on the content.
- **MVTools uses half-pixel vectors up to 1080p,** so slow pans and credit rolls no
  longer judder (a 1 px-per-frame pan came out as exact repeats). The speed test
  measures it the same way, and MVTools results from earlier speed tests are
  forgotten once.
- **Identical frames are copied instead of computed:** about 15 % faster on
  animation (more on screen recordings), with no visible difference.
- **The speed test measures what playback holds.** The engines playback chooses
  between (GPU motion vectors, MVTools) are measured first, while the machine is
  cool, and at every size up to 4K; the best of them is then held for 150 s, so a
  fanless Mac's warm speed is what is kept. RIFE is cut short once it is plainly
  far too slow (it took minutes per model). It takes a few minutes in all.
- **One bar for "keeps up".** Setup, the speed test and playback now agree: an
  engine Setup proposes is one playback will use. "Repeatable" is shown but no
  longer changes the recommendation, and the speed test's plot draws the real-time
  bar, with the target as a fainter line.
- **Speed-test results are kept honest.** A result records the versions it was
  measured with and the power and heat state; one made while a video played or a
  copy ran is kept but not used, one from other versions is not used, and one older
  than 60 days is measured again in the background (still used meanwhile). A
  failed in-mpv pass is retried once instead of ruling GPU motion vectors out for
  good.
- **Speed limits learned during playback recover.** A slow scene lowered an
  engine's limit for 30 days and nothing raised it. A clean run now raises it
  again, an unconfirmed limit recovers with age, and nothing is learned while
  macOS reports serious heat or Low Power Mode. Limits learned by earlier versions
  are forgotten once.
- **Sharper (FSRCNNX) only where it shows:** it runs on every smoothed frame, so it
  is left off when the picture is enlarged less than 1.8×, while smoothing at a
  smaller size, and on battery or when hot.
- **Automatic tries GPU motion vectors at a smaller size** before settling for
  MVTools at the full size.
- **Faster seeking with RIFE (Fedora).** The RIFE plugin keeps its GPU instance and
  its model across mpv's seeks: seek to playback 1.5–1.9 s → about 0.3–0.4 s.
  When nothing is playing it holds about 116 MiB of video memory.

### Fixed

- **Chroma shifted half a pixel on interpolated frames** of video whose colour
  samples are centred or top-left (most UHD and HDR10 files), so colour edges
  wobbled between source and in-between frames. Every conversion now uses the
  source's chroma location, with a sharper Spline36 kernel.
- **8-bit offline renders were tagged with the wrong chroma location** (centre),
  so players shifted colour by half a pixel. The source's location is now kept.
- **MVTools showed black frames at the start** of live playback.
- **A film that kept dropping a few frames was left that way.** Automatic
  replaced its engine only after 15 % of frames dropped for 10 s; a 4K film on
  MVTools dropped up to 10 % for half a minute while other programs loaded the CPU,
  and nothing changed. Now 4 % for 30 s moves that video to what keeps up (a
  smaller size, block mode or another engine), without marking the engine as slow
  for other videos.
- **Engines were marked as slow for 30 days by drops that were not theirs.** Drops
  while ButterEye built a TensorRT engine on the same GPU, and near-total drops
  (95–101 %, from an engine that kept up seconds before: a hidden window or another
  program on the GPU), were stored as the engine's speed. On the dev box that
  ruled out GPU motion vectors at every size and TensorRT at 1080p, which left a
  4K film on the CPU. Drops during a TensorRT build now move only that video to
  another engine, near-total drops change nothing (any engine would drop them),
  and what was stored before is forgotten once.
- **An encoder failure was reported as a smoothing-script failure** ("try another
  smoothness setting") when ffmpeg stopped first, for example on a full disk.
- **Variable frame rate was detected where there was none** in MP4s with edit
  lists, which converted them to an odd rate such as 47.89 fps.
- **HDR10 copies made without mkvmerge lacked the container's mastering
  metadata.**
- **A live mpv was reported as "Lost the connection"** after an unexpected error
  in ButterEye (a full disk while writing the smoothing script); the filter is now
  rolled back and the video keeps playing.
- **Quitting mpv during the first seconds of smoothing said "filter failed".**
- **The smoothing on/off key in mpv could switch twice** while ButterEye was busy.
- **ButterEye's own starting mpv was listed as one to attach to.**
- **A slow detach at quit was reported as failed** although it had completed.
- **"mpv not found" suggested `sudo dnf install mpv` on a Mac.**
- **`buttereye render` and `play` with a relative path** failed every copy with
  "The smoothing script failed" (vspipe runs in another folder).
- **"Future exception was never retrieved"** was printed when mpv closed while
  ButterEye was sending it a request.
- **Setup's wording for GPU motion vectors** now says what playback does with them.
- **Saving settings over a hand-edited `config.toml`** keeps a `config.toml.bak`
  of it first (the writer keeps no comments or layout).
- **The status badge used the theme's coloured icon** on desktops with no icon
  theme chosen (qt6ct, bare Hyprland) instead of the glyph in the text colour.

### macOS

- **Copies keep the Mac awake** while they run (the display can still sleep), and
  App Nap no longer slows them down.
- **HEVC and ProRes 422 HQ on the Mac's media engine** (VideoToolbox) for smooth
  copies; HEVC is the default for SDR. A copy made with GPU motion vectors or
  MVTools encodes much faster this way.
- **mpv decodes with VideoToolbox** instead of trying NVIDIA's decoder first.
- **The GUI's log and settings moved to `~/Library`** (Logs/ButterEye, where
  Console.app and Storage see them, and Application Support), from
  `~/.local/state/buttereye`; the old files are moved once.
- **"Open with ButterEye" and dropping a video on the Dock icon** play it.
- **Copies started from Finder use Homebrew's mkvmerge** when mkvtoolnix is
  installed (Finder's PATH has no `/opt/homebrew/bin`), keeping chapters,
  attachments and HDR10 mastering data. Without it, MP4 subtitles become SubRip
  and the copy still succeeds. A build can carry mkvmerge with
  `BUTTEREYE_BUNDLE_MKVMERGE=1`; it is not carried by default because it brings
  61 MiB of Homebrew's Qt and ICU.
- **The speed test measured nothing inside mpv on a Mac.** Its generated source
  has no end, so every engine was handed an endless clip and mvgpu refused it;
  mpv aborted on every pass, the vspipe speed was shown instead and nothing was
  marked real time. The clip is now bounded, and `buttereye bench` prints the
  notices that say when a pass could not be measured.
- **A rebuilt or re-copied ButterEye.app kept its speed test.** Results are tied
  to the bundled programs' contents, not their file dates.
- **The macOS build failed on Homebrew formulae with "@" in their name**
  (openssl@4): their licence entry is spelled `openssl.40.4` in the SBOM.
- **A copy run from Downloads** (macOS runs it from a hidden read-only copy) is
  told to move ButterEye.app to Applications, instead of trying to clear the
  download mark where it can't.
- **No "GPU faults can't be checked" notice** and no 0.3 s wait at the end of
  every GPU session on a Mac.
- **Smaller app:** debug symbols are stripped from the bundled programs.

### Linux and Windows

- **Copies keep the machine awake** while they run: a `systemd-inhibit` lock on
  Linux (shown by `systemd-inhibit --list`), a power request on Windows that also
  covers Modern Standby laptops. The display can still sleep.
- **Copies pause on Windows too** while a video plays or the speed test runs, and
  continue after (they used to keep going at lower priority and share the GPU).
- **Battery and Power Saver are read on Linux and Windows laptops.** On a
  discharging laptop Sharper is left off; with the power saver on (the ACPI
  platform profile on Linux, Energy Saver on Windows) playback keeps more
  headroom and no longer teaches ButterEye that an engine is slow; and Details →
  Settings → Power offers "Use the Battery profile on battery". Desktops don't
  show the Power box.
- **NVIDIA's encoders (NVENC) are test-encoded before they are offered** for a
  copy, as the Mac's media engine is, so a driver that lists `hevc_nvenc` but can't
  run it no longer fails the copy. A failure that looks temporary (the GPU busy or
  out of memory) is tried again next time.
- **ProRes 422 HQ on the CPU** (`prores_ks`) for smooth copies, listed last, when
  ffmpeg has it. The files are very large and the audio is AAC.
- **RIFE needs a speed test first on integrated GPUs,** as on a Mac: until one
  shows it keeps up, live playback uses another engine instead of stalling and
  falling back at the start of the video.
- **A video named on the command line with a relative path** now plays from the
  GUI too (`buttereye-gui clip.mkv`).
- **"Install ffmpeg-free or RPM Fusion's ffmpeg"** was suggested on every system
  when the encoders were missing; it now names the command for this one.

### Linux AppImage

- **The AppImage carries mvgpu,** ButterEye's GPU motion vectors, so it smooths
  4K and high frame rates on the GPU. RIFE still needs the Fedora packages.
- **Its FFmpeg encodes with NVENC** on an NVIDIA machine (SDR copies; HDR10 still
  uses x265 or SVT-AV1). Nothing NVIDIA is bundled: the driver's own libraries
  are used when present.

### Windows

- **"Open with" lists ButterEye** for MP4, MKV, WebM, MOV, AVI and TS files, and
  the video opens in it. Uninstalling removes the entry (only for the copy that
  registered it).
- **The GUI's settings and log moved** next to the core's, to
  `%APPDATA%\ButterEye\gui.ini` and `%LOCALAPPDATA%\ButterEye\state\logs\gui.log`,
  from `~/.local/state/buttereye`; the old files are moved once.
- **The installer refuses a stale plugin build** (mvgpu or RIFE built from older
  sources than the release) and says to run `build-plugins.sh` again.

### Packages

- `buttereye-vs-mvgpu` 0.0.2 (the odd-pixel motion, sub-pixel and edge-safe median
  work above) and `buttereye-vs-rife-ncnn` 9.33-3 (the instance kept across seeks);
  `buttereye` recommends at least these versions.

## 0.0.5 — 2026-10-14

On a Mac, ButterEye now gets the programs inside it past Gatekeeper on its own the
first time it is set up.

### Changed

- **macOS: first-time setup clears the download mark itself.** On its first run from
  ButterEye.app (the simple window's silent setup, the Setup wizard's first check, or
  `buttereye setup`), ButterEye runs
  `xattr -dr com.apple.quarantine <path to>/ButterEye.app` when any program inside the
  app is still marked, so mpv, ffmpeg and vspipe are no longer held behind a prompt
  after the app was opened with System Settings ▸ Open Anyway. It runs only when
  something is marked; a failure is logged and the doctor still names the command.

### Added

- **A Linux AppImage (experimental)** on the release, for distributions other than
  Fedora: one file, mpv included, glibc 2.35 or newer, MVTools on the CPU only.

## 0.0.4 — 2026-10-13

Live smoothing no longer makes frames your screen can't show, which on a 60 Hz
screen also made ButterEye believe a fast enough engine was too slow.

### Fixed

- **"Double (2×)" no longer asks for more frames than your display shows.** A 50 fps
  video on a 60 Hz screen was smoothed to 100 fps; mpv threw 40 of every 100 frames
  away, ButterEye reported "Dropping frames" although the engine kept up, and then
  remembered the engine as too slow. Double and a fixed rate now stop at the display
  rate (50 fps on 60 Hz → 60, which is just as smooth on that screen), and a video
  the screen already shows at full rate plays as it is. Speed limits learned by
  earlier versions are forgotten once, since some came from those dropped frames.
- **A display rate such as 60.0024 Hz is read as 60**, so a 50 fps video goes to
  60 rather than to a rate that shares no frame with it (about 13 % less work with
  MVTools, measured).
- **Automatic prefers GPU motion vectors at a smaller size** when the display can't
  show twice the video's rate, instead of requiring a doubling no screen could show.

## 0.0.3 — 2026-10-13

ButterEye now runs on **macOS** (Apple silicon, experimental), and Automatic no longer
throws away a speed test that recommended GPU motion vectors.

### New: ButterEye for macOS (experimental)

- **One disk image.** `ButterEye-0.0.3-macos-arm64.dmg` holds a self-contained
  `ButterEye.app`: its own Python, Qt and VapourSynth, mpv with the VapourSynth filter,
  FFmpeg, the smoothing plugins (MVTools, GPU motion vectors and RIFE through MoltenVK)
  and the RIFE models. No Homebrew needed. Drag it to Applications. The command line is
  `/Applications/ButterEye.app/Contents/MacOS/buttereye`.
- **Not notarised.** The app is signed ad hoc, not by an Apple Developer ID, so macOS
  refuses to open a downloaded copy until you clear the download mark once:
  `xattr -dr com.apple.quarantine /Applications/ButterEye.app`. ButterEye's system
  checks say so, with that command, if any part of the app is still marked.
- **Needs macOS 27 and an Apple silicon Mac** (M1 or newer). Intel Macs are not
  supported.
- **What smooths live.** MVTools and GPU motion vectors smooth 1080p live on an M2.
  **RIFE is for smooth copies only on a Mac**: it runs at 6–10 fps at 1080p, far below
  live speed, so ButterEye never picks it for live playback unless a speed test on that
  Mac shows it keeps up, or you choose it yourself. At 1440p the CPU and GPU engines are
  right at the edge of real time, and at 4K nothing keeps up yet.
- **What is tested.** Everything was measured on one Mac (M2, macOS 27): the system
  checks, live smoothing, smooth copies, a speed test of every engine and the window.
  Other Macs, other macOS versions and System Settings ▸ Privacy & Security ▸ *Open
  Anyway* have not been tried. TensorRT does not exist on macOS.
- The app writes one line to `~/.config/vapoursynth/vapoursynth.toml` so VapourSynth
  finds the app's Python; it keeps every other line.
- The source code of everything built into the app is published with it
  (`ButterEye-0.0.3-macos-sources.tar`).

### Fixed

- **A speed test that recommended GPU motion vectors is no longer thrown away.**
  Setup and the speed test's top candidates ignored such a run and fell back to the
  first engine in quality order, without a measurement. They now take the best engine
  from the same run that kept up in real time.
- **Storage and Clean work again on Fedora 45.** With Python 3.15 and PySide6 6.11,
  the window's Storage page showed an error instead of the sizes, and Clean and the
  model list failed with BE-9999. ButterEye now works around the Python/PySide6 fault.
- **RIFE no longer crashes on a long model path** (Fedora). A model folder path of 242
  characters or more aborted `vspipe` or mpv with "buffer overflow detected"; fixed in
  `buttereye-vs-rife-ncnn` 9.33-2.
- **GPU motion vectors find MoltenVK-style portability drivers** (`buttereye-vs-mvgpu`
  0.0.1-2). Nothing changes on Fedora or Windows.

## 0.0.2 — 2026-10-11

ButterEye now runs on **Windows** as well as Fedora 44 and 45, and live playback no
longer stutters on 4K films whose engine was chosen too optimistically.

### Fixed

- **4K films no longer get an engine that can't keep up.** ButterEye estimated an
  engine's speed on a bigger picture from a 1080p speed test by pixel count alone;
  real cost grows faster, so a 3840×1616 HDR10 film got RIFE · TensorRT and dropped
  most of its frames. The estimate now accounts for that.
- **ButterEye corrects itself while you watch.** If an engine Automatic chose keeps
  dropping many frames, ButterEye switches to one that keeps up and remembers it for
  that size, so reopening the film doesn't repeat the problem. An engine you chose
  yourself is left alone, as before.

### New: ButterEye for Windows (experimental)

- **One installer.** `ButterEye-0.0.2-setup.exe` installs everything ButterEye needs
  except mpv (`winget install shinchiro.mpv`): its own Python, Qt and VapourSynth, the
  smoothing plugins (RIFE, GPU motion vectors, MVTools), the RIFE models, the GPU list
  tool and the video reader for smooth copies. It installs for your account only,
  without administrator rights, adds "ButterEye" to the Start menu (and the desktop if
  you want), `buttereye.cmd` for the command line and an uninstaller in Settings ▸ Apps.
  Running it again repairs an installation. It needs 64-bit Windows 10 version 1903 or
  newer and Microsoft's Visual C++ runtime, which it offers to download from Microsoft;
  ButterEye's system checks say when it is missing, and the window says so too.
- **Microsoft's terms.** Python's Windows build includes two files of Microsoft's
  runtime; the installer asks you to accept Microsoft's terms for them (everything
  else is free software, with every licence text installed alongside).
- **What is tested.** The installer installs, runs its system checks and smooths
  with MVTools on GitHub's Windows machines, and runs every engine on a real GPU under
  Wine. RIFE and GPU motion vectors on a real Windows PC with a graphics card have not
  been tried yet, the installer is not code-signed (Windows SmartScreen will warn),
  and the experimental TensorRT path and the GPU-fault history are Linux-only for now.
- **Under the hood:** everything that differs between the systems goes through one
  module (`buttereye.core.osapi`: named pipes, job objects, Windows folders and
  locks); the system checks read the installer's record and suggest Windows fixes;
  the plugins are cross-built on Fedora (`packaging/windows/`, with a fix for RIFE's
  model loading on Windows); `tests/windows/` runs under Wine and on GitHub's Windows
  machines. The source code of everything built for Windows is published with it.

## 0.0.1 — 2026-10-10

The first release. ButterEye is in **alpha**: it works on the machines it was tested
on, but expect rough edges. It runs on **Fedora 44 and 45 only** (Linux, x86_64).

### What ButterEye does

- **Smoother playback in mpv.** Drop a video on the window (or run `buttereye play
  FILE`) and it plays in your distribution's own mpv with new in-between frames, so
  motion looks smoother. ButterEye never edits your `mpv.conf`; it starts mpv with its
  own settings and adds a VapourSynth filter while the video plays.
- **Four smoothing engines, picked for your computer:**
  - **RIFE** (a neural network) on the GPU through **Vulkan**, on AMD, Intel or NVIDIA
    cards.
  - **RIFE through NVIDIA TensorRT** (experimental): about four times faster than the
    Vulkan path on the cards it was measured on. You build the TensorRT plugin yourself
    (`contrib/build-vstrt.sh`); once it is set up, ButterEye uses it by default.
    ButterEye never ships or downloads NVIDIA software.
  - **mvgpu**, ButterEye's own GPU motion-vector engine (Vulkan compute): very fast,
    even at 4K and high refresh rates, for videos RIFE can't keep up with live.
  - **MVTools** on the CPU, for computers without a usable GPU.
- **A speed test** measures each engine inside mpv once per GPU, and **Automatic**
  chooses the best engine that keeps up. If a video is too demanding, ButterEye smooths
  it at a smaller size or switches engine instead of stuttering, and it notices a stalled
  or crashed filter and recovers.
- **Frame-rate choices:** double (2×), 60 fps, or match your display's refresh rate.
- **HDR10.** Smooth copies of HDR10 videos (also HDR10+ and Dolby Vision with an HDR10
  base layer) are saved as HDR10 with x265 or SVT-AV1, keeping the mastering data.
  Live smoothing of HDR10 is available as an **experimental** option on the video's row.
- **Smooth copies.** The second drop zone ("Save a smooth copy") renders a smoothed file
  in the background, with progress, time left, cancel, and an optional smaller output
  size. Copies are saved as MKV and keep the audio, subtitles and chapters.
- **Picture options:** Standard, Sharper (the bundled FSRCNNX upscaling shader), Less
  banding (mpv's own debanding), or both. Your own mpv shaders are kept.
- **On-screen info in mpv:** a short line with the frame rates, whether the CPU or the
  GPU smooths, and the engine in use (choose how long it stays and what it shows in Details ▸ Settings).
- **Per-file logs:** every playing video and every smooth copy has **Copy log**, which
  copies what ButterEye and mpv or the encoder logged about that file. Closed or crashed
  players keep their row for a while, with the reason.
- **System checks** (Details ▸ System): mpv, VapourSynth, the plugins, the GPU,
  recent GPU faults and conflicting `mpv.conf` settings, each with a plain explanation
  and the command that fixes it.
- **A `buttereye` command** for everything the window does (`doctor`, `setup`, `play`,
  `render`, `bench`, `profiles`, `licence` and more), with `--json` output.
- **The Carino look:** a dark interface in the Carino Systems visual language (gold
  accents, IBM Plex and Red Hat Display type), keyboard and screen-reader friendly.
- **Fedora 44 and 45 RPMs** for the application and its plugins: `buttereye`,
  `buttereye-vs-rife-ncnn`, `buttereye-rife-ncnn-models`, `buttereye-vs-mvtools` and
  `buttereye-vs-mvgpu`, built from source with networking off.
- **About ▸ Releases and updates** opens this project's releases page; ButterEye itself
  never goes online to check for updates.

### Known limits

- **Linux only, Fedora 44 and 45 only.** macOS and Windows are not supported (see the
  README). Other Linux distributions may work from source but are untested.
- **4K live smoothing** with RIFE needs a very fast GPU (RTX 4080/4090 class with
  TensorRT); on other GPUs ButterEye smooths 4K at a smaller size or uses mvgpu.
- **TensorRT is experimental** and needs NVIDIA's own packages, which NVIDIA does not
  publish for Fedora. The first play at a new size builds an engine (about 30 s at
  1080p); the video plays with the Vulkan path meanwhile.
- **Live HDR** covers HDR10 (PQ) only and is experimental. HLG videos play without
  smoothing; HDR10+ and Dolby Vision dynamic metadata is not carried live. Smooth
  copies refuse HLG, Dolby Vision profile 5 and Dolby Vision without an HDR10 base.
- **Not in this release:** attaching to an mpv you started yourself, acting on
  leftover filters, writing the include line into your `mpv.conf` for you, bug-report
  bundles, model downloads and installing a model from a file, and denoising.
- **No COPR repository yet.** Install the RPMs from the GitHub release (see the
  README); a `dnf copr enable` repository is planned.
- The interface is in English only.
