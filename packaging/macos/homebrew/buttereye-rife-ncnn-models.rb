# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# The curated RIFE ncnn model set (SCOPE §5.4 and §9 package 3), a separate
# component from the plugin because it has its own licence chain and its own
# provenance: packaging/plugins/PROVENANCE-rife-ncnn-models.md records, per
# model directory, the upstream weight release and the ncnn converter, with
# checksums. Only models whose provenance is recorded are packaged.
#
# The files are architecture-independent, so these are the identical bytes the
# COPR package and the Windows installer ship. All four were verified against
# PROVENANCE-rife-ncnn-models.md on arm64 during macOS phase 3
# (packaging/macos/RIFE-SPIKE.md).
class ButtereyeRifeNcnnModels < Formula
  desc "Curated RIFE ncnn models for ButterEye"
  homepage "https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan"
  # UNFILLED, as in buttereye-vs-rife-ncnn.rb and for the same reason: this is
  # upstream tag r9_mod_v33 repacked down to the three curated model directories
  # plus LICENSE and README by packaging/plugins/repack-rife-ncnn.sh, which
  # prints the SHA-256 of what it made. The repack needs GNU tar --sort and this
  # Mac has only bsdtar, so the tarball could not be produced or checksummed
  # here. The release step publishes it and fills this in.
  url "https://github.com/MiguelCarino/ButterEye/releases/download/v0.0.3/buttereye-rife-ncnn-models-r9_mod_v33.tar.gz"
  version "9.33"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  # The weights and the ncnn conversions are MIT throughout, and the chain is
  # four separate copyrights rather than one: the conversions from
  # styler00dollar/VapourSynth-RIFE-ncnn-Vulkan (© HolyWu and contributors), of
  # the RIFE v4.x weights from hzwer/Practical-RIFE (© hzwer), continuing
  # ECCV2022-RIFE (© Megvii Inc.), following nihui/rife-ncnn-vulkan (© 2020
  # nihui). One SPDX identifier, four texts to ship (below).
  license "MIT"

  # Data only - no compiler, no architecture, nothing to link. It is still
  # arm64-only here, because ButterEye on an Intel Mac is unmeasured as a whole
  # and a models formula that installed cleanly where no engine can run would be
  # a misleading success.
  depends_on arch: :arm64
  depends_on :macos

  # The licence texts SCOPE §9 asks for that the upstream tarball does not carry
  # (it has only its own LICENSE), and the provenance note. These are the spec's
  # Source2-Source5, the same pinned copies in packaging/plugins/, fetched here
  # with checksums because a formula cannot read files out of a repository it is
  # not building. They are not optional: conveying the models without them is the
  # licence failure, not a missing nicety.
  #
  #   LICENSE.Practical-RIFE     the weights' origin (© hzwer)
  resource "LICENSE.Practical-RIFE" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/LICENSE.Practical-RIFE"
    sha256 "7932fb49341512b959b1744a6d9cbb39e5a1ec89da438a34d0454d5d8df9fecd"
  end

  #   LICENSE.ECCV2022-RIFE      the research code the weights continue (© Megvii)
  resource "LICENSE.ECCV2022-RIFE" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/LICENSE.ECCV2022-RIFE"
    sha256 "f629619f078e3ed699f1ea401248cb7796c36334e3b8ec0eb2c082eceaa5f084"
  end

  #   LICENSE.rife-ncnn-vulkan   the ncnn implementation they follow (© nihui)
  resource "LICENSE.rife-ncnn-vulkan" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/licenses/LICENSE.rife-ncnn-vulkan"
    sha256 "a73beab18143600af0b10c6050a953ec233775ce31c2bf3373a794db535329fd"
  end

  #   the provenance chain itself: which model came from where, with checksums
  resource "PROVENANCE-rife-ncnn-models.md" do
    url "https://raw.githubusercontent.com/MiguelCarino/ButterEye/v0.0.3/packaging/plugins/PROVENANCE-rife-ncnn-models.md"
    sha256 "e26b872626b9304594d6a93dd46c127f012b48519dc657dcc9f7fc7499088568"
  end

  def install
    # <prefix>/share/buttereye/rife-ncnn-models is where ButterEye looks on
    # macOS: types._packaged_data_dir() answers <brew prefix>/share/buttereye
    # with no fallback, and checks_pkgs appends rife-ncnn-models. The analogue of
    # Fedora's %{_datadir}/buttereye/rife-ncnn-models.
    #
    # The directory NAMES are load-bearing and must not be renamed: the plugin
    # reads the padding (64 for v4.26, 128 for v4.22-lite) and the v2/v4
    # architecture out of the name, so a tidied-up name produces wrong frames
    # rather than an error.
    (share/"buttereye/rife-ncnn-models").install Dir["models/*"]
    doc.install "README.md", "BUTTEREYE-REPACK.txt"
    (doc/"licenses").install "LICENSE"  # the tag's own, out of the tarball
    resources.each do |r|
      r.stage { (doc/"licenses").install Dir["*"] }
    end
  end

  def caveats
    <<~EOS
      Models in
        #{HOMEBREW_PREFIX}/share/buttereye/rife-ncnn-models
      which is where `buttereye models` and the doctor look. Do not rename the
      model directories: the plugin reads the padding and the architecture out of
      the directory name.

      They need the engine, which is a separate formula:

        brew install buttereye-vs-rife-ncnn

      On Apple Silicon that engine is for offline render, not live playback.
    EOS
  end

  test do
    models = HOMEBREW_PREFIX/"share/buttereye/rife-ncnn-models"
    # The three the repack curates (SCOPE §5.4; v4.25-lite was dropped on
    # 2026-10-07 after two Xid 109 GPU faults, docs/spikes/m0f.md).
    ["rife-v4.26_ensembleFalse", "rife-v4.22_lite_ensembleFalse",
     "rife-v4.18_ensembleFalse"].each do |dir|
      # Both files, not just the directory: a model missing flownet.bin loads as
      # a network graph that is not ready and renders black frames.
      assert_path_exists models/dir/"flownet.param"
      assert_path_exists models/dir/"flownet.bin"
    end
  end
end
