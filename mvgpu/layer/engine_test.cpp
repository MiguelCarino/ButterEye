// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// Stage S4a: the engine (src/engine) inside the layer, on the app's device and
// queue, through a table filled from the next layer. BUTTEREYE_FRAMES_ENGINE_TEST=1
// runs it once per device, on the first k path present: four drawn frames go
// through record_load / record_vectors / record_select / record_render with gdc
// read from a buffer and the means reduced on the GPU, the means are read back and
// compared with the same means taken on the CPU, and one line is logged. A test
// knob only: nothing the layer shows comes from the engine yet.
#include <cinttypes>
#include <cmath>
#include <cstring>
#include <exception>
#include <vector>

#include "engine.h"
#include "layer.h"

namespace bef {

namespace {

// 8-bit 4:2:0 frame k packed as the engine's raw_in holds it: a ramp moving 3 px a
// frame, every other frame brighter
std::vector<uint8_t> draw(const mvgpu::Engine& e, int k) {
    std::vector<uint8_t> f(e.raw_size(), 128);
    const mvgpu::Plane& y = e.plane(0);
    for (int r = 0; r < y.h; ++r)
        for (int x = 0; x < y.w; ++x) {
            int v = ((x - 3 * k) * 5 + r * 3) & 127;
            f[y.raw_off + static_cast<size_t>(r * y.w + x)] = static_cast<uint8_t>(64 + v + (k % 2) * 20);
        }
    return f;
}

float cpu_mean(const mvgpu::Engine& e, const std::vector<uint8_t>& f) {
    const mvgpu::Plane& y = e.plane(0);
    double sum = 0;
    size_t cnt = 0;
    for (int r = 0; r < y.h; r += 4)
        for (int x = 0; x < y.w; x += 4, ++cnt) sum += f[y.raw_off + static_cast<size_t>(r * y.w + x)];
    return static_cast<float>(sum / static_cast<double>(cnt) / 255.0);
}

}  // namespace

void engine_test(Device* d, VkQueue queue, uint32_t family) {
    const DeviceTable& t = d->t;
    const VkDevice dev = d->handle;
    mvgpu::VkFns fns;
    if (!mvgpu::fill_from_device(fns, t.GetDeviceProcAddr, dev)) {
        logf("device %u: engine test FAILED: an entry point is missing below the layer", d->ordinal);
        return;
    }
    VkPhysicalDeviceProperties props{};
    d->instance->t.GetPhysicalDeviceProperties(d->physical, &props);
    const int w = 160, h = 96;
    VkCommandPool pool = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    std::string what = "ok";
    float gpu[mvgpu::Engine::SLOTS] = {}, cpu[mvgpu::Engine::SLOTS] = {};
    uint64_t sum = 1469598103934665603ull;  // FNV-1a of the rendered frame
    try {
        mvgpu::Engine eng(fns, dev, d->memory, props.limits, w, h, 8, mvgpu::Tuning{}, true);
        mvgpu::Memory mem(fns, dev, d->memory);
        const auto HOST = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
        const uint32_t frame = eng.raw_size();
        mvgpu::Buffer up = mem.make(VkDeviceSize(frame) * mvgpu::Engine::SLOTS, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, HOST);
        mvgpu::Buffer down = mem.make(VkDeviceSize(frame) + 16, VK_BUFFER_USAGE_TRANSFER_DST_BIT, HOST);
        for (int s = 0; s < mvgpu::Engine::SLOTS; ++s) {
            std::vector<uint8_t> f = draw(eng, s);
            cpu[s] = cpu_mean(eng, f);
            std::memcpy(static_cast<uint8_t*>(up.map) + size_t(frame) * s, f.data(), frame);
        }
        VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr, 0, family};
        mvgpu::check(t.CreateCommandPool(dev, &pci, nullptr, &pool), "vkCreateCommandPool");
        VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, pool,
                                        VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
        VkCommandBuffer cb = VK_NULL_HANDLE;
        mvgpu::check(t.AllocateCommandBuffers(dev, &cai, &cb), "vkAllocateCommandBuffers");
        if (d->set_loader_data) mvgpu::check(d->set_loader_data(dev, cb), "vkSetDeviceLoaderData");
        VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, nullptr, 0};
        mvgpu::check(t.CreateFence(dev, &fi, nullptr, &fence), "vkCreateFence");
        VkCommandBufferBeginInfo cbi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                     VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
        mvgpu::check(t.BeginCommandBuffer(cb, &cbi), "vkBeginCommandBuffer");
        // one submit, as the layer will record it: every frame in, then the pairs
        for (int s = 0; s < mvgpu::Engine::SLOTS; ++s) {
            VkBufferCopy cp{VkDeviceSize(frame) * s, 0, frame};
            fns.CmdCopyBuffer(cb, up.buf, eng.raw_input().buf, 1, &cp);
            eng.barrier(cb);
            eng.record_load(cb, s);
        }
        for (int a = 0; a < 3; ++a) eng.record_vectors(cb, a, a > 0);
        eng.record_select(cb, 1, 0.5f, true, true);
        eng.record_render(cb, 1, 0.5f);
        VkBufferCopy co{0, 0, frame};
        fns.CmdCopyBuffer(cb, eng.raw_output().buf, down.buf, 1, &co);
        VkBufferCopy cm{0, frame, 16};
        fns.CmdCopyBuffer(cb, eng.means_buffer().buf, down.buf, 1, &cm);
        VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_TRANSFER_WRITE_BIT,
                           VK_ACCESS_HOST_READ_BIT};
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, nullptr, 0,
                             nullptr);
        mvgpu::check(t.EndCommandBuffer(cb), "vkEndCommandBuffer");
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 0, nullptr, nullptr, 1, &cb, 0, nullptr};
        mvgpu::check(t.QueueSubmit(queue, 1, &si, fence), "vkQueueSubmit");
        mvgpu::check(t.WaitForFences(dev, 1, &fence, VK_TRUE, 10000000000ull), "vkWaitForFences");
        const auto* p = static_cast<const uint8_t*>(down.map);
        for (uint32_t i = 0; i < frame; ++i) sum = (sum ^ p[i]) * 1099511628211ull;
        std::memcpy(gpu, p + frame, sizeof(gpu));
        for (int s = 0; s < mvgpu::Engine::SLOTS; ++s)
            if (!(std::fabs(gpu[s] - cpu[s]) <= 1e-5f)) what = "FAILED: GPU means differ from the CPU's";
        mem.free(up);
        mem.free(down);
    } catch (const std::exception& e) {
        what = std::string("FAILED: ") + e.what();
    }
    if (fence) t.DestroyFence(dev, fence, nullptr);
    if (pool) t.DestroyCommandPool(dev, pool, nullptr);
    logf("device %u: engine test %dx%d %s; GPU means %.6f %.6f %.6f %.6f, CPU %.6f %.6f %.6f %.6f; frame %016" PRIx64,
         d->ordinal, w, h, what.c_str(), gpu[0], gpu[1], gpu[2], gpu[3], cpu[0], cpu[1], cpu[2], cpu[3], sum);
}

}  // namespace bef
