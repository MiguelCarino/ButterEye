// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames: shared state of the layer.
//
// Stage S1 made it a pass-through layer that also works as a present-timing probe:
// it finds mpv's instance and device, enables the present extensions (with
// BUTTEREYE_FRAMES_KMAX >= 2), logs what mpv asks for and times every present.
// Stage S2 adds the paced duplicate path: with k >= 2 every app present becomes k
// presents (k-1 copies of the app's image, then the image itself), paced by FIFO.
// Stage S3 adds the blend (BUTTEREYE_FRAMES_MODE=blend): the k-1 added presents show the
// app's previous and current images mixed at m/k, made by one compute dispatch (blend.h).
// Stage S4a builds mvgpu's engine (src/engine) into the layer, with its function table
// filled from the next layer; BUTTEREYE_FRAMES_ENGINE_TEST runs it once (engine_test.cpp).
// Stage S4c adds motion-compensated frames (BUTTEREYE_FRAMES_MODE=mc): the engine runs on
// the app's images inside the present's submit and the added presents show its frames (mc.h).
// The S4 follow-ups split that work into a submit for the pair and one per in-between frame,
// make the engine on a worker thread, count cuts, and can give the work a queue of its own
// (BUTTEREYE_FRAMES_QUEUE) and a global priority (BUTTEREYE_FRAMES_PRIORITY).
#pragma once

#define VK_NO_PROTOTYPES
// the negotiate entry point is the only symbol the layer exports; everything
// else is built with hidden visibility
#pragma GCC visibility push(default)
#include <vulkan/vulkan.h>
#include <vulkan/vk_layer.h>
#pragma GCC visibility pop

#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace bef {

// The next layer's entry points that the layer itself calls, per instance and per
// device. Everything the layer does not intercept goes straight to the next layer.
#define BEF_INSTANCE_FNS(X)                  \
    X(DestroyInstance)                       \
    X(EnumerateDeviceExtensionProperties)    \
    X(GetPhysicalDeviceProperties)           \
    X(GetPhysicalDeviceFeatures2)            \
    X(GetPhysicalDeviceFeatures2KHR)         \
    X(GetPhysicalDeviceMemoryProperties)     \
    X(GetPhysicalDeviceQueueFamilyProperties) \
    X(GetPhysicalDeviceFormatProperties)     \
    X(GetPhysicalDeviceSurfaceCapabilitiesKHR) \
    X(GetPhysicalDeviceSurfaceCapabilities2KHR) \
    X(GetPhysicalDeviceSurfaceFormatsKHR)    \
    X(GetPhysicalDeviceSurfaceFormats2KHR)

#define BEF_DEVICE_FNS(X)                  \
    X(DestroyDevice)                       \
    X(GetDeviceQueue)                      \
    X(GetDeviceQueue2)                     \
    X(CreateSwapchainKHR)                  \
    X(DestroySwapchainKHR)                 \
    X(GetSwapchainImagesKHR)               \
    X(AcquireNextImageKHR)                 \
    X(QueuePresentKHR)                     \
    X(QueueSubmit)                         \
    X(CreateSemaphore)                     \
    X(DestroySemaphore)                    \
    X(CreateFence)                         \
    X(DestroyFence)                        \
    X(WaitForFences)                       \
    X(ResetFences)                         \
    X(CreateCommandPool)                   \
    X(DestroyCommandPool)                  \
    X(AllocateCommandBuffers)              \
    X(FreeCommandBuffers)                  \
    X(ResetCommandBuffer)                  \
    X(BeginCommandBuffer)                  \
    X(EndCommandBuffer)                    \
    X(CmdPipelineBarrier)                  \
    X(CmdCopyImage)                        \
    X(SetSwapchainPresentTimingQueueSizeEXT) \
    X(GetSwapchainTimingPropertiesEXT)     \
    X(GetSwapchainTimeDomainPropertiesEXT) \
    X(GetPastPresentationTimingEXT)        \
    X(GetCalibratedTimestampsKHR)          \
    X(WaitForPresent2KHR)                  \
    /* S3: the blend (images, one compute pipeline, timestamps, the dump) */ \
    X(CreateImage)                         \
    X(DestroyImage)                        \
    X(GetImageMemoryRequirements)          \
    X(BindImageMemory)                     \
    X(CreateBuffer)                        \
    X(DestroyBuffer)                       \
    X(GetBufferMemoryRequirements)         \
    X(BindBufferMemory)                    \
    X(AllocateMemory)                      \
    X(FreeMemory)                          \
    X(MapMemory)                           \
    X(UnmapMemory)                         \
    X(CreateImageView)                     \
    X(DestroyImageView)                    \
    X(CreateShaderModule)                  \
    X(DestroyShaderModule)                 \
    X(CreateDescriptorSetLayout)           \
    X(DestroyDescriptorSetLayout)          \
    X(CreatePipelineLayout)                \
    X(DestroyPipelineLayout)               \
    X(CreateComputePipelines)              \
    X(DestroyPipeline)                     \
    X(CreateDescriptorPool)                \
    X(DestroyDescriptorPool)               \
    X(AllocateDescriptorSets)              \
    X(UpdateDescriptorSets)                \
    X(CmdBindPipeline)                     \
    X(CmdBindDescriptorSets)               \
    X(CmdPushConstants)                    \
    X(CmdDispatch)                         \
    X(CmdCopyImageToBuffer)                \
    X(CreateQueryPool)                     \
    X(DestroyQueryPool)                    \
    X(CmdResetQueryPool)                   \
    X(CmdWriteTimestamp)                   \
    X(GetQueryPoolResults)

struct InstanceTable {
    PFN_vkGetInstanceProcAddr GetInstanceProcAddr = nullptr;
#define BEF_X(n) PFN_vk##n n = nullptr;
    BEF_INSTANCE_FNS(BEF_X)
#undef BEF_X
};

struct DeviceTable {
    PFN_vkGetDeviceProcAddr GetDeviceProcAddr = nullptr;
#define BEF_X(n) PFN_vk##n n = nullptr;
    BEF_DEVICE_FNS(BEF_X)
#undef BEF_X
};

struct Instance {
    VkInstance handle = VK_NULL_HANDLE;
    InstanceTable t;
    // mpv's instance: it enabled VK_KHR_surface and VK_KHR_wayland_surface (mvgpu
    // and ncnn enable neither). Only devices of a marked instance are hooked.
    bool marked = false;
    // instance extensions enabled in the end (the app's plus any the layer added)
    bool surface_caps2 = false;
    bool surface_maint1 = false;
    uint32_t ordinal = 0;
};

struct Device {
    VkDevice handle = VK_NULL_HANDLE;
    VkPhysicalDevice physical = VK_NULL_HANDLE;
    Instance* instance = nullptr;
    DeviceTable t;
    PFN_vkSetDeviceLoaderData set_loader_data = nullptr;
    // swapchain and present hooks are active (device of a marked instance whose
    // creation did not need the retry)
    bool hooked = false;
    // extensions the layer added and the device accepted
    bool present_id2 = false;
    bool present_timing = false;
    bool present_wait2 = false;
    bool swapchain_maint1 = false;
    uint32_t ordinal = 0;
    // S3: for the layer's own images and timestamps
    VkPhysicalDeviceMemoryProperties memory{};
    float timestamp_period = 0.0f;               // ns per timestamp tick
    std::vector<uint32_t> timestamp_bits;        // per queue family (0: no timestamps)
    bool engine_tested = false;                  // BUTTEREYE_FRAMES_ENGINE_TEST ran on it
    // the layer's own queue for the mc work (BUTTEREYE_FRAMES_QUEUE), when the device got one
    VkQueue own_queue = VK_NULL_HANDLE;
    uint32_t own_family = UINT32_MAX;
    std::mutex own_lock;                         // the own queue is the layer's: it syncs its submits
    int priority = 0;                            // the global priority the device was made with (0: none)
};

constexpr int kModeDup = 0;    // the added presents are copies of the app's image
constexpr int kModeBlend = 1;  // they are blends of the app's previous and current image
constexpr int kModeMc = 2;     // they are motion-compensated in-between frames (mc.h)

// the largest k the layer handles (the swapchain image limit is 8 on the target)
constexpr int kKLimit = 4;

constexpr int kQueueApp = 0;       // the mc work goes on the app's present queue
constexpr int kQueueCompute = 1;   // on a queue of its own in a compute-only family (async compute)
constexpr int kQueueGraphics = 2;  // on a queue of its own in the graphics family

struct Config {
    int kmax = 0;                     // BUTTEREYE_FRAMES_KMAX; < 2: no injection, no change
    int k = 1;                        // BUTTEREYE_FRAMES=k|off: presents per app present
    std::string log_path;             // BUTTEREYE_FRAMES_LOG: per-present CSV
    bool quiet = false;               // BUTTEREYE_FRAMES_QUIET=1: nothing on stderr
    bool timing = true;               // BUTTEREYE_FRAMES_TIMING=0: no present-timing queries
    uint32_t acquire_us = 0;          // BUTTEREYE_FRAMES_ACQUIRE_US; 0: 2.5 refreshes
    int depth = 0;                    // BUTTEREYE_FRAMES_DEPTH: most presents left waiting for the
                                      // screen after the layer's k presents (0: no limit)
    int hold = -1;                    // BUTTEREYE_FRAMES_HOLD: after its k presents the layer returns
                                      // only once at most this many are left waiting (0: no wait,
                                      // -1: k - 1, so the app's image shows k - 1 refreshes late)
    int images = -1;                  // BUTTEREYE_FRAMES_IMAGES: images to add (-1: kmax); k <= images + 1
    std::string shm_path;             // BUTTEREYE_FRAMES_SHM, else $XDG_RUNTIME_DIR/buttereye/frames-<pid>
    int mode = kModeDup;              // BUTTEREYE_FRAMES_MODE=dup|blend|mc: what the added presents show
    bool show = false;                // BUTTEREYE_FRAMES_SHOW=1: a marker square on made frames
    std::string dump_path;            // BUTTEREYE_FRAMES_DUMP: raw images of one blended present
    int dump_at = 30;                 // BUTTEREYE_FRAMES_DUMP_AT: which blended present (1-based)
    // BUTTEREYE_FRAMES_TEST_FORMATS=A2R10G10B10_UNORM,...: a test knob that hides every other
    // surface format from the app, so the app picks one of these (format checks)
    std::vector<VkFormat> test_formats;
    // BUTTEREYE_FRAMES_ENGINE_TEST=1: run the motion engine once per device on the first
    // k path present and log what it gave (engine_test.cpp; a test knob)
    bool engine_test = false;
    // BUTTEREYE_FRAMES_TEST_CREATE_FAIL=1: a test knob. The layer's create of a swapchain that
    // replaces an old one (oldSwapchain set) is made to fail after the driver made it: the new
    // swapchain is destroyed and the create returns VK_ERROR_OUT_OF_DEVICE_MEMORY, so the old one
    // is retired as after a real failure, and the layer's retry runs
    bool test_create_fail = false;
    // S4 follow-ups
    bool split = true;                // BUTTEREYE_FRAMES_SPLIT=0: the mc work in one submit (as in S4c)
    bool mc_thread = true;            // BUTTEREYE_FRAMES_MC_THREAD=0: make the engine inside the present
    int queue = kQueueApp;            // BUTTEREYE_FRAMES_QUEUE=app|compute|graphics
    int priority = 0;                 // BUTTEREYE_FRAMES_PRIORITY=low|medium|high|realtime (0: leave alone):
                                      // the VkQueueGlobalPriorityKHR of the queue the mc work runs on
};

const Config& config();

// dispatch key: the loader's table pointer stored in every dispatchable handle
inline void* key(const void* handle) { return *static_cast<void* const*>(handle); }

Device* find_device(void* k);
uint64_t now_ns();
// S4a: the engine through the next layer's table, once (BUTTEREYE_FRAMES_ENGINE_TEST)
void engine_test(Device* d, VkQueue queue, uint32_t family);  // CLOCK_MONOTONIC, the clock mpv's own timing uses
void logf(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
// one CSV row of the per-present log (no-op without BUTTEREYE_FRAMES_LOG)
void log_row(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
void log_flush();
const char* stype_name(VkStructureType s);

// queue -> family, recorded by the GetDeviceQueue hooks (UINT32_MAX: unknown)
uint32_t queue_family(VkQueue queue);
VKAPI_ATTR void VKAPI_CALL GetDeviceQueue(VkDevice device, uint32_t family, uint32_t index,
                                          VkQueue* out);
VKAPI_ATTR void VKAPI_CALL GetDeviceQueue2(VkDevice device, const VkDeviceQueueInfo2* info,
                                           VkQueue* out);

// shm.cpp: the control block ButterEye shares with the layer (see shm.cpp)
struct ShmControl {
    bool present = false;  // the block exists and is mapped
    int k = 0;             // 0: keep the env value
    int mode = -1;         // -1: keep the env value; else kModeDup, kModeBlend or kModeMc
    uint32_t event_seq = 0;
    int rect_x = 0, rect_y = 0, rect_w = 0, rect_h = 0;  // the video rectangle (w 0: not given)
    int source_h = 0;                                    // the video's height (0: not given)
};
ShmControl shm_read();
struct ShmStats {
    uint64_t refresh_ns;
    uint64_t app_presents, frames_presents, passed, generated;
    uint64_t missed_refreshes, timed_intervals;
    uint64_t latency_ns_last;
    uint64_t blended;
    uint64_t mc_made;
    uint64_t mc_cuts;
    uint32_t k_active;
};
void shm_write_stats(const ShmStats& s);

// swapchain.cpp
VKAPI_ATTR VkResult VKAPI_CALL CreateSwapchainKHR(VkDevice device,
                                                  const VkSwapchainCreateInfoKHR* ci,
                                                  const VkAllocationCallbacks* alloc,
                                                  VkSwapchainKHR* out);
VKAPI_ATTR void VKAPI_CALL DestroySwapchainKHR(VkDevice device, VkSwapchainKHR swapchain,
                                               const VkAllocationCallbacks* alloc);
VKAPI_ATTR VkResult VKAPI_CALL QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR* info);
VKAPI_ATTR VkResult VKAPI_CALL AcquireNextImageKHR(VkDevice device, VkSwapchainKHR swapchain, uint64_t timeout,
                                                   VkSemaphore semaphore, VkFence fence, uint32_t* index);
// surface formats (the BUTTEREYE_FRAMES_TEST_FORMATS filter; layer.cpp)
VKAPI_ATTR VkResult VKAPI_CALL GetPhysicalDeviceSurfaceFormatsKHR(VkPhysicalDevice pd, VkSurfaceKHR surface,
                                                                  uint32_t* count, VkSurfaceFormatKHR* formats);
VKAPI_ATTR VkResult VKAPI_CALL GetPhysicalDeviceSurfaceFormats2KHR(VkPhysicalDevice pd,
                                                                   const VkPhysicalDeviceSurfaceInfo2KHR* info,
                                                                   uint32_t* count, VkSurfaceFormat2KHR* formats);
const char* format_name(VkFormat f);
VkFormat format_by_name(const std::string& name);  // VK_FORMAT_UNDEFINED when unknown

// summary lines for every swapchain of a device that is going away
void device_gone(VkDevice device);

}  // namespace bef
