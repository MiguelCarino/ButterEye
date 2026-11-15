// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// The engine (engine.h). No Vulkan function is called by name here: with no
// prototypes, a call that bypassed the table would not compile.
#define VK_NO_PROTOTYPES
#include "engine.h"

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <string>
#include <vector>

#include "shaders.h"

namespace mvgpu {

namespace {

bool env_is(const char* name, const char* value) {
    const char* v = std::getenv(name);
    return v && std::string(v) == value;
}

}  // namespace

Tuning Tuning::from_env() {
    Tuning t;
    t.tri = env_is("MVGPU_TRI", "1");
    t.show = std::getenv("MVGPU_SHOW") ? std::atoi(std::getenv("MVGPU_SHOW")) : 0;
    t.tri_dbg = std::getenv("MVGPU_TRI_DBG") ? std::atoi(std::getenv("MVGPU_TRI_DBG")) : 0;
    t.edge = env_is("MVGPU_EDGE", "1");
    t.uni = !env_is("MVGPU_UNI", "0");
    t.uni_land = std::getenv("MVGPU_UNI_LAND") ? float(std::atof(std::getenv("MVGPU_UNI_LAND"))) : 4.0f;
    t.fine = !env_is("MVGPU_FINE", "0");
    t.subpel_steps = env_is("MVGPU_SUBPEL", "0") ? 0 : 1;
    t.checked_median = !env_is("MVGPU_MEDIAN", "0");
    t.tpen = std::getenv("MVGPU_TEMPORAL") ? float(std::atof(std::getenv("MVGPU_TEMPORAL"))) : 0.0f;
    t.act = std::getenv("MVGPU_ACT") ? float(std::atof(std::getenv("MVGPU_ACT"))) : 0.5f;
    t.select_vectors = !env_is("MVGPU_SELECT", "0");
    if (const char* occ = std::getenv("MVGPU_OCC")) std::sscanf(occ, "%f,%f", &t.occ_lo, &t.occ_hi);
    if (const char* sm = std::getenv("MVGPU_SELM")) t.sel_margin = float(std::atof(sm));
    if (const char* fail = std::getenv("MVGPU_FAIL")) std::sscanf(fail, "%f,%f", &t.fail_lo, &t.fail_hi);
    if (const char* ut = std::getenv("MVGPU_UNI_T"))
        std::sscanf(ut, "%f,%f,%f", &t.uni_flag, &t.uni_abs, &t.uni_rel);
    if (const char* tt = std::getenv("MVGPU_TRI_T"))
        std::sscanf(tt, "%f,%f,%f,%f,%f,%f,%f", &t.tri_gate, &t.tri_thr, &t.tri_ratio, &t.tri_pre, &t.tri_bad,
                    &t.tri_fine, &t.tri_reach);
    if (const char* et = std::getenv("MVGPU_EDGE_T")) std::sscanf(et, "%f,%f", &t.edge_thr, &t.edge_act);
    return t;
}

uint32_t Memory::find_mem(uint32_t type_bits, VkMemoryPropertyFlags want) const {
    // Host-visible buffers are staging: prefer system RAM. With Resizable BAR the
    // first host-visible type is often VRAM mapped over PCIe, where CPU reads run
    // at ~1 GB/s and writes at ~3 GB/s (measured: 22 ms to read one 4K frame).
    bool host = want & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
    for (int pass = 0; pass < 2; ++pass)
        for (uint32_t i = 0; i < memprops.memoryTypeCount; ++i) {
            VkMemoryPropertyFlags f = memprops.memoryTypes[i].propertyFlags;
            if (!(type_bits & (1u << i)) || (f & want) != want) continue;
            if (host && pass == 0 && (f & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT)) continue;
            return i;
        }
    // a readback buffer without a cached type: settle for uncached host memory
    if (want & VK_MEMORY_PROPERTY_HOST_CACHED_BIT)
        return find_mem(type_bits, want & ~VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
    throw std::runtime_error("no suitable GPU memory type");
}

Buffer Memory::make(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags props) {
    Buffer b;
    b.size = std::max<VkDeviceSize>(size, 16);
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = b.size;
    bi.usage = usage;
    bi.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    check(vk.CreateBuffer(dev, &bi, nullptr, &b.buf), "vkCreateBuffer");
    try {
        VkMemoryRequirements req;
        vk.GetBufferMemoryRequirements(dev, b.buf, &req);
        VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        ai.allocationSize = req.size;
        ai.memoryTypeIndex = find_mem(req.memoryTypeBits, props);
        check(vk.AllocateMemory(dev, &ai, nullptr, &b.mem), "vkAllocateMemory");
        check(vk.BindBufferMemory(dev, b.buf, b.mem, 0), "vkBindBufferMemory");
        if (props & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
            check(vk.MapMemory(dev, b.mem, 0, VK_WHOLE_SIZE, 0, &b.map), "vkMapMemory");
    } catch (...) {
        free(b);
        throw;
    }
    return b;
}

void Memory::free(Buffer& b) {
    if (b.map) vk.UnmapMemory(dev, b.mem);
    if (b.buf) vk.DestroyBuffer(dev, b.buf, nullptr);
    if (b.mem) vk.FreeMemory(dev, b.mem, nullptr);
    b = Buffer{};
}

void Engine::check_limits(const VkPhysicalDeviceLimits& limits, int w, int h) {
    // the coarse search runs 512 invocations per group; 1-D passes use 256 x rows
    auto need = [](bool ok, const std::string& what) {
        if (!ok) throw std::runtime_error("GPU limit: " + what);
    };
    need(limits.maxComputeWorkGroupInvocations >= 512 && limits.maxComputeWorkGroupSize[0] >= 512 &&
             limits.maxComputeWorkGroupSize[1] >= 16,
         "compute work groups of 512 invocations are not supported");
    // the largest storage buffer is the per-pixel vector field (16 bytes a pixel)
    VkDeviceSize field_bytes = VkDeviceSize(w) * h * 16;
    need(field_bytes <= limits.maxStorageBufferRange,
         std::to_string(w) + "x" + std::to_string(h) + " needs storage buffers of " +
             std::to_string(field_bytes >> 20) + " MiB; this GPU allows " +
             std::to_string(limits.maxStorageBufferRange >> 20) + " MiB");
    uint64_t samples = uint64_t(w) * h;
    uint64_t groups = (samples + 255) / 256;
    uint64_t gx = std::min<uint64_t>(groups, limits.maxComputeWorkGroupCount[0]);
    need(gx > 0 && (groups + gx - 1) / gx <= limits.maxComputeWorkGroupCount[1],
         "the frame is too large for this GPU's dispatch size");
}

Engine::Engine(const VkFns& fns, VkDevice device, const VkPhysicalDeviceMemoryProperties& memprops,
               const VkPhysicalDeviceLimits& lim, int width, int height, int bitdepth, const Tuning& tuning,
               bool gdc_from_buffer)
    : vk(fns), dev(device), mem(vk, device, memprops), limits(lim), tune(tuning), gdc_buf(gdc_from_buffer),
      w(width), h(height), bits(bitdepth) {
    check_limits(limits, w, h);
    // a constructor that throws never runs ~Engine(): release what init() made
    try {
        init();
    } catch (...) {
        destroy();
        throw;
    }
}

Engine::~Engine() { destroy(); }

Pipe Engine::pipeline(const uint32_t* code, size_t nbytes, int bindings, uint32_t push, uint32_t wg, uint32_t kids,
                      bool gdc) {
    // a shader that reads gdc has one binding more: the gdc buffer, last
    if (gdc) ++bindings;
    Pipe p;
    p.bindings = bindings;
    VkShaderModule mod = VK_NULL_HANDLE;
    try {
        std::vector<VkDescriptorSetLayoutBinding> b(bindings);
        for (int i = 0; i < bindings; ++i) {
            b[i] = {};
            b[i].binding = i;
            b[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
            b[i].descriptorCount = 1;
            b[i].stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
        }
        VkDescriptorSetLayoutCreateInfo dl{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO};
        dl.bindingCount = bindings;
        dl.pBindings = b.data();
        check(vk.CreateDescriptorSetLayout(dev, &dl, nullptr, &p.dsl), "vkCreateDescriptorSetLayout");
        VkPushConstantRange pr{VK_SHADER_STAGE_COMPUTE_BIT, 0, push};
        VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
        pl.setLayoutCount = 1;
        pl.pSetLayouts = &p.dsl;
        pl.pushConstantRangeCount = 1;
        pl.pPushConstantRanges = &pr;
        check(vk.CreatePipelineLayout(dev, &pl, nullptr, &p.layout), "vkCreatePipelineLayout");
        VkShaderModuleCreateInfo sm{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
        sm.codeSize = nbytes;
        sm.pCode = code;
        check(vk.CreateShaderModule(dev, &sm, nullptr, &mod), "vkCreateShaderModule");
        // constant 0 the group size and 1 the child blocks per group (search.comp);
        // 2 GDC_FROM_BUFFER. Without either nothing is specialized, as the plugin always did.
        const uint32_t data[3] = {wg, kids, VK_TRUE};
        VkSpecializationMapEntry entries[3];
        uint32_t n = 0;
        if (wg) {
            entries[n++] = {0, 0, sizeof(uint32_t)};
            entries[n++] = {1, 4, sizeof(uint32_t)};
        }
        if (gdc && gdc_buf) entries[n++] = {2, 8, sizeof(VkBool32)};
        VkSpecializationInfo spec{n, entries, sizeof(data), data};
        VkComputePipelineCreateInfo ci{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
        ci.stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO};
        ci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        ci.stage.module = mod;
        ci.stage.pName = "main";
        ci.stage.pSpecializationInfo = n ? &spec : nullptr;
        ci.layout = p.layout;
        check(vk.CreateComputePipelines(dev, VK_NULL_HANDLE, 1, &ci, nullptr, &p.pipe), "vkCreateComputePipelines");
    } catch (...) {
        if (mod) vk.DestroyShaderModule(dev, mod, nullptr);
        if (p.layout) vk.DestroyPipelineLayout(dev, p.layout, nullptr);
        if (p.dsl) vk.DestroyDescriptorSetLayout(dev, p.dsl, nullptr);
        throw;
    }
    vk.DestroyShaderModule(dev, mod, nullptr);
    return p;
}

VkDescriptorSet Engine::set(const Pipe& p, std::initializer_list<const Buffer*> bufs) {
    VkDescriptorSetAllocateInfo ai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    ai.descriptorPool = dpool;
    ai.descriptorSetCount = 1;
    ai.pSetLayouts = &p.dsl;
    VkDescriptorSet s;
    check(vk.AllocateDescriptorSets(dev, &ai, &s), "vkAllocateDescriptorSets");
    std::vector<VkDescriptorBufferInfo> infos;
    infos.reserve(bufs.size() + 1);
    for (const Buffer* b : bufs) infos.push_back({b->buf, 0, VK_WHOLE_SIZE});
    // the pipeline's last binding is the gdc buffer when it has one more than given
    if (infos.size() + 1 == size_t(p.bindings)) infos.push_back({gdcv.buf, 0, VK_WHOLE_SIZE});
    std::vector<VkWriteDescriptorSet> wr(infos.size());
    for (size_t i = 0; i < infos.size(); ++i) {
        wr[i] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        wr[i].dstSet = s;
        wr[i].dstBinding = uint32_t(i);
        wr[i].descriptorCount = 1;
        wr[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        wr[i].pBufferInfo = &infos[i];
    }
    vk.UpdateDescriptorSets(dev, uint32_t(wr.size()), wr.data(), 0, nullptr);
    return s;
}

void Engine::init() {
    bytes = bits > 8 ? 2 : 1;
    // layout: Y, U, V (4:2:0), float offsets and 4-byte aligned packed offsets
    int cw = (w + 1) / 2, ch = (h + 1) / 2;
    planes[0] = {w, h, 0, 0};
    planes[1] = {cw, ch, uint32_t(w * h), 0};
    planes[2] = {cw, ch, uint32_t(w * h + cw * ch), 0};
    floats = uint32_t(w * h + 2 * cw * ch);
    uint32_t off = 0;
    for (auto& p : planes) {
        p.raw_off = off;
        off += (uint32_t(p.w * p.h * bytes) + 3u) & ~3u;
    }
    raw_bytes = off;
    uint32_t po = 0;
    for (int l = 0; l < LEVELS; ++l) {
        lw[l] = l ? (lw[l - 1] + 1) / 2 : w;
        lh[l] = l ? (lh[l - 1] + 1) / 2 : h;
        bw[l] = (lw[l] + BLOCK - 1) / BLOCK;
        bh[l] = (lh[l] + BLOCK - 1) / BLOCK;
        loff[l] = po;
        po += uint32_t(lw[l] * lh[l]);
    }
    pyr_floats = po;

    const auto DEV = VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT;
    const auto SB = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    const auto TD = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    raw_in = mem.make(raw_bytes, SB | TD, DEV);
    raw_out = mem.make(raw_bytes, SB | VK_BUFFER_USAGE_TRANSFER_SRC_BIT, DEV);
    out = mem.make(VkDeviceSize(floats) * 4, SB, DEV);
    field = mem.make(VkDeviceSize(w) * h * 16, SB, DEV);
    // record_load copies each slot's luma plane into its pyramid: transfer usage on
    // both (missing before the engine was split out; validation reported it)
    for (int s = 0; s < SLOTS; ++s) {
        slot[s] = mem.make(VkDeviceSize(floats) * 4, SB | VK_BUFFER_USAGE_TRANSFER_SRC_BIT, DEV);
        pyr[s] = mem.make(VkDeviceSize(pyr_floats) * 4, SB | TD, DEV);
    }
    for (int l = 0; l < LEVELS; ++l) {
        vraw[l] = mem.make(VkDeviceSize(bw[l]) * bh[l] * 8, SB, DEV);
        vec[l] = mem.make(VkDeviceSize(bw[l]) * bh[l] * 8, SB, DEV);
    }
    bw4 = (w + 3) / 4;
    bh4 = (h + 3) / 4;
    vraw4 = mem.make(VkDeviceSize(bw4) * bh4 * 8, SB, DEV);
    vec4 = mem.make(VkDeviceSize(bw4) * bh4 * 8, SB, DEV);
    vsub = mem.make(VkDeviceSize(std::max(bw4 * bh4, bw[0] * bh[0])) * 8, SB, DEV);
    for (int s = 0; s < SLOTS; ++s) {
        vfin[s] = mem.make(VkDeviceSize(std::max(bw4 * bh4, bw[0] * bh[0])) * 8, SB, DEV);
        vsd[s] = mem.make(VkDeviceSize(std::max(bw4 * bh4, bw[0] * bh[0])) * 4, SB, DEV);
    }
    ufwd = mem.make(VkDeviceSize(bw[0]) * bh[0] * 16, SB, DEV);
    ubwd = mem.make(VkDeviceSize(bw[0]) * bh[0] * 16, SB, DEV);
    gdcv = mem.make(4 * sizeof(float), SB, DEV);
    if (gdc_buf) {
        means = mem.make(SLOTS * sizeof(float), SB | TD | VK_BUFFER_USAGE_TRANSFER_SRC_BIT, DEV);
        mean_rows = mem.make(VkDeviceSize((h + 3) / 4) * sizeof(float), SB, DEV);
    }

    // 20 sets and up to 104 buffers a slot, plus the pack's
    VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 512};
    VkDescriptorPoolCreateInfo dp{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dp.maxSets = 128;
    dp.poolSizeCount = 1;
    dp.pPoolSizes = &ps;
    check(vk.CreateDescriptorPool(dev, &dp, nullptr, &dpool), "vkCreateDescriptorPool");

    // group sizes: powers of two >= the 17*17, 5*3*3 and 5*5*5 candidates
    const bool G = true;  // reads gdc
    p_unpack = pipeline(unpack_spv, sizeof(unpack_spv), 2, 20);
    p_down2 = pipeline(down2_spv, sizeof(down2_spv), 1, 24);
    p_search_coarse = pipeline(search_spv, sizeof(search_spv), 5, 64, 512, 1, G);
    p_search_fine = pipeline(search_spv, sizeof(search_spv), 5, 64, 64, 1, G);
    p_search_full = pipeline(search_spv, sizeof(search_spv), 5, 64, 256, 1, G);
    p_search4 = pipeline(search_spv, sizeof(search_spv), 5, 64, 256, 4, G);
    p_median = pipeline(median_spv, sizeof(median_spv), 4, 44, 0, 1, G);
    p_subpel = pipeline(subpel_spv, sizeof(subpel_spv), 4, 32, 0, 1, G);
    p_uni = pipeline(uni_spv, sizeof(uni_spv), 4, 56, 0, 1, G);
    p_proj = pipeline(proj_spv, sizeof(proj_spv), 7, 52, 0, 1, G);
    p_select = pipeline(select_spv, sizeof(select_spv), 9, 92, 0, 1, G);
    p_render = pipeline(render_spv, sizeof(render_spv), 4, 48);
    p_pack = pipeline(pack_spv, sizeof(pack_spv), 2, 20);
    if (gdc_buf) p_mean = pipeline(mean_spv, sizeof(mean_spv), 4, 32);

    for (int s = 0; s < SLOTS; ++s) {
        const int c = (s + 1) % SLOTS, b = (s + SLOTS - 1) % SLOTS, n = (s + 2) % SLOTS;
        ds_unpack[s] = set(p_unpack, {&raw_in, &slot[s]});
        ds_down2[s] = set(p_down2, {&pyr[s]});
        ds_subpel[s] = set(p_subpel, {&pyr[s], &pyr[c], &vec[0], &vsub});
        ds_subpel4[s] = set(p_subpel, {&pyr[s], &pyr[c], &vec4, &vsub});
        ds_select[s] = set(p_select, {&slot[s], &slot[c], &vfin[s], &field, &vsd[s], &slot[b], &slot[n], &vfin[b], &vfin[c]});
        ds_uni_f[s] = set(p_uni, {&pyr[s], &pyr[c], &vsub, &ufwd});
        ds_uni_b[s] = set(p_uni, {&pyr[s], &pyr[c], &vsub, &ubwd});
        ds_proj[s] = set(p_proj, {&pyr[s], &pyr[c], &vsub, &ufwd, &ubwd, &vfin[s], &vsd[s]});
        ds_search4[s] = set(p_search4, {&pyr[s], &pyr[c], &vec[0], &vraw4, &vec4});
        ds_render[s] = set(p_render, {&slot[s], &slot[c], &field, &out});
        for (int l = 0; l < LEVELS; ++l) {
            const Pipe& sp = l == LEVELS - 1 ? p_search_coarse : l == 0 ? p_search_full : p_search_fine;
            const Buffer& parent = l == LEVELS - 1 ? vec[l] : vec[l + 1];
            ds_search[s][l] = set(sp, {&pyr[s], &pyr[c], &parent, &vraw[l], &vec[l]});
        }
        if (gdc_buf) ds_mean[s] = set(p_mean, {&slot[s], &mean_rows, &means, &gdcv});
    }
    for (int s = 0; s < SLOTS; ++s) {
        const int c = (s + 1) % SLOTS;
        for (int l = 0; l < LEVELS; ++l) ds_median[s][l] = set(p_median, {&pyr[s], &pyr[c], &vraw[l], &vec[l]});
        ds_median4[s] = set(p_median, {&pyr[s], &pyr[c], &vraw4, &vec4});
    }
    ds_pack = set(p_pack, {&out, &raw_out});
}

void Engine::destroy() {
    // safe on a partly built engine: every handle is checked. The caller has made
    // sure the device is idle (or at least done with the engine's work).
    for (Pipe* p : {&p_unpack, &p_down2, &p_search_coarse, &p_search_fine, &p_search_full, &p_search4, &p_median,
                    &p_subpel, &p_uni, &p_proj, &p_select, &p_render, &p_pack, &p_mean}) {
        if (p->pipe) vk.DestroyPipeline(dev, p->pipe, nullptr);
        if (p->layout) vk.DestroyPipelineLayout(dev, p->layout, nullptr);
        if (p->dsl) vk.DestroyDescriptorSetLayout(dev, p->dsl, nullptr);
        *p = Pipe{};
    }
    if (dpool) vk.DestroyDescriptorPool(dev, dpool, nullptr);
    dpool = VK_NULL_HANDLE;
    for (Buffer* b : {&raw_in, &raw_out, &out, &field, &vraw4, &vec4, &vsub, &ufwd, &ubwd, &gdcv, &means, &mean_rows})
        mem.free(*b);
    for (int i = 0; i < SLOTS; ++i) {
        mem.free(slot[i]);
        mem.free(pyr[i]);
        mem.free(vfin[i]);
        mem.free(vsd[i]);
    }
    for (int l = 0; l < LEVELS; ++l) {
        mem.free(vraw[l]);
        mem.free(vec[l]);
    }
}

uint64_t Engine::memory_bytes() const {
    uint64_t n = 0;
    for (const Buffer* b : {&raw_in, &raw_out, &out, &field, &vraw4, &vec4, &vsub, &ufwd, &ubwd, &gdcv, &means,
                            &mean_rows})
        n += b->size;
    for (int i = 0; i < SLOTS; ++i) n += slot[i].size + pyr[i].size + vfin[i].size + vsd[i].size;
    for (int l = 0; l < LEVELS; ++l) n += vraw[l].size + vec[l].size;
    return n;
}

void Engine::barrier(VkCommandBuffer cmd) const {
    VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    mb.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    // transfer writes too: in one command buffer (the layer) the next frame's copy into
    // raw_in follows the last one's (the plugin submits each load on its own)
    mb.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_READ_BIT |
                       VK_ACCESS_TRANSFER_WRITE_BIT;
    vk.CmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                          VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &mb, 0,
                          nullptr, 0, nullptr);
}

void Engine::dispatch1d(VkCommandBuffer cmd, uint32_t groups) const {
    // 1-D passes as rows of at most maxComputeWorkGroupCount[0] groups (65535 on
    // some drivers; 8K luma alone needs 129,600 groups of 256)
    uint32_t gx = std::max<uint32_t>(1, std::min(groups, limits.maxComputeWorkGroupCount[0]));
    vk.CmdDispatch(cmd, gx, (groups + gx - 1) / gx, 1);
}

void Engine::push(VkCommandBuffer cmd, const Pipe& p, VkDescriptorSet s, const void* pc, uint32_t size) const {
    vk.CmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p.pipe);
    vk.CmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p.layout, 0, 1, &s, 0, nullptr);
    vk.CmdPushConstants(cmd, p.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, size, pc);
}

void Engine::record_set_means(VkCommandBuffer cmd, const float (&m)[SLOTS]) {
    if (!gdc_buf) return;
    vk.CmdUpdateBuffer(cmd, means.buf, 0, sizeof(m), m);
    barrier(cmd);
}

void Engine::record_gdc(VkCommandBuffer cmd, int a) {
    const int c = (a + 1) % SLOTS, b = (a + SLOTS - 1) % SLOTS, n = (a + 2) % SLOTS;
    struct { int32_t w, h, mode, slot, a, c, b, n; } pc{w, h, 2, a, a, c, b, n};
    push(cmd, p_mean, ds_mean[a], &pc, sizeof(pc));
    vk.CmdDispatch(cmd, 1, 1, 1);
    barrier(cmd);
}

void Engine::record_load(VkCommandBuffer cmd, int s) {
    float scale = 1.0f / float((1u << bits) - 1u);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t count, raw_off, out_off, bytes; float scale; } pc{
            uint32_t(p.w * p.h), p.raw_off, p.float_off, uint32_t(bytes), scale};
        push(cmd, p_unpack, ds_unpack[s], &pc, sizeof(pc));
        dispatch1d(cmd, (pc.count + 255) / 256);
    }
    barrier(cmd);
    record_pyramid(cmd, s);
}

void Engine::record_pyramid(VkCommandBuffer cmd, int s) {
    // pyramid level 0 is the luma plane: copy it, then halve
    VkBufferCopy yc{0, VkDeviceSize(loff[0]) * 4, VkDeviceSize(w) * h * 4};
    vk.CmdCopyBuffer(cmd, slot[s].buf, pyr[s].buf, 1, &yc);
    barrier(cmd);
    for (int l = 1; l < LEVELS; ++l) {
        struct { uint32_t src_off, dst_off; int32_t sw, sh, dw, dh; } pc{
            loff[l - 1], loff[l], lw[l - 1], lh[l - 1], lw[l], lh[l]};
        push(cmd, p_down2, ds_down2[s], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, (lw[l] + 15) / 16, (lh[l] + 15) / 16, 1);
        barrier(cmd);
    }
    if (gdc_buf) {
        // the mean of every 4th pixel of every 4th row (as the plugin's CPU mean):
        // a sum per row, then the rows' sum
        struct { int32_t w, h, mode, slot, a, c, b, n; } pc{w, h, 0, s, 0, 0, 0, 0};
        push(cmd, p_mean, ds_mean[s], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, uint32_t((h + 3) / 4), 1, 1);
        barrier(cmd);
        pc.mode = 1;
        push(cmd, p_mean, ds_mean[s], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, 1, 1, 1);
        barrier(cmd);
    }
}

void Engine::record_vectors(VkCommandBuffer cmd, int a, bool follows) {
    if (gdc_buf) record_gdc(cmd, a);
    // the vector buffers still hold the pair before this one: its vectors are tried
    const bool temporal = follows && tune.tpen >= 0.0f;
    const float gdc = smean[a] - smean[(a + 1) % SLOTS];  // A - C
    const bool fine = tune.fine;
    for (int l = LEVELS - 1; l >= 0; --l) {
        bool coarse = l == LEVELS - 1;
        // level 0 (full size) stores the motion m = 2u: +-1 px of u is +-2 of m, around
        // 4 x the parent's u
        bool full = l == 0;
        struct { uint32_t off; int32_t w, h, bw, bh, pw, ph, radius, npred, bs, pscale, half_units; float gdc, act; int32_t temporal; float tpen; } pc{
            loff[l], lw[l], lh[l], bw[l], bh[l],
            coarse ? 1 : bw[l + 1], coarse ? 1 : bh[l + 1], coarse ? COARSE_RADIUS : full ? 2 : 1,
            coarse ? 1 : NPRED + (temporal ? 1 : 0),
            BLOCK, full ? 4 : 2, full ? 1 : 0, gdc, tune.act, temporal ? 1 : 0, tune.tpen};
        push(cmd, coarse ? p_search_coarse : full ? p_search_full : p_search_fine, ds_search[a][l], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, bw[l], bh[l], 1);
        barrier(cmd);
        struct { uint32_t off; int32_t w, h, bw, bh, bs, half_units, mode; float rel, abs, gdc; } mc{
            loff[l], lw[l], lh[l], bw[l], bh[l], BLOCK, full ? 1 : 0, full && tune.checked_median ? 1 : 0,
            tune.med_rel, tune.med_abs, gdc};
        push(cmd, p_median, ds_median[a][l], &mc, sizeof(mc));
        vk.CmdDispatch(cmd, (bw[l] + 15) / 16, (bh[l] + 15) / 16, 1);
        barrier(cmd);
    }
    if (fine) {  // 4x4 blocks refine +-2 (motion units) around their 8x8 block's vector (same level)
        struct { uint32_t off; int32_t w, h, bw, bh, pw, ph, radius, npred, bs, pscale, half_units; float gdc, act; int32_t temporal; float tpen; } pc{
            loff[0], w, h, bw4, bh4, bw[0], bh[0], 2, NPRED + (temporal ? 1 : 0), 4, 1, 1, gdc, tune.act,
            temporal ? 1 : 0, tune.tpen};
        push(cmd, p_search4, ds_search4[a], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, bw[0], bh[0], 1);  // a group per 8x8 block does its four 4x4 blocks
        barrier(cmd);
        struct { uint32_t off; int32_t w, h, bw, bh, bs, half_units, mode; float rel, abs, gdc; } mc{
            loff[0], w, h, bw4, bh4, 4, 1, tune.checked_median ? 1 : 0, tune.med_rel, tune.med_abs, gdc};
        push(cmd, p_median, ds_median4[a], &mc, sizeof(mc));
        vk.CmdDispatch(cmd, (bw4 + 15) / 16, (bh4 + 15) / 16, 1);
        barrier(cmd);
    }
    // the final motion as float half-vectors, +-1/4 px of u where that matches better
    int sbw = fine ? bw4 : bw[0], sbh = fine ? bh4 : bh[0];
    struct { uint32_t off; int32_t w, h, bw, bh, bs, steps; float gdc; } spc{
        loff[0], w, h, sbw, sbh, fine ? 4 : BLOCK, tune.subpel_steps, gdc};
    push(cmd, p_subpel, fine ? ds_subpel4[a] : ds_subpel[a], &spc, sizeof(spc));
    // a group per 8x8 block or per four 4x4 blocks, in rows of up to 1024 groups
    uint32_t sg = uint32_t(sbw * sbh + (fine ? 3 : 0)) / (fine ? 4 : 1);
    vk.CmdDispatch(cmd, std::min(sg, 1024u), (sg + 1023) / 1024, 1);
    barrier(cmd);
    // what those vectors leave unexplained in each source frame is searched for
    // from that frame, and carried to the in-between frame
    int sbs = fine ? 4 : BLOCK;
    if (tune.uni) {
        for (int dir = 0; dir < 2; ++dir) {
            struct { uint32_t off; int32_t w, h, bw, bh, sbw, sbh, sbs, radius, dir; float gdc, flag, abs_thr, rel; } uc{
                loff[0], w, h, bw[0], bh[0], sbw, sbh, sbs, tune.uni_radius, dir, gdc, tune.uni_flag, tune.uni_abs,
                tune.uni_rel};
            push(cmd, p_uni, dir ? ds_uni_b[a] : ds_uni_f[a], &uc, sizeof(uc));
            vk.CmdDispatch(cmd, bw[0], bh[0], 1);
        }
        barrier(cmd);
    }
    struct { uint32_t off; int32_t w, h, sbw, sbh, sbs, bw, bh, reach, enable; float gdc, abs_thr, land; } jc{
        loff[0], w, h, sbw, sbh, sbs, bw[0], bh[0], tune.uni_radius / 16 + 1, tune.uni ? 1 : 0, gdc,
        tune.uni_abs * 1.5f, tune.uni_land};
    push(cmd, p_proj, ds_proj[a], &jc, sizeof(jc));
    vk.CmdDispatch(cmd, (sbw + 15) / 16, (sbh + 15) / 16, 1);
    barrier(cmd);
}

void Engine::record_select(VkCommandBuffer cmd, int a, float t, bool has_prev, bool has_next) {
    if (gdc_buf) record_gdc(cmd, a);
    const int c = (a + 1) % SLOTS;
    const float gdc = smean[a] - smean[c];  // A - C
    const Tuning& u = tune;
    struct {
        int32_t w, h, bw, bh; float t, lambda; int32_t smooth, bs; float margin, gdc;
        int32_t tri, edge; float gdp, gdn, tri_gate, tri_thr, tri_ratio, tri_pre, tri_bad, edge_thr, edge_act, tri_fine, tri_reach;
    } sc{w, h, u.fine ? bw4 : bw[0], u.fine ? bh4 : bh[0], t, 0.05f, u.select_vectors ? 0 : 1, u.fine ? 4 : BLOCK,
         u.sel_margin, gdc, u.tri ? (has_prev ? 1 : 0) | (has_next ? 2 : 0) | u.tri_dbg : 0, u.edge ? 1 : 0,
         smean[a] - smean[(a + SLOTS - 1) % SLOTS], smean[c] - smean[(a + 2) % SLOTS], u.tri_gate, u.tri_thr,
         u.tri_ratio, u.tri_pre, u.tri_bad, u.edge_thr, u.edge_act, u.tri_fine, u.tri_reach};
    push(cmd, p_select, ds_select[a], &sc, sizeof(sc));
    vk.CmdDispatch(cmd, (w + 15) / 16, (h + 15) / 16, 1);
    barrier(cmd);
}

void Engine::record_render(VkCommandBuffer cmd, int a, float t) {
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t off; int32_t w, h, lw, lh, sub; float t, lo, hi, fail_lo, fail_hi; int32_t show; } pc{
            p.float_off, p.w, p.h, w, h, i ? 2 : 1, t, tune.occ_lo, tune.occ_hi, tune.fail_lo, tune.fail_hi,
            tune.show};
        push(cmd, p_render, ds_render[a], &pc, sizeof(pc));
        vk.CmdDispatch(cmd, (p.w + 15) / 16, (p.h + 15) / 16, 1);
    }
    barrier(cmd);
    float maxv = float((1u << bits) - 1u);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t count, raw_off, in_off, bytes; float maxv; } pc{
            uint32_t(p.w * p.h), p.raw_off, p.float_off, uint32_t(bytes), maxv};
        uint32_t words = (pc.count * bytes + 3) / 4;
        push(cmd, p_pack, ds_pack, &pc, sizeof(pc));
        dispatch1d(cmd, (words + 255) / 256);
    }
    barrier(cmd);
}

}  // namespace mvgpu
