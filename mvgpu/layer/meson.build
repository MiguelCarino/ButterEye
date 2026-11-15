# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# VK_LAYER_BUTTEREYE_frames: an explicit Vulkan layer (Linux, Wayland). Built only
# with -Dlayer=enabled. It takes the Vulkan headers only and never links the loader:
# every call goes to the next layer through the tables the loader hands over.
if host_machine.system() != 'linux'
  error('the frames layer is Linux only (-Dlayer=enabled)')
endif

vulkan_headers = vulkan.partial_dependency(compile_args: true)
threads = dependency('threads')

# the blend shader (stage S3) as a SPIR-V array in a header
blend_spv = custom_target('blend_spv',
  input: 'blend.comp',
  output: 'blend_spv.h',
  command: [glslang, '-V', '--target-env', 'vulkan1.1', '--vn', 'blend_spv', '@INPUT@', '-o', '@OUTPUT@'])

# the S4 shaders (motion-compensated frames: ingest, cut test, render), the same way
layer_spv = []
foreach sh : ['ingest', 'cut', 'render_rgb']
  layer_spv += custom_target(sh + '_spv',
    input: sh + '.comp',
    output: sh + '_spv.h',
    command: [glslang, '-V', '--target-env', 'vulkan1.1', '--vn', sh + '_spv', '@INPUT@', '-o', '@OUTPUT@'])
endforeach

# the engine (../src/engine) is built in too: it calls Vulkan only through its table,
# which the layer fills from the next layer, so the loader is still not linked
frames_layer = shared_module('VkLayer_buttereye_frames',
  'layer.cpp', 'swapchain.cpp', 'shm.cpp', 'blend.cpp', 'mc.cpp', 'engine_test.cpp', blend_spv, layer_spv,
  engine_src, shader_headers,
  include_directories: engine_inc,
  name_prefix: 'lib',
  dependencies: [vulkan_headers, threads],
  cpp_args: ['-Wno-missing-field-initializers'],
  gnu_symbol_visibility: 'hidden',
  link_args: ['-Wl,--no-undefined'],
  install: true,
  install_dir: layerdir)

# the manifest sits next to the library in the build folder; a library_path with a
# slash is taken relative to the manifest. This one is for runs from the build folder
# (VK_ADD_LAYER_PATH); meson install writes its own (../meson.build)
configure_file(input: 'VkLayer_buttereye_frames.json.in',
  output: 'VkLayer_buttereye_frames.json',
  configuration: {
    'LIBRARY_PATH': './libVkLayer_buttereye_frames.so',
    'API_VERSION': vulkan.version(),
  })

# a headless test program for the layer's paths (lavapipe, no window); it is an
# ordinary Vulkan program, so it does link the loader
executable('frames-present-test', 'present_test.cpp',
  dependencies: [vulkan],
  cpp_args: ['-Wno-missing-field-initializers'],
  install: false)

# frames-test (../tools/frames_test.cpp): the layer's motion-compensated path without a
# window or a swapchain: images in, the in-between frames out, through the same blend and
# mc code the layer runs. For truth.py (tools/frames_truth.py) and the tests. Links the loader.
executable('frames-test', '../tools/frames_test.cpp', 'blend.cpp', 'mc.cpp', blend_spv, layer_spv,
  engine_src, shader_headers,
  include_directories: engine_inc,
  dependencies: [vulkan],
  cpp_args: ['-Wno-missing-field-initializers'],
  install: false)
