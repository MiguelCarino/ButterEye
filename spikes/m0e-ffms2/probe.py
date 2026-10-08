# SPDX-License-Identifier: AGPL-3.0-or-later
# Spike M0(e): ffms2 on VapourSynth R72. Usage:
#   python -I probe.py <libffms2.so.5> <index-cache-dir> <video>...
import sys, time
import vapoursynth as vs
core = vs.core
core.std.LoadPlugin(sys.argv[1])
cache = sys.argv[2]
for path in sys.argv[3:]:
    t = time.monotonic()
    try:
        clip = core.ffms2.Source(path, cachefile=f"{cache}/{path.rsplit('/',1)[-1]}.ffindex")
    except vs.Error as e:
        print(path, "OPEN FAILED", e); continue
    t_idx = time.monotonic() - t
    f0 = clip.get_frame(0); mid = clip.get_frame(clip.num_frames // 2); last = clip.get_frame(clip.num_frames - 1)
    t = time.monotonic(); n = min(clip.num_frames, 240)
    for i in range(n): clip.get_frame(i)
    rate = n / (time.monotonic() - t)
    # seek accuracy: frame 100 read twice (random then sequential) must match
    import hashlib
    def h(i):
        f = clip.get_frame(i); return hashlib.md5(bytes(f[0])).hexdigest()
    a = h(100); clip2 = core.ffms2.Source(path, cachefile=f"{cache}/{path.rsplit('/',1)[-1]}.ffindex")
    for i in range(101): clip2.get_frame(i)
    b = hashlib.md5(bytes(clip2.get_frame(100)[0])).hexdigest()
    print(f"{path.rsplit('/',1)[-1]}: {clip.width}x{clip.height} {clip.format.name} {clip.fps} frames={clip.num_frames} index={t_idx:.2f}s decode={rate:.0f}fps seek_ok={a==b} props={dict((k,f0.props[k]) for k in ('_Matrix','_Primaries','_Transfer','_ColorRange') if k in f0.props)}")
