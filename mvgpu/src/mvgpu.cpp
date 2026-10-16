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
// then one motion-compensated render per in-between frame. A source frame whose
// _SceneChangeNext is set is repeated instead of interpolated.

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

#include "shaders.h"

namespace {

constexpr int BLOCK = 8;
constexpr int LEVELS = 4;
constexpr int COARSE_RADIUS = 8;

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

// a failed Vulkan call; ``result`` tells a lost device from a transient failure
struct VkError : std::runtime_error {
    VkResult result;
    VkError(const std::string& what, VkResult r) : std::runtime_error(what), result(r) {}
};

void check(VkResult r, const char* what) {
    if (r != VK_SUCCESS) throw VkError(std::string(what) + " failed (" + std::to_string(r) + ")", r);
}

struct Buffer {
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    VkDeviceSize size = 0;
    void* map = nullptr;
};

struct Pipe {
    VkPipeline pipe = VK_NULL_HANDLE;
    VkPipelineLayout layout = VK_NULL_HANDLE;
    VkDescriptorSetLayout dsl = VK_NULL_HANDLE;
    int bindings = 0;
};

struct Plane {
    int w, h;
    uint32_t float_off;  // in the float slot buffers
    uint32_t raw_off;    // byte offset in the packed transfer buffers (4-aligned)
};

class Gpu {
public:
    Gpu(int w, int h, int bits, int device);
    ~Gpu();
    Gpu(const Gpu&) = delete;
    Gpu& operator=(const Gpu&) = delete;
    // upload source frame planes into slot s and build its luma pyramid
    void load(int slot, const VSFrame* f, const VSAPI* api);
    // vectors for the pair (slot a, slot c), then render at t into dst
    // render the pair (slot a, slot 1-a) at t into a ring buffer; returns its index
    int render(int a, float t, bool new_vectors);
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
    VkDescriptorPool dpool = VK_NULL_HANDLE;
    VkPhysicalDeviceMemoryProperties memprops{};

    int w, h, bytes, bits;
    std::array<Plane, 3> planes{};
    uint32_t floats = 0, raw_bytes = 0;
    std::array<int, LEVELS> lw{}, lh{}, bw{}, bh{};
    std::array<uint32_t, LEVELS> loff{};
    uint32_t pyr_floats = 0;

    Buffer up, raw_in, raw_out, out, field;
    // readback ring: a render lands in a free buffer; the caller copies it out
    // after the GPU lock is released, so copies of different frames overlap
    static constexpr int RING = 8;
    std::array<Buffer, RING> down;
    std::array<bool, RING> busy{};
    std::mutex ring_lock;
    std::condition_variable ring_free;
    std::array<Buffer, 2> slot, pyr;
    std::array<Buffer, LEVELS> vraw, vec;
    Buffer vraw4, vec4;  // 4x4 refinement at full size
    Buffer vsub;         // the final half-vectors as floats, refined to 1/4 px (subpel.comp)
    int bw4 = 0, bh4 = 0;
    // MVGPU_FINE=0: vectors per 8x8 block only (for comparisons)
    bool fine = !(std::getenv("MVGPU_FINE") && std::string(std::getenv("MVGPU_FINE")) == "0");
    std::array<VkDescriptorSet, 2> ds_median4{};
    std::array<VkDescriptorSet, 2> ds_search4{}, ds_subpel{}, ds_subpel4{};
    // MVGPU_SUBPEL=0: no 1/2 px refinement of the final motion (for comparisons)
    int subpel_steps = std::getenv("MVGPU_SUBPEL") && std::string(std::getenv("MVGPU_SUBPEL")) == "0" ? 0 : 1;
    // MVGPU_MEDIAN=0: the plain component median at the full-size levels too (for
    // comparisons); otherwise a vector median kept only where it matches the block
    // about as well: SAD within med_rel of the block's own plus med_abs a pixel
    bool checked_median = !(std::getenv("MVGPU_MEDIAN") && std::string(std::getenv("MVGPU_MEDIAN")) == "0");
    float med_rel = 0.5f, med_abs = 0.008f;

    Pipe p_unpack, p_down2, p_search_coarse, p_search_fine, p_search_full, p_search4, p_median, p_subpel, p_select,
        p_render, p_pack;
    std::array<VkDescriptorSet, 2> ds_select{};
    // MVGPU_SELECT=0: the old bilinear block vectors (for comparisons)
    bool select_vectors = !(std::getenv("MVGPU_SELECT") && std::string(std::getenv("MVGPU_SELECT")) == "0");
    // mean |A - C| (0-1 luma) where blending starts to give way to the nearer
    // frame, and where it has fully; MVGPU_OCC="lo,hi" overrides (tuning)
    float occ_lo = 0.04f, occ_hi = 0.12f;
    std::array<VkDescriptorSet, 2> ds_unpack{}, ds_down2{};
    std::array<std::array<VkDescriptorSet, LEVELS>, 2> ds_search{};  // [order][level]
    std::array<std::array<VkDescriptorSet, LEVELS>, 2> ds_median{};  // [order][level]
    std::array<VkDescriptorSet, 2> ds_render{};
    VkDescriptorSet ds_pack{};

    VkPhysicalDeviceLimits limits{};

    void init(int device);
    void destroy();
    void check_limits() const;
    void dispatch1d(uint32_t groups);
    void ensure_ok() const;
    uint32_t find_mem(uint32_t bits, VkMemoryPropertyFlags want) const;
    Buffer make(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags props);
    void free(Buffer& b);
    Pipe pipeline(const uint32_t* code, size_t bytes, int bindings, uint32_t push, uint32_t wg = 0, uint32_t kids = 1);
    VkDescriptorSet set(const Pipe& p, std::initializer_list<const Buffer*> bufs);
    void begin();
    void submit();
    void barrier();
    void push(const Pipe& p, VkDescriptorSet s, const void* pc, uint32_t size);
};

uint32_t Gpu::find_mem(uint32_t type_bits, VkMemoryPropertyFlags want) const {
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

Buffer Gpu::make(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags props) {
    Buffer b;
    b.size = std::max<VkDeviceSize>(size, 16);
    VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bi.size = b.size;
    bi.usage = usage;
    bi.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    check(vkCreateBuffer(dev, &bi, nullptr, &b.buf), "vkCreateBuffer");
    try {
        VkMemoryRequirements req;
        vkGetBufferMemoryRequirements(dev, b.buf, &req);
        VkMemoryAllocateInfo ai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        ai.allocationSize = req.size;
        ai.memoryTypeIndex = find_mem(req.memoryTypeBits, props);
        check(vkAllocateMemory(dev, &ai, nullptr, &b.mem), "vkAllocateMemory");
        check(vkBindBufferMemory(dev, b.buf, b.mem, 0), "vkBindBufferMemory");
        if (props & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)
            check(vkMapMemory(dev, b.mem, 0, VK_WHOLE_SIZE, 0, &b.map), "vkMapMemory");
    } catch (...) {
        free(b);
        throw;
    }
    return b;
}

void Gpu::free(Buffer& b) {
    if (b.map) vkUnmapMemory(dev, b.mem);
    if (b.buf) vkDestroyBuffer(dev, b.buf, nullptr);
    if (b.mem) vkFreeMemory(dev, b.mem, nullptr);
    b = Buffer{};
}

Pipe Gpu::pipeline(const uint32_t* code, size_t nbytes, int bindings, uint32_t push, uint32_t wg, uint32_t kids) {
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
        check(vkCreateDescriptorSetLayout(dev, &dl, nullptr, &p.dsl), "vkCreateDescriptorSetLayout");
        VkPushConstantRange pr{VK_SHADER_STAGE_COMPUTE_BIT, 0, push};
        VkPipelineLayoutCreateInfo pl{VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO};
        pl.setLayoutCount = 1;
        pl.pSetLayouts = &p.dsl;
        pl.pushConstantRangeCount = 1;
        pl.pPushConstantRanges = &pr;
        check(vkCreatePipelineLayout(dev, &pl, nullptr, &p.layout), "vkCreatePipelineLayout");
        VkShaderModuleCreateInfo sm{VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO};
        sm.codeSize = nbytes;
        sm.pCode = code;
        check(vkCreateShaderModule(dev, &sm, nullptr, &mod), "vkCreateShaderModule");
        // search.comp: constant 0 the group size, 1 the child blocks per group
        const uint32_t data[2] = {wg, kids};
        const VkSpecializationMapEntry entries[2] = {{0, 0, sizeof(uint32_t)}, {1, 4, sizeof(uint32_t)}};
        VkSpecializationInfo spec{2, entries, sizeof(data), data};
        VkComputePipelineCreateInfo ci{VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO};
        ci.stage = {VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO};
        ci.stage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
        ci.stage.module = mod;
        ci.stage.pName = "main";
        ci.stage.pSpecializationInfo = wg ? &spec : nullptr;  // 0: not search.comp
        ci.layout = p.layout;
        check(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &ci, nullptr, &p.pipe), "vkCreateComputePipelines");
    } catch (...) {
        vkDestroyShaderModule(dev, mod, nullptr);  // null handles are ignored
        vkDestroyPipelineLayout(dev, p.layout, nullptr);
        vkDestroyDescriptorSetLayout(dev, p.dsl, nullptr);
        throw;
    }
    vkDestroyShaderModule(dev, mod, nullptr);
    return p;
}

VkDescriptorSet Gpu::set(const Pipe& p, std::initializer_list<const Buffer*> bufs) {
    VkDescriptorSetAllocateInfo ai{VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO};
    ai.descriptorPool = dpool;
    ai.descriptorSetCount = 1;
    ai.pSetLayouts = &p.dsl;
    VkDescriptorSet s;
    check(vkAllocateDescriptorSets(dev, &ai, &s), "vkAllocateDescriptorSets");
    std::vector<VkDescriptorBufferInfo> infos;
    infos.reserve(bufs.size());
    for (const Buffer* b : bufs) infos.push_back({b->buf, 0, VK_WHOLE_SIZE});
    std::vector<VkWriteDescriptorSet> w(infos.size());
    for (size_t i = 0; i < infos.size(); ++i) {
        w[i] = {VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET};
        w[i].dstSet = s;
        w[i].dstBinding = uint32_t(i);
        w[i].descriptorCount = 1;
        w[i].descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
        w[i].pBufferInfo = &infos[i];
    }
    vkUpdateDescriptorSets(dev, uint32_t(w.size()), w.data(), 0, nullptr);
    return s;
}

Gpu::Gpu(int width, int height, int bitdepth, int device) : w(width), h(height), bits(bitdepth) {
    // a constructor that throws never runs ~Gpu(): release what init() made
    try {
        init(device);
    } catch (...) {
        destroy();
        throw;
    }
}

void Gpu::check_limits() const {
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

void Gpu::dispatch1d(uint32_t groups) {
    // 1-D passes as rows of at most maxComputeWorkGroupCount[0] groups (65535 on
    // some drivers; 8K luma alone needs 129,600 groups of 256)
    uint32_t gx = std::max<uint32_t>(1, std::min(groups, limits.maxComputeWorkGroupCount[0]));
    vkCmdDispatch(cmd, gx, (groups + gx - 1) / gx, 1);
}

void Gpu::ensure_ok() const {
    if (!failed.empty()) throw std::runtime_error(failed);
}

void Gpu::init(int device) {
    bytes = bits > 8 ? 2 : 1;
    if (const char* occ = std::getenv("MVGPU_OCC")) std::sscanf(occ, "%f,%f", &occ_lo, &occ_hi);
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
    check_limits();
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
    const auto HOST = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
    const auto SB = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT;
    up = make(raw_bytes, VK_BUFFER_USAGE_TRANSFER_SRC_BIT, HOST);
    for (auto& d : down)
        d = make(raw_bytes, VK_BUFFER_USAGE_TRANSFER_DST_BIT, HOST | VK_MEMORY_PROPERTY_HOST_CACHED_BIT);
    raw_in = make(raw_bytes, SB | VK_BUFFER_USAGE_TRANSFER_DST_BIT, DEV);
    raw_out = make(raw_bytes, SB | VK_BUFFER_USAGE_TRANSFER_SRC_BIT, DEV);
    out = make(VkDeviceSize(floats) * 4, SB, DEV);
    field = make(VkDeviceSize(w) * h * 16, SB, DEV);
    for (int s = 0; s < 2; ++s) {
        slot[s] = make(VkDeviceSize(floats) * 4, SB, DEV);
        pyr[s] = make(VkDeviceSize(pyr_floats) * 4, SB, DEV);
    }
    for (int l = 0; l < LEVELS; ++l) {
        vraw[l] = make(VkDeviceSize(bw[l]) * bh[l] * 8, SB, DEV);
        vec[l] = make(VkDeviceSize(bw[l]) * bh[l] * 8, SB, DEV);
    }
    bw4 = (w + 3) / 4;
    bh4 = (h + 3) / 4;
    vraw4 = make(VkDeviceSize(bw4) * bh4 * 8, SB, DEV);
    vec4 = make(VkDeviceSize(bw4) * bh4 * 8, SB, DEV);
    vsub = make(VkDeviceSize(std::max(bw4 * bh4, bw[0] * bh[0])) * 8, SB, DEV);

    VkDescriptorPoolSize ps{VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, 256};
    VkDescriptorPoolCreateInfo dp{VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO};
    dp.maxSets = 64;
    dp.poolSizeCount = 1;
    dp.pPoolSizes = &ps;
    check(vkCreateDescriptorPool(dev, &dp, nullptr, &dpool), "vkCreateDescriptorPool");

    // group sizes: powers of two >= the 17*17, 5*3*3 and 5*5*5 candidates
    p_unpack = pipeline(unpack_spv, sizeof(unpack_spv), 2, 20);
    p_down2 = pipeline(down2_spv, sizeof(down2_spv), 1, 24);
    p_search_coarse = pipeline(search_spv, sizeof(search_spv), 4, 48, 512);
    p_search_fine = pipeline(search_spv, sizeof(search_spv), 4, 48, 64);
    p_search_full = pipeline(search_spv, sizeof(search_spv), 4, 48, 128);
    p_search4 = pipeline(search_spv, sizeof(search_spv), 4, 48, 128, 4);
    p_median = pipeline(median_spv, sizeof(median_spv), 4, 40);
    p_subpel = pipeline(subpel_spv, sizeof(subpel_spv), 4, 28);
    p_select = pipeline(select_spv, sizeof(select_spv), 4, 32);
    p_render = pipeline(render_spv, sizeof(render_spv), 4, 36);
    p_pack = pipeline(pack_spv, sizeof(pack_spv), 2, 20);

    for (int s = 0; s < 2; ++s) {
        ds_unpack[s] = set(p_unpack, {&raw_in, &slot[s]});
        ds_down2[s] = set(p_down2, {&pyr[s]});
        ds_subpel[s] = set(p_subpel, {&pyr[s], &pyr[1 - s], &vec[0], &vsub});
        ds_subpel4[s] = set(p_subpel, {&pyr[s], &pyr[1 - s], &vec4, &vsub});
        ds_select[s] = set(p_select, {&slot[s], &slot[1 - s], &vsub, &field});
        ds_search4[s] = set(p_search4, {&pyr[s], &pyr[1 - s], &vec[0], &vraw4});
        ds_render[s] = set(p_render, {&slot[s], &slot[1 - s], &field, &out});
        for (int l = 0; l < LEVELS; ++l) {
            const Pipe& sp = l == LEVELS - 1 ? p_search_coarse : l == 0 ? p_search_full : p_search_fine;
            const Buffer& parent = l == LEVELS - 1 ? vec[l] : vec[l + 1];
            ds_search[s][l] = set(sp, {&pyr[s], &pyr[1 - s], &parent, &vraw[l]});
        }
    }
    for (int s = 0; s < 2; ++s) {
        for (int l = 0; l < LEVELS; ++l) ds_median[s][l] = set(p_median, {&pyr[s], &pyr[1 - s], &vraw[l], &vec[l]});
        ds_median4[s] = set(p_median, {&pyr[s], &pyr[1 - s], &vraw4, &vec4});
    }
    ds_pack = set(p_pack, {&out, &raw_out});
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
    for (Pipe* p : {&p_unpack, &p_down2, &p_search_coarse, &p_search_fine, &p_search_full, &p_search4, &p_median,
                    &p_subpel, &p_select, &p_render, &p_pack}) {
        vkDestroyPipeline(dev, p->pipe, nullptr);
        vkDestroyPipelineLayout(dev, p->layout, nullptr);
        vkDestroyDescriptorSetLayout(dev, p->dsl, nullptr);
    }
    vkDestroyDescriptorPool(dev, dpool, nullptr);
    for (Buffer* b : {&up, &raw_in, &raw_out, &out, &field, &slot[0], &slot[1], &pyr[0], &pyr[1]}) free(*b);
    for (auto& d : down) free(d);
    for (int l = 0; l < LEVELS; ++l) {
        free(vraw[l]);
        free(vec[l]);
    }
    free(vraw4);
    free(vec4);
    free(vsub);
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

void Gpu::barrier() {
    VkMemoryBarrier mb{VK_STRUCTURE_TYPE_MEMORY_BARRIER};
    mb.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_WRITE_BIT;
    mb.dstAccessMask = VK_ACCESS_SHADER_READ_BIT | VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_TRANSFER_READ_BIT;
    vkCmdPipelineBarrier(cmd, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT,
                         VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1, &mb, 0,
                         nullptr, 0, nullptr);
}

void Gpu::push(const Pipe& p, VkDescriptorSet s, const void* pc, uint32_t size) {
    vkCmdBindPipeline(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p.pipe);
    vkCmdBindDescriptorSets(cmd, VK_PIPELINE_BIND_POINT_COMPUTE, p.layout, 0, 1, &s, 0, nullptr);
    vkCmdPushConstants(cmd, p.layout, VK_SHADER_STAGE_COMPUTE_BIT, 0, size, pc);
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
        const Plane& p = planes[i];
        const uint8_t* src = api->getReadPtr(f, i);
        ptrdiff_t stride = api->getStride(f, i);
        size_t row = size_t(p.w) * bytes;
        for (int y = 0; y < p.h; ++y) std::memcpy(dst + p.raw_off + y * row, src + y * stride, row);
    }
    auto t1 = Clock::now();
    begin();
    VkBufferCopy cp{0, 0, raw_bytes};
    vkCmdCopyBuffer(cmd, up.buf, raw_in.buf, 1, &cp);
    barrier();
    float scale = 1.0f / float((1u << bits) - 1u);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t count, raw_off, out_off, bytes; float scale; } pc{
            uint32_t(p.w * p.h), p.raw_off, p.float_off, uint32_t(bytes), scale};
        push(p_unpack, ds_unpack[s], &pc, sizeof(pc));
        dispatch1d((pc.count + 255) / 256);
    }
    barrier();
    // pyramid level 0 is the luma plane: copy it, then halve
    VkBufferCopy yc{0, VkDeviceSize(loff[0]) * 4, VkDeviceSize(w) * h * 4};
    vkCmdCopyBuffer(cmd, slot[s].buf, pyr[s].buf, 1, &yc);
    barrier();
    for (int l = 1; l < LEVELS; ++l) {
        struct { uint32_t src_off, dst_off; int32_t sw, sh, dw, dh; } pc{
            loff[l - 1], loff[l], lw[l - 1], lh[l - 1], lw[l], lh[l]};
        push(p_down2, ds_down2[s], &pc, sizeof(pc));
        vkCmdDispatch(cmd, (lw[l] + 15) / 16, (lh[l] + 15) / 16, 1);
        barrier();
    }
    submit();
    t_load_cpu += ms(t0, t1);
    t_load_gpu += ms(t1, Clock::now());
    ++n_load;
}

int Gpu::render(int a, float t, bool new_vectors) {
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
    if (new_vectors) {
        for (int l = LEVELS - 1; l >= 0; --l) {
            bool coarse = l == LEVELS - 1;
            // level 0 (full size) stores the motion m = 2u: +-1 px of u is +-2 of m, around
            // 4 x the parent's u
            bool full = l == 0;
            struct { uint32_t off; int32_t w, h, bw, bh, pw, ph, radius, npred, bs, pscale, half_units; } pc{
                loff[l], lw[l], lh[l], bw[l], bh[l],
                coarse ? 1 : bw[l + 1], coarse ? 1 : bh[l + 1], coarse ? COARSE_RADIUS : full ? 2 : 1,
                coarse ? 1 : 5,
                BLOCK, full ? 4 : 2, full ? 1 : 0};
            push(coarse ? p_search_coarse : full ? p_search_full : p_search_fine, ds_search[a][l], &pc, sizeof(pc));
            vkCmdDispatch(cmd, bw[l], bh[l], 1);
            barrier();
            struct { uint32_t off; int32_t w, h, bw, bh, bs, half_units, mode; float rel, abs; } mc{
                loff[l], lw[l], lh[l], bw[l], bh[l], BLOCK, full ? 1 : 0, full && checked_median ? 1 : 0, med_rel,
                med_abs};
            push(p_median, ds_median[a][l], &mc, sizeof(mc));
            vkCmdDispatch(cmd, (bw[l] + 15) / 16, (bh[l] + 15) / 16, 1);
            barrier();
        }
        if (fine) {  // 4x4 blocks refine +-2 (motion units) around their 8x8 block's vector (same level)
            struct { uint32_t off; int32_t w, h, bw, bh, pw, ph, radius, npred, bs, pscale, half_units; } pc{
                loff[0], w, h, bw4, bh4, bw[0], bh[0], 2, 5, 4, 1, 1};
            push(p_search4, ds_search4[a], &pc, sizeof(pc));
            vkCmdDispatch(cmd, bw[0], bh[0], 1);  // a group per 8x8 block does its four 4x4 blocks
            barrier();
            struct { uint32_t off; int32_t w, h, bw, bh, bs, half_units, mode; float rel, abs; } mc{
                loff[0], w, h, bw4, bh4, 4, 1, checked_median ? 1 : 0, med_rel, med_abs};
            push(p_median, ds_median4[a], &mc, sizeof(mc));
            vkCmdDispatch(cmd, (bw4 + 15) / 16, (bh4 + 15) / 16, 1);
            barrier();
        }
        // the final motion as float half-vectors, +-1/4 px of u where that matches better
        int sbw = fine ? bw4 : bw[0], sbh = fine ? bh4 : bh[0];
        struct { uint32_t off; int32_t w, h, bw, bh, bs, steps; } spc{
            loff[0], w, h, sbw, sbh, fine ? 4 : BLOCK, subpel_steps};
        push(p_subpel, fine ? ds_subpel4[a] : ds_subpel[a], &spc, sizeof(spc));
        // a group per 8x8 block or per four 4x4 blocks, in rows of up to 1024 groups
        uint32_t sg = uint32_t(sbw * sbh + (fine ? 3 : 0)) / (fine ? 4 : 1);
        vkCmdDispatch(cmd, std::min(sg, 1024u), (sg + 1023) / 1024, 1);
        barrier();
    }
    {
        struct { int32_t w, h, bw, bh; float t, lambda; int32_t smooth, bs; } sc{
            w, h, fine ? bw4 : bw[0], fine ? bh4 : bh[0], t, 0.05f, select_vectors ? 0 : 1, fine ? 4 : BLOCK};
        push(p_select, ds_select[a], &sc, sizeof(sc));
        vkCmdDispatch(cmd, (w + 15) / 16, (h + 15) / 16, 1);
        barrier();
    }
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t off; int32_t w, h, lw, lh, sub; float t, lo, hi; } pc{
            p.float_off, p.w, p.h, w, h, i ? 2 : 1, t, occ_lo, occ_hi};
        push(p_render, ds_render[a], &pc, sizeof(pc));
        vkCmdDispatch(cmd, (p.w + 15) / 16, (p.h + 15) / 16, 1);
    }
    barrier();
    float maxv = float((1u << bits) - 1u);
    for (int i = 0; i < 3; ++i) {
        const Plane& p = planes[i];
        struct { uint32_t count, raw_off, in_off, bytes; float maxv; } pc{
            uint32_t(p.w * p.h), p.raw_off, p.float_off, uint32_t(bytes), maxv};
        uint32_t words = (pc.count * bytes + 3) / 4;
        push(p_pack, ds_pack, &pc, sizeof(pc));
        dispatch1d((words + 255) / 256);
    }
    barrier();
    VkBufferCopy cp{0, 0, raw_bytes};
    vkCmdCopyBuffer(cmd, raw_out.buf, down[r].buf, 1, &cp);
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
        const Plane& p = planes[i];
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
    std::array<int, 2> slot_frame{-1, -1};  // source frame held by each slot
    int pair = -1;                          // source index whose pair has vectors
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
    if (ar == arInitial) {
        api->requestFrameFilter(i, d->node, ctx);
        if (between) api->requestFrameFilter(i + 1, d->node, ctx);
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
    int err = 0;
    bool scene = api->mapGetInt(api->getFramePropertiesRO(fa), "_SceneChangeNext", 0, &err) == 1 && !err;
    if (!between || scene) {
        VSFrame* f = api->copyFrame(fa, core);
        api->freeFrame(fa);
        return finish(f);
    }
    const VSFrame* fc = api->getFrameFilter(i + 1, d->node, ctx);
    VSFrame* dst = api->newVideoFrame(&d->vi.format, d->vi.width, d->vi.height, fa, core);
    try {
        int r;
        {
            std::lock_guard<std::mutex> g(d->lock);
            // slots alternate by source index parity; each source frame is loaded once
            int sa = i % 2, sc = (i + 1) % 2;
            if (d->slot_frame[sa] != i) { d->gpu->load(sa, fa, api); d->slot_frame[sa] = i; }
            if (d->slot_frame[sc] != i + 1) { d->gpu->load(sc, fc, api); d->slot_frame[sc] = i + 1; }
            bool fresh = d->pair != i;
            r = d->gpu->render(sa, float(double(fn) / double(fd)), fresh);
            d->pair = i;
        }
        d->gpu->deliver(r, dst, api);  // outside the GPU lock: copies overlap
    } catch (const std::exception& e) {
        {
            // a slot may hold half an upload and the vectors half a search: forget
            // both, so the next request loads and searches again
            std::lock_guard<std::mutex> g(d->lock);
            d->pair = -1;
            d->slot_frame = {-1, -1};
        }
        api->freeFrame(fa);
        api->freeFrame(fc);
        api->freeFrame(dst);
        api->setFilterError((std::string("mvgpu: ") + e.what()).c_str(), ctx);
        return nullptr;
    }
    api->freeFrame(fa);
    api->freeFrame(fc);
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
