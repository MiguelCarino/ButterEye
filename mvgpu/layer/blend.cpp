// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames, stage S3: the blend's GPU objects and commands (blend.h).
// Every call goes through the device's table, i.e. to the next layer, never to the loader.
#include "blend.h"

#include "blend_spv.h"

#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <vector>

namespace bef {

namespace {

struct Push {
    uint32_t bits, red_low, k, show, width, height;
};

int memory_type(const VkPhysicalDeviceMemoryProperties& mp, uint32_t bits, VkMemoryPropertyFlags want) {
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return static_cast<int>(i);
    return -1;
}

bool make_image(Blend& b, uint32_t layers, VkImageUsageFlags usage, VkImage* img, VkDeviceMemory* mem,
                VkImageView* view, bool array = false) {
    const DeviceTable& t = b.d->t;
    VkImageCreateInfo ici{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
    ici.imageType = VK_IMAGE_TYPE_2D;
    ici.format = VK_FORMAT_R32_UINT;
    ici.extent = {b.extent.width, b.extent.height, 1};
    ici.mipLevels = 1;
    ici.arrayLayers = layers;
    ici.samples = VK_SAMPLE_COUNT_1_BIT;
    ici.tiling = VK_IMAGE_TILING_OPTIMAL;
    ici.usage = usage;
    // shared with the layer's own queue when that is of another family (no ownership transfers)
    ici.sharingMode = b.nshare == 2 ? VK_SHARING_MODE_CONCURRENT : VK_SHARING_MODE_EXCLUSIVE;
    ici.queueFamilyIndexCount = b.nshare == 2 ? 2 : 0;
    ici.pQueueFamilyIndices = b.nshare == 2 ? b.share : nullptr;
    ici.initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    if (t.CreateImage(b.dev, &ici, nullptr, img) != VK_SUCCESS) return false;
    VkMemoryRequirements req;
    t.GetImageMemoryRequirements(b.dev, *img, &req);
    int type = memory_type(b.d->memory, req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
    if (type < 0) type = memory_type(b.d->memory, req.memoryTypeBits, 0);
    if (type < 0) return false;
    VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, nullptr, req.size,
                             static_cast<uint32_t>(type)};
    if (t.AllocateMemory(b.dev, &mai, nullptr, mem) != VK_SUCCESS) return false;
    b.bytes += req.size;
    if (t.BindImageMemory(b.dev, *img, *mem, 0) != VK_SUCCESS) return false;
    VkImageViewCreateInfo vci{VK_STRUCTURE_TYPE_IMAGE_VIEW_CREATE_INFO};
    vci.image = *img;
    // Out is an array view even with one layer (k = 2): the shaders declare uimage2DArray
    vci.viewType = layers > 1 || array ? VK_IMAGE_VIEW_TYPE_2D_ARRAY : VK_IMAGE_VIEW_TYPE_2D;
    vci.format = VK_FORMAT_R32_UINT;
    vci.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, layers};
    return t.CreateImageView(b.dev, &vci, nullptr, view) == VK_SUCCESS;
}

void image_barrier(VkImageMemoryBarrier& m, VkImage image, VkImageLayout from, VkImageLayout to,
                   VkAccessFlags src, VkAccessFlags dst, uint32_t layers = 1) {
    m = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    m.srcAccessMask = src;
    m.dstAccessMask = dst;
    m.oldLayout = from;
    m.newLayout = to;
    m.srcQueueFamilyIndex = m.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    m.image = image;
    m.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, layers};
}

VkImageCopy whole(uint32_t w, uint32_t h, uint32_t src_layer, uint32_t dst_layer) {
    VkImageCopy r{};
    r.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, src_layer, 1};
    r.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, dst_layer, 1};
    r.extent = {w, h, 1};
    return r;
}

}  // namespace

bool format_layout(VkFormat f, FormatLayout* out) {
    switch (f) {
        case VK_FORMAT_R8G8B8A8_UNORM:
        case VK_FORMAT_R8G8B8A8_SRGB:
        case VK_FORMAT_A8B8G8R8_UNORM_PACK32:
        case VK_FORMAT_A8B8G8R8_SRGB_PACK32:
            *out = {8, 1};
            return true;
        case VK_FORMAT_B8G8R8A8_UNORM:
        case VK_FORMAT_B8G8R8A8_SRGB:
            *out = {8, 0};
            return true;
        case VK_FORMAT_A2B10G10R10_UNORM_PACK32:
            *out = {10, 1};
            return true;
        case VK_FORMAT_A2R10G10B10_UNORM_PACK32:
            *out = {10, 0};
            return true;
        default:
            return false;
    }
}

bool blend_create(Blend& b, Device* d, VkDevice dev, VkExtent2D extent, VkFormat format, uint32_t layers,
                  uint32_t family, uint32_t query_slots, std::string* why, uint32_t other_family) {
    b = Blend{};
    if (other_family != UINT32_MAX && other_family != family) {
        b.share[0] = family;
        b.share[1] = other_family;
        b.nshare = 2;
    }
    b.d = d;
    b.dev = dev;
    b.extent = extent;
    b.format = format;
    b.layers = layers;
    auto fail = [&](const char* w) {
        *why = w;
        blend_destroy(b);
        return false;
    };
    if (!format_layout(format, &b.fl)) return fail("the swapchain format is not a 32-bit RGB(A) format");
    if (layers < 1) return fail("no in-between frames");
    const DeviceTable& t = d->t;
    if (d->instance && d->instance->t.GetPhysicalDeviceFormatProperties) {
        VkFormatProperties fp{};
        d->instance->t.GetPhysicalDeviceFormatProperties(d->physical, VK_FORMAT_R32_UINT, &fp);
        const VkFormatFeatureFlags need = VK_FORMAT_FEATURE_STORAGE_IMAGE_BIT | VK_FORMAT_FEATURE_TRANSFER_SRC_BIT |
                                          VK_FORMAT_FEATURE_TRANSFER_DST_BIT;
        if ((fp.optimalTilingFeatures & need) != need) return fail("R32_UINT cannot be a storage image here");
    }
    const VkImageUsageFlags hu = VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT |
                                 VK_IMAGE_USAGE_TRANSFER_SRC_BIT;
    for (int i = 0; i < 2; ++i)
        if (!make_image(b, 1, hu, &b.hist[i], &b.mem[i], &b.hist_view[i])) return fail("history image");
    if (!make_image(b, layers, VK_IMAGE_USAGE_STORAGE_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT, &b.out, &b.mem[2],
                    &b.out_view, true))
        return fail("output image");

    VkDescriptorSetLayoutBinding bind[3];
    for (uint32_t i = 0; i < 3; ++i)
        bind[i] = {i, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 1, VK_SHADER_STAGE_COMPUTE_BIT, nullptr};
    VkDescriptorSetLayoutCreateInfo dli{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, nullptr, 0, 3, bind};
    if (t.CreateDescriptorSetLayout(dev, &dli, nullptr, &b.dsl) != VK_SUCCESS) return fail("set layout");
    VkPushConstantRange pr{VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof(Push)};
    VkPipelineLayoutCreateInfo pli{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, nullptr, 0, 1, &b.dsl, 1, &pr};
    if (t.CreatePipelineLayout(dev, &pli, nullptr, &b.pl) != VK_SUCCESS) return fail("pipeline layout");
    VkShaderModuleCreateInfo smi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, nullptr, 0, sizeof(blend_spv),
                                 blend_spv};
    VkShaderModule sm = VK_NULL_HANDLE;
    if (t.CreateShaderModule(dev, &smi, nullptr, &sm) != VK_SUCCESS) return fail("shader module");
    VkComputePipelineCreateInfo cpi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    cpi.stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0, VK_SHADER_STAGE_COMPUTE_BIT, sm,
                 "main", nullptr};
    cpi.layout = b.pl;
    const VkResult pr2 = t.CreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpi, nullptr, &b.pipe);
    t.DestroyShaderModule(dev, sm, nullptr);
    if (pr2 != VK_SUCCESS) return fail("compute pipeline");

    VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 6};
    VkDescriptorPoolCreateInfo dpi{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, nullptr, 0, 2, 1, &ps};
    if (t.CreateDescriptorPool(dev, &dpi, nullptr, &b.dpool) != VK_SUCCESS) return fail("descriptor pool");
    VkDescriptorSetLayout dsls[2] = {b.dsl, b.dsl};
    VkDescriptorSetAllocateInfo dai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, nullptr, b.dpool, 2, dsls};
    if (t.AllocateDescriptorSets(dev, &dai, b.sets) != VK_SUCCESS) return fail("descriptor sets");
    for (uint32_t c = 0; c < 2; ++c) {
        VkDescriptorImageInfo ii[3] = {{VK_NULL_HANDLE, b.hist_view[1 - c], VK_IMAGE_LAYOUT_GENERAL},
                                       {VK_NULL_HANDLE, b.hist_view[c], VK_IMAGE_LAYOUT_GENERAL},
                                       {VK_NULL_HANDLE, b.out_view, VK_IMAGE_LAYOUT_GENERAL}};
        VkWriteDescriptorSet w[3];
        for (uint32_t i = 0; i < 3; ++i)
            w[i] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, nullptr, b.sets[c], i, 0, 1,
                    VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, &ii[i], nullptr, nullptr};
        t.UpdateDescriptorSets(dev, 3, w, 0, nullptr);
    }

    if (query_slots && family < d->timestamp_bits.size() && d->timestamp_bits[family] && d->timestamp_period > 0) {
        VkQueryPoolCreateInfo qi{VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO, nullptr, 0, VK_QUERY_TYPE_TIMESTAMP,
                                 query_slots * kStamps, 0};
        if (t.CreateQueryPool(dev, &qi, nullptr, &b.queries) == VK_SUCCESS) b.query_slots = query_slots;
        else b.queries = VK_NULL_HANDLE;
    }
    return true;
}

void blend_destroy(Blend& b) {
    if (!b.d) return;
    const DeviceTable& t = b.d->t;
    VkDevice dev = b.dev;
    if (b.queries) t.DestroyQueryPool(dev, b.queries, nullptr);
    if (b.dpool) t.DestroyDescriptorPool(dev, b.dpool, nullptr);
    if (b.pipe) t.DestroyPipeline(dev, b.pipe, nullptr);
    if (b.pl) t.DestroyPipelineLayout(dev, b.pl, nullptr);
    if (b.dsl) t.DestroyDescriptorSetLayout(dev, b.dsl, nullptr);
    if (b.out_view) t.DestroyImageView(dev, b.out_view, nullptr);
    for (VkImageView v : b.hist_view)
        if (v) t.DestroyImageView(dev, v, nullptr);
    if (b.out) t.DestroyImage(dev, b.out, nullptr);
    for (VkImage i : b.hist)
        if (i) t.DestroyImage(dev, i, nullptr);
    for (VkDeviceMemory m : b.mem)
        if (m) t.FreeMemory(dev, m, nullptr);
    b = Blend{};
}

bool blend_record(Blend& b, VkCommandBuffer cb, VkImage app, const VkImage* targets, uint32_t got, uint32_t k,
                  bool show, uint32_t qslot) {
    const DeviceTable& t = b.d->t;
    const uint32_t w = b.extent.width, h = b.extent.height;
    const bool timed = b.queries && qslot < b.query_slots;
    const uint32_t q0 = qslot * kStamps;
    if (timed) {
        t.CmdResetQueryPool(cb, b.queries, q0, kStamps);
        t.CmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, b.queries, q0);
    }
    // everything earlier on this queue that wrote H or Out (copies, the shader) is done
    // and visible before this present's copies and dispatch touch them
    VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr,
                       VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_WRITE_BIT,
                       VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT |
                           VK_ACCESS_SHADER_WRITE_BIT};
    VkImageMemoryBarrier first[3];
    uint32_t nfirst = 0;
    if (!b.general) {
        image_barrier(first[nfirst++], b.hist[0], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT);
        image_barrier(first[nfirst++], b.hist[1], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT);
        image_barrier(first[nfirst++], b.out, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_SHADER_WRITE_BIT, b.layers);
        b.general = true;
    }
    const VkPipelineStageFlags work = VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    t.CmdPipelineBarrier(cb, work, work, 0, 1, &mb, 0, nullptr, nfirst, first);

    // 1. the app's image into H
    const uint32_t next = b.have ? 1 - b.cur : b.cur;
    const VkImageCopy one = whole(w, h, 0, 0);
    t.CmdCopyImage(cb, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, b.hist[next], VK_IMAGE_LAYOUT_GENERAL, 1, &one);
    b.cur = next;
    if (b.have < 2) ++b.have;
    if (timed) t.CmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, b.queries, q0 + 1);

    const bool blend = b.have == 2 && got > 0;
    if (blend) {
        // 2. the blend: one dispatch, one z slice per in-between frame
        VkMemoryBarrier m2{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_TRANSFER_WRITE_BIT,
                           VK_ACCESS_SHADER_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 1, &m2, 0,
                             nullptr, 0, nullptr);
        t.CmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, b.pipe);
        t.CmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, b.pl, 0, 1, &b.sets[b.cur], 0, nullptr);
        const Push p{b.fl.bits, b.fl.red_low, k, show ? 1u : 0u, w, h};
        t.CmdPushConstants(cb, b.pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof p, &p);
        const uint32_t layers = got < b.layers ? got : b.layers;
        t.CmdDispatch(cb, (w + 15) / 16, (h + 15) / 16, layers);
        if (timed) t.CmdWriteTimestamp(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, b.queries, q0 + 2);
        VkMemoryBarrier m3{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_SHADER_WRITE_BIT,
                           VK_ACCESS_TRANSFER_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &m3, 0,
                             nullptr, 0, nullptr);
        // 3. Out's layers into the swapchain images, bit for bit
        for (uint32_t m = 0; m < got; ++m) {
            const VkImageCopy r = whole(w, h, m < layers ? m : layers - 1, 0);
            t.CmdCopyImage(cb, b.out, VK_IMAGE_LAYOUT_GENERAL, targets[m], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &r);
        }
    } else {
        // only one app image so far: copies, as in S2
        if (timed) t.CmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, b.queries, q0 + 2);
        for (uint32_t m = 0; m < got; ++m)
            t.CmdCopyImage(cb, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, targets[m],
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &one);
    }
    if (timed) t.CmdWriteTimestamp(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, b.queries, q0 + 3);
    return blend;
}

bool blend_times(Blend& b, uint32_t qslot, double ns[kStamps]) {
    if (!b.queries || qslot >= b.query_slots) return false;
    uint64_t v[kStamps];
    const VkResult r = b.d->t.GetQueryPoolResults(b.dev, b.queries, qslot * kStamps, kStamps, sizeof v, v,
                                                  sizeof(uint64_t), VK_QUERY_RESULT_64_BIT);
    if (r != VK_SUCCESS) return false;
    for (uint32_t i = 0; i < kStamps; ++i)
        ns[i] = static_cast<double>(v[i] - v[0]) * static_cast<double>(b.d->timestamp_period);
    return true;
}

bool blend_dump(Blend& b, VkQueue queue, uint32_t family, uint32_t got, uint32_t k, bool show,
                VkColorSpaceKHR colour_space, const std::string& path) {
    const DeviceTable& t = b.d->t;
    VkDevice dev = b.dev;
    const uint32_t w = b.extent.width, h = b.extent.height;
    const uint32_t layers = got < b.layers ? got : b.layers;
    const VkDeviceSize plane = static_cast<VkDeviceSize>(w) * h * 4;
    const VkDeviceSize size = plane * (2 + layers);
    bool ok = false;
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    VkCommandPool pool = VK_NULL_HANDLE;
    VkCommandBuffer cb = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    do {
        VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, nullptr, 0, size, VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                              VK_SHARING_MODE_EXCLUSIVE, 0, nullptr};
        if (t.CreateBuffer(dev, &bi, nullptr, &buf) != VK_SUCCESS) break;
        VkMemoryRequirements req;
        t.GetBufferMemoryRequirements(dev, buf, &req);
        const int type = memory_type(b.d->memory, req.memoryTypeBits,
                                     VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        if (type < 0) break;
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, nullptr, req.size,
                                 static_cast<uint32_t>(type)};
        if (t.AllocateMemory(dev, &mai, nullptr, &mem) != VK_SUCCESS) break;
        if (t.BindBufferMemory(dev, buf, mem, 0) != VK_SUCCESS) break;
        VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr, 0, family};
        if (t.CreateCommandPool(dev, &pci, nullptr, &pool) != VK_SUCCESS) break;
        VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, pool,
                                        VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
        if (t.AllocateCommandBuffers(dev, &cai, &cb) != VK_SUCCESS) break;
        if (b.d->set_loader_data && b.d->set_loader_data(dev, cb) != VK_SUCCESS) break;
        VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, nullptr, 0};
        if (t.CreateFence(dev, &fi, nullptr, &fence) != VK_SUCCESS) break;
        VkCommandBufferBeginInfo cbi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                     VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
        if (t.BeginCommandBuffer(cb, &cbi) != VK_SUCCESS) break;
        VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr,
                           VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                             VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &mb, 0, nullptr, 0, nullptr);
        VkBufferImageCopy r{};
        r.imageExtent = {w, h, 1};
        r.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        r.bufferOffset = 0;
        t.CmdCopyImageToBuffer(cb, b.hist[1 - b.cur], VK_IMAGE_LAYOUT_GENERAL, buf, 1, &r);
        r.bufferOffset = plane;
        t.CmdCopyImageToBuffer(cb, b.hist[b.cur], VK_IMAGE_LAYOUT_GENERAL, buf, 1, &r);
        for (uint32_t m = 0; m < layers; ++m) {
            r.bufferOffset = plane * (2 + m);
            r.imageSubresource.baseArrayLayer = m;
            t.CmdCopyImageToBuffer(cb, b.out, VK_IMAGE_LAYOUT_GENERAL, buf, 1, &r);
        }
        VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_TRANSFER_WRITE_BIT,
                           VK_ACCESS_HOST_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, nullptr,
                             0, nullptr);
        if (t.EndCommandBuffer(cb) != VK_SUCCESS) break;
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 0, nullptr, nullptr, 1, &cb, 0, nullptr};
        if (t.QueueSubmit(queue, 1, &si, fence) != VK_SUCCESS) break;
        if (t.WaitForFences(dev, 1, &fence, VK_TRUE, 5000000000ull) != VK_SUCCESS) break;
        void* p = nullptr;
        if (t.MapMemory(dev, mem, 0, size, 0, &p) != VK_SUCCESS) break;
        FILE* f = std::fopen(path.c_str(), "wb");
        if (f) {
            // header: 16 little-endian u32, then the planes
            const uint32_t head[16] = {0x44464542u /* "BEFD" */, 1, w, h, b.fl.bits, b.fl.red_low, k, layers,
                                       show ? 1u : 0u, static_cast<uint32_t>(b.format),
                                       static_cast<uint32_t>(colour_space), 0, 0, 0, 0, 0};
            ok = std::fwrite(head, sizeof head, 1, f) == 1 &&
                 std::fwrite(p, 1, static_cast<size_t>(size), f) == static_cast<size_t>(size);
            ok = std::fclose(f) == 0 && ok;
        }
        t.UnmapMemory(dev, mem);
    } while (false);
    if (fence) t.DestroyFence(dev, fence, nullptr);
    if (pool) t.DestroyCommandPool(dev, pool, nullptr);
    if (buf) t.DestroyBuffer(dev, buf, nullptr);
    if (mem) t.FreeMemory(dev, mem, nullptr);
    return ok;
}

}  // namespace bef
