# SPDX-License-Identifier: AGPL-3.0-or-later
# ButterEye COPR package (SCOPE §9 package 2). Build: packaging/plugins/build.sh
%global tag r9_mod_v33
# ncnn as pinned by the r9_mod_v33 subprojects/ncnn submodule, and the glslang
# fork pinned by that ncnn commit. Bundled and linked statically by default;
# "--with system_ncnn" builds against Fedora's ncnn instead (M0(f) showed GPU
# faults with gpu_thread >= 2 on ncnn 20250916, see docs/spikes/m0f.md).
%global ncnn_commit    305837fd4a722ebc47c5d72e72d8ec9ae970e932
%global ncnn_date      20250503
%global ncnn_short     305837fd
%global glslang_commit a9ac7d5f307e5db5b8c4fbf904bdba8fca6283bc
%global glslang_short  a9ac7d5f
%bcond system_ncnn 0

Name:           buttereye-vs-rife-ncnn
Version:        9.33
Release:        2%{?dist}
Summary:        RIFE frame interpolation for VapourSynth on ncnn/Vulkan (ButterEye build)

# Plugin MIT (HolyWu); bundled RIFE/VapourSynth4.h LGPL-2.1-or-later;
# RIFE/VSHelper4.h WTFPL.
%if %{with system_ncnn}
License:        MIT AND LGPL-2.1-or-later AND WTFPL
%else
# + bundled ncnn (BSD-3-Clause AND BSD-2-Clause AND Zlib) and glslang
# (BSD-3-Clause AND BSD-2-Clause AND MIT AND Apache-2.0 AND
# GPL-3.0-or-later WITH Bison-exception-2.2), per SCOPE.md §8.2.
License:        MIT AND LGPL-2.1-or-later AND WTFPL AND BSD-3-Clause AND BSD-2-Clause AND Zlib AND Apache-2.0 AND GPL-3.0-or-later WITH Bison-exception-2.2
%endif
URL:            https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan
# Upstream tag repacked without models/ and subprojects/ncnn:
#   ./repack-rife-ncnn.sh <dir>
Source0:        %{name}-%{tag}.tar.gz
Source1:        repack-rife-ncnn.sh
Source2:        https://github.com/Tencent/ncnn/archive/%{ncnn_commit}/ncnn-%{ncnn_commit}.tar.gz
Source3:        https://github.com/nihui/glslang/archive/%{glslang_commit}/glslang-%{glslang_commit}.tar.gz
# Licence texts SCOPE §9 asks for (pinned copies in packaging/plugins/licenses/):
# nihui/rife-ncnn-vulkan (the ncnn RIFE implementation this plugin derives from),
# and the texts for the bundled RIFE/VapourSynth4.h (LGPL-2.1+) and VSHelper4.h (WTFPL).
Source4:        LICENSE.rife-ncnn-vulkan
Source5:        LGPL-2.1.txt
Source6:        WTFPL.txt
# Fedora's ncnn (20250916) removed Option::use_shader_pack8 (system_ncnn only).
Patch0:         buttereye-vs-rife-ncnn-no-pack8.patch
# Reuse host frame buffers (one locked ncnn::PoolAllocator per filter, bounded by
# gpu_thread) instead of three fresh full-frame mmaps per interpolated frame;
# page faults were 25-40 % of vspipe CPU. Output is bit-identical.
Patch1:         buttereye-vs-rife-ncnn-host-pool.patch
# load_param_model's non-Windows branch sprintf'd its two model paths into two
# fixed char[256] stack buffers: a model_path of 242 characters or more aborted
# the process (SIGABRT), taking vspipe or mpv with it. Reachable here, not only
# off Linux. Applied to both builds.
Patch2:         buttereye-vs-rife-ncnn-model-path.patch
# mpv rebuilds the VapourSynth core on every seek; upstream destroyed the ncnn
# GPU instance with its last filter, so each seek paid ~1.3 s to create it and
# load the model again. Keep the instance for the process, pin the module and
# reuse the last loaded model (dropped after a lost device). Frame buffers are
# freed when no filter is left, so the idle cost is the instance and the
# model's weights (~116 MiB). Not yet tested on Windows. Bit-identical.
Patch3:         buttereye-vs-rife-ncnn-keep-instance.patch

BuildRequires:  gcc-c++
BuildRequires:  meson
BuildRequires:  cmake
BuildRequires:  pkgconfig(vapoursynth)
%if %{with system_ncnn}
BuildRequires:  ncnn-devel
# ncnn's CMake config includes glslang's targets (NCNN_SYSTEM_GLSLANG=ON).
BuildRequires:  glslang-devel
%else
Provides:       bundled(ncnn) = 0^%{ncnn_date}git%{ncnn_short}
Provides:       bundled(glslang) = 0^git%{glslang_short}
%endif
# ncnn uses simplevk and dlopens Vulkan: no automatic dependency.
Requires:       vulkan-loader

%description
VapourSynth plugin running RIFE video frame interpolation on any Vulkan GPU
through ncnn. Installed into ButterEye's private plugin directory
(%{_libdir}/buttereye/vapoursynth); it is not autoloaded by VapourSynth.

%prep
%autosetup -N -n %{name}-%{tag}
cp -p %{SOURCE4} %{SOURCE5} %{SOURCE6} .
%if %{with system_ncnn}
%autopatch -p1
%else
%patch -P1 -p1
%patch -P2 -p1
%patch -P3 -p1
mkdir -p subprojects/ncnn/glslang
tar -xzf %{SOURCE2} -C subprojects/ncnn --strip-components=1
tar -xzf %{SOURCE3} -C subprojects/ncnn/glslang --strip-components=1
cp -p subprojects/ncnn/LICENSE.txt LICENSE.ncnn.txt
cp -p subprojects/ncnn/glslang/LICENSE.txt LICENSE.glslang.txt
%endif
# Upstream meson.build has CRLF line endings, so edit with checked seds.
sed -i "s|vapoursynth_dep.get_variable(pkgconfig: 'libdir') / 'vapoursynth'|get_option('libdir') / 'buttereye' / 'vapoursynth'|" meson.build
sed -i "/^install_subdir('models',/,/^)/d" meson.build
grep -q "'buttereye' / 'vapoursynth'" meson.build
if grep -q "install_subdir('models'" meson.build; then exit 1; fi

%build
%meson -Duse_system_ncnn=%{?with_system_ncnn:true}%{!?with_system_ncnn:false}
%meson_build

%install
%meson_install

%check
%if %{with system_ncnn}
# The module must link against the system ncnn.
ldd %{buildroot}%{_libdir}/buttereye/vapoursynth/librife.so | grep -q 'libncnn.so'
%else
# Bundled ncnn is static: no libncnn.so dependency may leak in.
if ldd %{buildroot}%{_libdir}/buttereye/vapoursynth/librife.so | grep -q 'libncnn'; then exit 1; fi
%endif

%files
%license LICENSE
%license LICENSE.rife-ncnn-vulkan LGPL-2.1.txt WTFPL.txt
%if %{without system_ncnn}
%license LICENSE.ncnn.txt LICENSE.glslang.txt
%endif
%doc README.md BUTTEREYE-REPACK.txt
%dir %{_libdir}/buttereye
%dir %{_libdir}/buttereye/vapoursynth
%{_libdir}/buttereye/vapoursynth/librife.so

%changelog
* Thu Oct 15 2026 The ButterEye contributors - 9.33-2
- Keep the GPU instance and the loaded model across mpv's per-seek core
  rebuilds (Patch3): seek -> playback-restart 1.5 s -> 0.25-0.36 s at 640x360.
  Frame buffers (host and GPU) are freed when the last filter goes, so an idle
  RIFE keeps ~116 MiB of VRAM instead of up to 3.7 GiB at 2160p. The instance
  is still destroyed at exit and after a lost device.

* Mon Oct 12 2026 The ButterEye contributors - 9.33-2
- Fix the model-path buffer overflow in load_param_model (Patch2): a model_path
  of 242 characters or more overran parampath into modelpath and aborted the
  process; the paths are std::string now. Output is bit-identical.

* Sat Oct 10 2026 The ButterEye contributors - 9.33-1
- First release package (from the M0(f) spike spec); ship the
  rife-ncnn-vulkan, LGPL-2.1 and WTFPL licence texts

* Thu Oct 08 2026 ButterEye contributors - 9.33-0.3.spike
- Pool host frame buffers in process/process_v4 (Patch1): fewer page faults,
  less kernel CPU, bit-identical output

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.2.spike
- Bundle the pinned ncnn 305837fd + glslang a9ac7d5 (static) by default;
  system ncnn + pack8 patch kept behind --with system_ncnn

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.1.spike
- M0(f) spike build against Fedora ncnn
