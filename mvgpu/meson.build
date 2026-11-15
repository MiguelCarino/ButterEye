# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# 1.1 is the lowest bound that works: it is the version that learned to read a
# meson.options file (rather than meson_options.txt), and nothing else here needs
# more. It cannot shut out a build host that worked before, because an older meson
# would have ignored meson.options and failed for want of -Dplugindir anyway - so
# the Fedora package (packaging/plugins/buttereye-vs-mvgpu.spec, unversioned
# BuildRequires: meson) is already building with 1.1 or newer.
project('mvgpu', 'cpp', version: '0.0.4', license: 'AGPL-3.0-or-later',
        meson_version: '>=1.1',
        default_options: ['cpp_std=c++17', 'buildtype=release', 'warning_level=2'])

vapoursynth = dependency('vapoursynth', version: '>=55')
vulkan = dependency('vulkan')
glslang = find_program(get_option('glslang'), 'glslang', 'glslangValidator')

# VapourSynth plugin versions have a major and a minor part only: 0.0.1 is
# reported as (0, 1) and x.y.z as (x, y * 100 + z), always from this version
ver = meson.project_version().split('.')
add_project_arguments(
  '-DMVGPU_VERSION_MAJOR=' + ver[0],
  '-DMVGPU_VERSION_MINOR=' + (ver[1].to_int() * 100 + ver[2].to_int()).to_string(),
  language: 'cpp')

shader_headers = []
foreach s : ['unpack', 'down2', 'search', 'median', 'subpel', 'uni', 'proj', 'select', 'render', 'pack', 'mean']
  shader_headers += custom_target(s + '_spv',
    input: 'shaders' / s + '.comp',
    output: s + '.h',
    command: [glslang, '-V', '--target-env', 'vulkan1.1', '--vn', s + '_spv',
              '@INPUT@', '-o', '@OUTPUT@'])
endforeach
shaders_h = configure_file(input: 'src/shaders.h.in', output: 'shaders.h', copy: true)

# the engine (src/engine): the GPU work, shared with the present layer; it calls Vulkan
# only through a function table, so it builds into the layer without the loader
engine_src = files('src/engine/engine.cpp')
engine_inc = include_directories('src', 'src/engine')

shared_module('mvgpu', 'src/mvgpu.cpp', engine_src, shader_headers,
  include_directories: engine_inc,
  dependencies: [vapoursynth, vulkan],
  cpp_args: ['-Wno-missing-field-initializers'],
  install: true,
  install_dir: get_option('plugindir'))

# the present layer (mvgpu/layer); off unless asked for, so the plugin build is unchanged.
# With it comes engine-check (tools/), a developer check of the engine's GPU-side
# brightness path against the plugin's
if get_option('layer').enabled()
  executable('engine-check', 'tools/engine_check.cpp', engine_src, shader_headers,
    include_directories: engine_inc,
    dependencies: [vulkan],
    cpp_args: ['-Wno-missing-field-initializers'],
    install: false)
  # installed (meson install) as an explicit layer: the library in a private folder and
  # its manifest in an explicit_layer.d folder. The loader lists an explicit layer but
  # loads it only when asked to (VK_LOADER_LAYERS_ENABLE or VK_INSTANCE_LAYERS), which
  # ButterEye does only for an mpv it starts in layer mode (mpvctl/frames.py layer_env)
  layerdir = get_option('layerdir') != '' ? get_option('layerdir') : get_option('libdir') / 'buttereye'
  subdir('layer')
  layer_manifestdir = get_option('layer_manifestdir')
  if layer_manifestdir == ''
    layer_manifestdir = get_option('datadir') / 'vulkan' / 'explicit_layer.d'
  endif
  layer_file = 'libVkLayer_buttereye_frames.so'
  if get_option('layer_library_path') == 'relative'
    # a path with a slash is taken relative to the manifest's folder
    if layerdir.startswith('/') or layer_manifestdir.startswith('/')
      error('-Dlayer_library_path=relative needs layerdir and layer_manifestdir inside the prefix')
    endif
    up = ''
    foreach part : layer_manifestdir.split('/')
      if part == '..'
        error('layer_manifestdir must not contain ..')
      elif part != '' and part != '.'
        up += '../'
      endif
    endforeach
    layer_library_path = up + (layerdir / layer_file)
  else
    layer_library_path = get_option('prefix') / layerdir / layer_file
  endif
  installed_manifest = configure_file(input: 'layer/VkLayer_buttereye_frames.json.in',
    output: 'VkLayer_buttereye_frames.installed.json',
    configuration: {
      'LIBRARY_PATH': layer_library_path,
      'API_VERSION': vulkan.version(),
    })
  install_data(installed_manifest, install_dir: layer_manifestdir,
    rename: 'VkLayer_buttereye_frames.json', install_mode: 'rw-r--r--')
endif
