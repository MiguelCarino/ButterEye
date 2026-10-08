<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# ButterEye error codes (F21)

Generated from `buttereye/core/errors.py` (`ErrorCode`), then hand-edited fix texts. Tests assert codes, never text; every code below must exist in the enum and vice versa (`tests/core/test_errors.py`).

Exit codes (SCOPE §4.1): 0 ok, 1 runtime failure, 2 usage or config error, 3 blocking `doctor` issue, 4 dependency missing, 130 cancelled (`OperationCancelled`, whatever its code).

| Code | Name | Exit | Kind | Meaning | Fix |
|---|---|---|---|---|---|
| BE-1001 | `MPV_TOO_OLD` | 3 | Blocking | mpv is older than 0.41.0. | Update mpv: `sudo dnf upgrade mpv`. |
| BE-1002 | `MPV_NO_VS_FILTER` | 3 | Blocking | Your mpv was built without VapourSynth (`mpv --vf=help` lists no `vapoursynth`). | Install the distro mpv package: `sudo dnf install mpv`. |
| BE-1003 | `MPV_SANDBOXED` | 3 | Blocking | mpv is a Flatpak or Snap; sandboxed mpv is unsupported in v1 (SCOPE §9). | Install the host mpv package and use it instead. |
| BE-1004 | `MPV_NOT_FOUND` | 4 | Blocking | No `mpv` binary on PATH. | `sudo dnf install mpv`. |
| BE-1005 | `PROBE_FAILED` | 3 | Blocking | The in-mpv probe (F1) did not produce a result. | Open the System page, press F5 to run the checks again, and check the probe log under `$XDG_STATE_HOME/buttereye/logs/`. |
| BE-1010 | `VULKAN_LOADER_MISSING` | 4 | Blocking for RIFE | The Vulkan loader or every GPU ICD is missing. | Install `vulkan-loader` and your driver's ICD (Mesa: `mesa-vulkan-drivers`; NVIDIA: the driver's Vulkan package). |
| BE-1011 | `VULKAN_CPU_ONLY` | 4 | Degraded | Only a CPU Vulkan device (llvmpipe) exists: MVTools only, RIFE unavailable (§5.3.2). | Install the Vulkan driver for your GPU. |
| BE-1020 | `PKG_MISSING` | 4 | Blocking or degraded | A ButterEye COPR package (or an optional tool such as mkvtoolnix) is not installed. | Run the `dnf copr enable` / `dnf install` lines doctor shows. ButterEye never runs dnf. |
| BE-1021 | `LICENCE_FILE_MISSING` | 1 | Degraded | An installed package's `%license` file is absent (F1/F18). This is a packaging bug. | Reinstall the package (`sudo dnf reinstall <package>`) and report the bug. |
| BE-1022 | `VARIANT_SYSTEM_NCNN` | 1 | Degraded | The RIFE plugin was built against Fedora's ncnn, which faults at `gpu_thread` >= 2 (spike M0(f)). | Install the default bundled-ncnn build of `buttereye-vs-rife-ncnn` from the ButterEye COPR. |
| BE-1030 | `RIFE_GPU_FAULT` | 1 | Degraded | NVIDIA Xid errors in `journalctl -k` since the last RIFE-ncnn session: GPU fault in RIFE-ncnn. | Use MVTools for the affected file, or retry RIFE knowing faults may repeat. `journalctl -k -g Xid` shows the lines. |
| BE-1031 | `JOURNAL_UNREADABLE` | 1 | Info | The kernel log could not be read, so GPU faults cannot be checked (never reported as OK). | Add your user to the `systemd-journal` group if you want fault checks. |
| BE-1040 | `USER_CONF_CONFLICT` | 1 | Note (Degraded once attaching is in the build; players ButterEye starts override these options) | Your mpv.conf sets a conflicting option (non-copy `hwdec`, `interpolation=yes`, or `save-position-on-quit` without `watch-later-options-remove=vf`). | Change the setting doctor names, or let the ButterEye profile override it. |
| BE-1041 | `MPVSOCKETS_IN_USE` | 1 | Note (Degraded once attaching is in the build; Play follows the moved socket) | The mpvSockets script is active; it creates sockets ButterEye will not attach to blindly. | Remove mpvSockets or attach with `--socket` explicitly. |
| BE-1050 | `TRT_UNSUPPORTED` | 4 | Experimental | An item of the experimental TensorRT section (a)-(g) failed. | See the TensorRT items on the System page; TRT never blocks RIFE-ncnn or MVTools. |
| BE-2001 | `CONFIG_INVALID` | 2 | Config | config.toml is not valid TOML; defaults are in use and the line/column is reported. | Fix the file at the reported line, or start fresh (a .bak is kept). |
| BE-2002 | `CONFIG_NEWER_SCHEMA` | 2 | Config | config.toml was written by a newer ButterEye; it is loaded read-only. | Update ButterEye, or edit the file by hand. |
| BE-2003 | `CONFIG_CONFLICT` | 2 | Config | config.toml changed on disk since it was loaded. | Reload it, or overwrite it with your changes. |
| BE-2004 | `CONFIG_VALUE` | 2 | Config | A setting has an invalid value (field path reported). | Correct the named field. |
| BE-2005 | `CONFIG_UNKNOWN_KEY` | 2 | Warning | config.toml has keys this version does not know; they are preserved on save. | None needed; remove them if they are typos. |
| BE-2010 | `RUNTIME_DIR_UNSAFE` | 1 | Runtime | `$XDG_RUNTIME_DIR/buttereye` is not 0700 or not owned by you (§4.3). ButterEye never chmods it. | Fix the permissions yourself (`chmod 700`) or remove the directory. |
| BE-2020 | `GUI_ALREADY_RUNNING` | 1 | Runtime | Another ButterEye window holds the GUI lock. | Switch to the open window. |
| BE-3001 | `SOCKET_UNSAFE` | 1 | Runtime | A discovered mpv socket failed the ownership/peer-credential checks and was refused. | Start mpv yourself with `--input-ipc-server` in a private directory, or open the video from the Play page so ButterEye starts mpv itself. |
| BE-3002 | `INSTANCE_UNSUPPORTED` | 3 | Blocking (this instance) | The mpv instance is too old, lacks the VapourSynth filter, or is sandboxed. | Use the distro mpv package. |
| BE-3003 | `VF_ROLLED_BACK` | 1 | Runtime | mpv rejected the filter change; the previous filter was restored (F4). | Try another profile; the cause and mpv's log tail are shown. |
| BE-3004 | `FILTER_STALLED` | 1 | Runtime | Video stalled while audio kept playing; interpolation was turned off automatically. | Use MVTools for this file, or keep interpolation off. It is never re-enabled automatically. |
| BE-3005 | `IPC_LOST` | 1 | Runtime | The connection to mpv was lost. | Reconnect with Attach, or restart playback. |
| BE-3006 | `DEVICE_LOST` | 1 | Runtime | The GPU reported device loss during interpolation. | Switch to MVTools, or retry RIFE knowing faults may repeat. |
| BE-3007 | `FILE_NOT_LOCAL` | 2 | Usage | Only local files can be opened; streaming URLs are not supported. | Download the file first. |
| BE-4001 | `FFMS2_MISSING` | 4 | Dependency (a note while offline render isn't in the build) | Offline render needs ffms2 to read video for VapourSynth. | `sudo dnf install ffms2`, then press F5. |
| BE-4002 | `CODEC_NOT_DECODABLE` | 4 | Dependency | ffms2 cannot decode this video's codec with the installed FFmpeg. | Install a libavcodec build that decodes it (e.g. libavcodec-freeworld). |
| BE-4003 | `HDR_CLASS_REFUSED` | 2 | Usage | HLG and Dolby Vision sources cannot be rendered in v1 (§7.4). | Use an SDR or HDR10 source. |
| BE-4004 | `NO_SPACE` | 1 | Runtime | Not enough free space for the estimated output. | Free space or choose another output folder. |
| BE-4005 | `VSPIPE_FRAME_ERROR` | 1 | Runtime | vspipe reported a frame error during render. | Check the job log; try another profile. |
| BE-4006 | `ENCODER_FAILED` | 1 | Runtime | The ffmpeg encoder exited with an error. | Check the job log; pick another encoder. |
| BE-4007 | `REMUX_FAILED` | 1 | Runtime | The final remux (mkvmerge or ffmpeg) failed. | Check the job log; install mkvtoolnix for the preferred path. |
| BE-4008 | `OUTPUT_EXISTS` | 2 | Usage | The output file exists and overwrite was not confirmed. | Confirm overwrite or choose another name. |
| BE-5001 | `BENCH_FAILED` | 1 | Runtime | A benchmark run failed. | Check the bench log; results of other configurations are kept. |
| BE-6001 | `HASH_MISMATCH` | 1 | Runtime | A download's SHA-256 does not match the manifest; nothing was installed. There is no override. | Retry later; report it if it persists. |
| BE-6002 | `HOST_NOT_ALLOWED` | 1 | Runtime | A download URL is outside the host allowlist (§4.12). | This indicates a manifest bug; report it. |
| BE-6003 | `DOWNLOAD_FAILED` | 1 | Runtime | A download failed. | Check the network and retry. |
| BE-6004 | `SEVENZIP_MISSING` | 4 | Dependency | 7z is needed to unpack this model archive. | `sudo dnf install 7zip`. |
| BE-9001 | `NOT_IMPLEMENTED` | 1 | Build | This build lacks the code path for the feature. | Wait for a later build; nothing is wrong with your system. |
| BE-9999 | `INTERNAL` | 1 | Bug | An unexpected internal error (traceback in the log). Also the neutral placeholder code of `OperationCancelled`. | Report it with the log from `$XDG_STATE_HOME/buttereye/logs/`. |
