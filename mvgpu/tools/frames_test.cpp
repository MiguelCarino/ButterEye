// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// frames-test: the present layer's motion-compensated path (layer/mc.h) without a window.
//
//   frames-test [options] FRAME0.ppm FRAME1.ppm [FRAME2.ppm ...]
//
// The frames go through the layer's own code in order, as the app's images of consecutive
// presents would: each is packed into an image of the swapchain format, copied into the
// history, ingested, and from the second frame on the k-1 in-between frames of the pair
// (frame n-1, frame n) are made, copied into images of that format and read back. They are
// written as 16-bit PPMs, PREFIX-<n-1>-<m>.ppm (frame m of k, t = m/k), with the format's
// channel values as they are (maxval 255 or 1023).
//
// Options:
//   --format NAME   the swapchain format (default A2B10G10R10_UNORM_PACK32; also
//                   A2R10G10B10_UNORM_PACK32, B8G8R8A8_UNORM, R8G8B8A8_UNORM)
//   --pq            the images are HDR10 (PQ, BT.2020): luma as the layer takes it there
//   --k K           in-between frames per pair: K-1 (default 2, t = 1/2)
//   --rect X,Y,W,H  the video rectangle (default: the whole image)
//   --src-h H       the video's height (the engine's scale; default: the rectangle's, at
//                   most 1080)
//   --mode mc|blend what the layer makes (default mc)
//   --out PREFIX    where the frames go (default: not written)
//   --repeat N      run the sequence N times (for timings; frames are written once)
//   --times         print the GPU time of each pair's work, per stage
// The engine's MVGPU_* settings and BUTTEREYE_FRAMES_STILL / _CUT apply as in the layer.
// The input PPMs are P6 with maxval 255 or more (16-bit); all of one size.
#include "blend.h"
#include "mc.h"

#include <cinttypes>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

extern "C" VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL vkGetInstanceProcAddr(VkInstance instance, const char* name);

namespace {

using bef::Blend;
using bef::Device;
using bef::Instance;
using bef::Mc;

struct Image {
    int w = 0, h = 0;
    std::vector<uint16_t> rgb;  // 3 per pixel
    int maxval = 255;
};

bool read_ppm(const char* path, Image& img) {
    FILE* f = std::fopen(path, "rb");
    if (!f) return false;
    char magic[3] = {};
    bool ok = std::fscanf(f, "%2s %d %d %d", magic, &img.w, &img.h, &img.maxval) == 4 &&
              std::strcmp(magic, "P6") == 0 && img.w > 0 && img.h > 0 && img.maxval > 0 && img.maxval < 65536;
    if (ok) {
        std::fgetc(f);
        const size_t n = static_cast<size_t>(img.w) * img.h * 3;
        img.rgb.resize(n);
        if (img.maxval < 256) {
            std::vector<uint8_t> b(n);
            ok = std::fread(b.data(), 1, n, f) == n;
            for (size_t i = 0; i < n; ++i) img.rgb[i] = b[i];
        } else {
            std::vector<uint8_t> b(n * 2);
            ok = std::fread(b.data(), 1, n * 2, f) == n * 2;
            for (size_t i = 0; i < n; ++i) img.rgb[i] = static_cast<uint16_t>(b[2 * i] << 8 | b[2 * i + 1]);
        }
    }
    std::fclose(f);
    return ok;
}

bool write_ppm(const std::string& path, int w, int h, int maxval, const std::vector<uint16_t>& rgb) {
    FILE* f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    std::fprintf(f, "P6\n%d %d\n%d\n", w, h, maxval);
    std::vector<uint8_t> b(rgb.size() * 2);
    for (size_t i = 0; i < rgb.size(); ++i) {
        b[2 * i] = static_cast<uint8_t>(rgb[i] >> 8);
        b[2 * i + 1] = static_cast<uint8_t>(rgb[i] & 0xff);
    }
    const bool ok = std::fwrite(b.data(), 1, b.size(), f) == b.size();
    return std::fclose(f) == 0 && ok;
}

int die(const char* what) {
    std::fprintf(stderr, "frames-test: %s\n", what);
    return 1;
}

#define CHECK(x)                                                                        \
    do {                                                                                \
        VkResult r_ = (x);                                                              \
        if (r_ != VK_SUCCESS) {                                                         \
            std::fprintf(stderr, "frames-test: %s failed (%d)\n", #x, static_cast<int>(r_)); \
            return 1;                                                                   \
        }                                                                               \
    } while (0)

int memory_type(const VkPhysicalDeviceMemoryProperties& mp, uint32_t bits, VkMemoryPropertyFlags want) {
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
        if ((bits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) return static_cast<int>(i);
    return -1;
}

void image_barrier(const bef::DeviceTable& t, VkCommandBuffer cb, VkImage img, VkImageLayout from, VkImageLayout to,
                   VkAccessFlags src, VkAccessFlags dst) {
    VkImageMemoryBarrier m{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    m.srcAccessMask = src;
    m.dstAccessMask = dst;
    m.oldLayout = from;
    m.newLayout = to;
    m.srcQueueFamilyIndex = m.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    m.image = img;
    m.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 0, nullptr, 0,
                         nullptr, 1, &m);
}

}  // namespace

// layer.cpp's format names, for --format (frames-test does not link layer.cpp)
namespace bef {
VkFormat format_by_name(const std::string& name) {
    static const struct {
        const char* n;
        VkFormat f;
    } names[] = {{"A2B10G10R10_UNORM_PACK32", VK_FORMAT_A2B10G10R10_UNORM_PACK32},
                 {"A2R10G10B10_UNORM_PACK32", VK_FORMAT_A2R10G10B10_UNORM_PACK32},
                 {"B8G8R8A8_UNORM", VK_FORMAT_B8G8R8A8_UNORM},
                 {"R8G8B8A8_UNORM", VK_FORMAT_R8G8B8A8_UNORM},
                 {"B8G8R8A8_SRGB", VK_FORMAT_B8G8R8A8_SRGB},
                 {"R8G8B8A8_SRGB", VK_FORMAT_R8G8B8A8_SRGB}};
    for (const auto& x : names)
        if (name == x.n) return x.f;
    return VK_FORMAT_UNDEFINED;
}
}  // namespace bef

int main(int argc, char** argv) {
    std::string format_name = "A2B10G10R10_UNORM_PACK32", out_prefix, mode = "mc";
    bool pq = false, times = false;
    int k = 2, repeat = 1;
    bef::McSettings set = bef::mc_settings_from_env();
    std::vector<const char*> inputs;
    for (int i = 1; i < argc; ++i) {
        std::string a = argv[i];
        auto next = [&]() -> const char* { return i + 1 < argc ? argv[++i] : ""; };
        if (a == "--format") format_name = next();
        else if (a == "--pq") pq = true;
        else if (a == "--k") k = std::atoi(next());
        else if (a == "--rect") std::sscanf(next(), "%d,%d,%d,%d", &set.rect.x, &set.rect.y, &set.rect.w, &set.rect.h);
        else if (a == "--src-h") set.source_h = std::atoi(next());
        else if (a == "--mode") mode = next();
        else if (a == "--out") out_prefix = next();
        else if (a == "--repeat") repeat = std::atoi(next());
        else if (a == "--times") times = true;
        else if (a.rfind("--", 0) == 0) return die(("unknown option " + a).c_str());
        else inputs.push_back(argv[i]);
    }
    set.pq = pq;
    if (inputs.size() < 2) return die("give at least two frames (see the top of frames_test.cpp)");
    if (k < 2 || k > bef::kKLimit) return die("--k must be 2 to 4");
    if (mode != "mc" && mode != "blend") return die("--mode is mc or blend");
    const VkFormat fmt = bef::format_by_name(format_name);
    bef::FormatLayout fl;
    if (fmt == VK_FORMAT_UNDEFINED || !bef::format_layout(fmt, &fl)) return die("unknown --format");
    std::vector<Image> frames(inputs.size());
    for (size_t i = 0; i < inputs.size(); ++i) {
        if (!read_ppm(inputs[i], frames[i])) return die((std::string("cannot read ") + inputs[i]).c_str());
        if (frames[i].w != frames[0].w || frames[i].h != frames[0].h) return die("the frames differ in size");
    }
    const uint32_t W = static_cast<uint32_t>(frames[0].w), H = static_cast<uint32_t>(frames[0].h);

    // instance and device through the loader; the layer's tables are filled from them
    Instance inst;
    inst.t.GetInstanceProcAddr = vkGetInstanceProcAddr;
    auto create_instance =
        reinterpret_cast<PFN_vkCreateInstance>(vkGetInstanceProcAddr(VK_NULL_HANDLE, "vkCreateInstance"));
    VkApplicationInfo app{VK_STRUCTURE_TYPE_APPLICATION_INFO, nullptr, "frames-test", 1, nullptr, 0,
                          VK_API_VERSION_1_1};
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, nullptr, 0, &app, 0, nullptr, 0, nullptr};
    CHECK(create_instance(&ici, nullptr, &inst.handle));
#define BEF_X(n) inst.t.n = reinterpret_cast<PFN_vk##n>(vkGetInstanceProcAddr(inst.handle, "vk" #n));
    BEF_INSTANCE_FNS(BEF_X)
#undef BEF_X
    auto enum_pd = reinterpret_cast<PFN_vkEnumeratePhysicalDevices>(
        vkGetInstanceProcAddr(inst.handle, "vkEnumeratePhysicalDevices"));
    uint32_t npd = 0;
    CHECK(enum_pd(inst.handle, &npd, nullptr));
    if (!npd) return die("no Vulkan device");
    std::vector<VkPhysicalDevice> pds(npd);
    CHECK(enum_pd(inst.handle, &npd, pds.data()));
    Device dev;
    dev.instance = &inst;
    dev.physical = pds[0];
    const char* want = std::getenv("FRAMES_TEST_DEVICE");
    VkPhysicalDeviceProperties props{};
    for (VkPhysicalDevice pd : pds) {
        inst.t.GetPhysicalDeviceProperties(pd, &props);
        if (!want || std::strstr(props.deviceName, want)) {
            dev.physical = pd;
            break;
        }
    }
    inst.t.GetPhysicalDeviceProperties(dev.physical, &props);
    uint32_t nq = 0;
    inst.t.GetPhysicalDeviceQueueFamilyProperties(dev.physical, &nq, nullptr);
    std::vector<VkQueueFamilyProperties> qf(nq);
    inst.t.GetPhysicalDeviceQueueFamilyProperties(dev.physical, &nq, qf.data());
    uint32_t family = UINT32_MAX;
    for (uint32_t i = 0; i < nq && family == UINT32_MAX; ++i)
        if (qf[i].queueFlags & VK_QUEUE_COMPUTE_BIT) family = i;
    if (family == UINT32_MAX) return die("no compute queue");
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, nullptr, 0, family, 1, &prio};
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, nullptr, 0, 1, &qci, 0, nullptr, 0, nullptr,
                           nullptr};
    auto create_device =
        reinterpret_cast<PFN_vkCreateDevice>(vkGetInstanceProcAddr(inst.handle, "vkCreateDevice"));
    CHECK(create_device(dev.physical, &dci, nullptr, &dev.handle));
    dev.t.GetDeviceProcAddr =
        reinterpret_cast<PFN_vkGetDeviceProcAddr>(vkGetInstanceProcAddr(inst.handle, "vkGetDeviceProcAddr"));
#define BEF_X(n) dev.t.n = reinterpret_cast<PFN_vk##n>(dev.t.GetDeviceProcAddr(dev.handle, "vk" #n));
    BEF_DEVICE_FNS(BEF_X)
#undef BEF_X
    auto copy_b2i = reinterpret_cast<PFN_vkCmdCopyBufferToImage>(
        dev.t.GetDeviceProcAddr(dev.handle, "vkCmdCopyBufferToImage"));
    auto device_idle = reinterpret_cast<PFN_vkDeviceWaitIdle>(dev.t.GetDeviceProcAddr(dev.handle, "vkDeviceWaitIdle"));
    auto destroy_device = reinterpret_cast<PFN_vkDestroyDevice>(dev.t.GetDeviceProcAddr(dev.handle, "vkDestroyDevice"));
    inst.t.GetPhysicalDeviceMemoryProperties(dev.physical, &dev.memory);
    dev.timestamp_period = props.limits.timestampPeriod;
    for (auto& q : qf) dev.timestamp_bits.push_back(q.timestampValidBits);
    const bef::DeviceTable& t = dev.t;
    VkDevice d = dev.handle;
    VkQueue queue = VK_NULL_HANDLE;
    t.GetDeviceQueue(d, family, 0, &queue);

    // the app's image, the targets (k - 1) and one host buffer for the upload and the readback
    const uint32_t got = static_cast<uint32_t>(k - 1);
    std::vector<VkImage> imgs(1 + got);
    std::vector<VkDeviceMemory> mems(1 + got);
    for (uint32_t i = 0; i <= got; ++i) {
        VkImageCreateInfo ci{VK_STRUCTURE_TYPE_IMAGE_CREATE_INFO};
        ci.imageType = VK_IMAGE_TYPE_2D;
        ci.format = fmt;
        ci.extent = {W, H, 1};
        ci.mipLevels = ci.arrayLayers = 1;
        ci.samples = VK_SAMPLE_COUNT_1_BIT;
        ci.tiling = VK_IMAGE_TILING_OPTIMAL;
        ci.usage = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        CHECK(t.CreateImage(d, &ci, nullptr, &imgs[i]));
        VkMemoryRequirements req;
        t.GetImageMemoryRequirements(d, imgs[i], &req);
        const int type = memory_type(dev.memory, req.memoryTypeBits, VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT);
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, nullptr, req.size,
                                 static_cast<uint32_t>(type < 0 ? 0 : type)};
        CHECK(t.AllocateMemory(d, &mai, nullptr, &mems[i]));
        CHECK(t.BindImageMemory(d, imgs[i], mems[i], 0));
    }
    const VkDeviceSize plane = static_cast<VkDeviceSize>(W) * H * 4;
    VkBuffer host = VK_NULL_HANDLE;
    VkDeviceMemory host_mem = VK_NULL_HANDLE;
    {
        VkBufferCreateInfo bi{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, nullptr, 0, plane * (1 + got),
                              VK_BUFFER_USAGE_TRANSFER_SRC_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT,
                              VK_SHARING_MODE_EXCLUSIVE, 0, nullptr};
        CHECK(t.CreateBuffer(d, &bi, nullptr, &host));
        VkMemoryRequirements req;
        t.GetBufferMemoryRequirements(d, host, &req);
        const int type = memory_type(dev.memory, req.memoryTypeBits,
                                     VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        if (type < 0) return die("no host-visible memory");
        VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, nullptr, req.size, static_cast<uint32_t>(type)};
        CHECK(t.AllocateMemory(d, &mai, nullptr, &host_mem));
        CHECK(t.BindBufferMemory(d, host, host_mem, 0));
    }
    void* map = nullptr;
    CHECK(t.MapMemory(d, host_mem, 0, VK_WHOLE_SIZE, 0, &map));
    auto* words = static_cast<uint32_t*>(map);

    VkCommandPool pool = VK_NULL_HANDLE;
    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr,
                                VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, family};
    CHECK(t.CreateCommandPool(d, &pci, nullptr, &pool));
    VkCommandBuffer cb = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, pool,
                                    VK_COMMAND_BUFFER_LEVEL_PRIMARY, 1};
    CHECK(t.AllocateCommandBuffers(d, &cai, &cb));
    VkFence fence = VK_NULL_HANDLE;
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, nullptr, 0};
    CHECK(t.CreateFence(d, &fi, nullptr, &fence));

    Blend blend;
    std::string why;
    if (!bef::blend_create(blend, &dev, d, {W, H}, fmt, got, family, 1, &why)) return die(why.c_str());
    Mc mc;
    if (mode == "mc" && !bef::mc_create(mc, blend, set, family, 1, &why)) return die(why.c_str());
    if (mode == "mc")
        std::fprintf(stderr, "frames-test: %s, %ux%u %s%s, engine %dx%d over %d,%d %dx%d, k %d\n", props.deviceName,
                     W, H, format_name.c_str(), pq ? " PQ" : "", mc.sw, mc.sh, mc.rect.x, mc.rect.y, mc.rect.w,
                     mc.rect.h, k);

    const uint32_t mask = (1u << fl.bits) - 1u, abits = 32u - 3u * fl.bits;
    int status = 0;
    for (int rep = 0; rep < repeat && !status; ++rep) {
        bef::blend_forget(blend);
        for (size_t n = 0; n < frames.size(); ++n) {
            const Image& f = frames[n];
            for (uint32_t p = 0; p < W * H; ++p) {
                uint32_t c[3];
                for (int ch = 0; ch < 3; ++ch)
                    c[ch] = static_cast<uint32_t>(std::lround(static_cast<double>(f.rgb[3 * p + ch]) * mask /
                                                              f.maxval));
                const uint32_t lo = fl.red_low ? c[0] : c[2], hi = fl.red_low ? c[2] : c[0];
                words[p] = lo | c[1] << fl.bits | hi << (2 * fl.bits) | ((1u << abits) - 1u) << (3 * fl.bits);
            }
            VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                        VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
            CHECK(t.ResetCommandBuffer(cb, 0));
            CHECK(t.BeginCommandBuffer(cb, &bi));
            image_barrier(t, cb, imgs[0], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                          VK_ACCESS_TRANSFER_WRITE_BIT);
            VkBufferImageCopy r{};
            r.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
            r.imageExtent = {W, H, 1};
            copy_b2i(cb, host, imgs[0], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &r);
            image_barrier(t, cb, imgs[0], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL,
                          VK_ACCESS_TRANSFER_WRITE_BIT, VK_ACCESS_TRANSFER_READ_BIT);
            for (uint32_t m = 0; m < got; ++m)
                image_barrier(t, cb, imgs[1 + m], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                              VK_ACCESS_TRANSFER_WRITE_BIT);
            const bool made = mode == "mc" ? bef::mc_record(mc, blend, cb, imgs[0], &imgs[1], got,
                                                            static_cast<uint32_t>(k), false, 0)
                                           : bef::blend_record(blend, cb, imgs[0], &imgs[1], got,
                                                               static_cast<uint32_t>(k), false, 0);
            for (uint32_t m = 0; m < got; ++m) {
                image_barrier(t, cb, imgs[1 + m], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL,
                              VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_ACCESS_TRANSFER_WRITE_BIT,
                              VK_ACCESS_TRANSFER_READ_BIT);
                r.bufferOffset = plane * (1 + m);
                t.CmdCopyImageToBuffer(cb, imgs[1 + m], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, host, 1, &r);
            }
            VkMemoryBarrier hb{VK_STRUCTURE_TYPE_MEMORY_BARRIER, nullptr, VK_ACCESS_TRANSFER_WRITE_BIT,
                               VK_ACCESS_HOST_READ_BIT};
            t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &hb, 0,
                                 nullptr, 0, nullptr);
            CHECK(t.EndCommandBuffer(cb));
            VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 0, nullptr, nullptr, 1, &cb, 0, nullptr};
            CHECK(t.QueueSubmit(queue, 1, &si, fence));
            CHECK(t.WaitForFences(d, 1, &fence, VK_TRUE, 60000000000ull));
            CHECK(t.ResetFences(d, 1, &fence));
            if (times && made && mode == "mc") {
                bef::McTimes x;
                if (bef::mc_times(mc, 0, false, &x))
                    std::printf("times pair %zu: copy in %.3f ingest %.3f vectors+cut %.3f frames %.3f copies out %.3f"
                                " total %.3f ms\n",
                                n - 1, x.copy_in / 1e6, x.ingest / 1e6, x.vectors / 1e6, x.frames / 1e6, x.out / 1e6,
                                x.total / 1e6);
            }
            if (made && rep == 0 && !out_prefix.empty()) {
                for (uint32_t m = 0; m < got; ++m) {
                    std::vector<uint16_t> rgb(static_cast<size_t>(W) * H * 3);
                    const uint32_t* src = words + plane / 4 * (1 + m);
                    for (uint32_t p = 0; p < W * H; ++p) {
                        const uint32_t v = src[p];
                        const uint16_t lo = static_cast<uint16_t>(v & mask);
                        const uint16_t mid = static_cast<uint16_t>(v >> fl.bits & mask);
                        const uint16_t hi = static_cast<uint16_t>(v >> (2 * fl.bits) & mask);
                        rgb[3 * p] = fl.red_low ? lo : hi;
                        rgb[3 * p + 1] = mid;
                        rgb[3 * p + 2] = fl.red_low ? hi : lo;
                    }
                    const std::string path =
                        out_prefix + "-" + std::to_string(n - 1) + "-" + std::to_string(m + 1) + ".ppm";
                    if (!write_ppm(path, static_cast<int>(W), static_cast<int>(H), static_cast<int>(mask), rgb)) {
                        std::fprintf(stderr, "frames-test: cannot write %s\n", path.c_str());
                        status = 1;
                    }
                }
            }
        }
    }
    device_idle(d);
    if (mode == "mc") bef::mc_destroy(mc);
    bef::blend_destroy(blend);
    t.DestroyFence(d, fence, nullptr);
    t.DestroyCommandPool(d, pool, nullptr);
    t.UnmapMemory(d, host_mem);
    t.DestroyBuffer(d, host, nullptr);
    t.FreeMemory(d, host_mem, nullptr);
    for (uint32_t i = 0; i <= got; ++i) {
        t.DestroyImage(d, imgs[i], nullptr);
        t.FreeMemory(d, mems[i], nullptr);
    }
    destroy_device(d, nullptr);
    reinterpret_cast<PFN_vkDestroyInstance>(vkGetInstanceProcAddr(inst.handle, "vkDestroyInstance"))(inst.handle,
                                                                                                       nullptr);
    return status;
}
