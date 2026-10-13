# SPDX-License-Identifier: AGPL-3.0-or-later
# ButterEye COPR package (SCOPE §9 package 8): mvgpu, ButterEye's own GPU block
# motion-vector engine (mvgpu/ in the ButterEye repository). Build:
# packaging/build.sh, which packs Source0 reproducibly from the working tree
# (mvgpu/ plus the repository's COPYING).

Name:           buttereye-vs-mvgpu
Version:        0.0.1
Release:        2%{?dist}
Summary:        GPU block motion-vector frame interpolation for VapourSynth (Vulkan)
License:        AGPL-3.0-or-later
URL:            https://github.com/MiguelCarino/ButterEye
Source0:        %{name}-%{version}.tar.gz

ExclusiveArch:  x86_64
BuildRequires:  gcc-c++
BuildRequires:  meson
BuildRequires:  ninja-build
BuildRequires:  binutils
# GLSL -> SPIR-V at build time: the shaders ship as source, no blobs
BuildRequires:  glslang
BuildRequires:  pkgconfig(vapoursynth)
BuildRequires:  pkgconfig(vulkan)
BuildRequires:  vulkan-headers
# no explicit Requires: libmvgpu.so links libvulkan.so.1 (checked below), so
# rpm generates libvulkan.so.1()(64bit), which the Vulkan loader provides

%description
mvgpu interpolates frames on the GPU through Vulkan compute: a block motion
search between each source pair, per-pixel vector selection and an occlusion
fallback, rendered at any output rate. ButterEye uses it live where RIFE can't
keep up (4K at high rates). Installed into ButterEye's private plugin
directory (%{_libdir}/buttereye/vapoursynth); not autoloaded.

%prep
%autosetup -n %{name}-%{version}

%build
%meson -Dplugindir=%{_lib}/buttereye/vapoursynth
%meson_build

%install
%meson_install

%check
so=%{buildroot}%{_libdir}/buttereye/vapoursynth/libmvgpu.so
test -f "$so"
# a VapourSynth API4 plugin: the entry point is exported
nm -D --defined-only "$so" | grep -qw VapourSynthPluginInit2
# links the Vulkan loader and the C/C++ runtime, nothing else
needed=$(objdump -p "$so" | awk '$1 == "NEEDED" {print $2}' | sort)
echo "NEEDED: $needed"
echo "$needed" | grep -qx 'libvulkan.so.1'
unexpected=$(echo "$needed" | grep -vxE 'libvulkan\.so\.1|libstdc\+\+\.so\.6|libm\.so\.6|libgcc_s\.so\.1|libc\.so\.6' || :)
if [ -n "$unexpected" ]; then echo "unexpected libraries: $unexpected"; exit 1; fi

%files
%license COPYING
%doc README.md
%dir %{_libdir}/buttereye
%dir %{_libdir}/buttereye/vapoursynth
%{_libdir}/buttereye/vapoursynth/libmvgpu.so

%changelog
* Tue Oct 13 2026 The ButterEye contributors - 0.0.1-2
- Retry vkCreateInstance with VK_KHR_portability_enumeration only when the
  loader refuses with VK_ERROR_INCOMPATIBLE_DRIVER (a portability driver such
  as MoltenVK); a native driver answers the first call, so Linux behaviour is
  unchanged. meson.build now states meson_version '>=1.1'.

* Sat Oct 10 2026 The ButterEye contributors - 0.0.1-1
- First package: mvgpu.Interpolate as a live engine for ButterEye
