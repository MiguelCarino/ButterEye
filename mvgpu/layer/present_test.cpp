// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// frames-present-test: a small Vulkan program that presents N cleared frames to a
// headless surface (VK_EXT_headless_surface), so the layer's instance, device,
// swapchain and present paths can be tested without a window or a GPU (lavapipe).
//
// usage: frames-present-test [frames] [--wayland-ext] [--pause-at N:MS] [--event-at N]
//                            [--format NAME] [--colour] [--list-formats] [--rect-at N:X,Y,W,H]
//                            [--present-id] [--recreate-at N]
//   --wayland-ext  also enables VK_KHR_wayland_surface (no Wayland surface is made),
//                  so the layer treats this instance like mpv's
//   --pause-at     sleeps MS milliseconds before frame N (a break in the cadence); may be given
//                  more than once
//   --event-at     before frame N, bumps the event sequence of the layer's control
//                  block at $BUTTEREYE_FRAMES_SHM (as ButterEye does on a seek)
//   --format       makes the swapchain in this surface format (e.g. B8G8R8A8_UNORM, number
//                  or name); exits 3 when the surface does not offer it
//   --colour       clears frame f to (r, g, b) = (v, 0.25, 1 - v), v = (f % 16) / 15, so that
//                  red and blue differ (the default clears to grey v)
//   --list-formats prints the surface formats ("format <number> <colour space>") and exits
//   --rect-at      before frame N, writes the video rectangle X,Y WxH into the control block
//                  (as ButterEye does once mpv reports the video's place)
//   --present-id   chains the app's own VkPresentIdKHR (ids 1, 2, ...) to every present
//   --recreate-at  before frame N, recreates the swapchain with the old one as oldSwapchain
//                  (as on a resize), then destroys the old one
// Prints "presented <n> frames" and "format <number>" and exits 0 on success.
#include <vulkan/vulkan.h>

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define CHECK(x)                                                              \
    do {                                                                      \
        VkResult r_ = (x);                                                    \
        if (r_ != VK_SUCCESS) {                                               \
            std::fprintf(stderr, "frames-present-test: %s -> %d\n", #x, r_); \
            return 1;                                                         \
        }                                                                     \
    } while (0)

int main(int argc, char** argv) {
    int frames = 60;
    bool wayland = false;
    int event_at = -1;
    std::vector<std::pair<int, int>> pauses;  // (frame, ms)
    bool colour = false, list = false, app_ids = false;
    int rect_at = -1, recreate_at = -1;
    int rect[4] = {0, 0, 0, 0};
    const char* want_format = nullptr;
    for (int i = 1; i < argc; ++i) {
        if (!std::strcmp(argv[i], "--wayland-ext")) wayland = true;
        else if (!std::strcmp(argv[i], "--pause-at") && i + 1 < argc) {
            int at = -1, ms = 0;
            if (std::sscanf(argv[++i], "%d:%d", &at, &ms) == 2) pauses.emplace_back(at, ms);
        }
        else if (!std::strcmp(argv[i], "--event-at") && i + 1 < argc)
            event_at = std::atoi(argv[++i]);
        else if (!std::strcmp(argv[i], "--format") && i + 1 < argc)
            want_format = argv[++i];
        else if (!std::strcmp(argv[i], "--colour")) colour = true;
        else if (!std::strcmp(argv[i], "--list-formats")) list = true;
        else if (!std::strcmp(argv[i], "--rect-at") && i + 1 < argc)
            std::sscanf(argv[++i], "%d:%d,%d,%d,%d", &rect_at, &rect[0], &rect[1], &rect[2], &rect[3]);
        else if (!std::strcmp(argv[i], "--present-id")) app_ids = true;
        else if (!std::strcmp(argv[i], "--recreate-at") && i + 1 < argc)
            recreate_at = std::atoi(argv[++i]);
        else frames = std::atoi(argv[i]);
    }

    std::vector<const char*> iexts = {"VK_KHR_surface", "VK_EXT_headless_surface"};
    if (wayland) iexts.push_back("VK_KHR_wayland_surface");
    VkApplicationInfo ai{VK_STRUCTURE_TYPE_APPLICATION_INFO, nullptr, "frames-present-test", 1,
                         "frames-present-test", 1, VK_API_VERSION_1_3};
    VkInstanceCreateInfo ici{VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, nullptr, 0, &ai, 0, nullptr,
                             static_cast<uint32_t>(iexts.size()), iexts.data()};
    VkInstance inst;
    CHECK(vkCreateInstance(&ici, nullptr, &inst));

    uint32_t npd = 1;
    VkPhysicalDevice pd;
    VkResult er = vkEnumeratePhysicalDevices(inst, &npd, &pd);
    if ((er != VK_SUCCESS && er != VK_INCOMPLETE) || npd == 0) {
        std::fprintf(stderr, "frames-present-test: no physical device\n");
        return 1;
    }

    auto create_headless = reinterpret_cast<PFN_vkCreateHeadlessSurfaceEXT>(
        vkGetInstanceProcAddr(inst, "vkCreateHeadlessSurfaceEXT"));
    if (!create_headless) {
        std::fprintf(stderr, "frames-present-test: no VK_EXT_headless_surface\n");
        return 1;
    }
    VkHeadlessSurfaceCreateInfoEXT hci{VK_STRUCTURE_TYPE_HEADLESS_SURFACE_CREATE_INFO_EXT, nullptr, 0};
    VkSurfaceKHR surface;
    CHECK(create_headless(inst, &hci, nullptr, &surface));

    uint32_t nq = 0;
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &nq, nullptr);
    std::vector<VkQueueFamilyProperties> qf(nq);
    vkGetPhysicalDeviceQueueFamilyProperties(pd, &nq, qf.data());
    uint32_t family = UINT32_MAX;
    for (uint32_t i = 0; i < nq && family == UINT32_MAX; ++i) {
        VkBool32 present = VK_FALSE;
        vkGetPhysicalDeviceSurfaceSupportKHR(pd, i, surface, &present);
        if (present && (qf[i].queueFlags & VK_QUEUE_GRAPHICS_BIT)) family = i;
    }
    if (family == UINT32_MAX) {
        std::fprintf(stderr, "frames-present-test: no queue that can present\n");
        return 1;
    }
    float prio = 1.0f;
    VkDeviceQueueCreateInfo qci{VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, nullptr, 0, family, 1, &prio};
    const char* dexts[] = {"VK_KHR_swapchain"};
    VkDeviceCreateInfo dci{VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, nullptr, 0, 1, &qci, 0, nullptr, 1, dexts,
                           nullptr};
    VkDevice dev;
    CHECK(vkCreateDevice(pd, &dci, nullptr, &dev));
    VkQueue queue;
    vkGetDeviceQueue(dev, family, 0, &queue);

    VkSurfaceCapabilitiesKHR caps;
    CHECK(vkGetPhysicalDeviceSurfaceCapabilitiesKHR(pd, surface, &caps));
    uint32_t nf = 0;
    vkGetPhysicalDeviceSurfaceFormatsKHR(pd, surface, &nf, nullptr);
    std::vector<VkSurfaceFormatKHR> fmts(nf);
    vkGetPhysicalDeviceSurfaceFormatsKHR(pd, surface, &nf, fmts.data());
    if (nf == 0) return 1;
    if (list) {
        for (auto& f : fmts) std::printf("format %d %d\n", f.format, f.colorSpace);
        return 0;
    }
    uint32_t pick = 0;
    if (want_format) {
        static const struct {
            const char* name;
            VkFormat f;
        } names[] = {{"B8G8R8A8_UNORM", VK_FORMAT_B8G8R8A8_UNORM},
                     {"B8G8R8A8_SRGB", VK_FORMAT_B8G8R8A8_SRGB},
                     {"R8G8B8A8_UNORM", VK_FORMAT_R8G8B8A8_UNORM},
                     {"R8G8B8A8_SRGB", VK_FORMAT_R8G8B8A8_SRGB},
                     {"A2R10G10B10_UNORM", VK_FORMAT_A2R10G10B10_UNORM_PACK32},
                     {"A2B10G10R10_UNORM", VK_FORMAT_A2B10G10R10_UNORM_PACK32},
                     {"R16G16B16A16_SFLOAT", VK_FORMAT_R16G16B16A16_SFLOAT}};
        VkFormat f = static_cast<VkFormat>(std::atoi(want_format));
        for (auto& n : names)
            if (!std::strcmp(n.name, want_format)) f = n.f;
        pick = nf;
        for (uint32_t i = 0; i < nf && pick == nf; ++i)
            if (fmts[i].format == f) pick = i;
        if (pick == nf) {
            std::fprintf(stderr, "frames-present-test: the surface does not offer format %s\n", want_format);
            return 3;
        }
    }
    VkExtent2D extent = caps.currentExtent;
    if (extent.width == UINT32_MAX) extent = {64, 64};
    uint32_t want = caps.minImageCount < 3 ? 3 : caps.minImageCount;
    if (caps.maxImageCount && want > caps.maxImageCount) want = caps.maxImageCount;
    VkSwapchainCreateInfoKHR sci{VK_STRUCTURE_TYPE_SWAPCHAIN_CREATE_INFO_KHR};
    sci.surface = surface;
    sci.minImageCount = want;
    sci.imageFormat = fmts[pick].format;
    sci.imageColorSpace = fmts[pick].colorSpace;
    sci.imageExtent = extent;
    sci.imageArrayLayers = 1;
    sci.imageUsage = VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
    sci.imageSharingMode = VK_SHARING_MODE_EXCLUSIVE;
    sci.preTransform = caps.currentTransform;
    sci.compositeAlpha = VK_COMPOSITE_ALPHA_OPAQUE_BIT_KHR;
    sci.presentMode = VK_PRESENT_MODE_FIFO_KHR;
    sci.clipped = VK_TRUE;
    VkSwapchainKHR sc;
    CHECK(vkCreateSwapchainKHR(dev, &sci, nullptr, &sc));
    uint32_t ni = 0;
    vkGetSwapchainImagesKHR(dev, sc, &ni, nullptr);
    std::vector<VkImage> images(ni);
    vkGetSwapchainImagesKHR(dev, sc, &ni, images.data());

    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr,
                                VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, family};
    VkCommandPool pool;
    CHECK(vkCreateCommandPool(dev, &pci, nullptr, &pool));
    const uint32_t inflight = 2;
    std::vector<VkCommandBuffer> cmds(inflight);
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, pool,
                                    VK_COMMAND_BUFFER_LEVEL_PRIMARY, inflight};
    CHECK(vkAllocateCommandBuffers(dev, &cai, cmds.data()));
    std::vector<VkSemaphore> acquired(inflight), rendered(ni);
    std::vector<VkFence> fences(inflight);
    VkSemaphoreCreateInfo semi{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO};
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, nullptr, VK_FENCE_CREATE_SIGNALED_BIT};
    for (uint32_t i = 0; i < inflight; ++i) {
        CHECK(vkCreateSemaphore(dev, &semi, nullptr, &acquired[i]));
        CHECK(vkCreateFence(dev, &fi, nullptr, &fences[i]));
    }
    for (uint32_t i = 0; i < ni; ++i) CHECK(vkCreateSemaphore(dev, &semi, nullptr, &rendered[i]));

    int presented = 0;
    for (int f = 0; f < frames; ++f) {
        for (const auto& [at, ms] : pauses)
            if (f == at) usleep(static_cast<useconds_t>(ms) * 1000);
        if (f == recreate_at) {
            CHECK(vkDeviceWaitIdle(dev));
            sci.oldSwapchain = sc;
            VkSwapchainKHR sc2;
            CHECK(vkCreateSwapchainKHR(dev, &sci, nullptr, &sc2));
            vkDestroySwapchainKHR(dev, sc, nullptr);
            sc = sc2;
            sci.oldSwapchain = VK_NULL_HANDLE;
            for (uint32_t i = 0; i < ni; ++i) vkDestroySemaphore(dev, rendered[i], nullptr);
            vkGetSwapchainImagesKHR(dev, sc, &ni, nullptr);
            images.resize(ni);
            vkGetSwapchainImagesKHR(dev, sc, &ni, images.data());
            rendered.resize(ni);
            for (uint32_t i = 0; i < ni; ++i) CHECK(vkCreateSemaphore(dev, &semi, nullptr, &rendered[i]));
        }
        if (f == event_at || f == rect_at) {
            const char* path = std::getenv("BUTTEREYE_FRAMES_SHM");
            int fd = path ? open(path, O_RDWR) : -1;
            void* m = fd >= 0 ? mmap(nullptr, 256, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0) : MAP_FAILED;
            if (fd >= 0) close(fd);
            if (m == MAP_FAILED) {
                std::fprintf(stderr, "frames-present-test: no control block\n");
                return 1;
            }
            auto* w = static_cast<uint32_t*>(m);
            __atomic_fetch_add(&w[3], 1u, __ATOMIC_ACQ_REL);  // seqlock: odd
            if (f == event_at) __atomic_fetch_add(&w[6], 1u, __ATOMIC_RELAXED);  // event sequence
            if (f == rect_at)
                for (int x = 0; x < 4; ++x)  // the video rectangle, offsets 28..40
                    __atomic_store_n(&w[7 + x], static_cast<uint32_t>(rect[x]), __ATOMIC_RELAXED);
            __atomic_fetch_add(&w[3], 1u, __ATOMIC_ACQ_REL);  // even again
            munmap(m, 256);
        }
        const uint32_t slot = static_cast<uint32_t>(f) % inflight;
        CHECK(vkWaitForFences(dev, 1, &fences[slot], VK_TRUE, UINT64_MAX));
        uint32_t idx;
        CHECK(vkAcquireNextImageKHR(dev, sc, UINT64_MAX, acquired[slot], VK_NULL_HANDLE, &idx));
        CHECK(vkResetFences(dev, 1, &fences[slot]));
        VkCommandBuffer cb = cmds[slot];
        CHECK(vkResetCommandBuffer(cb, 0));
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                    VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
        CHECK(vkBeginCommandBuffer(cb, &bi));
        VkImageSubresourceRange range{VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
        VkImageMemoryBarrier b{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
        b.srcAccessMask = 0;
        b.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        b.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
        b.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        b.image = images[idx];
        b.subresourceRange = range;
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &b);
        float v = static_cast<float>(f % 16) / 15.0f;
        VkClearColorValue clear{{v, v, v, 1.0f}};
        if (colour) clear = {{v, 0.25f, 1.0f - v, 1.0f}};
        vkCmdClearColorImage(cb, images[idx], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &clear, 1, &range);
        b.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        b.dstAccessMask = 0;
        b.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
        b.newLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
        vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0,
                             nullptr, 0, nullptr, 1, &b);
        CHECK(vkEndCommandBuffer(cb));
        VkPipelineStageFlags stage = VK_PIPELINE_STAGE_TRANSFER_BIT;
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 1, &acquired[slot], &stage, 1, &cb, 1,
                        &rendered[idx]};
        CHECK(vkQueueSubmit(queue, 1, &si, fences[slot]));
        const uint64_t id = static_cast<uint64_t>(f) + 1;
        VkPresentIdKHR pid{VK_STRUCTURE_TYPE_PRESENT_ID_KHR, nullptr, 1, &id};
        VkPresentInfoKHR pi{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, app_ids ? &pid : nullptr, 1, &rendered[idx], 1, &sc,
                            &idx, nullptr};
        VkResult pr = vkQueuePresentKHR(queue, &pi);
        if (pr != VK_SUCCESS && pr != VK_SUBOPTIMAL_KHR) {
            std::fprintf(stderr, "frames-present-test: vkQueuePresentKHR -> %d\n", pr);
            return 1;
        }
        ++presented;
    }
    CHECK(vkDeviceWaitIdle(dev));
    for (uint32_t i = 0; i < inflight; ++i) {
        vkDestroySemaphore(dev, acquired[i], nullptr);
        vkDestroyFence(dev, fences[i], nullptr);
    }
    for (uint32_t i = 0; i < ni; ++i) vkDestroySemaphore(dev, rendered[i], nullptr);
    vkDestroyCommandPool(dev, pool, nullptr);
    vkDestroySwapchainKHR(dev, sc, nullptr);
    vkDestroyDevice(dev, nullptr);
    vkDestroySurfaceKHR(inst, surface, nullptr);
    vkDestroyInstance(inst, nullptr);
    std::printf("presented %d frames\nformat %d\n", presented, sci.imageFormat);
    return 0;
}
