# SPDX-License-Identifier: AGPL-3.0-or-later
# ButterEye COPR package (SCOPE §9 package 8): mvgpu, ButterEye's own GPU block
# motion-vector engine (mvgpu/ in the ButterEye repository). Build:
# packaging/build.sh, which packs Source0 reproducibly from the working tree
# (mvgpu/ plus the repository's COPYING).

Name:           buttereye-vs-mvgpu
Version:        0.0.4
Release:        1%{?dist}
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

The package also carries VK_LAYER_BUTTEREYE_frames, an explicit Vulkan
layer built from the same engine that makes the last in-between frames when
mpv presents them (experimental, Wayland). The Vulkan loader lists an
explicit layer but never loads it by itself: only an mpv that ButterEye
starts with its GPU frames setting on asks for it.

%prep
%autosetup -n %{name}-%{version}

%build
%meson -Dplugindir=%{_lib}/buttereye/vapoursynth -Dlayer=enabled
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
# the frames layer: it exports the loader's negotiation entry point and never links
# the loader (every call goes down the chain), only the C/C++ runtime
layer=%{buildroot}%{_libdir}/buttereye/libVkLayer_buttereye_frames.so
test -f "$layer"
nm -D --defined-only "$layer" | grep -qw vkNegotiateLoaderLayerInterfaceVersion
needed=$(objdump -p "$layer" | awk '$1 == "NEEDED" {print $2}' | sort)
echo "layer NEEDED: $needed"
unexpected=$(echo "$needed" | grep -vxE 'libstdc\+\+\.so\.6|libm\.so\.6|libgcc_s\.so\.1|libc\.so\.6' || :)
if [ -n "$unexpected" ]; then echo "unexpected libraries in the layer: $unexpected"; exit 1; fi
# its manifest: explicit (the loader's explicit_layer.d, no enable_environment, which
# only an implicit layer has) and naming the installed library by its absolute path
manifest=%{buildroot}%{_datadir}/vulkan/explicit_layer.d/VkLayer_buttereye_frames.json
cat "$manifest"
grep -q '"name": "VK_LAYER_BUTTEREYE_frames"' "$manifest"
grep -qF '"library_path": "%{_libdir}/buttereye/libVkLayer_buttereye_frames.so"' "$manifest"
if grep -q 'enable_environment' "$manifest"; then echo "the layer manifest must stay explicit"; exit 1; fi
test ! -e %{buildroot}%{_datadir}/vulkan/implicit_layer.d
test ! -e %{buildroot}%{_sysconfdir}/vulkan

%files
%license COPYING
%doc README.md
%dir %{_libdir}/buttereye
%dir %{_libdir}/buttereye/vapoursynth
%{_libdir}/buttereye/vapoursynth/libmvgpu.so
%{_libdir}/buttereye/libVkLayer_buttereye_frames.so
# explicit_layer.d itself belongs to vulkan-loader, which libvulkan.so.1 pulls in
%{_datadir}/vulkan/explicit_layer.d/VkLayer_buttereye_frames.json

%changelog
* Sat Nov 14 2026 The ButterEye contributors - 0.0.4-1
- Ships the explicit Vulkan layer VK_LAYER_BUTTEREYE_frames, which makes the
  last in-between frames on the graphics card as each picture goes to the
  screen; it loads only into an mpv that ButterEye starts with the option on.
- The motion-vector engine is split out of the VapourSynth plugin so the
  plugin and the layer share it; the plugin's output is bit-identical.
* Thu Oct 29 2026 The ButterEye contributors - 0.0.3-1
- Where the vectors fail over an area the in-between frame fades to a blend;
  a block vector replaces the smooth one only when clearly better, so film
  grain no longer speckles; global brightness changes are compensated in the
  search, median, subpel and select passes.
* Wed Oct 28 2026 The ButterEye contributors - 0.0.2-1
- Match odd-pixel motion: the symmetric search stored the half-vector as whole
  pixels, so pans of 1, 3 or 5 px a frame blurred or fell back to the nearer
  frame. The search now counts whole pixels of motion at full size, a new
  subpel.comp pass adds a +-1/4 px step and render.comp samples Catmull-Rom.
- The full-size 3x3 median picks one of the nine neighbour vectors, and only
  when it matches the block about as well, so thin edges and small flat objects
  keep their vectors. The occlusion fallback takes the earlier frame where
  vectors converge and the later one where they spread.
- 10-35 % more GPU time per frame; MVGPU_SUBPEL=0 and MVGPU_MEDIAN=0 switch the
  new passes off.

* Tue Oct 13 2026 The ButterEye contributors - 0.0.1-2
- Retry vkCreateInstance with VK_KHR_portability_enumeration only when the
  loader refuses with VK_ERROR_INCOMPATIBLE_DRIVER (a portability driver such
  as MoltenVK); a native driver answers the first call, so Linux behaviour is
  unchanged. meson.build now states meson_version '>=1.1'.

* Sat Oct 10 2026 The ButterEye contributors - 0.0.1-1
- First package: mvgpu.Interpolate as a live engine for ButterEye
