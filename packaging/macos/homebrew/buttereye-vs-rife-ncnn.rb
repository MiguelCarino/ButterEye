# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# RIFE-ncnn-Vulkan for VapourSynth, ButterEye's build. The peer of
# packaging/plugins/buttereye-vs-rife-ncnn.spec, and the spec stays the authority
# for the licence expression and the patch set; this formula is the macOS
# recipe for the same source.
#
# Everything here was measured in packaging/macos/RIFE-SPIKE.md (macOS phase 3):
# the pinned commit builds natively on arm64, loads into VapourSynth R81, finds
# the GPU through MoltenVK and renders a correct in-between frame (mean absolute
# error 0.0045 against the geometric truth, where blending the two neighbours
# scores 0.0331).
#
# TWO DECISIONS THIS FORMULA MAKES, both of which cost something:
#
# 1. Homebrew's ncnn, not the pinned static one the Fedora package bundles.
#    The Fedora package bundles ncnn 305837fd and glslang a9ac7d5f and links them
#    statically for two reasons (SCOPE §8.2): reproducibility, because the SRPM
#    carries the exact bytes, and GPU faults that Fedora's newer ncnn produced on
#    NVIDIA at gpu_thread >= 2. The second reason is gone here - an M2 exposes one
#    compute queue, so gpu_thread is pinned at 1 whatever ncnn does, and 8 of 8
#    1080p runs were clean on both builds. The first is not gone, and a third cost
#    appears: the two ncnns do not agree about pixels. Same clip, same model, four
#    interpolated 640x368 RGBS frames, compared sample by sample: 21.3% of samples
#    differ at all, 1.8% by more than one 8-bit step, a handful by half the range.
#    Visually a thin halo on moving edges, not a wrong frame - but it is not the
#    Linux output, and RIFE-SPIKE.md proved the cause is the ncnn version and not
#    ButterEye's patch (the bundled build with and without the no-pack8 patch is
#    bit-identical). Against that: the binary is 35x smaller, the build is 4
#    ninja targets instead of 466, brew does the security updates, and the licence
#    expression collapses from eight licences to three because nothing is linked
#    statically any more. A tap that bundles a 2025 ncnn while Homebrew ships
#    `ncnn` as a maintained formula everything else links is fighting the
#    platform. So: Homebrew's ncnn, and the docs say out loud that it is a
#    different product (packaging/macos/SOURCES.md, and caveats below).
#    The bundled variant is proven to build on a Mac and stays the fallback if
#    reproducibility ever outweighs this; it would need the two pinned tarballs
#    as resources, -Duse_system_ncnn=false, and the ncnn and glslang licence
#    texts and licence expression added here.
#
# 2. The source is ButterEye's repack, not the upstream tag archive.
#    The upstream tag carries all 74 model directories, about 1.4 GB, and the
#    ncnn submodule; packaging/plugins/repack-rife-ncnn.sh is what strips them,
#    reproducibly, and its output is what the SRPM already carries. A formula
#    cannot do that itself (no sparse checkout in any brew download strategy,
#    and the repack needs GNU tar's --sort, which a Mac does not have), so this
#    formula consumes the same tarball the release step publishes.
#    THIS NEEDS AN OWNER DECISION and must not be read as settled: SCOPE §8.1
#    says "the COPR is the one distribution channel for third-party code". A tap
#    that conveys repacked RIFE source adds a second one. The duty itself is met
#    either way - the tarball IS the Corresponding Source and travels with a
#    checksum - but the policy sentence is the owner's to change.
class ButtereyeVsRifeNcnn < Formula
  desc "RIFE frame interpolation for VapourSynth on ncnn/Vulkan (ButterEye build)"
  homepage "https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan"
  # UNFILLED: this asset does not exist yet. It is upstream tag r9_mod_v33,
  # commit c3ec6aabc07c8fa37a4f58d7fed9e2ad1fc1b13f, repacked without models/ and
  # subprojects/ncnn by packaging/plugins/repack-rife-ncnn.sh - the same bytes the
  # COPR SRPM carries, which that script prints the SHA-256 of. It could not be
  # produced on the Mac this was written on: the repack needs GNU tar --sort and
  # this machine has only bsdtar 3.5.3 (measured). The release step publishes it
  # and fills the checksum.
  url "https://github.com/MiguelCarino/ButterEye/releases/download/v0.0.3/buttereye-vs-rife-ncnn-r9_mod_v33.tar.gz"
  version "9.33"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  # Plugin MIT (HolyWu, deriving from nihui/rife-ncnn-vulkan, MIT);
  # bundled RIFE/VapourSynth4.h LGPL-2.1-or-later; RIFE/VSHelper4.h WTFPL.
  # This is the spec's `--with system_ncnn` expression, and it is the right one
  # here because nothing is linked statically. "MIT" alone would be wrong.
  license all_of: ["MIT", "LGPL-2.1-or-later", "WTFPL"]

  depends_on "meson" => :build
  depends_on "ninja" => :build
  depends_on "pkgconf" => :build
  depends_on arch: :arm64
  # libomp is keg-only, which is the whole reason the flags below are explicit.
  depends_on "libomp"
  depends_on :macos
  depends_on "molten-vk"
  depends_on "ncnn"
  depends_on "vapoursynth"
  depends_on "vulkan-loader"

  # The licence texts SCOPE §9 asks for that the tarball does not carry (it has
  # only its own LICENSE): the implementation this derives from, and the texts
  # for the two bundled headers. These are the spec's Source4-Source6, the same
  # pinned copies in packaging/plugins/licenses/, fetched here with checksums
  # because a formula cannot read files out of a repository it is not building.
  # Conveying the plugin without them is the licence failure, not a missing
  # nicety - packaging/macos/build-plugins.sh refuses a librife.dylib that
  # arrives without them, and the same duty applies to a keg.
  resource "LICENSE.rife-ncnn-vulkan" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/LICENSE.rife-ncnn-vulkan"
    sha256 "a73beab18143600af0b10c6050a953ec233775ce31c2bf3373a794db535329fd"
  end

  resource "LGPL-2.1.txt" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/LGPL-2.1.txt"
    sha256 "20e50fe7aae3e56378ebf0417d9de904f55a0e61e4df315333e632a4d3555d95"
  end

  resource "WTFPL.txt" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/WTFPL.txt"
    sha256 "96f17857f3eb28a7d93dad930bc099a3cb65a9a2afb37069bfd1ba5ec5964389"
  end

  # ButterEye's existing patches, the two the Fedora `--with system_ncnn` build
  # already applies, both measured applying to this commit with no fuzz
  # (RIFE-SPIKE.md). Unlike the Fedora spec, the no-pack8 patch is applied here
  # whatever ncnn is used: elempack == 8 never reaches the custom warp layer
  # through MoltenVK, so against the pinned ncnn it is bit-identical, and against
  # Homebrew's (which, like Fedora's, has dropped Option::use_shader_pack8) it is
  # required to compile at all.
  patch do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/buttereye-vs-rife-ncnn-no-pack8.patch"
    sha256 "c8939b15c42bf488682c2de97a1ac83035656906da01b2c1475c9bd6db184eae"
  end
  patch do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/buttereye-vs-rife-ncnn-host-pool.patch"
    sha256 "a043cee798dc5a7e5070986f08b1e9bf41701eca0f6f67caf9494f0a5a5fddff"
  end
  # The third patch, and it is not cosmetic: upstream's load_param_model sprintfs
  # two paths into two fixed char[256] stack buffers from the model directory
  # ButterEye passes straight through from the user's model_path. Measured
  # threshold exactly 242 characters; past it parampath overruns modelpath and the
  # process ABORTS with SIGABRT (exit 134), taking vspipe or mpv with it. It is
  # the #else branch of upstream's #if _WIN32 split, which is also the branch
  # Fedora builds, so this is not a macOS bug and the patch is applied
  # unconditionally in the spec too.
  # NEEDS RE-CHECKING AT RELEASE, unlike the two above. This patch is new in
  # this phase and is not committed yet, so the checksum below is of the file as
  # it stands in the working tree; the two above are the checksums of committed
  # blobs (verified against v0.0.2) and cannot move. The release step must
  # recompute this one from the tag it publishes.
  patch do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/buttereye-vs-rife-ncnn-model-path.patch"
    sha256 "6ac8359af9e64d4cba22e3fbbbd3817eec91cd69e06c21f5b93b22aceae3de05"
  end

  # These three, in this order, and the Windows-paths patch deliberately left out
  # (it is #if _WIN32 only), are exactly what packaging/macos/build-rife.sh
  # applies and what buttereye-vs-rife-ncnn.spec numbers. The two build paths
  # have to agree, because the developer build and the installed build should not
  # be different plugins; if that script's patch set or meson flags change, this
  # formula changes with it. One difference that cannot be closed here:
  # build-rife.sh passes `patch -F0`, so a hunk that only nearly matches fails
  # rather than landing somewhere plausible, and Homebrew's patch DSL offers no
  # way to ask for that.

  def install
    # The two edits buttereye-vs-rife-ncnn.spec's %prep makes, for the same two
    # reasons. First: Homebrew's vapoursynth.pc is three lines (prefix,
    # includedir, Cflags) with no libdir, because VapourSynth lives in a libexec
    # virtualenv, so upstream's get_variable(pkgconfig: 'libdir') makes meson
    # fail outright - and ButterEye wants its own private, non-autoloaded
    # directory anyway (SCOPE §7.1). Second: the models are a separate component
    # and the repacked tarball has no models/ for install_subdir to find.
    inreplace "meson.build" do |s|
      s.sub!("vapoursynth_dep.get_variable(pkgconfig: 'libdir') / 'vapoursynth'",
             "get_option('libdir') / 'buttereye' / 'vapoursynth'")
      s.gsub!(/^install_subdir\('models',.*?^\)\n/m, "")
    end
    # The spec checks the same thing and fails the build if the sed missed: an
    # install_subdir left in place would make `meson install` fail on a directory
    # the repack removed, with a message about a missing directory rather than
    # about this edit.
    odie "the install_subdir('models') edit did not apply" if \
      File.read("meson.build").include?("install_subdir('models'")

    # This installs into <prefix>/lib/buttereye/vapoursynth too, so it triggers
    # the same measured doctor regression buttereye-vs-mvgpu.rb documents at
    # length: creating that directory takes macos_plugin_dir() off Homebrew's
    # autoloaded MVTools and the doctor then reports a blocking "No interpolation
    # plugin is installed" on a machine where MVTools is installed and running.
    # Read that comment before publishing either formula; the fix is in
    # buttereye/core, not in a formula.

    # The OpenMP flags RIFE-SPIKE.md found, and they are not optional. warp.cpp
    # has a `#pragma omp parallel for` for the CPU warp path; the plugin's own
    # meson.build hands clang `-Xpreprocessor -fopenmp` as COMPILE args with no
    # link args, and the result still links and still loads because meson gives
    # a shared_module `-undefined dynamic_lookup` and the CPU path is never taken
    # while Vulkan works. Measured: force the binding with RTLD_NOW and the
    # unflagged build fails on '___kmpc_for_static_fini'. So a GPU that failed to
    # create a pipeline would abort instead of falling back to the CPU - a bomb
    # with no fuse, which these two flags remove.
    omp = Formula["libomp"]
    system "meson", "setup", "build",
           "-Duse_system_ncnn=true",
           "-Dcpp_args=-I#{omp.opt_include}",
           "-Dcpp_link_args=-L#{omp.opt_lib} -lomp",
           *std_meson_args
    system "meson", "compile", "-C", "build", "--verbose"
    system "meson", "install", "-C", "build"

    # -DVulkan_LIBRARY is measured NOT needed: it only reaches ncnn's cmake
    # subproject, which this build does not have, and even there RIFE's own
    # dependency('vulkan') is what puts -lvulkan on the final link.

    # Everything this conveys, with its licence beside it: the tag's own LICENSE
    # out of the tarball and the three texts above.
    (doc/"licenses").install "LICENSE"
    resources.each do |r|
      r.stage { (doc/"licenses").install Dir["*"] }
    end

    # No source tree is copied into the keg, and nothing needs to be: the
    # Corresponding Source of this plugin is this formula's `url` plus the three
    # patch URLs above, every one of them checksummed, and
    # `brew fetch --build-from-source buttereye-vs-rife-ncnn` retrieves exactly
    # that set. packaging/macos/SOURCES.md says so in as many words. (The
    # contrast with buttereye-vs-mvgpu, which does ship its source in the keg, is
    # not inconsistency: that one is ButterEye's own AGPL code, where leaving no
    # room for doubt is worth a megabyte.)
  end

  def caveats
    <<~EOS
      librife.dylib is installed in
        #{HOMEBREW_PREFIX}/lib/buttereye/vapoursynth
      ButterEye's own plugin directory, which VapourSynth does not autoload.

      The models are a separate formula and RIFE does not run without them:

        brew install buttereye-rife-ncnn-models

      OFFLINE RENDER ONLY on Apple Silicon. Measured on an M2: 8.5-9.8 fps at
      1080p, against the roughly 48 fps that live 2x 24p playback needs, so
      `buttereye render` uses it and live playback should not. ncnn exposes one
      compute queue on this hardware, so gpu_thread must be 1; higher values are
      refused by ncnn with a warning and are slower.

      This is built against Homebrew's ncnn, which is NOT the pinned static ncnn
      the Fedora package bundles. The output differs: 21.3% of samples differ at
      all and 1.8% by more than one 8-bit step against the pinned build, which
      looks like a thin halo on moving edges. Do not compare a render from here
      with a Linux render and expect identical pixels.
    EOS
  end

  test do
    plugin = HOMEBREW_PREFIX/"lib/buttereye/vapoursynth/librife.dylib"
    assert_path_exists plugin

    # No versioned Cellar path may be baked in, or a brew upgrade of ncnn or
    # libomp breaks the plugin. This is the one rule of
    # packaging/macos/check-dylibs.sh that this formula can check on its own.
    refute_match "/Cellar/", shell_output("otool -L #{plugin}")

    (testpath/"load.py").write <<~PYTHON
      import vapoursynth as vs
      vs.core.std.LoadPlugin(#{plugin.to_s.inspect})
      print(vs.core.rife)
    PYTHON
    system Formula["python@3.14"].opt_bin/"python3.14", testpath/"load.py"
  end
end
