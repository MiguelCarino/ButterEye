# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
# 1.1 is the lowest bound that works: it is the version that learned to read a
# meson.options file (rather than meson_options.txt), and nothing else here needs
# more. It cannot shut out a build host that worked before, because an older meson
# would have ignored meson.options and failed for want of -Dplugindir anyway - so
# the Fedora package (packaging/plugins/buttereye-vs-mvgpu.spec, unversioned
# BuildRequires: meson) is already building with 1.1 or newer.
project('mvgpu', 'cpp', version: '0.0.2', license: 'AGPL-3.0-or-later',
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
foreach s : ['unpack', 'down2', 'search', 'median', 'subpel', 'select', 'render', 'pack']
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
