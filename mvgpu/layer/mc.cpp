// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames, stage S4: motion-compensated in-between frames (mc.h).
// Every call goes through the device's table or the engine's, never to the loader.
#include "mc.h"

#include "cut_spv.h"
#include "ingest_spv.h"
#include "render_rgb_spv.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <exception>

namespace bef {

namespace {

struct InPush {
    int32_t rx, ry, rw, rh, sw, sh;
    uint32_t bits, red_low, pq;
};
struct CutPush {
    uint32_t off;
    int32_t w, h, ring, valid;
    float hard, lo, ratio, floor_;
    int32_t force;
};
struct RenderPush {
    int32_t rx, ry, rw, rh, sw, sh, width, height;
    uint32_t bits, red_low, k, m;
    float lo, hi, fail_lo, fail_hi, still;
    uint32_t show;
};

void image_barrier(VkImageMemoryBarrier& m, VkImage image, VkImageLayout from, VkImageLayout to, VkAccessFlags src,
                   VkAccessFlags dst, uint32_t layers = 1) {
    m = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    m.srcAccessMask = src;
    m.dstAccessMask = dst;
    m.oldLayout = from;
    m.newLayout = to;
    m.srcQueueFamilyIndex = m.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    m.image = image;
    m.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, layers};
}

VkImageCopy whole(uint32_t w, uint32_t h, uint32_t src_layer) {
    VkImageCopy r{};
    r.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, src_layer, 1};
    r.dstSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    r.extent = {w, h, 1};
    return r;
}

// a compute pipeline with `images` storage images, then `buffers` storage buffers
bool make_pipe(Mc& mc, int i, const uint32_t* code, size_t size, uint32_t images, uint32_t buffers,
               uint32_t push) {
    const DeviceTable& t = mc.d->t;
    VkDescriptorSetLayoutBinding b[8];
    const uint32_t n = images + buffers;
    for (uint32_t j = 0; j < n; ++j)
        b[j] = {j, j < images ? VK_DESCRIPTOR_TYPE_STORAGE_IMAGE : VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 1,
                VK_SHADER_STAGE_COMPUTE_BIT, nullptr};
    VkDescriptorSetLayoutCreateInfo dli{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO, nullptr, 0, n, b};
    if (t.CreateDescriptorSetLayout(mc.dev, &dli, nullptr, &mc.dsl[i]) != VK_SUCCESS) return false;
    VkPushConstantRange pr{VK_SHADER_STAGE_COMPUTE_BIT, 0, push};
    VkPipelineLayoutCreateInfo pli{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, nullptr, 0, 1, &mc.dsl[i], 1, &pr};
    if (t.CreatePipelineLayout(mc.dev, &pli, nullptr, &mc.pl[i]) != VK_SUCCESS) return false;
    VkShaderModuleCreateInfo smi{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO, nullptr, 0, size, code};
    VkShaderModule sm = VK_NULL_HANDLE;
    if (t.CreateShaderModule(mc.dev, &smi, nullptr, &sm) != VK_SUCCESS) return false;
    VkComputePipelineCreateInfo cpi{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
    cpi.stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, nullptr, 0, VK_SHADER_STAGE_COMPUTE_BIT, sm,
                 "main", nullptr};
    cpi.layout = mc.pl[i];
    const VkResult r = t.CreateComputePipelines(mc.dev, VK_NULL_HANDLE, 1, &cpi, nullptr, &mc.pipe[i]);
    t.DestroyShaderModule(mc.dev, sm, nullptr);
    return r == VK_SUCCESS;
}

bool alloc_set(Mc& mc, int i, VkDescriptorSet* out) {
    VkDescriptorSetAllocateInfo dai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, nullptr, mc.dpool, 1, &mc.dsl[i]};
    return mc.d->t.AllocateDescriptorSets(mc.dev, &dai, out) == VK_SUCCESS;
}

void write_set(Mc& mc, VkDescriptorSet s, std::initializer_list<VkImageView> views,
               std::initializer_list<VkBuffer> bufs) {
    VkDescriptorImageInfo ii[4];
    VkDescriptorBufferInfo bi[4];
    VkWriteDescriptorSet w[8];
    uint32_t n = 0, ni = 0, nb = 0;
    for (VkImageView v : views) {
        ii[ni] = {VK_NULL_HANDLE, v, VK_IMAGE_LAYOUT_GENERAL};
        w[n] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, nullptr, s, n, 0, 1, VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                &ii[ni++], nullptr, nullptr};
        ++n;
    }
    for (VkBuffer b : bufs) {
        bi[nb] = {b, 0, VK_WHOLE_SIZE};
        w[n] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, nullptr, s, n, 0, 1, VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                nullptr, &bi[nb++], nullptr};
        ++n;
    }
    mc.d->t.UpdateDescriptorSets(mc.dev, n, w, 0, nullptr);
}

float env_float(const char* name, float dflt) {
    const char* v = std::getenv(name);
    return v && *v ? static_cast<float>(std::atof(v)) : dflt;
}

}  // namespace

McSettings mc_settings_from_env() {
    McSettings s;
    if (const char* r = std::getenv("BUTTEREYE_FRAMES_RECT"))
        std::sscanf(r, "%d,%d,%d,%d", &s.rect.x, &s.rect.y, &s.rect.w, &s.rect.h);
    if (const char* h = std::getenv("BUTTEREYE_FRAMES_SRC_H")) s.source_h = std::atoi(h);
    s.still = env_float("BUTTEREYE_FRAMES_STILL", s.still);
    if (const char* c = std::getenv("BUTTEREYE_FRAMES_CUT")) {
        if (std::strcmp(c, "force") == 0)
            s.cut_force = true;
        else
            std::sscanf(c, "%f,%f,%f,%f", &s.cut_hard, &s.cut_lo, &s.cut_ratio, &s.cut_floor);
    }
    s.tuning = mvgpu::Tuning::from_env();
    return s;
}

bool mc_create(Mc& mc, const Blend& b, const McSettings& set, uint32_t family, uint32_t query_slots,
               std::string* why) {
    mc_destroy(mc);
    mc.d = b.d;
    mc.dev = b.dev;
    mc.set = set;
    auto fail = [&](const std::string& w) {
        *why = w;
        mc_destroy(mc);
        return false;
    };
    const int W = static_cast<int>(b.extent.width), H = static_cast<int>(b.extent.height);
    McRect r = set.rect;
    if (r.w <= 0 || r.h <= 0) r = {0, 0, W, H};
    r.x = std::clamp(r.x, 0, W - 1);
    r.y = std::clamp(r.y, 0, H - 1);
    r.w = std::clamp(r.w, 1, W - r.x);
    r.h = std::clamp(r.h, 1, H - r.y);
    mc.rect = r;
    // source-equivalent scale: the video's lines when known, never more than the rectangle
    // has or than max_h
    int sh = r.h;
    if (set.source_h > 0) sh = std::min(sh, set.source_h);
    sh = std::min(sh, set.max_h);
    if (sh < 32) sh = std::min(32, r.h);
    mc.sh = sh;
    mc.sw = std::max(1, static_cast<int>(std::lround(static_cast<double>(r.w) * sh / r.h)));
    if (!mvgpu::fill_from_device(mc.fns, mc.d->t.GetDeviceProcAddr, mc.dev))
        return fail("an engine entry point is missing below the layer");
    VkPhysicalDeviceProperties props{};
    mc.d->instance->t.GetPhysicalDeviceProperties(mc.d->physical, &props);
    try {
        mc.eng = std::make_unique<mvgpu::Engine>(mc.fns, mc.dev, mc.d->memory, props.limits, mc.sw, mc.sh, 8,
                                                 set.tuning, true);
        mc.mem = std::make_unique<mvgpu::Memory>(mc.fns, mc.dev, mc.d->memory);
        mc.state = mc.mem->make(64, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_SRC_BIT,
                                VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        // the cut test's d and flag per query slot, for the cut counter (host-visible, coherent)
        const uint32_t n = query_slots ? query_slots : 1;
        mc.cuts = mc.mem->make(8 * n, VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                               VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        std::memset(mc.cuts.map, 0xff, 8 * n);
        mc.slot_frames.assign(n, 0);
    } catch (const std::exception& e) {
        return fail(std::string("engine: ") + e.what());
    }
    if (!make_pipe(mc, 0, ingest_spv, sizeof(ingest_spv), 1, 1, sizeof(InPush))) return fail("ingest pipeline");
    if (!make_pipe(mc, 1, cut_spv, sizeof(cut_spv), 0, 3, sizeof(CutPush))) return fail("cut pipeline");
    if (!make_pipe(mc, 2, render_rgb_spv, sizeof(render_rgb_spv), 3, 2, sizeof(RenderPush)))
        return fail("render pipeline");
    VkDescriptorPoolSize ps[2] = {{VK_DESCRIPTOR_TYPE_STORAGE_IMAGE, 16}, {VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 32}};
    VkDescriptorPoolCreateInfo dpi{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, nullptr, 0, 16, 2, ps};
    if (mc.d->t.CreateDescriptorPool(mc.dev, &dpi, nullptr, &mc.dpool) != VK_SUCCESS) return fail("descriptor pool");
    const int S = mvgpu::Engine::SLOTS;
    for (int h = 0; h < 2; ++h)
        for (int s = 0; s < S; ++s) {
            if (!alloc_set(mc, 0, &mc.set_in[h][s])) return fail("descriptor sets");
            write_set(mc, mc.set_in[h][s], {b.hist_view[h]}, {mc.eng->slot_buffer(s).buf});
        }
    for (int a = 0; a < S; ++a) {
        if (!alloc_set(mc, 1, &mc.set_cut[a])) return fail("descriptor sets");
        write_set(mc, mc.set_cut[a], {}, {mc.eng->pyramid(a).buf, mc.eng->pyramid((a + 1) % S).buf, mc.state.buf});
    }
    for (int c = 0; c < 2; ++c) {
        if (!alloc_set(mc, 2, &mc.set_render[c])) return fail("descriptor sets");
        write_set(mc, mc.set_render[c], {b.hist_view[1 - c], b.hist_view[c], b.out_view},
                  {mc.eng->field_buffer().buf, mc.state.buf});
    }
    if (query_slots && family < mc.d->timestamp_bits.size() && mc.d->timestamp_bits[family] &&
        mc.d->timestamp_period > 0) {
        VkQueryPoolCreateInfo qi{VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO, nullptr, 0, VK_QUERY_TYPE_TIMESTAMP,
                                 query_slots * kMcStamps, 0};
        if (mc.d->t.CreateQueryPool(mc.dev, &qi, nullptr, &mc.queries) == VK_SUCCESS) mc.query_slots = query_slots;
        else mc.queries = VK_NULL_HANDLE;
    }
    return true;
}

void mc_destroy(Mc& mc) {
    if (!mc.d) return;
    const DeviceTable& t = mc.d->t;
    if (mc.queries) t.DestroyQueryPool(mc.dev, mc.queries, nullptr);
    if (mc.dpool) t.DestroyDescriptorPool(mc.dev, mc.dpool, nullptr);
    for (int i = 0; i < 3; ++i) {
        if (mc.pipe[i]) t.DestroyPipeline(mc.dev, mc.pipe[i], nullptr);
        if (mc.pl[i]) t.DestroyPipelineLayout(mc.dev, mc.pl[i], nullptr);
        if (mc.dsl[i]) t.DestroyDescriptorSetLayout(mc.dev, mc.dsl[i], nullptr);
        mc.pipe[i] = VK_NULL_HANDLE;
        mc.pl[i] = VK_NULL_HANDLE;
        mc.dsl[i] = VK_NULL_HANDLE;
    }
    if (mc.mem) {
        mc.mem->free(mc.state);
        mc.mem->free(mc.cuts);
    }
    mc.slot_frames.clear();
    mc.eng.reset();
    mc.mem.reset();
    mc.queries = VK_NULL_HANDLE;
    mc.dpool = VK_NULL_HANDLE;
    mc.query_slots = 0;
    mc.slot = -1;
    mc.pairs = mc.ring = mc.valid = 0;
    mc.d = nullptr;
}

namespace {

// the memory barrier between the steps: transfer and shader writes before, any of the work's
// accesses after
void work_barrier(const DeviceTable& t, VkCommandBuffer cb, uint32_t nimg = 0, const VkImageMemoryBarrier* img = nullptr) {
    VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr,
                       VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_WRITE_BIT,
                       VK_ACCESS_TRANSFER_READ_BIT | VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT |
                           VK_ACCESS_SHADER_WRITE_BIT};
    const VkPipelineStageFlags work = VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
    t.CmdPipelineBarrier(cb, work, work, 0, 1, &mb, 0, nullptr, nimg, img);
}

bool timed(const Mc& mc, uint32_t qslot) { return mc.queries && qslot < mc.query_slots; }

void stamp(Mc& mc, VkCommandBuffer cb, uint32_t qslot, uint32_t i, VkPipelineStageFlagBits stage) {
    if (timed(mc, qslot)) mc.d->t.CmdWriteTimestamp(cb, stage, mc.queries, qslot * kMcStamps + i);
}

}  // namespace

uint32_t mc_copy_in_target(const Blend& b) { return b.have ? 1 - b.cur : b.cur; }

void mc_record_copy_in(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, uint32_t qslot) {
    const DeviceTable& t = mc.d->t;
    if (timed(mc, qslot)) t.CmdResetQueryPool(cb, mc.queries, qslot * kMcStamps, kMcStamps);
    stamp(mc, cb, qslot, 0, VK_PIPELINE_STAGE_TRANSFER_BIT);
    // a run starts again when the history did (pass-through, event, another mode)
    if (b.have == 0) {
        mc.slot = -1;
        mc.pairs = mc.ring = mc.valid = 0;
    }
    VkImageMemoryBarrier first[3];
    uint32_t nfirst = 0;
    if (!b.general) {
        for (int i = 0; i < 2; ++i)
            image_barrier(first[nfirst++], b.hist[i], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                          VK_ACCESS_TRANSFER_WRITE_BIT | VK_ACCESS_SHADER_READ_BIT);
        image_barrier(first[nfirst++], b.out, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_GENERAL, 0,
                      VK_ACCESS_SHADER_WRITE_BIT, b.layers);
        b.general = true;
    }
    work_barrier(t, cb, nfirst, first);
    // 1. the app's image into H
    const uint32_t next = mc_copy_in_target(b);
    const VkImageCopy one = whole(b.extent.width, b.extent.height, 0);
    t.CmdCopyImage(cb, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, b.hist[next], VK_IMAGE_LAYOUT_GENERAL, 1, &one);
    b.cur = next;
    if (b.have < 2) ++b.have;
    stamp(mc, cb, qslot, 1, VK_PIPELINE_STAGE_TRANSFER_BIT);
}

bool mc_record_pair(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, uint32_t got, uint32_t qslot, bool copy_in) {
    const DeviceTable& t = mc.d->t;
    mvgpu::Engine& e = *mc.eng;
    if (copy_in) mc_record_copy_in(mc, b, cb, app, qslot);
    // after the copy in (here, or before on another queue or in another submit)
    e.barrier(cb);

    // 2. its luma at source scale into the next engine slot, the pyramid and the mean
    const int S = mvgpu::Engine::SLOTS;
    const int s = (mc.slot + 1 + S) % S;
    t.CmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pipe[0]);
    t.CmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pl[0], 0, 1, &mc.set_in[b.cur][s], 0, nullptr);
    const InPush ip{mc.rect.x, mc.rect.y, mc.rect.w, mc.rect.h, mc.sw, mc.sh, b.fl.bits, b.fl.red_low,
                    mc.set.pq ? 1u : 0u};
    t.CmdPushConstants(cb, mc.pl[0], VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof ip, &ip);
    t.CmdDispatch(cb, (static_cast<uint32_t>(mc.sw) + 15) / 16, (static_cast<uint32_t>(mc.sh) + 15) / 16, 1);
    e.barrier(cb);
    e.record_pyramid(cb, s);
    stamp(mc, cb, qslot, 2, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);

    const int a = mc.slot;
    mc.slot = s;
    const bool made = a >= 0 && b.have == 2 && got > 0;
    if (!made) {
        mc.pairs = 0;
        stamp(mc, cb, qslot, 3, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
        return false;
    }
    // 3. the pair's vectors and the cut test
    e.record_vectors(cb, a, mc.pairs > 0);
    t.CmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pipe[1]);
    t.CmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pl[1], 0, 1, &mc.set_cut[a], 0, nullptr);
    const CutPush cp{e.level_offset(2), e.level_width(2), e.level_height(2), mc.ring, mc.valid,
                     mc.set.cut_hard, mc.set.cut_lo, mc.set.cut_ratio, mc.set.cut_floor,
                     mc.set.cut_force ? 1 : 0};
    t.CmdPushConstants(cb, mc.pl[1], VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof cp, &cp);
    t.CmdDispatch(cb, 1, 1, 1);
    e.barrier(cb);
    mc.ring = (mc.ring + 1) % 4;
    mc.valid = std::min(mc.valid + 1, 4);
    ++mc.pairs;
    // d and the cut flag out for the counter, readable once the submit's fence is done
    if (mc.cuts.map && qslot < mc.slot_frames.size()) {
        const VkBufferCopy rc{0, 8ull * qslot, 8};
        mc.fns.CmdCopyBuffer(cb, mc.state.buf, mc.cuts.buf, 1, &rc);
        VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_TRANSFER_WRITE_BIT,
                           VK_ACCESS_HOST_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, nullptr, 0,
                             nullptr);
    }
    stamp(mc, cb, qslot, 3, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    return true;
}

void mc_record_frame(Mc& mc, Blend& b, VkCommandBuffer cb, uint32_t m, uint32_t k, VkImage target, bool show,
                     uint32_t qslot, bool render, bool copy_out) {
    const DeviceTable& t = mc.d->t;
    mvgpu::Engine& e = *mc.eng;
    const uint32_t w = b.extent.width, h = b.extent.height;
    const uint32_t base = kMcPairStamps + kMcFrameStamps * (m - 1);
    const uint32_t layer = std::min(m, b.layers) - 1;
    if (render) {
        // once everything before it is done: the frame's own time, also when it shares a command buffer
        stamp(mc, cb, qslot, base, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
        // the pair's work (or the frame before, whose render read the field this select rewrites)
        work_barrier(t, cb);
        const int a = (mc.slot - 1 + mvgpu::Engine::SLOTS) % mvgpu::Engine::SLOTS;
        const mvgpu::Tuning& u = e.tuning();
        e.record_select(cb, a, static_cast<float>(m) / static_cast<float>(k), false, false);
        t.CmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pipe[2]);
        t.CmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, mc.pl[2], 0, 1, &mc.set_render[b.cur], 0, nullptr);
        const RenderPush rp{mc.rect.x, mc.rect.y, mc.rect.w, mc.rect.h, mc.sw, mc.sh,
                            static_cast<int32_t>(w), static_cast<int32_t>(h), b.fl.bits, b.fl.red_low, k, m,
                            u.occ_lo, u.occ_hi, u.fail_lo, u.fail_hi, mc.set.still, show ? 1u : 0u};
        t.CmdPushConstants(cb, mc.pl[2], VK_SHADER_STAGE_COMPUTE_BIT, 0, sizeof rp, &rp);
        t.CmdDispatch(cb, (w + 15) / 16, (h + 15) / 16, 1);
        stamp(mc, cb, qslot, base + 1, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT);
    }
    if (copy_out) {
        // Out's layer, bit for bit, into the swapchain image
        VkMemoryBarrier m3{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_SHADER_WRITE_BIT,
                           VK_ACCESS_TRANSFER_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &m3, 0,
                             nullptr, 0, nullptr);
        const VkImageCopy r = whole(w, h, layer);
        t.CmdCopyImage(cb, b.out, VK_IMAGE_LAYOUT_GENERAL, target, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &r);
        stamp(mc, cb, qslot, base + 2, VK_PIPELINE_STAGE_TRANSFER_BIT);
    }
}

void mc_record_done(Mc& mc, uint32_t qslot, uint32_t frames) {
    if (qslot < mc.slot_frames.size()) mc.slot_frames[qslot] = static_cast<uint8_t>(frames);
    if (qslot < mc.slot_frames.size() && mc.cuts.map && frames == 0)
        std::memset(static_cast<char*>(mc.cuts.map) + 8ull * qslot, 0xff, 8);  // no cut test this time
}

bool mc_record(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, const VkImage* targets, uint32_t got, uint32_t k,
               bool show, uint32_t qslot) {
    const DeviceTable& t = mc.d->t;
    const bool made = mc_record_pair(mc, b, cb, app, got, qslot);
    if (made) {
        for (uint32_t m = 1; m <= got; ++m) mc_record_frame(mc, b, cb, m, k, targets[m - 1], show, qslot);
    } else {
        // the first image of a run: copies, as in S2
        const VkImageCopy one = whole(b.extent.width, b.extent.height, 0);
        for (uint32_t m = 0; m < got; ++m)
            t.CmdCopyImage(cb, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, targets[m],
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &one);
    }
    mc_record_done(mc, qslot, made ? got : 0);
    return made;
}

bool mc_times(Mc& mc, uint32_t qslot, bool split, McTimes* out) {
    if (!mc.queries || qslot >= mc.query_slots || qslot >= mc.slot_frames.size()) return false;
    const uint32_t frames = mc.slot_frames[qslot];
    const uint32_t n = kMcPairStamps + kMcFrameStamps * frames;
    uint64_t v[kMcStamps];
    const VkResult r = mc.d->t.GetQueryPoolResults(mc.dev, mc.queries, qslot * kMcStamps, n, sizeof v, v,
                                                   sizeof(uint64_t), VK_QUERY_RESULT_64_BIT);
    if (r != VK_SUCCESS) return false;
    const double p = static_cast<double>(mc.d->timestamp_period);
    auto at = [&](uint32_t i) { return static_cast<double>(static_cast<int64_t>(v[i] - v[0])) * p; };
    McTimes x;
    x.copy_in = at(1);
    x.ingest = at(2) - at(1);
    x.vectors = at(3) - at(2);
    x.pair = at(3);
    double last = at(3), largest = x.pair;
    for (uint32_t m = 0; m < frames; ++m) {
        const uint32_t s0 = kMcPairStamps + kMcFrameStamps * m;
        x.frames += at(s0 + 1) - at(s0);
        x.out += at(s0 + 2) - at(s0 + 1);
        x.frame[m] = at(s0 + 2) - at(s0);
        largest = std::max(largest, x.frame[m]);
        last = std::max(last, at(s0 + 2));
    }
    x.total = last;
    x.largest = split ? largest : last;
    *out = x;
    return true;
}

int mc_cut(Mc& mc, uint32_t qslot, float* d) {
    if (!mc.cuts.map || qslot >= mc.slot_frames.size()) return -1;
    float v[2];
    std::memcpy(v, static_cast<const char*>(mc.cuts.map) + 8ull * qslot, 8);
    if (!(v[1] == 0.0f || v[1] == 1.0f)) return -1;  // the 0xff fill: not written
    *d = v[0];
    return v[1] == 1.0f ? 1 : 0;
}

}  // namespace bef
