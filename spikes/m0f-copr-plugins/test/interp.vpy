# SPDX-License-Identifier: AGPL-3.0-or-later
#
# M0(f) spike: minimal 2x interpolation script, usable from mpv and vspipe.
#
#   mpv:    BUTTEREYE_BACKEND=rife mpv --hwdec=auto-copy --vf=vapoursynth=file=interp.vpy video.mkv
#   vspipe: vspipe -p -a backend=rife -a size=1920x1080 -e 239 interp.vpy --
#
# Settings (vspipe -a key=value, or BUTTEREYE_<KEY> environment variables):
#   backend  rife | mvtools                      (default rife)
#   model    RIFE ncnn model directory name      (default rife-v4.26_ensembleFalse)
#   size     WxH of the synthetic vspipe source  (default 1920x1080)
#   gpu      Vulkan device index for RIFE        (default 0)
#   threads  RIFE gpu_thread (parallel GPU jobs)  (default 4; 2 is too slow for 1080p real time)
import os
import vapoursynth as vs

core = vs.core
PLUGIN_DIR = '/usr/lib64/buttereye/vapoursynth'
MODEL_DIR = '/usr/share/buttereye/rife-ncnn-models'


def setting(key, default):
    value = globals().get(key)
    if isinstance(value, bytes):
        value = value.decode()
    return value or os.environ.get('BUTTEREYE_' + key.upper(), default)


def load(namespace, filename):
    # Absolute-path load from the private directory, guarded against a copy
    # that is already loaded (distro or autoload).
    if not hasattr(core, namespace):
        core.std.LoadPlugin(os.path.join(PLUGIN_DIR, filename))


backend = setting('backend', 'rife')

if 'video_in' in globals():
    # mpv: video_in has no usable fps, so take it from the container.
    fps = globals().get('container_fps') or 24000 / 1001
    clip = core.std.AssumeFPS(video_in, fpsnum=round(fps * 1000), fpsden=1000)
else:
    w, h = (int(x) for x in setting('size', '1920x1080').split('x'))
    clip = core.std.BlankClip(format=vs.YUV420P8, width=w, height=h,
                              fpsnum=24000, fpsden=1001, length=120)

if backend == 'rife':
    load('rife', 'librife.so')
    # mpv only tags _ColorSpace, so pick the matrix explicitly (spike: by height).
    matrix = '709' if clip.height >= 720 else '170m'
    rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s=matrix)
    model = os.path.join(MODEL_DIR, setting('model', 'rife-v4.26_ensembleFalse'))
    rgb = core.rife.RIFE(rgb, factor_num=2, factor_den=1, model_path=model,
                         gpu_id=int(setting('gpu', '0')),
                         gpu_thread=int(setting('threads', '4')))
    out = core.resize.Bicubic(rgb, format=clip.format.id, matrix_s=matrix)
elif backend == 'mvtools':
    load('mv', 'mvtools.so')
    sup = core.mv.Super(clip, pel=2)
    bv = core.mv.Analyse(sup, isb=True, blksize=16, overlap=4)
    fv = core.mv.Analyse(sup, isb=False, blksize=16, overlap=4)
    out = core.mv.FlowFPS(clip, sup, bv, fv, num=clip.fps.numerator * 2,
                          den=clip.fps.denominator)
else:
    raise vs.Error(f'unknown backend {backend!r}')

out.set_output()
