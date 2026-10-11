; SPDX-License-Identifier: AGPL-3.0-or-later
; Copyright (C) 2026 The ButterEye contributors
;
; ButterEye's Windows installer (docs/PLATFORMS.md, phase 4). Built by
; build-installer.sh with Fedora's makensis (mingw32-nsis) from a staged folder:
;
;   makensis -DVERSION=0.0.1 -DPAYLOAD=<stage> -DLICENSE_TXT=<COPYING, CRLF>
;            -DICON=<buttereye.ico> -DOUTFILE=<setup.exe> [-DVCREDIST=<vc_redist.x64.exe>]
;
; Per user, no administrator rights: everything goes to
; %LOCALAPPDATA%\Programs\ButterEye (or /D=<folder>), the uninstaller is
; registered under HKCU. The one exception is Microsoft's Visual C++ runtime,
; which VapourSynth and Qt need and which only installs machine-wide: when it is
; missing or too old the installer offers to download it from Microsoft
; (signature checked) and Windows asks for permission once. -DVCREDIST embeds
; a copy instead (only if you may redistribute it, see packaging/README.md).
;
; Silent use: setup.exe /S [/D=C:\Folder]  (/D last, unquoted)
;             Uninstall.exe /S             (returns at once, finishes in the background)
;             Uninstall.exe /S _?=<folder> (waits; leaves Uninstall.exe and the folder)

Unicode true
ManifestDPIAware true
RequestExecutionLevel user
SetCompressor /SOLID lzma
SetCompressorDictSize 64

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "x64.nsh"
!include "WinVer.nsh"
!include "FileFunc.nsh"
!include "WinMessages.nsh"

!ifndef VERSION
  !error "VERSION is not defined"
!endif
!define APPNAME   "ButterEye"
!define PUBLISHER "The ButterEye contributors"
!define HOMEPAGE  "https://github.com/MiguelCarino/ButterEye"
!define UNINSTKEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\ButterEye"
!define VCKEY     "SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"
!define VCURL     "https://aka.ms/vc14/vc_redist.x64.exe"
!define VCWINGET  "winget install --exact --id Microsoft.VCRedist.2015+.x64"
; VapourSynth's own minimum (its _check_visual_studio_runtime): 14.50.35719
!define VCMINOR   50
!define VCBUILD   35719
!define GUI_ARGS  "-m buttereye.gui"

Name "${APPNAME}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "${UNINSTKEY}" "InstallLocation"
BrandingText "${APPNAME} ${VERSION}"
ShowInstDetails show
ShowUninstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APPNAME}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "CompanyName" "${PUBLISHER}"
VIAddVersionKey "LegalCopyright" "${PUBLISHER} (AGPL-3.0-or-later)"
VIAddVersionKey "FileDescription" "${APPNAME} ${VERSION} installer"

!define MUI_ICON "${ICON}"
!define MUI_UNICON "${ICON}"
!define MUI_ABORTWARNING
!define MUI_COMPONENTSPAGE_NODESC

!define MUI_LICENSEPAGE_TEXT_TOP "ButterEye is free software under the GNU Affero General Public License."
!define MUI_LICENSEPAGE_TEXT_BOTTOM "There is nothing to accept: the licence gives you rights. Click Next to continue."
!define MUI_LICENSEPAGE_BUTTON "$(^NextBtn)"
!insertmacro MUI_PAGE_LICENSE "${LICENSE_TXT}"
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_SHOW DirShow
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE DirLeave
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Start ButterEye"
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchApp
!insertmacro MUI_PAGE_FINISH

!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES

!insertmacro MUI_LANGUAGE "English"

; ------------------------------------------------------------------ helpers
; What ButterEye puts in its folder. Removed by name, never the whole folder:
; the user may have chosen a folder with other things in it (/D).
!macro REMOVE_OWNED un
Function ${un}RemoveOwned
  RMDir /r "$INSTDIR\python"
  RMDir /r "$INSTDIR\buttereye"
  RMDir /r "$INSTDIR\vapoursynth"
  RMDir /r "$INSTDIR\licenses"
  RMDir /r "$INSTDIR\share\rife-ncnn-models"
  RMDir "$INSTDIR\share"
  FindFirst $0 $1 "$INSTDIR\buttereye-*.dist-info"
  ${DoWhile} $1 != ""
    RMDir /r "$INSTDIR\$1"
    FindNext $0 $1
  ${Loop}
  FindClose $0
  Delete "$INSTDIR\vulkaninfo.exe"
  Delete "$INSTDIR\buttereye.cmd"
  Delete "$INSTDIR\buttereye.ico"
  Delete "$INSTDIR\buttereye-install.json"
FunctionEnd
!macroend
!insertmacro REMOVE_OWNED ""
!insertmacro REMOVE_OWNED "un."

; $0 = "ok" when $INSTDIR is new, empty or an earlier ButterEye install
Function CheckInstDir
  StrCpy $0 "ok"
  ${If} ${FileExists} "$INSTDIR\buttereye-install.json"
    Return
  ${EndIf}
  FindFirst $1 $2 "$INSTDIR\*.*"
  ${DoWhile} $2 != ""
    ${If} $2 != "."
    ${AndIf} $2 != ".."
      StrCpy $0 "not empty"
      ${Break}
    ${EndIf}
    FindNext $1 $2
  ${Loop}
  FindClose $1
FunctionEnd

Function DirShow
  ; an existing installation: installing again repairs it
  ${If} ${FileExists} "$INSTDIR\buttereye-install.json"
    GetDlgItem $0 $HWNDPARENT 1
    SendMessage $0 ${WM_SETTEXT} 0 "STR:&Repair"
  ${EndIf}
FunctionEnd

Function DirLeave
  Call CheckInstDir
  ${If} $0 != "ok"
    MessageBox MB_ICONEXCLAMATION|MB_OK "$INSTDIR already has other files in it.$\n$\nChoose an empty folder, or the folder ButterEye is installed in."
    Abort
  ${EndIf}
FunctionEnd

Function LaunchApp
  Exec '"$INSTDIR\python\pythonw.exe" ${GUI_ARGS}'
FunctionEnd

; $R0 = "ok" when the x64 Visual C++ runtime is at least 14.${VCMINOR}.${VCBUILD}
Function VcRuntimeOk
  StrCpy $R0 "no"
  SetRegView 64
  ClearErrors
  ReadRegDWORD $R1 HKLM "${VCKEY}" "Installed"
  ReadRegDWORD $R2 HKLM "${VCKEY}" "Major"
  ReadRegDWORD $R3 HKLM "${VCKEY}" "Minor"
  ReadRegDWORD $R4 HKLM "${VCKEY}" "Bld"
  ${If} $R1 = 1
    ${If} $R2 > 14
      StrCpy $R0 "ok"
    ${ElseIf} $R2 = 14
      ${If} $R3 > ${VCMINOR}
        StrCpy $R0 "ok"
      ${ElseIf} $R3 = ${VCMINOR}
      ${AndIf} $R4 >= ${VCBUILD}
        StrCpy $R0 "ok"
      ${EndIf}
    ${EndIf}
  ${EndIf}
FunctionEnd

; Download Microsoft's redistributable and run it; $R5 = its exit code or "failed".
; PowerShell (Windows 10 and later) downloads it over HTTPS and accepts it only
; with a valid Authenticode signature from Microsoft.
Function GetVcRuntime
  StrCpy $R5 "failed"
  InitPluginsDir
  StrCpy $R6 "$PLUGINSDIR\vc_redist.x64.exe"
!ifdef VCREDIST
  File "/oname=$R6" "${VCREDIST}"
!else
  DetailPrint "Downloading Microsoft's Visual C++ runtime..."
  System::Call 'kernel32::SetEnvironmentVariable(t "BUTTEREYE_VCREDIST", t "$R6")'
  nsExec::ExecToLog `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$$ErrorActionPreference = 'Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri '${VCURL}' -OutFile $$env:BUTTEREYE_VCREDIST; $$s = Get-AuthenticodeSignature -LiteralPath $$env:BUTTEREYE_VCREDIST; if ($$s.Status -ne 'Valid' -or $$s.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { Remove-Item -LiteralPath $$env:BUTTEREYE_VCREDIST; exit 2 }"`
  Pop $R7
  ${If} $R7 != 0
    DetailPrint "The download failed or was not signed by Microsoft ($R7)."
    Return
  ${EndIf}
!endif
  DetailPrint "Installing Microsoft's Visual C++ runtime (Windows asks for permission)..."
  ExecWait '"$R6" /install /passive /norestart' $R5
  Delete "$R6"
FunctionEnd

Function VcRuntime
  Call VcRuntimeOk
  ${If} $R0 == "ok"
    DetailPrint "Microsoft's Visual C++ runtime: installed"
    Return
  ${EndIf}
  StrCpy $R5 "skipped"
!ifdef VCREDIST
  Call GetVcRuntime
!else
  ${IfNot} ${Silent}
    MessageBox MB_YESNO|MB_ICONQUESTION "ButterEye needs Microsoft's Visual C++ runtime (14.${VCMINOR} or newer), and this PC doesn't have it.$\n$\nDownload it from Microsoft and install it now? Windows will ask for permission." /SD IDNO IDNO +2
    Call GetVcRuntime
  ${EndIf}
!endif
  ${If} $R5 = 3010
    SetRebootFlag true
  ${EndIf}
  Call VcRuntimeOk
  ${If} $R0 != "ok"
    DetailPrint "Microsoft's Visual C++ runtime is still missing ($R5); install it with: ${VCWINGET}"
    ${IfNot} ${Silent}
      MessageBox MB_ICONINFORMATION|MB_OK "ButterEye is installed, but its window can't open and it can't smooth video until Microsoft's Visual C++ runtime is installed.$\n$\nGet it from ${VCURL}$\nor run: ${VCWINGET}$\n$\nThe command line's checks (buttereye.cmd doctor) will remind you."
    ${EndIf}
  ${EndIf}
FunctionEnd

; ------------------------------------------------------------------ install
Function .onInit
  ${IfNot} ${RunningX64}
    MessageBox MB_ICONSTOP|MB_OK "${APPNAME} needs 64-bit Windows." /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  ; Qt 6 uses the ICU that comes with Windows 10 version 1903 (build 18362)
  ${WinVerGetBuild} $0
  ${IfNot} ${AtLeastWin10}
  ${OrIf} $0 < 18362
    MessageBox MB_ICONSTOP|MB_OK "${APPNAME} needs Windows 10 version 1903 or newer." /SD IDOK
    SetErrorLevel 2
    Quit
  ${EndIf}
  SetShellVarContext current
  SetRegView 64
FunctionEnd

Section "ButterEye" SecMain
  SectionIn RO
  Call CheckInstDir
  ${If} $0 != "ok"
    DetailPrint "$INSTDIR already has other files in it; choose an empty folder."
    SetErrorLevel 2
    Abort
  ${EndIf}
  ; Repair or upgrade: no file of the earlier version survives
  ${If} ${FileExists} "$INSTDIR\buttereye-install.json"
    DetailPrint "Removing the earlier installation's files..."
    Call RemoveOwned
  ${EndIf}

  SetOutPath "$INSTDIR"
  File /r "${PAYLOAD}\*"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  DetailPrint "Preparing ButterEye's Python files..."
  nsExec::ExecToLog '"$INSTDIR\python\python.exe" -m compileall -q "$INSTDIR\buttereye" "$INSTDIR\python\Lib\site-packages"'
  Pop $0

  CreateShortCut "$SMPROGRAMS\${APPNAME}.lnk" "$INSTDIR\python\pythonw.exe" "${GUI_ARGS}" \
      "$INSTDIR\buttereye.ico" 0 SW_SHOWNORMAL "" "Smooth motion for mpv"

  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayName" "${APPNAME}"
  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr   HKCU "${UNINSTKEY}" "DisplayIcon" "$INSTDIR\buttereye.ico"
  WriteRegStr   HKCU "${UNINSTKEY}" "Publisher" "${PUBLISHER}"
  WriteRegStr   HKCU "${UNINSTKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${UNINSTKEY}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
  WriteRegStr   HKCU "${UNINSTKEY}" "QuietUninstallString" '"$INSTDIR\Uninstall.exe" /S'
  WriteRegStr   HKCU "${UNINSTKEY}" "URLInfoAbout" "${HOMEPAGE}"
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINSTKEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINSTKEY}" "EstimatedSize" $0

  Call VcRuntime
SectionEnd

Section /o "Desktop shortcut" SecDesktop
  SetOutPath "$INSTDIR"
  CreateShortCut "$DESKTOP\${APPNAME}.lnk" "$INSTDIR\python\pythonw.exe" "${GUI_ARGS}" \
      "$INSTDIR\buttereye.ico" 0 SW_SHOWNORMAL "" "Smooth motion for mpv"
SectionEnd

; ------------------------------------------------------------------ uninstall
Function un.onInit
  SetShellVarContext current
  SetRegView 64
FunctionEnd

Section "Uninstall"
  Delete "$SMPROGRAMS\${APPNAME}.lnk"
  Delete "$DESKTOP\${APPNAME}.lnk"
  Call un.RemoveOwned
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  DeleteRegKey HKCU "${UNINSTKEY}"
  ; your settings (%APPDATA%\ButterEye) and history (%LOCALAPPDATA%\ButterEye) stay
SectionEnd
