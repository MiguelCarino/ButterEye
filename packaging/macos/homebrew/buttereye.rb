# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye's Homebrew tap formula (docs/PLATFORMS.md, the macOS phase table,
# phase 4). A tap and not an .app bundle, and that is a decision about money
# rather than an unwritten feature: notarisation needs a paid Apple Developer ID,
# and without one an unsigned bundle downloaded from a GitHub release arrives
# with com.apple.quarantine and current macOS refuses it outright - the
# right-click bypass is gone from the UI. brew fetches over curl and sets no
# quarantine attribute (measured: xattr -l on a downloaded bottle tarball in
# ~/Library/Caches/Homebrew/downloads and on an installed keg dylib prints
# nothing), so Gatekeeper never gets involved and nothing here needs to be signed
# or notarised. See packaging/macos/SOURCES.md.
#
# Unlike the Windows installer this bundles no runtime. Homebrew's python@3.14 IS
# the interpreter VapourSynth's module is installed for, so `import vapoursynth`
# works in the very interpreter ButterEye runs under - measured below, and the
# reason this formula must keep Homebrew's default system_site_packages.
class Buttereye < Formula
  include Language::Python::Virtualenv

  desc "Smooth-motion (frame interpolation) control center for mpv"
  homepage "https://github.com/MiguelCarino/ButterEye"
  # UNFILLED, on purpose. v0.0.2 is the newest tag and it predates the whole
  # macOS port (phases 0 and 1 land after it, phases 2-4 are not committed), so
  # pointing this at v0.0.2 would install a ButterEye with no macOS platform
  # layer at all. The release step of macOS phase 5 tags 0.0.3 and fills the
  # checksum; the shape is proven - the v0.0.2 archive at this URL shape
  # measures 2 505 842 bytes, sha256
  # 36a5920a4901068df0ef801aa5f13ea6ef577f267743fbf1d0979d19aa88497c. Until
  # then install with --HEAD, which is the only working path.
  url "https://github.com/MiguelCarino/ButterEye/archive/refs/tags/v0.0.3.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  # ButterEye's own code is AGPL-3.0-or-later; the bundled FSRCNNX x2 8-0-4-1 mpv
  # shader (buttereye/data/shaders/, igv release 1.1, unmodified) is
  # LGPL-3.0-or-later. Same expression as packaging/buttereye.spec.
  license all_of: ["AGPL-3.0-or-later", "LGPL-3.0-or-later"]
  head "https://github.com/MiguelCarino/ButterEye.git", branch: "main"

  # arm64 only, and said rather than discovered: every macOS measurement behind
  # phases 0-4 was made on Apple Silicon, MoltenVK is the only Vulkan driver on a
  # Mac, and the prefix on an Intel Mac is /usr/local where nothing has been
  # tried. This prints "The arm64 architecture is required for this software."
  depends_on arch: :arm64
  # ffmpeg: the offline render is `vspipe | ffmpeg` (buttereye/core/render).
  depends_on "ffmpeg"
  # ffms2: the render's source filter, installed into VapourSynth's own autoload
  # directory as libffms2.dylib. Homebrew's bottle; ButterEye never builds it.
  depends_on "ffms2"
  # molten-vk: macOS has no native Vulkan. Both GPU engines reach the GPU through
  # MoltenVK as a portability driver, so without it the doctor's Vulkan section
  # has no device to report.
  # macOS only. The arm64 requirement above is not enough on its own: a Linux
  # aarch64 machine satisfies it, and nothing in this formula would work there.
  depends_on :macos
  depends_on "molten-vk"
  depends_on "mpv"
  # pyside: Homebrew's PySide6 (6.11.2), for `buttereye-gui`. It is the only
  # PySide6 on this platform that is not a wheel: pyproject.toml deliberately
  # declares no Qt dependency so neither this formula nor the Fedora package has
  # pip fetch a Qt of its own. The cost is real and worth stating - the formula
  # pulls 29 dependencies including qtwebengine and llvm - but ButterEye's GUI
  # uses Essentials modules only (SCOPE F17, enforced by a test), so what the
  # extra Qt Addons buy is nothing but disk.
  depends_on "pyside"
  depends_on "python@3.14"
  depends_on "vapoursynth"
  # vapoursynth-mvtools: the no-GPU interpolation path, and the only engine a Mac
  # has today that needs no further build (bottle 29, arm64, measured
  # interpolating at 3484 fps on an M2 - docs/PLATFORMS.md phase 3).
  depends_on "vapoursynth-mvtools"
  # vulkan-loader: libmvgpu.dylib links its libvulkan.1.dylib, and the doctor
  # looks for the loader under the Homebrew prefix.
  depends_on "vulkan-loader"

  # Not dependencies, deliberately:
  #
  #   mkvtoolnix   A RECOMMENDATION, not a dependency. Homebrew has no peer of
  #                rpm's Recommends:, so every dependency here is mandatory, and
  #                mkvmerge is needed for exactly one option of the offline
  #                render (keeping a source's non-video streams - subtitles,
  #                chapters, extra audio). SCOPE §9 has it under Recommends: for
  #                the same reason. It is not small either: 11 direct and 42
  #                recursive runtime dependencies, including qtbase for a GUI
  #                ButterEye never runs. The doctor already prints
  #                `brew install mkvtoolnix` when a render needs it
  #                (commands.install_command), which is the right place for an
  #                optional tool: at the moment it is wanted.
  #   vulkan-tools ButterEye runs `vulkaninfo` to list GPUs and degrades without
  #                it (the doctor says so and falls back to the Vulkan device
  #                list). Same reasoning as mkvtoolnix.
  #   buttereye-vs-mvgpu, buttereye-vs-rife-ncnn, buttereye-rife-ncnn-models
  #                The engines in this tap. ButterEye runs with MVTools alone, a
  #                GPU engine is a choice, and RIFE on a Mac is offline-only
  #                (see its formula), so making either mandatory would force a
  #                long source build on a user who wanted the MVTools path.
  #                caveats names them.

  def install
    # Homebrew's default here is system_site_packages: true, and this formula
    # DEPENDS on it: VapourSynth's Python module is not a wheel this formula
    # could install, it is a .pth in python@3.14's site-packages pointing into
    # the vapoursynth keg's libexec virtualenv. Measured on an arm64 Mac,
    # 2026-10-12: a venv made with --system-site-packages imports vapoursynth
    # (R81, from the Cellar), and the same venv without the flag answers
    # ModuleNotFoundError. Passing system_site_packages: false here would also
    # hide Homebrew's PySide6, so the GUI would stop starting. Do not add it.
    venv = virtualenv_install_with_resources

    doc.install "COPYING", "LICENSES", "README.md", "CHANGELOG.md"
    # `buttereye licence` (SCOPE F18) must be able to show the AGPL text and the
    # §7 generated-output permission. It looks for them beside the installed
    # package (licences.py: _SRC_ROOT, the directory above the buttereye
    # package) and in /usr/share/doc/buttereye, which no keg can provide, so the
    # symlinks put them where it already looks instead of asking for a code
    # change here.
    venv.site_packages.install_symlink doc/"COPYING", doc/"LICENSES"

    # The build scripts and the plugin check travel with the install: a user who
    # wants RIFE or mvgpu built by hand rather than from this tap needs them,
    # and `brew test` below runs the dylib check on whatever is installed.
    pkgshare.install "packaging/macos/build-plugins.sh", "packaging/macos/check-dylibs.sh"

    # Nothing creates lib/buttereye/vapoursynth here, and that is deliberate.
    # types.macos_plugin_dir() returns that directory as soon as it EXISTS and
    # otherwise falls back to Homebrew VapourSynth's autoload directory, where
    # the mvtools.dylib bottle lives. An empty private directory would therefore
    # take the fallback away and leave ButterEye looking at nothing. The plugin
    # formulae in this tap create it, with a plugin in it.
  end

  def caveats
    <<~EOS
      Nothing in this tap is signed or notarised, and nothing needs to be: brew
      fetches over curl, so none of it carries com.apple.quarantine and
      Gatekeeper is never asked. Apple Silicon only.

      Live smoothing in mpv needs one environment variable. mpv 0.41.0 has the
      vapoursynth filter compiled in, but it loads VSScript by dlopen-ing the
      bare name "libvapoursynth-script.dylib", and Homebrew's only copy is
      called libvsscript.dylib inside VapourSynth's libexec virtualenv. dyld
      never searches #{HOMEBREW_PREFIX}/lib for a bare name (measured), so a
      symlink there would not help; mpv's own VSSCRIPT_PATH does:

        vs=$(brew --prefix vapoursynth)/libexec/lib/python3.14/site-packages/vapoursynth
        export VSSCRIPT_PATH="$vs/libvsscript.dylib"

      ButterEye sets it for the mpv and vspipe processes it starts, so `buttereye
      play` and `buttereye render` need nothing from you. The two lines above are
      for running mpv --vf=vapoursynth=... by hand. Point it only at a file that
      exists: when VSSCRIPT_PATH is set, mpv uses it and does not fall back.

      Interpolation engines are separate formulae, because ButterEye runs with
      the MVTools path alone:

        brew install buttereye-vs-mvgpu                                 # GPU motion vectors (Vulkan/MoltenVK)
        brew install buttereye-vs-rife-ncnn buttereye-rife-ncnn-models  # RIFE, offline render only on a Mac

      Optional tools ButterEye asks for when it wants them:

        brew install mkvtoolnix     # offline render keeping subtitles, chapters and extra audio
        brew install vulkan-tools   # vulkaninfo, for the doctor's GPU list

      Start with:

        buttereye doctor
    EOS
  end

  test do
    # Not `version.to_s`: ButterEye's version comes from buttereye/__init__.py,
    # so a --HEAD install prints a release number where version is "HEAD". This
    # asserts the CLI runs and the number is well formed, which is the part a
    # formula can know.
    assert_match(/^ButterEye \d+\.\d+\.\d+$/, shell_output("#{bin}/buttereye --version").lines.first.strip)
    # The two symlinks the install step makes, checked as files rather than
    # through the CLI: `buttereye licence` prints the licence NAME from a
    # constant whether or not it found the texts, so asserting on its output
    # would pass with both symlinks missing. licences.py looks for exactly these
    # two paths, relative to the directory above the installed package.
    refute_empty Dir[libexec/"lib/python3.*/site-packages/COPYING"]
    refute_empty Dir[libexec/"lib/python3.*/site-packages/LICENSES/AdditionRef-*.txt"]
    assert_match "Affero", shell_output("#{bin}/buttereye licence")
    # Homebrew's python@3.14 is the interpreter ButterEye runs under, and the
    # whole macOS port rests on VapourSynth being importable there. If a future
    # Homebrew change breaks that, this is where it shows.
    system libexec/"bin/python", "-c", "import vapoursynth"
  end
end
