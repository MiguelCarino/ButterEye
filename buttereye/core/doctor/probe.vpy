# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
#
# ButterEye in-mpv probe (SCOPE F1). Run by doctor as:
#   mpv --no-config --vo=null --ao=null --frames=1 \
#       --vf=vapoursynth=file=probe.vpy:user-data=<json> av://lavfi:testsrc2,format=yuv420p
# All data arrives through mpv's user_data (JSON); nothing is interpolated into code.
# It records the VapourSynth core, Python and plugin load results inside the very
# mpv process that will run ButterEye's scripts, writes them as JSON (0600) and
# passes the frame through unchanged.
import importlib.util
import json
import os
import sys

import vapoursynth as vs

core = vs.core
_cfg = json.loads(globals().get("user_data") or "{}")
_out = _cfg["out"]

for _p in _cfg.get("pythonpath", []):
    if isinstance(_p, str) and _p not in sys.path:
        sys.path.insert(0, _p)

_cv = core.core_version
_result = {
    "vs_core": "R%d" % _cv.release_major,
    "vs_api": "%d.%d" % (vs.__api_version__.api_major, vs.__api_version__.api_minor),
    "python": sys.version.split()[0],
    "executable": sys.executable,
    "sys_path": list(sys.path),
    "plugins": [],
    "modules": {},
}

for _entry in _cfg.get("plugins", []):
    _ns = _entry["ns"]
    _path = _entry.get("path")
    _rec = {"ns": _ns, "path": _path, "preloaded": hasattr(core, _ns), "loaded": False,
            "error": None, "loaded_from": None, "version": None}
    try:
        if _rec["preloaded"]:
            _rec["loaded"] = True
            _rec["loaded_from"] = getattr(getattr(core, _ns), "plugin_path", None)
        elif _path and os.path.isfile(_path):
            core.std.LoadPlugin(_path)
            _rec["loaded"] = hasattr(core, _ns)
            _rec["loaded_from"] = _path
        else:
            _rec["error"] = "not found"
        if _rec["loaded"] and _ns == "trt" and hasattr(core.trt, "Version"):
            _rec["version"] = str(core.trt.Version())
    except Exception as _exc:  # noqa: BLE001 - the probe reports every failure
        _rec["error"] = "%s: %s" % (type(_exc).__name__, _exc)
    _result["plugins"].append(_rec)

for _mod in _cfg.get("modules", []):
    try:
        _result["modules"][_mod] = importlib.util.find_spec(_mod) is not None
    except Exception:  # noqa: BLE001
        _result["modules"][_mod] = False

_tmp = _out + ".tmp"
_fd = os.open(_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(_fd, "w", encoding="utf-8") as _fh:
    json.dump(_result, _fh)
os.replace(_tmp, _out)

video_in.set_output()  # noqa: F821 - provided by mpv
