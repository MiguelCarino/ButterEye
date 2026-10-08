# SPDX-License-Identifier: AGPL-3.0-or-later
# M0(f) spike spec — throwaway, see ../README.md.
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
Release:        0.2.spike%{?dist}
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
# Fedora's ncnn (20250916) removed Option::use_shader_pack8 (system_ncnn only).
Patch0:         buttereye-vs-rife-ncnn-no-pack8.patch

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
%if %{with system_ncnn}
%autopatch -p1
%else
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
%if %{without system_ncnn}
%license LICENSE.ncnn.txt LICENSE.glslang.txt
%endif
%doc README.md BUTTEREYE-REPACK.txt
%dir %{_libdir}/buttereye
%dir %{_libdir}/buttereye/vapoursynth
%{_libdir}/buttereye/vapoursynth/librife.so

%changelog
* Wed Oct 07 2026 ButterEye contributors - 9.33-0.2.spike
- Bundle the pinned ncnn 305837fd + glslang a9ac7d5 (static) by default;
  system ncnn + pack8 patch kept behind --with system_ncnn

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.1.spike
- M0(f) spike build against Fedora ncnn
