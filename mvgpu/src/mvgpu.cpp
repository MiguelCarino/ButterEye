// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// mvgpu: GPU block motion-vector frame interpolation for VapourSynth (spike
// M0(r)), on Vulkan compute so it runs on any vendor.
//
//   core.mvgpu.Interpolate(clip, fps_num, fps_den[, device])
//
// YUV 4:2:0, 8- to 16-bit integer, constant frame rate in; the same format at
// fps_num/fps_den out. Per source pair: upload + luma pyramid per new frame (each
// source frame once), a symmetric block search (4 levels) with a vector median,
// then one motion-compensated render per in-between frame. The frame before the
// pair and the frame after it are held too, with the vectors of their pairs:
// what is covered or uncovered between the pair's frames is told apart by them.
// A source frame whose _SceneChangeNext is set is repeated instead of
// interpolated. The GPU work is the engine's (engine/engine.h), which the present
// layer shares; this file is the VapourSynth glue, the plugin's own Vulkan device,
// the uploads and the readback.

#include <VSHelper4.h>
#include <VapourSynth4.h>
#include <vulkan/vulkan.h>

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <array>
#include <cstdint>
#include <cstring>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include "engine/engine.h"

namespace {

using mvgpu::Buffer;
using mvgpu::check;
using mvgpu::Engine;
using mvgpu::Plane;
using mvgpu::VkError;

// VK_KHR_portability_enumeration reached the Vulkan headers in 1.3.212; a build
// host with older vulkan-headers must still compile, and the name and the bit are
// a frozen part of the extension's ABI, so spelling them out where the header is
// too old is safe. The header constants are preferred wherever they exist.
#ifdef VK_KHR_portability_enumeration
constexpr const char* PORTABILITY_EXT = VK_KHR_PORTABILITY_ENUMERATION_EXTENSION_NAME;
constexpr VkInstanceCreateFlags PORTABILITY_BIT = VK_INSTANCE_CREATE_ENUMERATE_PORTABILITY_BIT_KHR;
#else
constexpr const char* PORTABILITY_EXT = "VK_KHR_portability_enumeration";
constexpr VkInstanceCreateFlags PORTABILITY_BIT = 0x00000001;
#endif

// True when the loader advertises the portability-enumeration extension. Measured on
// loader 1.4.363: it advertises it unconditionally, with no portability driver
// installed and even with a single deliberately broken ICD, because the loader rather
// than a driver implements it. So this answers only "may I ask for it", never "is
// there a portability driver" - which is why the caller asks for it on evidence of a
// hidden driver instead of on this alone. An enabled extension the loader does not
// advertise would make vkCreateInstance fail with VK_ERROR_EXTENSION_NOT_PRESENT, so
// the check is still needed for loaders older than 1.3.212, where it did not exist.
bool portability_offered() {
    uint32_t n = 0;
    if (vkEnumerateInstanceExtensionProperties(nullptr, &n, nullptr) != VK_SUCCESS || !n)
        return false;
    std::vector<VkExtensionProperties> have(n);
    VkResult r = vkEnumerateInstanceExtensionProperties(nullptr, &n, have.data());
    if (r != VK_SUCCESS && r != VK_INCOMPLETE) return false;
    have.resize(n);  // a successful call reports how many it actually wrote
    for (const auto& e : have)
        if (std::strcmp(e.extensionName, PORTABILITY_EXT) == 0) return true;
    return false;
}

// the engine's table from the loader the plugin links
mvgpu::VkFns fill_from_loader() {
    mvgpu::VkFns f;
#define MVGPU_X(n) f.n = vk##n;
    MVGPU_VK_FNS(MVGPU_X)
#undef MVGPU_X
    return f;
}

// The plugin's half: its own instance and device, the uploads, the one command
// buffer it submits and waits for, and the readback ring. The GPU work itself is
// the engine's (engine/engine.h).
class Gpu {
public:
    // whether the frames around the pair are wanted (MVGPU_TRI=1)
    bool three_frames() const { return tuning.tri; }
    Gpu(int w, int h, int bits, int device);
    ~Gpu();
    Gpu(const Gpu&) = delete;
    Gpu& operator=(const Gpu&) = delete;
    // upload source frame planes into slot s and build its luma pyramid
    void load(int slot, const VSFrame* f, const VSAPI* api);
    // One job on the GPU: the vectors of each pair in `searches` (the slot of
    // its first frame; `follows`: the pair searched before it is the pair
    // before it), then the pair (slot a, slot a + 1) rendered at t into a ring
    // buffer, whose index is returned. has_prev, has_next: the frame before A
    // and the frame after C are loaded (slots a - 1 and a + 2) and belong to the
    // same shot, and so are the vectors of the pairs they close.
    struct Search { int a; bool follows; };
    int render(int a, float t, const std::vector<Search>& searches, bool has_prev, bool has_next);
    static constexpr int SLOTS = Engine::SLOTS;
    // copy ring buffer r into dst and give the buffer back (no GPU lock needed)
    void deliver(int r, VSFrame* dst, const VSAPI* api);
    std::string name;
    // MVGPU_PROFILE=1: mean milliseconds per call on stderr every 100 renders
    bool profile = std::getenv("MVGPU_PROFILE") != nullptr;
    double t_load_cpu = 0, t_load_gpu = 0, t_gpu = 0, t_out_cpu = 0;
    int n_load = 0, n_render = 0;

private:
    // set once the device is lost or a submitted batch failed: the GPU state is
    // unknown, so every later call fails at once (ButterEye then rolls back)
    std::string failed;
    VkInstance inst = VK_NULL_HANDLE;
    VkPhysicalDevice phys = VK_NULL_HANDLE;
    VkDevice dev = VK_NULL_HANDLE;
    VkQueue queue = VK_NULL_HANDLE;
    uint32_t qfam = 0;
    VkCommandPool pool = VK_NULL_HANDLE;
    VkCommandBuffer cmd = VK_NULL_HANDLE;
    VkFence fence = VK_NULL_HANDLE;
    VkPhysicalDeviceMemoryProperties memprops{};
    VkPhysicalDeviceLimits limits{};

    int w, h, bits, bytes = 1;
    const mvgpu::VkFns fns = fill_from_loader();
    const mvgpu::Tuning tuning = mvgpu::Tuning::from_env();
    std::unique_ptr<mvgpu::Memory> mem;
    std::unique_ptr<Engine> eng;
    Buffer up;
    // readback ring: a render lands in a free buffer; the caller copies it out
    // after the GPU lock is released, so copies of different frames overlap
    static constexpr int RING = 8;
    std::array<Buffer, RING> down;
    std::array<bool, RING> busy{};
    std::mutex ring_lock;
    std::condition_variable ring_free;

    void init(int device);
    void destroy();
    void ensure_ok() const;
    void begin();
    void submit();
};

Gpu::Gpu(int width, int height, int bitdepth, int device) : w(width), h(height), bits(bitdepth) {
    // a constructor that throws never runs ~Gpu(): release what init() made
    try {
        init(device);
    } catch (...) {
        destroy();
        throw;
    }
}

void Gpu::ensure_ok() const {
    if (!failed.empty()) throw std::runtime_error(failed);
}

void Gpu::init(int device) {
    bytes = bits > 8 ? 2 : 1;
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO};
    app.pApplicationName = "mvgpu";
    app.apiVersion = VK_API_VERSION_1_1;
    VkInstanceCreateInfo ic{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO};
    ic.pApplicationInfo = &app;
    // Ask first exactly as every build always has: no extensions, flags 0. Where a
    // native driver answers - every stock Linux and Windows machine - this succeeds
    // and nothing below runs, so their behaviour is unchanged to the byte.
    VkResult r = vkCreateInstance(&ic, nullptr, &inst);
    if (r == VK_ERROR_INCOMPATIBLE_DRIVER && portability_offered()) {
        // The loader hides every driver whose ICD declares "is_portability_driver"
        // from an application that has not said it wants to see them, and answers
        // VK_ERROR_INCOMPATIBLE_DRIVER (-9) instead. On a Mac that is the whole
        // story: MoltenVK (Vulkan on Metal) is the only driver there is.
        //
        // The trigger is the refusal, not the platform and not the extension's mere
        // presence. Not #ifdef __APPLE__, because being a portability driver is a
        // property of the ICDs installed on the machine rather than of the operating
        // system - translation layers exist on Linux and Windows too - and a platform
        // macro would be a lie about what was detected. Not the extension list alone,
        // because the loader advertises that extension everywhere (see
        // portability_offered above), so asking on that basis would quietly set the
        // flag on Linux as well, where a portability driver installed beside the real
        // one would then shift the device indices that ``device`` selects.
        const char* exts[1] = {PORTABILITY_EXT};
        ic.enabledExtensionCount = 1;
        ic.ppEnabledExtensionNames = exts;
        ic.flags |= PORTABILITY_BIT;
        r = vkCreateInstance(&ic, nullptr, &inst);
    }
    check(r, "vkCreateInstance");
    uint32_t n = 0;
    vkEnumeratePhysicalDevices(inst, &n, nullptr);
    std::vector<VkPhysicalDevice> devs(n);
    vkEnumeratePhysicalDevices(inst, &n, devs.data());
    if (devs.empty()) throw std::runtime_error("no Vulkan device");
    if (device >= 0) {
        if (device >= int(n)) throw std::runtime_error("no Vulkan device " + std::to_string(device));
        phys = devs[device];
    } else {  // the first discrete GPU, else the first non-CPU device
        // On Apple Silicon there is no discrete GPU at all: MoltenVK reports the M-series
        // GPU as INTEGRATED, so it is the second test that picks it. The last line keeps a
        // CPU-only install (lavapipe, SwiftShader) working rather than crashing on a null.
        for (auto d : devs) {
            VkPhysicalDeviceProperties pp;
            vkGetPhysicalDeviceProperties(d, &pp);
            if (pp.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) { phys = d; break; }
            if (!phys && pp.deviceType != VK_PHYSICAL_DEVICE_TYPE_CPU) phys = d;
        }
        if (!phys) phys = devs[0];
    }
    VkPhysicalDeviceProperties pp;
    vkGetPhysicalDeviceProperties(phys, &pp);
    name = pp.deviceName;
    limits = pp.limits;
    Engine::check_limits(limits, w, h);
    vkGetPhysicalDeviceMemoryProperties(phys, &memprops);
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &n, nullptr);
    std::vector<VkQueueFamilyProperties> fams(n);
    vkGetPhysicalDeviceQueueFamilyProperties(phys, &n, fams.data());
    qfam = UINT32_MAX;
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
    check(vkCreateDevice(phys, &di, nullptr, &dev), "vkCreateDevice");
    vkGetDeviceQueue(dev, qfam, 0, &queue);
    VkCommandPoolCreateInfo cp{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    cp.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    cp.queueFamilyIndex = qfam;
    check(vkCreateCommandPool(dev, &cp, nullptr, &pool), "vkCreateCommandPool");
    VkCommandBufferAllocateInfo ca{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    ca.commandPool = pool;
    ca.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    ca.commandBufferCount = 1;
    check(vkAllocateCommandBuffers(dev, &ca, &cmd), "vkAllocateCommandBuffers");
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
    check(vkCreateFence(dev, &fi, nullptr, &fence), "vkCreateFence");

    // the staging buffers come first, the engine's device buffers after them (the
    // order the plugin has always allocated in)
    mem = std::make_unique<mvgpu::Memory>(fns, dev, memprops);
    int cw = (w + 1) / 2, ch = (h + 1) / 2;
    uint32_t raw_bytes = 0;
    for (int i = 0; i < 3; ++i) raw_bytes += (uint32_t((i ? cw * ch : w * h) * bytes) + 3u) & ~3u;
    const auto HOST = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    up = mem->make(raw_bytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, HOST);
    for (auto& d : down)
        d = mem->make(raw_bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT, HOST | VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
    eng = std::make_unique<Engine>(fns, dev, memprops, limits, w, h, bits, tuning);
    if (eng->raw_size() != raw_bytes) throw std::logic_error("mvgpu: plane layout mismatch");
}

Gpu::~Gpu() { destroy(); }

void Gpu::destroy() {
    // safe on a partly built object: every handle is checked (or ignored when null)
    if (!dev) {
        if (inst) vkDestroyInstance(inst, nullptr);
        inst = VK_NULL_HANDLE;
        return;
    }
    vkDeviceWaitIdle(dev);
    eng.reset();
    if (mem) {
        mem->free(up);
        for (auto& d : down) mem->free(d);
    }
    mem.reset();
    vkDestroyFence(dev, fence, nullptr);
    vkDestroyCommandPool(dev, pool, nullptr);
    vkDestroyDevice(dev, nullptr);
    vkDestroyInstance(inst, nullptr);
    dev = VK_NULL_HANDLE;
    inst = VK_NULL_HANDLE;
}

void Gpu::begin() {
    vkResetCommandBuffer(cmd, 0);
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    check(vkBeginCommandBuffer(cmd, &bi), "vkBeginCommandBuffer");
}

void Gpu::submit() {
    try {
        check(vkEndCommandBuffer(cmd), "vkEndCommandBuffer");
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO};
        si.commandBufferCount = 1;
        si.pCommandBuffers = &cmd;
        check(vkQueueSubmit(queue, 1, &si, fence), "vkQueueSubmit");
    } catch (const VkError& e) {
        // nothing ran: a transient failure (e.g. out of memory) may pass next time
        if (e.result == VK_ERROR_DEVICE_LOST) failed = e.what();
        throw;
    }
    try {
        check(vkWaitForFences(dev, 1, &fence, VK_TRUE, UINT64_MAX), "vkWaitForFences");
        check(vkResetFences(dev, 1, &fence), "vkResetFences");
    } catch (const std::exception& e) {
        failed = e.what();  // the batch ran or is running: its state is unknown
        throw;
    }
}

using Clock = std::chrono::steady_clock;
double ms(Clock::time_point a, Clock::time_point b) {
    return std::chrono::duration<double, std::milli>(b - a).count();
}

void Gpu::load(int s, const VSFrame* f, const VSAPI* api) {
    ensure_ok();
    auto t0 = Clock::now();
    auto* dst = static_cast<uint8_t*>(up.map);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = eng->plane(i);
        const uint8_t* src = api->getReadPtr(f, i);
        ptrdiff_t stride = api->getStride(f, i);
        size_t row = size_t(p.w) * bytes;
        for (int y = 0; y < p.h; ++y) std::memcpy(dst + p.raw_off + y * row, src + y * stride, row);
        if (i == 0) {  // mean luma from every 4th pixel of every 4th row
            double sum = 0;
            size_t cnt = 0;
            for (int y = 0; y < p.h; y += 4) {
                const uint8_t* r = src + y * stride;
                for (int x = 0; x < p.w; x += 4, ++cnt)
                    sum += bytes == 1 ? r[x] : reinterpret_cast<const uint16_t*>(r)[x];
            }
            eng->set_mean(s, float(sum / double(cnt) / double((1u << bits) - 1u)));
        }
    }
    auto t1 = Clock::now();
    begin();
    VkBufferCopy cp{0, 0, eng->raw_size()};
    vkCmdCopyBuffer(cmd, up.buf, eng->raw_input().buf, 1, &cp);
    eng->barrier(cmd);
    eng->record_load(cmd, s);
    submit();
    t_load_cpu += ms(t0, t1);
    t_load_gpu += ms(t1, Clock::now());
    ++n_load;
}

int Gpu::render(int a, float t, const std::vector<Search>& searches, bool has_prev, bool has_next) {
    ensure_ok();
    // a free readback buffer (callers hold the GPU lock; deliver() frees them)
    int r = -1;
    {
        std::unique_lock<std::mutex> g(ring_lock);
        ring_free.wait(g, [&] {
            for (int i = 0; i < RING; ++i)
                if (!busy[i]) { r = i; return true; }
            return false;
        });
        busy[r] = true;
    }
    // give the buffer back if recording or submitting throws: a leaked buffer
    // would leave the next render waiting forever while it holds the GPU lock
    struct Release {
        Gpu* g;
        int r;
        bool armed = true;
        ~Release() {
            if (!armed) return;
            {
                std::lock_guard<std::mutex> lk(g->ring_lock);
                g->busy[r] = false;
            }
            g->ring_free.notify_one();
        }
    } release{this, r};
    auto t0 = Clock::now();
    begin();
    for (const Search& s : searches) eng->record_vectors(cmd, s.a, s.follows);
    eng->record_select(cmd, a, t, has_prev, has_next);
    eng->record_render(cmd, a, t);
    VkBufferCopy cp{0, 0, eng->raw_size()};
    vkCmdCopyBuffer(cmd, eng->raw_output().buf, down[r].buf, 1, &cp);
    // the fence makes device writes available to the device only: deliver()
    // reads down[r] on the host, so make the copy visible to host reads
    VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    hb.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    hb.dstAccessMask = VK_ACCESS_HOST_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0, nullptr, 0,
                         nullptr);
    submit();
    release.armed = false;
    t_gpu += ms(t0, Clock::now());
    return r;
}

void Gpu::deliver(int r, VSFrame* dst, const VSAPI* api) {
    auto t1 = Clock::now();
    auto* src = static_cast<const uint8_t*>(down[r].map);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = eng->plane(i);
        uint8_t* d = api->getWritePtr(dst, i);
        ptrdiff_t stride = api->getStride(dst, i);
        size_t row = size_t(p.w) * bytes;
        for (int y = 0; y < p.h; ++y) std::memcpy(d + y * stride, src + p.raw_off + y * row, row);
    }
    {
        std::lock_guard<std::mutex> g(ring_lock);
        busy[r] = false;
        t_out_cpu += ms(t1, Clock::now());
        ++n_render;
    }
    ring_free.notify_one();
    if (profile && n_render % 100 == 0)
        std::fprintf(stderr, "mvgpu: per load %.2f ms CPU + %.2f ms GPU; per render %.2f ms GPU + %.2f ms CPU\n",
                     t_load_cpu / std::max(n_load, 1), t_load_gpu / std::max(n_load, 1), t_gpu / n_render,
                     t_out_cpu / n_render);
}

struct Data {
    VSNode* node = nullptr;
    VSVideoInfo vi{};
    int64_t src_num = 0, src_den = 1;  // source rate
    int src_frames = 0;
    std::unique_ptr<Gpu> gpu;
    std::mutex lock;
    std::array<int, Gpu::SLOTS> slot_frame{-1, -1, -1, -1};  // source frame held by each slot
    std::array<int, Gpu::SLOTS> pair{-1, -1, -1, -1};        // pair (its first frame) whose vectors each slot holds
    int searched = -1;                                       // the pair searched last
};

// output frame n -> source index i and position t in [0, 1)
void position(const Data* d, int n, int& i, int64_t& frac_num, int64_t& frac_den) {
    // n / out_fps * src_fps = n * out_den * src_num / (out_num * src_den)
    int64_t num = int64_t(n) * d->vi.fpsDen * d->src_num;
    int64_t den = d->vi.fpsNum * d->src_den;
    i = int(num / den);
    frac_num = num % den;
    frac_den = den;
}

const VSFrame* VS_CC getFrame(int n, int ar, void* inst, void**, VSFrameContext* ctx, VSCore* core,
                              const VSAPI* api) {
    auto* d = static_cast<Data*>(inst);
    int i;
    int64_t fn, fd;
    position(d, n, i, fn, fd);
    i = std::min(i, d->src_frames - 1);
    bool between = fn != 0 && i + 1 < d->src_frames;
    // the frame before the pair and the one after it: what is covered or
    // uncovered between A and C is told apart by them (select.comp)
    // without the three-frame rule the pair is all that is read and searched
    const bool tri = d->gpu->three_frames();
    bool before = tri && between && i >= 1, after = tri && between && i + 2 < d->src_frames;
    if (ar == arInitial) {
        if (before) api->requestFrameFilter(i - 1, d->node, ctx);
        api->requestFrameFilter(i, d->node, ctx);
        if (between) api->requestFrameFilter(i + 1, d->node, ctx);
        if (after) api->requestFrameFilter(i + 2, d->node, ctx);
        return nullptr;
    }
    if (ar != arAllFramesReady) return nullptr;
    const VSFrame* fa = api->getFrameFilter(i, d->node, ctx);
    auto finish = [&](VSFrame* f) {
        VSMap* props = api->getFramePropertiesRW(f);
        api->mapSetInt(props, "_DurationNum", d->vi.fpsDen, maReplace);
        api->mapSetInt(props, "_DurationDen", d->vi.fpsNum, maReplace);
        return f;
    };
    auto cut_after = [&](const VSFrame* f) {
        int err = 0;
        return api->mapGetInt(api->getFramePropertiesRO(f), "_SceneChangeNext", 0, &err) == 1 && !err;
    };
    if (!between || cut_after(fa)) {
        VSFrame* f = api->copyFrame(fa, core);
        api->freeFrame(fa);
        return finish(f);
    }
    const int S = Gpu::SLOTS;
    // P, A, C, N: the frames i - 1 .. i + 2 (P and N only within the shot)
    std::array<const VSFrame*, 4> fr{nullptr, fa, api->getFrameFilter(i + 1, d->node, ctx), nullptr};
    if (before) {
        fr[0] = api->getFrameFilter(i - 1, d->node, ctx);
        if (cut_after(fr[0])) { api->freeFrame(fr[0]); fr[0] = nullptr; }
    }
    if (after && !cut_after(fr[2])) fr[3] = api->getFrameFilter(i + 2, d->node, ctx);
    auto release = [&] {
        for (const VSFrame* f : fr)
            if (f) api->freeFrame(f);
    };
    VSFrame* dst = api->newVideoFrame(&d->vi.format, d->vi.width, d->vi.height, fa, core);
    try {
        int r;
        {
            std::lock_guard<std::mutex> g(d->lock);
            // a source frame lives in the slot of its index mod 4, and is loaded once
            for (int k = 0; k < 4; ++k) {
                int n = i - 1 + k, s = ((n % S) + S) % S;
                if (!fr[k] || d->slot_frame[s] == n) continue;
                // the vectors that were searched on what this slot held are gone with it
                d->gpu->load(s, fr[k], api);
                d->slot_frame[s] = n;
            }
            // the vectors of the pair before, this pair and the pair after, in order
            std::vector<Gpu::Search> todo;
            for (int j = i - 1; j <= i + 1; ++j) {
                if ((j < i && !fr[0]) || (j > i && !fr[3])) continue;
                int s = j % S;
                if (d->pair[s] == j) continue;
                todo.push_back({s, d->searched == j - 1});
                d->pair[s] = j;
                d->searched = j;
            }
            r = d->gpu->render(i % S, float(double(fn) / double(fd)), todo, fr[0] != nullptr, fr[3] != nullptr);
        }
        d->gpu->deliver(r, dst, api);  // outside the GPU lock: copies overlap
    } catch (const std::exception& e) {
        {
            // a slot may hold half an upload and the vectors half a search: forget
            // both, so the next request loads and searches again
            std::lock_guard<std::mutex> g(d->lock);
            d->pair.fill(-1);
            d->searched = -1;
            d->slot_frame.fill(-1);
        }
        release();
        api->freeFrame(dst);
        api->setFilterError((std::string("mvgpu: ") + e.what()).c_str(), ctx);
        return nullptr;
    }
    release();
    return finish(dst);
}

void VS_CC freeData(void* inst, VSCore*, const VSAPI* api) {
    auto* d = static_cast<Data*>(inst);
    api->freeNode(d->node);
    delete d;
}

void VS_CC create(const VSMap* in, VSMap* out, void*, VSCore* core, const VSAPI* api) {
    auto d = std::make_unique<Data>();
    d->node = api->mapGetNode(in, "clip", 0, nullptr);
    const VSVideoInfo* vi = api->getVideoInfo(d->node);
    auto fail = [&](const std::string& msg) {
        api->mapSetError(out, ("mvgpu.Interpolate: " + msg).c_str());
        api->freeNode(d->node);
    };
    const VSVideoFormat& f = vi->format;
    if (!vsh::isConstantVideoFormat(vi) || f.colorFamily != cfYUV || f.sampleType != stInteger ||
        f.subSamplingW != 1 || f.subSamplingH != 1 || f.bitsPerSample > 16)
        return fail("only constant YUV 4:2:0 integer clips (8 to 16 bits)");
    if (vi->fpsNum <= 0 || vi->fpsDen <= 0) return fail("the clip needs a constant frame rate");
    int err = 0;
    int64_t num = api->mapGetInt(in, "fps_num", 0, &err);
    int64_t den = api->mapGetInt(in, "fps_den", 0, &err);
    if (err) den = 1;
    if (num <= 0 || den <= 0) return fail("fps_num and fps_den must be positive");
    int device = int(api->mapGetInt(in, "device", 0, &err));
    if (err) device = -1;
    d->src_num = vi->fpsNum;
    d->src_den = vi->fpsDen;
    d->src_frames = vi->numFrames;
    d->vi = *vi;
    vsh::reduceRational(&num, &den);
    d->vi.fpsNum = num;
    d->vi.fpsDen = den;
    // the output covers the same duration: frames * out_fps / src_fps
    d->vi.numFrames = int(int64_t(vi->numFrames) * num * vi->fpsDen / (den * vi->fpsNum));
    try {
        d->gpu = std::make_unique<Gpu>(vi->width, vi->height, f.bitsPerSample, device);
    } catch (const std::exception& e) {
        return fail(e.what());
    }
    // release first: argument evaluation order is unspecified, and &d->vi must
    // not be read after d gave up its pointer
    Data* data = d.release();
    VSFilterDependency deps[] = {{data->node, rpGeneral}};
    api->createVideoFilter(out, "Interpolate", &data->vi, getFrame, freeData, fmParallel, deps, 1, data, core);
}

}  // namespace

VS_EXTERNAL_API(void) VapourSynthPluginInit2(VSPlugin* plugin, const VSPLUGINAPI* vspapi) {
    vspapi->configPlugin("io.github.buttereye.mvgpu", "mvgpu", "GPU block motion-vector interpolation (Vulkan)",
                         VS_MAKE_VERSION(MVGPU_VERSION_MAJOR, MVGPU_VERSION_MINOR), VAPOURSYNTH_API_VERSION, 0,
                         plugin);
    vspapi->registerFunction("Interpolate", "clip:vnode;fps_num:int;fps_den:int:opt;device:int:opt;",
                             "clip:vnode;", create, nullptr, plugin);
}
