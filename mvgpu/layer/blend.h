// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames, stage S3: the blend's GPU objects and commands.
//
// Per swapchain: two history images H[0..1] that hold the app's last two images, one
// output image Out with a layer per in-between frame, the compute pipeline of
// blend.comp, and a timestamp query pool. All three images are R32_UINT: every
// swapchain format handled here packs a pixel in 32 bits, so the copies in and out are
// bit for bit and the shader unpacks the channels itself (see blend.comp).
#pragma once

#include "layer.h"

#include <string>

namespace bef {

// how a 32-bit swapchain format packs its channels
struct FormatLayout {
    uint32_t bits = 0;     // per colour channel: 8 or 10
    uint32_t red_low = 0;  // 1: red is at bit 0
};
// false for formats the blend does not handle (FP16, anything not 32 bits a pixel)
bool format_layout(VkFormat f, FormatLayout* out);

// timestamps per app present: start, after the copy into H, after the dispatch, end
constexpr uint32_t kStamps = 4;

struct Blend {
    Device* d = nullptr;
    VkDevice dev = VK_NULL_HANDLE;
    VkExtent2D extent{};
    VkFormat format = VK_FORMAT_UNDEFINED;  // the swapchain's
    FormatLayout fl;
    uint32_t layers = 0;  // in-between frames Out can hold (kmax - 1)
    VkImage hist[2] = {};
    VkImage out = VK_NULL_HANDLE;
    VkDeviceMemory mem[3] = {};
    VkImageView hist_view[2] = {};
    VkImageView out_view = VK_NULL_HANDLE;
    VkDescriptorSetLayout dsl = VK_NULL_HANDLE;
    VkPipelineLayout pl = VK_NULL_HANDLE;
    VkPipeline pipe = VK_NULL_HANDLE;
    VkDescriptorPool dpool = VK_NULL_HANDLE;
    VkDescriptorSet sets[2] = {};  // sets[c]: prev = H[1 - c], cur = H[c], Out
    VkQueryPool queries = VK_NULL_HANDLE;
    uint32_t query_slots = 0;
    bool general = false;  // the images have been moved to GENERAL
    uint32_t cur = 0;      // the H holding the newest app image
    uint32_t have = 0;     // app images held in H (0..2)
    uint64_t bytes = 0;    // device memory the three images use
    uint32_t share[2] = {};  // with nshare 2: the images are CONCURRENT between these families
    uint32_t nshare = 0;
};

// makes everything; on failure, frees what was made and says why. other_family: a second queue
// family that uses the images too (the layer's own queue), so they are made CONCURRENT
bool blend_create(Blend& b, Device* d, VkDevice dev, VkExtent2D extent, VkFormat format, uint32_t layers,
                  uint32_t family, uint32_t query_slots, std::string* why, uint32_t other_family = UINT32_MAX);
void blend_destroy(Blend& b);

// Records the S3 work of one app present into cb, after the caller's barrier that put
// `app` in TRANSFER_SRC_OPTIMAL and every target in TRANSFER_DST_OPTIMAL: the copy of
// `app` into H, then (with two images in H) the blend into Out and the copies of Out's
// layers into the targets, else plain copies of `app` (as in S2). Returns true when it
// blended. Timestamps go to the query slot `qslot` when the pool exists.
bool blend_record(Blend& b, VkCommandBuffer cb, VkImage app, const VkImage* targets, uint32_t got, uint32_t k,
                  bool show, uint32_t qslot);
// the query slot's timestamps, in ns since its start (false when not available)
bool blend_times(Blend& b, uint32_t qslot, double ns[kStamps]);
// a later present will not follow on from the images in H (pass-through, event, error)
inline void blend_forget(Blend& b) { b.have = 0; }

// Writes H[prev], H[cur] and the first `got` layers of Out to `path` (a 64-byte header,
// then the raw 32-bit pixels, row by row). Submits on `queue` and waits. For checks only.
bool blend_dump(Blend& b, VkQueue queue, uint32_t family, uint32_t got, uint32_t k, bool show,
                VkColorSpaceKHR colour_space, const std::string& path);

}  // namespace bef
