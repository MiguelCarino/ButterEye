# SPDX-License-Identifier: AGPL-3.0-or-later
# Spike M0(d): RIFE 2x on PQ RGB, the same chain as the generated script's _rife.
import vapoursynth as vs
core = vs.core
clip = video_in
core.std.LoadPlugin('/usr/lib64/buttereye/vapoursynth/librife.so')
clip = core.std.AssumeFPS(clip, fpsnum=24, fpsden=1)
rgb = core.resize.Bicubic(clip, format=vs.RGBS, matrix_in_s='2020ncl', range_in_s='limited')
rgb = core.rife.RIFE(rgb, model_path='/usr/share/buttereye/rife-ncnn-models/rife-v4.26_ensembleFalse', fps_num=48, fps_den=1, gpu_thread=4)
out = core.resize.Bicubic(rgb, format=vs.YUV420P10, matrix_s='2020ncl', range_s='limited', dither_type='none')
out.set_output()
