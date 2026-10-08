#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# M0(f) spike: check the installed plugin RPMs actually work.
#   1. each plugin loads from the private directory and lists the GPU
#   2. vspipe throughput, 2x, per backend, at 1080p / 1440p / 2160p
#   3. optional: play a real file in mpv (pass it as the first argument)
#
# Usage: test/run-test.sh [video-file]
set -uo pipefail

here=$(cd "$(dirname "$0")" && pwd)
vpy="$here/interp.vpy"
log="$here/../results/run-test-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$log")"
exec > >(tee "$log") 2>&1

echo "== Environment"
rpm -q mpv vapoursynth-libs ncnn buttereye-vs-rife-ncnn buttereye-rife-ncnn-models buttereye-vs-mvtools
vspipe --version | head -1

echo; echo "== 1. Plugin load + Vulkan devices"
python3 -I - <<'PY'
import vapoursynth as vs
core = vs.core
d = '/usr/lib64/buttereye/vapoursynth'
for ns, f in (('rife', 'librife.so'), ('mv', 'mvtools.so')):
    try:
        core.std.LoadPlugin(f'{d}/{f}')
        print(f'{ns}: loaded from {d}/{f}')
    except Exception as e:
        print(f'{ns}: FAILED: {e}')
PY
command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>/dev/null | grep -E 'deviceName|deviceType|driverName' \
  || echo "(install vulkan-tools to list Vulkan devices)"

echo; echo "== 2. vspipe throughput (120 source frames -> 240 output, synthetic clip)"
for backend in rife mvtools; do
  for size in 1920x1080 2560x1440 3840x2160; do
    printf '%-8s %-10s ' "$backend" "$size"
    start=$(date +%s.%N)
    if vspipe -a "backend=$backend" -a "size=$size" "$vpy" -- 2>"$here/../results/vspipe-$backend-$size.err"; then
      end=$(date +%s.%N)
      awk -v s="$start" -v e="$end" 'BEGIN { printf "%.1f output fps\n", 240 / (e - s) }'
    else
      echo "FAILED: $(tail -1 "$here/../results/vspipe-$backend-$size.err")"
    fi
  done
done
echo "(includes startup; real time at 2x needs >= 48 fps for 24p, >= 60 for 30p sources.)"

if [[ ${1:-} ]]; then
  echo; echo "== 3. mpv playback: press Shift+I for stats (output fps, dropped frames), q to quit"
  BUTTEREYE_BACKEND=${BUTTEREYE_BACKEND:-rife} mpv --hwdec=auto-copy --video-sync=display-resample \
    --vf="vapoursynth=file=$vpy:buffered-frames=4:concurrent-frames=4" "$1"
fi

echo; echo "Log: $log"
