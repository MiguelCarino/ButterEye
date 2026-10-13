# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# mvgpu: ButterEye's own GPU motion-vector engine, a VapourSynth plugin in Vulkan
# compute (mvgpu/ in the ButterEye repository). This is the one plugin in the tap
# that is ButterEye's own code, which is also why its source duty is the
# simplest: the tarball this formula builds IS the Corresponding Source.
#
# Measured working natively on arm64 (docs/PLATFORMS.md, macOS phase 3): Apple
# clang compiles it with no source change, Homebrew's glslang turns all seven
# compute shaders into SPIR-V, VapourSynth R81 loads the result, and on MoltenVK
# 1.4.2 (M2) the in-between frame is bit-exact against the geometric truth. The
# one macOS-specific thing it needed was a code change that is already in
# mvgpu/src/mvgpu.cpp: MoltenVK's ICD declares itself a portability driver, so
# the Vulkan loader hides it until an application asks for
# VK_KHR_portability_enumeration, and mvgpu retries with that bit set after - and
# only after - a plain vkCreateInstance answers VK_ERROR_INCOMPATIBLE_DRIVER.
class ButtereyeVsMvgpu < Formula
  desc "GPU motion-vector interpolation plugin for VapourSynth (ButterEye)"
  homepage "https://github.com/MiguelCarino/ButterEye"
  # UNFILLED, for the reason buttereye.rb gives at length, and here it is not
  # only about the macOS platform layer: the portability retry described above is
  # NOT in v0.0.2 (measured - `git show v0.0.2:mvgpu/src/mvgpu.cpp` has no
  # portability_enumeration), so a formula built from that tag would produce a
  # libmvgpu.dylib that cannot create a Vulkan instance on any Mac. The release
  # step fills this; until then, --HEAD.
  url "https://github.com/MiguelCarino/ButterEye/archive/refs/tags/v0.0.3.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "AGPL-3.0-or-later"
  head "https://github.com/MiguelCarino/ButterEye.git", branch: "main"

  depends_on "glslang" => :build
  depends_on "meson" => :build
  depends_on "ninja" => :build
  depends_on "pkgconf" => :build
  depends_on "vulkan-headers" => :build
  depends_on arch: :arm64
  depends_on :macos
  # molten-vk is the Vulkan driver on a Mac; without it the plugin loads, finds
  # no device and refuses the engine.
  depends_on "molten-vk"
  # vapoursynth at build time for its headers (pkg-config vapoursynth) and at run
  # time because VapourSynth is what loads this file.
  depends_on "vapoursynth"
  depends_on "vulkan-loader"

  def install
    # mvgpu's plugindir option defaults to Fedora's lib64/buttereye/vapoursynth.
    # <prefix>/lib/buttereye/vapoursynth is what ButterEye looks in on macOS
    # (types.macos_plugin_dir, and checks_pkgs reads libmvgpu.dylib there), the
    # analogue of Fedora's %{_libdir}/buttereye/vapoursynth and of the Windows
    # installer's <install>\vapoursynth: a private, NON-autoloaded directory, so
    # a generated script names the copy it loads instead of inheriting whatever
    # VapourSynth autoloads (SCOPE §7.1).

    # KNOWN, MEASURED, AND NOT FIXABLE FROM A FORMULA - read before publishing
    # this tap. types.macos_plugin_dir() returns this directory as soon as it
    # EXISTS, and otherwise falls back to Homebrew VapourSynth's autoload
    # directory, where the vapoursynth-mvtools bottle's mvtools.dylib lives. So
    # installing this formula moves ButterEye's idea of "the plugin directory"
    # off MVTools. Measured on 2026-10-12 against a mirrored Homebrew prefix with
    # only this directory added, everything else identical:
    #
    #   before   packages.mvtools  ok        MVTools plugin installed
    #   after    packages.mvtools  degraded  The MVTools plugin is not installed
    #            packages.plugins  BLOCKING  No interpolation plugin is installed
    #
    # Smoothing itself keeps working - the generated script's _load() skips
    # LoadPlugin for a namespace that is already there, and Homebrew autoloads
    # mvtools, so `probe.plugins` stayed ok in both runs - but the doctor hands
    # the user a blocking verdict about a plugin that is installed and running.
    # The fix belongs in buttereye/core (macos_plugin_dir answering a SEARCH PATH
    # rather than one directory, or checks_pkgs asking the Cellar for MVTools
    # before it looks on disk), not here: a formula cannot install into another
    # formula's keg, and symlinking mvtools.dylib into this directory would be
    # worse than the bug, because then VapourSynth would have the namespace twice
    # and the explicit LoadPlugin would fail.
    system "meson", "setup", "build", "mvgpu",
           "-Dplugindir=lib/buttereye/vapoursynth",
           *std_meson_args
    system "meson", "compile", "-C", "build", "--verbose"
    system "meson", "install", "-C", "build"

    # AGPL-3.0-or-later, and the Corresponding Source is the tarball this was
    # built from - so ship it rather than only promise it, the way
    # packaging/macos/build-plugins.sh puts sources/mvgpu in its output.
    (pkgshare/"sources").install "mvgpu"
    pkgshare.install "packaging/macos/check-dylibs.sh"
    doc.install "COPYING", "packaging/macos/SOURCES.md"
  end

  def caveats
    <<~EOS
      libmvgpu.dylib is installed in
        #{HOMEBREW_PREFIX}/lib/buttereye/vapoursynth
      which is ButterEye's own plugin directory and is deliberately NOT one
      VapourSynth autoloads: ButterEye loads the plugin by path.

      It reaches the GPU through MoltenVK. `buttereye doctor` reports the device
      it found; `vulkaninfo` (brew install vulkan-tools) is the second opinion.
    EOS
  end

  test do
    plugin = HOMEBREW_PREFIX/"lib/buttereye/vapoursynth/libmvgpu.dylib"
    assert_path_exists plugin

    # The same gate packaging/macos/build-plugins.sh runs on its own output:
    # arm64 Mach-O, an exported _VapourSynthPluginInit2, and nothing linked
    # beyond macOS itself and a short list of Homebrew formulae a macOS build may
    # link - through the versionless opt/<formula> symlink, so a brew upgrade
    # does not break the plugin. It asks whether this file would still work on a
    # machine that is not the build machine.
    system pkgshare/"check-dylibs.sh", plugin.dirname

    # And the question the check cannot answer: does VapourSynth load it? The
    # namespace is mvgpu's own; a load failure raises.
    (testpath/"load.py").write <<~PYTHON
      import vapoursynth as vs
      vs.core.std.LoadPlugin(#{plugin.to_s.inspect})
      print(vs.core.mvgpu)
    PYTHON
    system Formula["python@3.14"].opt_bin/"python3.14", testpath/"load.py"
  end
end
