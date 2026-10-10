# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
project('mvgpu', 'cpp', version: '0.1', license: 'AGPL-3.0-or-later',
        default_options: ['cpp_std=c++17', 'buildtype=release', 'warning_level=2'])

vapoursynth = dependency('vapoursynth', version: '>=55')
vulkan = dependency('vulkan')
glslang = find_program(get_option('glslang'), 'glslang', 'glslangValidator')

shader_headers = []
foreach s : ['unpack', 'down2', 'search', 'median', 'select', 'render', 'pack']
  shader_headers += custom_target(s + '_spv',
    input: 'shaders' / s + '.comp',
    output: s + '.h',
    command: [glslang, '-V', '--target-env', 'vulkan1.1', '--vn', s + '_spv',
              '@INPUT@', '-o', '@OUTPUT@'])
endforeach
shaders_h = configure_file(input: 'src/shaders.h.in', output: 'shaders.h', copy: true)

shared_module('mvgpu', 'src/mvgpu.cpp', shader_headers,
  dependencies: [vapoursynth, vulkan],
  cpp_args: ['-Wno-missing-field-initializers'],
  install: true,
  install_dir: get_option('plugindir'))
