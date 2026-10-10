# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye application package (SCOPE §9 package 1). Built from the sdist
# (python3 -m build --sdist) plus the desktop and AppStream files in packaging/.
# The plugins ship as their own packages (buttereye-vs-mvtools,
# buttereye-vs-rife-ncnn, buttereye-rife-ncnn-models).

%global pypi_name buttereye
%global pypi_version 0.1.0.dev0
%global app_id io.github.buttereye.ButterEye

Name:           buttereye
Version:        0.1.0~dev0
Release:        0.1%{?dist}
Summary:        Smoother motion for the videos you play in mpv
# ButterEye: AGPL-3.0-or-later (generated scripts and configs carry the §7
# permission in LICENSES/). Bundled mpv shader FSRCNNX x2 8-0-4-1 (igv, release
# 1.1, unmodified): LGPL-3.0-or-later.
License:        AGPL-3.0-or-later AND LGPL-3.0-or-later
URL:            https://github.com/MiguelCarino/ButterEye
Source0:        %{pypi_name}-%{pypi_version}.tar.gz
Source1:        %{app_id}.desktop
Source2:        %{app_id}.metainfo.xml

BuildArch:      noarch
BuildRequires:  python3-devel
BuildRequires:  desktop-file-utils
BuildRequires:  /usr/bin/appstreamcli

# The GUI uses PySide6 Essentials modules only (F17); VapourSynth and mpv run
# out of process. /usr/bin/ffmpeg as a file dependency: Fedora's ffmpeg-free
# doesn't Provide "ffmpeg".
Requires:       python3-pyside6
Requires:       python3-vapoursynth
Requires:       vapoursynth-tools
Requires:       mpv
Requires:       /usr/bin/ffmpeg
Requires:       vulkan-loader
Requires:       hicolor-icon-theme
# Carino Systems branding (GUI.md §12.3); Qt falls back to system fonts without them
Requires:       ibm-plex-sans-fonts
Requires:       ibm-plex-mono-fonts
Requires:       redhat-display-fonts
# MVTools is the only path without a GPU
Requires:       buttereye-vs-mvtools
Recommends:     buttereye-vs-rife-ncnn
Recommends:     buttereye-rife-ncnn-models
Recommends:     mkvtoolnix
Recommends:     ffms2
Recommends:     vulkan-tools
# experimental TensorRT path only (SCOPE §5.2)
Suggests:       buttereye-vsmlrt-py
Suggests:       buttereye-onnxconverter-common
Suggests:       7zip

Provides:       bundled(mpv-shader-fsrcnnx) = 1.1

%description
ButterEye interpolates video to a frame rate that suits your display, live in
mpv or as a saved copy, with RIFE on the GPU (Vulkan, or experimental NVIDIA
TensorRT built by the user) or MVTools on the CPU. It drives your
distribution's own mpv and never edits your mpv.conf.

%prep
%autosetup -n %{pypi_name}-%{pypi_version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files -l %{pypi_name}
desktop-file-install --dir=%{buildroot}%{_datadir}/applications %{SOURCE1}
install -Dpm 0644 %{SOURCE2} %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
install -Dpm 0644 %{pypi_name}/gui/icons/buttereye.svg \
    %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/%{app_id}.desktop
appstreamcli validate --no-net %{buildroot}%{_metainfodir}/%{app_id}.metainfo.xml
# The core imports without Qt, VapourSynth or a GPU; the shipped data is there.
%{py3_test_envvars} %{python3} - <<'EOF'
import buttereye.cli
import buttereye.core.api
import buttereye.core.backends.trt
from buttereye.core.mpvctl import shaders
from buttereye.core.scriptgen import generator

assert shaders.shader_for("sharper") is not None, "bundled shader missing"
assert "PLUGIN_DIR" in generator.render_script()
for rel in ("data/mpv/buttereye.lua", "core/scriptgen/template.vpy",
            "core/doctor/probe.vpy", "data/shaders/LGPL-3.0.txt"):
    assert (generator.TEMPLATE.parents[2] / rel).is_file(), rel
EOF

%files -f %{pyproject_files}
%license COPYING LICENSES/AdditionRef-ButterEye-generated-output.txt
%license %{pypi_name}/data/shaders/LGPL-3.0.txt %{pypi_name}/data/shaders/GPL-3.0.txt
%doc README.md
%{_bindir}/buttereye
%{_bindir}/buttereye-gui
%{_datadir}/applications/%{app_id}.desktop
%{_metainfodir}/%{app_id}.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/%{app_id}.svg

%changelog
* Sat Oct 10 2026 The ButterEye contributors - 0.1.0~dev0-0.1
- First package of the application (SCOPE §9, milestone M1)
