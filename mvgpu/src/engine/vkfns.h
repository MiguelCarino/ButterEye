// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// The Vulkan entry points the engine calls, as one table. The engine never calls
// Vulkan by name: the plugin fills the table from the loader (fill_from_loader in
// src/mvgpu.cpp), the present layer from the next layer's vkGetDeviceProcAddr
// (fill_from_device), because a layer that called the loader would re-enter the
// chain from the top. All are Vulkan 1.0 device-level functions.
#pragma once

#include <vulkan/vulkan.h>

namespace mvgpu {

#define MVGPU_VK_FNS(X)              \
    X(CreateBuffer)                  \
    X(DestroyBuffer)                 \
    X(GetBufferMemoryRequirements)   \
    X(AllocateMemory)                \
    X(FreeMemory)                    \
    X(BindBufferMemory)              \
    X(MapMemory)                     \
    X(UnmapMemory)                   \
    X(CreateDescriptorSetLayout)     \
    X(DestroyDescriptorSetLayout)    \
    X(CreatePipelineLayout)          \
    X(DestroyPipelineLayout)         \
    X(CreateShaderModule)            \
    X(DestroyShaderModule)           \
    X(CreateComputePipelines)        \
    X(DestroyPipeline)               \
    X(CreateDescriptorPool)          \
    X(DestroyDescriptorPool)         \
    X(AllocateDescriptorSets)        \
    X(UpdateDescriptorSets)          \
    X(CmdPipelineBarrier)            \
    X(CmdBindPipeline)               \
    X(CmdBindDescriptorSets)         \
    X(CmdPushConstants)              \
    X(CmdDispatch)                   \
    X(CmdCopyBuffer)                 \
    X(CmdUpdateBuffer)

struct VkFns {
#define MVGPU_X(n) PFN_vk##n n = nullptr;
    MVGPU_VK_FNS(MVGPU_X)
#undef MVGPU_X
};

// From the next layer's (or any) vkGetDeviceProcAddr; false when one is missing.
inline bool fill_from_device(VkFns& f, PFN_vkGetDeviceProcAddr gdpa, VkDevice dev) {
    bool ok = true;
#define MVGPU_X(n)                                                     \
    f.n = reinterpret_cast<PFN_vk##n>(gdpa(dev, "vk" #n));             \
    ok = ok && f.n != nullptr;
    MVGPU_VK_FNS(MVGPU_X)
#undef MVGPU_X
    return ok;
}

}  // namespace mvgpu
