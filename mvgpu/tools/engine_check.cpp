// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// engine-check: the engine's GPU brightness path (gdc from a buffer, the mean
// reduced on the GPU) against the plugin's push constants, on drawn frames.
//
//   engine-check [--size WxH] [--device N]
//
// Four drawn frames (a textured square moving over a panning background, every
// other frame brighter) go through the engine three ways, each with the default
// settings and with the three-frame and edge rules on:
//   push    gdc as push constants from the CPU means (what the plugin does)
//   buffer  gdc read from the buffer, the means copied in from the CPU
//   gpu     gdc read from the buffer, the means reduced on the GPU
// "push" and "buffer" must give the same bytes. "gpu" differs only as far as the
// GPU's float sum differs from the CPU's double one; it prints how far. One line of
// JSON on stdout; exit 1 when push and buffer differ or a mean is off by more than
// 1e-5. Developer tool, built with -Dlayer=enabled; it links the loader.
#include <vulkan/vulkan.h>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <vector>

#include "engine.h"

namespace {

using mvgpu::Buffer;
using mvgpu::check;
using mvgpu::Engine;

struct Ctx {
    VkInstance inst = VK_NULL_HANDLE;
    VkPhysicalDevice phys = VK_NULL_HANDLE;
    VkDevice dev = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    VkCommandPool pool = VK_NULL_HANDLE;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    VkPhysicalDeviceProperties props{};
    VkPhysicalDeviceMemoryProperties memprops{};
    mvgpu::VkFns fns;
};

void begin(Ctx& c) {
    check(vkResetCommandBuffer(c.cmd, 0), "vkResetCommandBuffer");
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    check(vkBeginCommandBuffer(c.cmd, &bi), "vkBeginCommandBuffer");
}

void submit(Ctx& c) {
    check(vkEndCommandBuffer(c.cmd), "vkEndCommandBuffer");
    VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    si.commandBufferCount = 1;
    si.pCommandBuffers = &c.cmd;
    check(vkQueueSubmit(c.queue, 1, &si, c.fence), "vkQueueSubmit");
    check(vkWaitForFences(c.dev, 1, &c.fence, VK_TRUE, UINT64_MAX), "vkWaitForFences");
    check(vkResetFences(c.dev, 1, &c.fence), "vkResetFences");
}

void host_barrier(VkCommandBuffer cmd) {
    VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    hb.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    hb.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, nullptr, 0,
                         nullptr);
}

void init(Ctx& c, int device) {
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "mvgpu-engine-check";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ic{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ic.pApplicationInfo = &app;
    check(vkCreateInstance(&ic, nullptr, &c.inst), "vkCreateInstance");
    uint32_t n = 0;
    vkEnumeratePhysicalDevices(c.inst, &n, nullptr);
    std::vector<VkPhysicalDevice> devs(n);
    vkEnumeratePhysicalDevices(c.inst, &n, devs.data());
    if (devs.empty() || device >= int(n)) throw std::runtime_error("no such Vulkan device");
    c.phys = devs[device < 0 ? 0 : device];
    vkGetPhysicalDeviceProperties(c.phys, &c.props);
    vkGetPhysicalDeviceMemoryProperties(c.phys, &c.memprops);
    vkGetPhysicalDeviceQueueFamilyProperties(c.phys, &n, nullptr);
    std::vector<VkQueueFamilyProperties> fams(n);
    vkGetPhysicalDeviceQueueFamilyProperties(c.phys, &n, fams.data());
    uint32_t qfam = UINT32_MAX;
    for (uint32_t i = 0; i < n; ++i)
        if (fams[i].queueFlags & VK_QUEUE_COMPUTE_BIT) { qfam = i; break; }
    if (qfam == UINT32_MAX) throw std::runtime_error("no compute queue");
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qi{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO};
    qi.queueFamilyIndex = qfam;
    qi.queueCount = 1;
    qi.pQueuePriorities = &prio;
    VkDeviceCreateInfo di{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO};
    di.queueCreateInfoCount = 1;
    di.pQueueCreateInfos = &qi;
    check(vkCreateDevice(c.phys, &di, nullptr, &c.dev), "vkCreateDevice");
    vkGetDeviceQueue(c.dev, qfam, 0, &c.queue);
    VkCommandPoolCreateInfo cp{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cp.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    cp.queueFamilyIndex = qfam;
    check(vkCreateCommandPool(c.dev, &cp, nullptr, &c.pool), "vkCreateCommandPool");
    VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    ca.commandPool = c.pool;
    ca.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ca.commandBufferCount = 1;
    check(vkAllocateCommandBuffers(c.dev, &ca, &c.cmd), "vkAllocateCommandBuffers");
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    check(vkCreateFence(c.dev, &fi, nullptr, &c.fence), "vkCreateFence");
    // the table through the device's own entry points (as the layer fills it from the next layer)
    if (!mvgpu::fill_from_device(c.fns, vkGetDeviceProcAddr, c.dev))
        throw std::runtime_error("a Vulkan entry point is missing");
}

void done(Ctx& c) {
    if (c.dev) {
        vkDeviceWaitIdle(c.dev);
        vkDestroyFence(c.dev, c.fence, nullptr);
        vkDestroyCommandPool(c.dev, c.pool, nullptr);
        vkDestroyDevice(c.dev, nullptr);
    }
    if (c.inst) vkDestroyInstance(c.inst, nullptr);
}

// 8-bit 4:2:0 frame k, packed as the engine's raw_in holds it (Y, U, V, each 4-aligned)
std::vector<uint8_t> draw(int w, int h, int k) {
    int cw = (w + 1) / 2, ch = (h + 1) / 2;
    auto al = [](int n) { return (n + 3) & ~3; };
    std::vector<uint8_t> f(size_t(al(w * h) + 2 * al(cw * ch)), 128);
    uint32_t seed = 7;
    auto rnd = [&seed] {
        seed = seed * 1664525u + 1013904223u;
        return int(seed >> 24);
    };
    std::vector<uint8_t> tex(64 * 64);
    for (auto& t : tex) t = uint8_t(48 + (rnd() * 160) / 256);
    const int lift = (k % 2) ? 24 : 0;
    for (int y = 0; y < h; ++y)
        for (int x = 0; x < w; ++x) {
            double xs = x - 3.0 * k;
            int v = int(110 + 30 * std::sin(xs / 7.0) * std::cos(y / 11.0) + 20 * std::sin((xs + y) / 23.0));
            int tx = x - (40 + 11 * k), ty = y - (30 + 4 * k);
            if (tx >= 0 && tx < 64 && ty >= 0 && ty < 64) v = tex[size_t(ty * 64 + tx)];
            f[size_t(y * w + x)] = uint8_t(std::min(255, std::max(0, v + lift)));
        }
    for (int y = 0; y < ch; ++y)
        for (int x = 0; x < cw; ++x) {
            f[size_t(al(w * h) + y * cw + x)] = uint8_t(128 + 20 * std::sin((x + k) / 9.0));
            f[size_t(al(w * h) + al(cw * ch) + y * cw + x)] = uint8_t(128 + 20 * std::cos((y - k) / 13.0));
        }
    return f;
}

// the plugin's CPU mean: every 4th pixel of every 4th row
float cpu_mean(const std::vector<uint8_t>& f, int w, int h) {
    double sum = 0;
    size_t cnt = 0;
    for (int y = 0; y < h; y += 4)
        for (int x = 0; x < w; x += 4, ++cnt) sum += f[size_t(y * w + x)];
    return float(sum / double(cnt) / 255.0);
}

enum class Gdc { push, buffer, gpu };

struct Result {
    std::vector<std::vector<uint8_t>> out;  // per t
    float gpu_means[Engine::SLOTS] = {};
};

Result run(Ctx& c, int w, int h, Gdc mode, const mvgpu::Tuning& tune) {
    Engine eng(c.fns, c.dev, c.memprops, c.props.limits, w, h, 8, tune, mode != Gdc::push);
    mvgpu::Memory mem(c.fns, c.dev, c.memprops);
    const auto HOST = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    Buffer up = mem.make(eng.raw_size(), VK_BUFFER_USAGE_TRANSFER_SRC_BIT, HOST);
    Buffer down = mem.make(eng.raw_size(), VK_BUFFER_USAGE_TRANSFER_DST_BIT, HOST);
    Buffer mdown = mem.make(16, VK_BUFFER_USAGE_TRANSFER_DST_BIT, HOST);
    Result res;
    float cpu[Engine::SLOTS];
    for (int s = 0; s < Engine::SLOTS; ++s) {
        std::vector<uint8_t> f = draw(w, h, s);
        cpu[s] = cpu_mean(f, w, h);
        eng.set_mean(s, cpu[s]);
        std::memcpy(up.map, f.data(), f.size());
        begin(c);
        VkBufferCopy cp{0, 0, eng.raw_size()};
        vkCmdCopyBuffer(c.cmd, up.buf, eng.raw_input().buf, 1, &cp);
        eng.barrier(c.cmd);
        eng.record_load(c.cmd, s);
        submit(c);
    }
    if (mode != Gdc::push) {
        begin(c);
        VkBufferCopy cp{0, 0, 16};
        vkCmdCopyBuffer(c.cmd, eng.means_buffer().buf, mdown.buf, 1, &cp);
        host_barrier(c.cmd);
        if (mode == Gdc::buffer) {
            eng.barrier(c.cmd);  // the copy has read the means before they are replaced
            eng.record_set_means(c.cmd, cpu);
        }
        submit(c);
        std::memcpy(res.gpu_means, mdown.map, sizeof(res.gpu_means));
    }
    // pairs 0, 1, 2 searched in order; pair 1 rendered with the frames around it
    for (float t : {0.5f, 1.0f / 3.0f}) {
        begin(c);
        if (res.out.empty())
            for (int a = 0; a < 3; ++a) eng.record_vectors(c.cmd, a, a > 0);
        eng.record_select(c.cmd, 1, t, true, true);
        eng.record_render(c.cmd, 1, t);
        VkBufferCopy cp{0, 0, eng.raw_size()};
        vkCmdCopyBuffer(c.cmd, eng.raw_output().buf, down.buf, 1, &cp);
        host_barrier(c.cmd);
        submit(c);
        const auto* p = static_cast<const uint8_t*>(down.map);
        res.out.emplace_back(p, p + eng.raw_size());
    }
    vkDeviceWaitIdle(c.dev);
    mem.free(up);
    mem.free(down);
    mem.free(mdown);
    return res;
}

}  // namespace

int main(int argc, char** argv) {
    int w = 320, h = 184, device = -1;
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--size") && i + 1 < argc) std::sscanf(argv[++i], "%dx%d", &w, &h);
        else if (!std::strcmp(argv[i], "--device") && i + 1 < argc) device = std::atoi(argv[++i]);
        else {
            std::fprintf(stderr, "usage: engine-check [--size WxH] [--device N]\n");
            return 2;
        }
    }
    Ctx c;
    bool ok = true;
    try {
        init(c, device);
        std::printf("{\"device\": \"%s\", \"size\": [%d, %d], \"runs\": [", c.props.deviceName, w, h);
        for (int rule = 0; rule < 2; ++rule) {
            mvgpu::Tuning tune;
            tune.tri = tune.edge = rule == 1;
            Result push = run(c, w, h, Gdc::push, tune);
            Result buf = run(c, w, h, Gdc::buffer, tune);
            Result gpu = run(c, w, h, Gdc::gpu, tune);
            size_t same_bytes_diff = 0, gpu_diff = 0, total = 0;
            int gpu_max = 0;
            for (size_t k = 0; k < push.out.size(); ++k)
                for (size_t i = 0; i < push.out[k].size(); ++i) {
                    ++total;
                    same_bytes_diff += push.out[k][i] != buf.out[k][i];
                    int d = std::abs(int(push.out[k][i]) - int(gpu.out[k][i]));
                    gpu_diff += d != 0;
                    gpu_max = std::max(gpu_max, d);
                }
            double mean_err = 0;
            for (int s = 0; s < Engine::SLOTS; ++s)
                mean_err = std::max(mean_err, std::fabs(double(gpu.gpu_means[s]) -
                                                        double(cpu_mean(draw(w, h, s), w, h))));
            ok = ok && same_bytes_diff == 0 && mean_err <= 1e-5;
            std::printf("%s{\"rules\": \"%s\", \"bytes\": %zu, \"push_vs_buffer_differ\": %zu, "
                        "\"push_vs_gpu_differ\": %zu, \"push_vs_gpu_max\": %d, \"mean_err\": %.3g, "
                        "\"means\": [%.6f, %.6f, %.6f, %.6f]}",
                        rule ? ", " : "", rule ? "tri+edge" : "default", total, same_bytes_diff, gpu_diff,
                        gpu_max, mean_err, gpu.gpu_means[0], gpu.gpu_means[1], gpu.gpu_means[2],
                        gpu.gpu_means[3]);
        }
        std::printf("], \"ok\": %s}\n", ok ? "true" : "false");
    } catch (const std::exception& e) {
        std::printf("\n{\"error\": \"%s\"}\n", e.what());
        ok = false;
    }
    done(c);
    return ok ? 0 : 1;
}
