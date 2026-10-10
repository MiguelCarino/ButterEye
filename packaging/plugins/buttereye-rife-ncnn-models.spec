# SPDX-License-Identifier: AGPL-3.0-or-later
# ButterEye COPR package (SCOPE §9 package 3). Build: packaging/plugins/build.sh
%global tag r9_mod_v33

Name:           buttereye-rife-ncnn-models
Version:        9.33
Release:        1%{?dist}
Summary:        Curated RIFE ncnn models for ButterEye
# Models from the MIT-licensed RIFE-ncnn-Vulkan repo, derived from MIT weights
# (hzwer/Practical-RIFE); provenance and checksums in PROVENANCE-rife-ncnn-models.md.
License:        MIT
URL:            https://github.com/styler00dollar/VapourSynth-RIFE-ncnn-Vulkan
# Only the curated model directories, repacked by:
#   ./repack-rife-ncnn.sh <dir>
Source0:        %{name}-%{tag}.tar.gz
Source1:        repack-rife-ncnn.sh
# Licence texts of the weights' and implementation's origins (SCOPE §9 package 3)
Source2:        LICENSE.Practical-RIFE
Source3:        LICENSE.ECCV2022-RIFE
Source4:        LICENSE.rife-ncnn-vulkan
Source5:        PROVENANCE-rife-ncnn-models.md
BuildArch:      noarch

%description
RIFE v4.26, v4.22-lite and v4.18 models in ncnn format, for the
buttereye-vs-rife-ncnn plugin. Load one with
rife.RIFE(model_path=%{_datadir}/buttereye/rife-ncnn-models/<dir>).

%prep
%autosetup -n %{name}-%{tag}
cp -p %{SOURCE2} %{SOURCE3} %{SOURCE4} %{SOURCE5} .

%build
# Nothing to build.

%install
install -d %{buildroot}%{_datadir}/buttereye/rife-ncnn-models
cp -a models/. %{buildroot}%{_datadir}/buttereye/rife-ncnn-models/

%files
%license LICENSE LICENSE.Practical-RIFE LICENSE.ECCV2022-RIFE LICENSE.rife-ncnn-vulkan
%doc README.md BUTTEREYE-REPACK.txt PROVENANCE-rife-ncnn-models.md
%dir %{_datadir}/buttereye
%{_datadir}/buttereye/rife-ncnn-models

%changelog
* Sat Oct 10 2026 The ButterEye contributors - 9.33-1
- First release package (from the M0(f) spike spec); ship the Practical-RIFE,
  ECCV2022-RIFE and rife-ncnn-vulkan licence texts and a provenance note

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.2.spike
- Drop rife-v4.25-lite (two Xid 109 GPU faults on the bundled ncnn build, M0(f))

* Wed Oct 07 2026 ButterEye contributors - 9.33-0.1.spike
- M0(f) spike: curated model subset
