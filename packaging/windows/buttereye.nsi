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
; Exit codes: 0 done, 2 not here (64-bit Windows 10 1903+ only; a folder with other
; files; ButterEye is already installed in another folder), 4 ButterEye, or a
; player it started, is running from that folder and still uses its files.
;
; One installation per user: the uninstall entry and the shortcuts belong to
; the folder the entry names, so a second copy elsewhere is refused (uninstall
; the first, or install into its folder to repair it), and an uninstaller run
; for another folder leaves the entry and shortcuts alone.

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

; ------------------------------------------------------------------ in use
; A running ButterEye holds a named mutex for its install folder
; (osapi.install_mutex_name): "Local\ButterEye-" + the folder's long path in lower
; case, "/" for each "\" (a mutex name can't hold a backslash). A player it
; started (mpv) or anything else may still have its DLLs and programs loaded:
; those can't be opened for writing and deleting, which is checked as well.
!macro IN_USE un
; $3 = the mutex name for $INSTDIR
Function ${un}MutexName
  System::Call 'kernel32::GetLongPathNameW(w "$INSTDIR", w .r4, i ${NSIS_MAX_STRLEN}) i .r5'
  ${If} $5 = 0
  ${OrIf} $5 >= ${NSIS_MAX_STRLEN}
    StrCpy $4 "$INSTDIR"
  ${EndIf}
  System::Call 'user32::CharLowerW(w r4 r4)'
  StrCpy $3 ""
  StrCpy $6 0
  ${Do}
    StrCpy $7 $4 1 $6
    ${If} $7 == ""
      ${Break}
    ${ElseIf} $7 == "\"
      StrCpy $7 "/"
    ${EndIf}
    StrCpy $3 "$3$7"
    IntOp $6 $6 + 1
  ${Loop}
  StrCpy $3 "Local\ButterEye-$3"
FunctionEnd

; $8 = a folder, $9 = a file pattern in it; $3 = the first match in use, else unchanged
Function ${un}LockedIn
  FindFirst $5 $6 "$8\$9"
  ${DoWhile} $6 != ""
    ; GENERIC_WRITE|DELETE, sharing everything, OPEN_EXISTING: fails for a
    ; loaded program or DLL, and for a file someone holds open without sharing
    System::Call 'kernel32::CreateFileW(w "$8\$6", i 0x40010000, i 7, p 0, i 3, i 0, p 0) i .r7'
    ${If} $7 = -1
      StrCpy $3 "$8\$6"
      ${Break}
    ${EndIf}
    System::Call 'kernel32::CloseHandle(i r7)'
    FindNext $5 $6
  ${Loop}
  FindClose $5
FunctionEnd

; $3 = "" when nothing runs from $INSTDIR, else what does
Function ${un}InUse
  Call ${un}MutexName
  System::Call 'kernel32::OpenMutexW(i 0x00100000, i 0, w r3) i .r4'
  StrCpy $3 ""
  ${If} $4 <> 0
    System::Call 'kernel32::CloseHandle(i r4)'
    StrCpy $3 "ButterEye"
    Return
  ${EndIf}
  StrCpy $8 "$INSTDIR\python"
  StrCpy $9 "*.dll"
  Call ${un}LockedIn
  StrCpy $9 "*.exe"
  Call ${un}LockedIn
  StrCpy $8 "$INSTDIR\python\Lib\site-packages\vapoursynth"
  Call ${un}LockedIn
  StrCpy $9 "*.dll"
  Call ${un}LockedIn
  StrCpy $8 "$INSTDIR\python\Lib\site-packages\PySide6"
  Call ${un}LockedIn
  StrCpy $8 "$INSTDIR\python\Lib\site-packages\shiboken6"
  Call ${un}LockedIn
  StrCpy $8 "$INSTDIR\vapoursynth"
  Call ${un}LockedIn
  StrCpy $8 "$INSTDIR"
  StrCpy $9 "vulkaninfo.exe"
  Call ${un}LockedIn
FunctionEnd

; Ask until nothing runs from $INSTDIR (silent: don't ask). $3 = "" when free.
Function ${un}WaitUntilFree
  retry:
  Call ${un}InUse
  StrCmp $3 "" 0 +2
  Return
  DetailPrint "ButterEye is running from $INSTDIR, or a player it started uses its files ($3)."
  IfSilent 0 +2
  Return
  MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "ButterEye is running from $INSTDIR, or a player it started still uses its files:$\n$3$\n$\nClose ButterEye and mpv, then click Retry." IDRETRY retry
FunctionEnd

; $3 = something of ButterEye's that is still in $INSTDIR after RemoveOwned, else ""
Function ${un}Leftovers
  StrCpy $3 ""
  ${If} ${FileExists} "$INSTDIR\python\*.*"
    StrCpy $3 "$INSTDIR\python"
  ${ElseIf} ${FileExists} "$INSTDIR\buttereye\*.*"
    StrCpy $3 "$INSTDIR\buttereye"
  ${ElseIf} ${FileExists} "$INSTDIR\vapoursynth\*.*"
    StrCpy $3 "$INSTDIR\vapoursynth"
  ${ElseIf} ${FileExists} "$INSTDIR\share\rife-ncnn-models\*.*"
    StrCpy $3 "$INSTDIR\share\rife-ncnn-models"
  ${ElseIf} ${FileExists} "$INSTDIR\vulkaninfo.exe"
    StrCpy $3 "$INSTDIR\vulkaninfo.exe"
  ${EndIf}
FunctionEnd

!macroend
!insertmacro IN_USE ""
!insertmacro IN_USE "un."

; $3 = the folder of another ButterEye this user has installed, else ""
Function OtherInstall
  StrCpy $3 ""
  ReadRegStr $4 HKCU "${UNINSTKEY}" "InstallLocation"
  ${If} $4 != ""
  ${AndIf} $4 != $INSTDIR
  ${AndIf} ${FileExists} "$4\buttereye-install.json"
    StrCpy $3 $4
  ${EndIf}
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
  Call OtherInstall
  ${If} $3 != ""
    MessageBox MB_ICONEXCLAMATION|MB_OK "ButterEye is already installed in $3.$\n$\nChoose that folder to repair or upgrade it, or uninstall it first."
    Abort
  ${EndIf}
  ${If} ${FileExists} "$INSTDIR\buttereye-install.json"
    Call WaitUntilFree
    ${If} $3 != ""
      Abort
    ${EndIf}
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

; PowerShell by its full path (never a powershell.exe next to setup.exe); under
; this 32-bit installer $SYSDIR reads as SysWOW64, Windows' own 32-bit copy.
!define POWERSHELL '"$SYSDIR\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command'

; Download Microsoft's redistributable and run it; $R5 = its exit code or "failed".
; It goes to $PLUGINSDIR (this installer's own temporary folder, only this user
; can write there) and is held open, readable but not writable or deletable by
; anyone, from before its check until it has run: the file checked is the file run.
Function GetVcRuntime
  StrCpy $R5 "failed"
  InitPluginsDir
  StrCpy $R6 "$PLUGINSDIR\vc_redist.x64.exe"
!ifdef VCREDIST
  File "/oname=$R6" "${VCREDIST}"
!else
  DetailPrint "Downloading Microsoft's Visual C++ runtime..."
  System::Call 'kernel32::SetEnvironmentVariable(t "BUTTEREYE_VCREDIST", t "$R6")'
  nsExec::ExecToLog `${POWERSHELL} "$$ErrorActionPreference = 'Stop'; [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri '${VCURL}' -OutFile $$env:BUTTEREYE_VCREDIST"`
  Pop $R7
  ${If} $R7 != 0
    DetailPrint "The download failed ($R7)."
    Delete "$R6"
    Return
  ${EndIf}
!endif
  ; GENERIC_READ, FILE_SHARE_READ only, OPEN_EXISTING
  System::Call 'kernel32::CreateFileW(w "$R6", i 0x80000000, i 1, p 0, i 3, i 0, p 0) i .R4'
  ${If} $R4 = -1
    DetailPrint "Can't open the downloaded Visual C++ runtime."
    Delete "$R6"
    Return
  ${EndIf}
  ; Microsoft's valid Authenticode signature (chained to Microsoft's root) on
  ; Microsoft's x64 Visual C++ redistributable, nothing else Microsoft signs
  System::Call 'kernel32::SetEnvironmentVariable(t "BUTTEREYE_VCREDIST", t "$R6")'
  nsExec::ExecToLog `${POWERSHELL} "$$ErrorActionPreference = 'Stop'; $$f = $$env:BUTTEREYE_VCREDIST; $$s = Get-AuthenticodeSignature -LiteralPath $$f; $$c = $$s.SignerCertificate; if ($$s.Status -ne 'Valid' -or -not $$c -or $$c.Subject -notmatch '(^|, )CN=Microsoft Corporation(,|$$)' -or $$c.Subject -notmatch '(^|, )O=Microsoft Corporation(,|$$)') { exit 2 }; $$ch = New-Object Security.Cryptography.X509Certificates.X509Chain; [void]$$ch.Build($$c); $$root = $$ch.ChainElements[$$ch.ChainElements.Count - 1].Certificate; if ($$root.Subject -notmatch '(^|, )CN=Microsoft Root Certificate Authority') { exit 3 }; $$v = (Get-Item -LiteralPath $$f).VersionInfo; if ($$v.OriginalFilename -ne 'VC_redist.x64.exe' -or $$v.CompanyName -ne 'Microsoft Corporation' -or $$v.ProductName -notmatch '^Microsoft Visual C\+\+ .*Redistributable \(x64\)') { exit 4 }"`
  Pop $R7
  ${If} $R7 != 0
    DetailPrint "The Visual C++ runtime file is not Microsoft's signed redistributable ($R7): not run."
    System::Call 'kernel32::CloseHandle(i R4)'
    Delete "$R6"
    Return
  ${EndIf}
  DetailPrint "Installing Microsoft's Visual C++ runtime (Windows asks for permission)..."
  ExecWait '"$R6" /install /passive /norestart' $R5
  System::Call 'kernel32::CloseHandle(i R4)'
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
  Call OtherInstall
  ${If} $3 != ""
    DetailPrint "ButterEye is already installed in $3: uninstall it first, or install into that folder to repair it."
    SetErrorLevel 2
    Abort
  ${EndIf}
  ; Repair or upgrade: no file of the earlier version survives
  ${If} ${FileExists} "$INSTDIR\buttereye-install.json"
    Call WaitUntilFree
    ${If} $3 != ""
      SetErrorLevel 4
      Abort
    ${EndIf}
    DetailPrint "Removing the earlier installation's files..."
    Call RemoveOwned
    Call Leftovers
    ${If} $3 != ""
      DetailPrint "$3 is still in use; close ButterEye and mpv, then install again."
      SetErrorLevel 4
      Abort
    ${EndIf}
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
  Call un.WaitUntilFree
  ${If} $3 != ""
    SetErrorLevel 4
    Abort
  ${EndIf}
FunctionEnd

Section "Uninstall"
  Call un.RemoveOwned
  Call un.Leftovers
  ${If} $3 != ""
    ; something started in the meantime: keep the uninstaller to run again
    DetailPrint "$3 is still in use; close ButterEye and mpv, then uninstall again."
    ${IfNot} ${Silent}
      MessageBox MB_ICONEXCLAMATION|MB_OK "Some of ButterEye's files are still in use and stay in $INSTDIR.$\n$\nClose ButterEye and mpv, then uninstall again."
    ${EndIf}
    SetErrorLevel 4
    Abort
  ${EndIf}
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"
  ; the entry and the shortcuts belong to the copy the entry names
  ReadRegStr $4 HKCU "${UNINSTKEY}" "InstallLocation"
  ${If} $4 == $INSTDIR
    Delete "$SMPROGRAMS\${APPNAME}.lnk"
    Delete "$DESKTOP\${APPNAME}.lnk"
    DeleteRegKey HKCU "${UNINSTKEY}"
  ${ElseIf} $4 != ""
    DetailPrint "The uninstall entry and the shortcuts belong to the ButterEye in $4: left alone."
  ${EndIf}
  ; your settings (%APPDATA%\ButterEye) and history (%LOCALAPPDATA%\ButterEye) stay
SectionEnd
