# SPDX-License-Identifier: AGPL-3.0-or-later
# ButterEye COPR package (SCOPE §9 package 4). Build: packaging/plugins/build.sh
%global tag v29_2

Name:           buttereye-vs-mvtools
Version:        29.2
Release:        1%{?dist}
Summary:        MVTools motion-compensation plugin for VapourSynth (ButterEye build)
# meson.build declares GPL-2.0-or-later (readme says "GPL 2"; see SCOPE.md §8.2);
# src/asm/include/x86inc.asm is ISC (x264 project).
License:        GPL-2.0-or-later AND ISC
URL:            https://github.com/dubhatervapoursynth/vapoursynth-mvtools
Source0:        %{url}/archive/%{tag}/vapoursynth-mvtools-%{tag}.tar.gz
# Headers from pkg-config instead of vs.get_include() (absent on R72), and
# install into ButterEye's private plugin directory.
Patch0:         buttereye-vs-mvtools-pkgconfig.patch

ExclusiveArch:  x86_64
BuildRequires:  gcc-c++
BuildRequires:  meson
BuildRequires:  nasm
BuildRequires:  pkgconfig(vapoursynth)
BuildRequires:  pkgconfig(fftw3f)

%description
MVTools for VapourSynth: block-based motion estimation and compensation,
used by ButterEye as the lightweight (CPU) interpolation fallback via
mv.FlowFPS / mv.BlockFPS. Installed into ButterEye's private plugin
directory (%{_libdir}/buttereye/vapoursynth); not autoloaded.

%prep
# GitHub strips the leading "v" from the tag in the top-level directory.
%autosetup -p1 -n vapoursynth-mvtools-%(echo %{tag} | sed 's/^v//')

%build
%meson
%meson_build

%install
%meson_install

%check
ldd %{buildroot}%{_libdir}/buttereye/vapoursynth/mvtools.so | grep -q 'libfftw3f.so'

%files
%license LICENSE src/asm/include/x86inc.asm
%doc readme.rst
%dir %{_libdir}/buttereye
%dir %{_libdir}/buttereye/vapoursynth
%{_libdir}/buttereye/vapoursynth/mvtools.so

%changelog
* Sat Oct 10 2026 The ButterEye contributors - 29.2-1
- First release package (from the M0(f) spike spec)

* Wed Oct 07 2026 ButterEye contributors - 29.2-0.1.spike
- M0(f) spike build against Fedora VapourSynth headers and fftw
