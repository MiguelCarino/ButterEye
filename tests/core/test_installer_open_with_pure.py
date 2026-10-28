# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The Windows installer's "Open with" registration, read from buttereye.nsi
(the end-to-end check is tests/windows/test_installer_windows.py, which needs
a built setup.exe): a ProgID of ButterEye's own under HKCU, offered for the
common video types, and removed only by the uninstaller of the copy the
uninstall entry names."""

from __future__ import annotations

import re
from pathlib import Path

NSI = Path(__file__).resolve().parents[2] / "packaging" / "windows" / "buttereye.nsi"
TYPES = ("mp4", "mkv", "webm", "mov", "avi", "ts")


def _code() -> str:
    """The script without its comments."""
    lines = (ln.split(";", 1)[0] if not ln.lstrip().startswith(";") else "" for ln in
             NSI.read_text("utf-8").splitlines())  # fmt: skip
    return "\n".join(lines)


def _section(code: str, start: str) -> str:
    begin = code.index(start)
    return code[begin : code.index("SectionEnd", begin)]


def test_the_progid_opens_the_gui_with_the_file() -> None:
    install = _section(_code(), 'Section "ButterEye" SecMain')
    assert 'WriteRegStr HKCU "${CLASSES}\\${PROGID}\\shell\\open\\command" "" \\' in install
    assert '\'"$INSTDIR\\python\\pythonw.exe" ${GUI_ARGS} "%1"\'' in install
    assert '"${CLASSES}\\${PROGID}\\DefaultIcon" "" "$INSTDIR\\buttereye.ico"' in install
    assert '"FriendlyTypeName"' in install
    # shown as ButterEye, not as pythonw's "Python"
    assert '"${CLASSES}\\${PROGID}\\shell\\open" "FriendlyAppName" "${APPNAME}"' in install
    assert '"${CLASSES}\\${PROGID}\\Application" "ApplicationName" "${APPNAME}"' in install
    assert "!insertmacro VIDEO_TYPES OPEN_WITH_ADD" in install
    assert "SHChangeNotify(i ${SHCNE_ASSOCCHANGED}" in install
    code = _code()
    assert '!define PROGID    "ButterEye.Video"' in code
    assert '!define CLASSES   "Software\\Classes"' in code
    assert "!define SHCNE_ASSOCCHANGED 0x08000000" in code
    # everything under HKCU: no administrator rights, nothing for other users
    assert not re.search(r"WriteReg\w+\s+HK(LM|CR)\b", code)


def test_offered_for_the_video_types_never_as_their_default() -> None:
    code = _code()
    begin = code.index("!macro VIDEO_TYPES")
    macro = code[begin : code.index("!macroend", begin)]
    assert re.findall(r'!insertmacro \$\{_M\} "(\w+)"', macro) == list(TYPES)
    assert 'WriteRegStr HKCU "${CLASSES}\\.${_EXT}\\OpenWithProgids" "${PROGID}" ""' in code
    # never the extension's default value, and never Applications\pythonw.exe
    assert not re.search(r'WriteRegStr HKCU "\$\{CLASSES\}\\\.\$\{_EXT\}" ""', code)
    assert "Applications\\pythonw.exe" not in code


def test_only_the_registered_copys_uninstaller_removes_it() -> None:
    uninstall = _section(_code(), 'Section "Uninstall"')
    owned = uninstall[uninstall.index("${If} $4 == $INSTDIR") : uninstall.index("${ElseIf}")]
    assert "!insertmacro VIDEO_TYPES OPEN_WITH_REMOVE" in owned
    assert 'DeleteRegKey HKCU "${CLASSES}\\${PROGID}"' in owned
    assert "SHChangeNotify(i ${SHCNE_ASSOCCHANGED}" in owned
    assert uninstall.count("OPEN_WITH_REMOVE") == 1 and uninstall.count("${PROGID}") == 1
    # the value only, never the extension's OpenWithProgids key other programs share
    code = _code()
    assert 'DeleteRegValue HKCU "${CLASSES}\\.${_EXT}\\OpenWithProgids" "${PROGID}"' in code
    assert not re.search(r"DeleteRegKey\s+HKCU\s+\S*OpenWithProgids", code)
