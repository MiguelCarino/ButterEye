# SPDX-License-Identifier: AGPL-3.0-or-later
# M0(f) spike spec — throwaway, see ../README.md.
%global tag r9_mod_v33

Name:           buttereye-rife-ncnn-models
Version:        9.33
Release:        0.2.spike%{?dist}
Summary:        Curated RIFE ncnn models for ButterEye
# Models from the MIT-licensed RIFE-ncnn-Vulkan repo, derived from MIT weights
# (hzwer/Practical-RIFE). Per-model provenance is still to be recorded (Q25).
License:        MIT
URL:            https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan
# Only the curated model directories, repacked by:
#   ./repack-rife-ncnn.sh <dir>
Source0:        %{name}-%{tag}.tar.gz
Source1:        repack-rife-ncnn.sh
BuildArch:      noarch

%description
RIFE v4.26, v4.22-lite and v4.18 models in ncnn format, for the
buttereye-vs-rife-ncnn plugin. Load one with
rife.RIFE(model_path=%{_datadir}/buttereye/rife-ncnn-models/<dir>).

%prep
%autosetup -n %{name}-%{tag}

%build
# Nothing to build.

%install
install -d %{buildroot}%{_datadir}/buttereye/rife-ncnn-models
cp -a models/. %{buildroot}%{_datadir}/buttereye/rife-ncnn-models/

%files
%license LICENSE
%doc README.md BUTTEREYE-REPACK.txt
%dir %{_datadir}/buttereye
%{_datadir}/buttereye/rife-ncnn-models

%changelog
* Wed Oct 07 2026 ButterEye contributors - 9.33-0.2.spike
- Drop rife-v4.25-lite (two Xid 109 GPU faults on the bundled ncnn build, M0(f))

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.1.spike
- M0(f) spike: curated model subset
