// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames: loader negotiation, dispatch, instance and device
// marking, and the injection of present extensions (with a retry that falls back to
// the app's own create info). See docs/spikes/m0z.md for how to turn it on.
#include "layer.h"

#include <cstdarg>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <unistd.h>
#include <initializer_list>
#include <string>
#include <type_traits>
#include <unordered_map>
#include <utility>
#include <vector>

namespace bef {

namespace {

std::mutex g_lock;  // guards the two maps and the ordinals
std::unordered_map<void*, Instance*> g_instances;
std::unordered_map<void*, Device*> g_devices;
std::unordered_map<uint64_t, uint32_t> g_queue_family;  // VkQueue handle -> family
uint32_t g_instance_count = 0;
uint32_t g_device_count = 0;

std::mutex g_log_lock;
FILE* g_log = nullptr;
bool g_log_tried = false;

FILE* log_file() {
    // called with g_log_lock held
    if (!g_log_tried) {
        g_log_tried = true;
        const std::string& p = config().log_path;
        if (!p.empty()) {
            g_log = std::fopen(p.c_str(), "a");
            if (g_log) {
                std::setvbuf(g_log, nullptr, _IOFBF, 1 << 20);
                std::fprintf(g_log,
                             "# P,t_ns,swapchain,image,kind,present_id,next_ns,own_ns,acquire_ns,result,reason,since_app_acquire_ns\n"
                             "# T,swapchain,present_id,stage_ns,time_domain,complete\n"
                             "# C,swapchain,stage_domain_ns,monotonic_ns,max_deviation_ns\n"
                             "# W,swapchain,kind(d=depth,h=hold),target_id,start_ns,return_ns,result\n"
                             "# G,swapchain,t_ns,added,blended,copy_in_ns,dispatch_ns,copies_out_ns,total_ns\n"
                             "# M,swapchain,t_ns,added,made,copy_in_ns,ingest_ns,vectors_cut_ns,frames_ns,copies_out_ns,"
                             "total_ns,pair_ns,frame1_ns,frame2_ns,frame3_ns,largest_submit_ns,submits(1 split|2 own queue),"
                             "cut(-1 unknown),cut_d\n");
            }
        }
    }
    return g_log;
}

__attribute__((destructor)) void close_log() {
    std::lock_guard<std::mutex> g(g_log_lock);
    if (g_log) {
        std::fclose(g_log);
        g_log = nullptr;
    }
}

Instance* find_instance(void* k) {
    std::lock_guard<std::mutex> g(g_lock);
    auto it = g_instances.find(k);
    return it == g_instances.end() ? nullptr : it->second;
}

template <typename T>
T* find_link(const void* chain, VkStructureType stype, VkLayerFunction fn) {
    for (auto* p = static_cast<const VkBaseInStructure*>(chain); p; p = p->pNext) {
        if (p->sType == stype && reinterpret_cast<const T*>(p)->function == fn)
            return const_cast<T*>(reinterpret_cast<const T*>(p));
    }
    return nullptr;
}

bool has_name(const char* const* names, uint32_t n, const char* want) {
    for (uint32_t i = 0; i < n; ++i)
        if (names[i] && std::strcmp(names[i], want) == 0) return true;
    return false;
}

std::string join(const std::vector<const char*>& v) {
    std::string s;
    for (const char* x : v) {
        if (!s.empty()) s += ' ';
        s += x;
    }
    return s.empty() ? "none" : s;
}

std::string chain_types(const void* chain) {
    std::string s;
    for (auto* p = static_cast<const VkBaseInStructure*>(chain); p; p = p->pNext) {
        if (!s.empty()) s += ' ';
        s += stype_name(p->sType);
    }
    return s.empty() ? "none" : s;
}

const char* kSurface = "VK_KHR_surface";
const char* kWayland = "VK_KHR_wayland_surface";
const char* kCaps2 = "VK_KHR_get_surface_capabilities2";
const char* kSurfMaintKHR = "VK_KHR_surface_maintenance1";
const char* kSurfMaintEXT = "VK_EXT_surface_maintenance1";
const char* kSwapchain = "VK_KHR_swapchain";
const char* kScMaintKHR = "VK_KHR_swapchain_maintenance1";
const char* kScMaintEXT = "VK_EXT_swapchain_maintenance1";
const char* kPresentId2 = "VK_KHR_present_id2";
const char* kPresentWait2 = "VK_KHR_present_wait2";
const char* kCalibrated = "VK_KHR_calibrated_timestamps";
const char* kPresentTiming = "VK_EXT_present_timing";

// ---------------------------------------------------------------- instance

VKAPI_ATTR VkResult VKAPI_CALL CreateInstance(const VkInstanceCreateInfo* ci,
                                              const VkAllocationCallbacks* alloc,
                                              VkInstance* out) {
    auto* link = find_link<VkLayerInstanceCreateInfo>(
        ci->pNext, VK_STRUCTURE_TYPE_LOADER_INSTANCE_CREATE_INFO, VK_LAYER_LINK_INFO);
    if (!link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    VkLayerInstanceLink* after = link->u.pLayerInfo;  // put back before a retry
    auto create = reinterpret_cast<PFN_vkCreateInstance>(gipa(VK_NULL_HANDLE, "vkCreateInstance"));
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;

    const uint32_t n = ci->enabledExtensionCount;
    const char* const* names = ci->ppEnabledExtensionNames;
    const bool marked = has_name(names, n, kSurface) && has_name(names, n, kWayland);
    const VkApplicationInfo* ai = ci->pApplicationInfo;
    const char* app = ai && ai->pApplicationName ? ai->pApplicationName : "(none)";
    const char* engine = ai && ai->pEngineName ? ai->pEngineName : "(none)";
    const uint32_t api = ai ? ai->apiVersion : VK_API_VERSION_1_0;

    std::vector<const char*> exts(names, names + n);
    std::vector<const char*> added;
    if (marked && config().kmax >= 2) {
        if (!has_name(names, n, kCaps2)) added.push_back(kCaps2);
        if (!has_name(names, n, kSurfMaintKHR) && !has_name(names, n, kSurfMaintEXT))
            added.push_back(kSurfMaintKHR);
    }

    VkResult r = VK_ERROR_INITIALIZATION_FAILED;
    bool injected = false;
    if (!added.empty()) {
        exts.insert(exts.end(), added.begin(), added.end());
        VkInstanceCreateInfo c2 = *ci;
        c2.enabledExtensionCount = static_cast<uint32_t>(exts.size());
        c2.ppEnabledExtensionNames = exts.data();
        r = create(&c2, alloc, out);
        injected = r == VK_SUCCESS;
        if (!injected) {
            logf("instance: create with %s failed (%d), retrying with the app's own list",
                 join(added).c_str(), r);
            link->u.pLayerInfo = after;
            exts.assign(names, names + n);
            added.clear();
        }
    }
    if (!injected) r = create(ci, alloc, out);
    if (r != VK_SUCCESS) return r;

    auto* in = new Instance;
    in->handle = *out;
    in->marked = marked;
    in->t.GetInstanceProcAddr = gipa;
#define BEF_X(fn) in->t.fn = reinterpret_cast<PFN_vk##fn>(gipa(*out, "vk" #fn));
    BEF_INSTANCE_FNS(BEF_X)
#undef BEF_X
    const char* const* fin = exts.data();
    const uint32_t fn = static_cast<uint32_t>(exts.size());
    in->surface_caps2 = has_name(fin, fn, kCaps2);
    in->surface_maint1 = has_name(fin, fn, kSurfMaintKHR) || has_name(fin, fn, kSurfMaintEXT);
    {
        std::lock_guard<std::mutex> g(g_lock);
        in->ordinal = ++g_instance_count;
        g_instances[key(*out)] = in;
    }
    logf("instance %u: app \"%s\", engine \"%s\", api %u.%u.%u, %u extensions, wayland surface %s"
         " -> %s; added %s; kmax %d",
         in->ordinal, app, engine, VK_API_VERSION_MAJOR(api), VK_API_VERSION_MINOR(api),
         VK_API_VERSION_PATCH(api), n, has_name(names, n, kWayland) ? "yes" : "no",
         marked ? "marked (hooked)" : "pass-through", join(added).c_str(), config().kmax);
    if (marked) logf("instance %u: k %d (BUTTEREYE_FRAMES), mode %s%s, present timing %s", in->ordinal, config().k,
                     config().mode == kModeBlend ? "blend" : config().mode == kModeMc ? "mc" : "dup", config().show ? " (marker on)" : "",
                     config().timing ? "on" : "off");
    if (marked && !config().test_formats.empty()) {
        std::string f;
        for (VkFormat x : config().test_formats) f += std::string(f.empty() ? "" : " ") + format_name(x);
        logf("instance %u: test knob: surface formats limited to %s", in->ordinal, f.c_str());
    }
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL DestroyInstance(VkInstance instance, const VkAllocationCallbacks* alloc) {
    if (!instance) return;
    Instance* in = nullptr;
    {
        std::lock_guard<std::mutex> g(g_lock);
        auto it = g_instances.find(key(instance));
        if (it != g_instances.end()) {
            in = it->second;
            g_instances.erase(it);
        }
    }
    if (!in) return;
    in->t.DestroyInstance(instance, alloc);
    delete in;
    log_flush();
}

// ---------------------------------------------------------------- device

// Feature bits the layer turns on. A struct the app already chained is changed in
// place for the create call and put back afterwards; a missing one is chained
// in front of the app's chain.
struct Injection {
    std::vector<const char*> added;
    VkPhysicalDeviceSwapchainMaintenance1FeaturesKHR maint1{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR, nullptr, VK_FALSE};
    VkPhysicalDevicePresentId2FeaturesKHR id2{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR, nullptr, VK_FALSE};
    VkPhysicalDevicePresentWait2FeaturesKHR wait2{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_2_FEATURES_KHR, nullptr, VK_FALSE};
    VkPhysicalDevicePresentTimingFeaturesEXT timing{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT, nullptr, VK_FALSE, VK_FALSE,
        VK_FALSE};
    struct Saved {
        VkBool32* p;
        VkBool32 old;
    };
    std::vector<Saved> saved;
    std::vector<const char*> merged, chained;
    const void* head = nullptr;
    bool maint1_on = false, id2_on = false, wait2_on = false, timing_on = false;

    void restore() {
        for (auto& s : saved) *s.p = s.old;
        saved.clear();
    }
};

VkBaseOutStructure* find_struct(const void* chain, VkStructureType s) {
    for (auto* p = static_cast<const VkBaseInStructure*>(chain); p; p = p->pNext)
        if (p->sType == s) return reinterpret_cast<VkBaseOutStructure*>(const_cast<VkBaseInStructure*>(p));
    return nullptr;
}

void plan_injection(Instance* in, VkPhysicalDevice pd, const VkDeviceCreateInfo* ci, Injection& inj) {
    PFN_vkGetPhysicalDeviceFeatures2 features2 =
        in->t.GetPhysicalDeviceFeatures2 ? in->t.GetPhysicalDeviceFeatures2 : in->t.GetPhysicalDeviceFeatures2KHR;
    if (!features2 || !in->t.EnumerateDeviceExtensionProperties) {
        logf("device: no vkGetPhysicalDeviceFeatures2, nothing injected");
        return;
    }
    uint32_t count = 0;
    in->t.EnumerateDeviceExtensionProperties(pd, nullptr, &count, nullptr);
    std::vector<VkExtensionProperties> avail(count);
    in->t.EnumerateDeviceExtensionProperties(pd, nullptr, &count, avail.data());
    auto offered = [&](const char* name) {
        for (auto& e : avail)
            if (std::strcmp(e.extensionName, name) == 0) return true;
        return false;
    };
    const char* const* names = ci->ppEnabledExtensionNames;
    const uint32_t n = ci->enabledExtensionCount;
    auto enabled = [&](const char* name) { return has_name(names, n, name); };

    // what the device supports
    VkPhysicalDeviceSwapchainMaintenance1FeaturesKHR q_maint1{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR, nullptr, VK_FALSE};
    VkPhysicalDevicePresentId2FeaturesKHR q_id2{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR, &q_maint1, VK_FALSE};
    VkPhysicalDevicePresentWait2FeaturesKHR q_wait2{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_2_FEATURES_KHR, &q_id2, VK_FALSE};
    VkPhysicalDevicePresentTimingFeaturesEXT q_timing{
        VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT, &q_wait2, VK_FALSE, VK_FALSE,
        VK_FALSE};
    VkPhysicalDeviceFeatures2 q{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_FEATURES_2, &q_timing, {}};
    // an extension that is not offered leaves its feature struct unwritten (VK_FALSE)
    features2(pd, &q);

    if (!enabled(kSwapchain)) {
        logf("device: the app did not enable %s, nothing injected", kSwapchain);
        return;
    }
    auto want = [&](const char* name) {
        if (enabled(name)) return true;
        if (!offered(name)) return false;
        inj.added.push_back(name);
        return true;
    };
    // VK_KHR_swapchain_maintenance1 (or the app's EXT one) needs surface_maintenance1
    if (enabled(kScMaintKHR) || enabled(kScMaintEXT))
        inj.maint1_on = q_maint1.swapchainMaintenance1;
    else if (in->surface_maint1 && q_maint1.swapchainMaintenance1)
        inj.maint1_on = want(offered(kScMaintKHR) ? kScMaintKHR : kScMaintEXT);
    if (in->surface_caps2 && q_id2.presentId2) inj.id2_on = want(kPresentId2);
    if (inj.id2_on && q_wait2.presentWait2) inj.wait2_on = want(kPresentWait2);
    // VK_EXT_present_timing needs the KHR calibrated timestamps (the EXT one is not enough)
    const bool calibrated = inj.id2_on && q_timing.presentTiming && want(kCalibrated);
    if (inj.id2_on && calibrated && q_timing.presentTiming) inj.timing_on = want(kPresentTiming);

    // feature bits: merge into the app's struct, else chain our own in front
    const void* head = ci->pNext;
    auto turn_on = [&](VkStructureType s, VkBaseOutStructure* ours, const char* label,
                       std::initializer_list<std::pair<size_t, VkBool32>> fields) {
        VkBaseOutStructure* app = find_struct(ci->pNext, s);
        auto* base = reinterpret_cast<unsigned char*>(app ? app : ours);
        for (auto& f : fields) {
            auto* p = reinterpret_cast<VkBool32*>(base + f.first);
            if (app) inj.saved.push_back({p, *p});
            if (f.second) *p = VK_TRUE;
        }
        if (app) {
            inj.merged.push_back(label);
        } else {
            ours->pNext = reinterpret_cast<VkBaseOutStructure*>(const_cast<void*>(head));
            head = ours;
            inj.chained.push_back(label);
        }
    };
    using M1 = VkPhysicalDeviceSwapchainMaintenance1FeaturesKHR;
    using I2 = VkPhysicalDevicePresentId2FeaturesKHR;
    using W2 = VkPhysicalDevicePresentWait2FeaturesKHR;
    using PT = VkPhysicalDevicePresentTimingFeaturesEXT;
    if (inj.maint1_on)
        turn_on(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR,
                reinterpret_cast<VkBaseOutStructure*>(&inj.maint1), "swapchainMaintenance1",
                {{offsetof(M1, swapchainMaintenance1), VK_TRUE}});
    if (inj.id2_on)
        turn_on(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR,
                reinterpret_cast<VkBaseOutStructure*>(&inj.id2), "presentId2",
                {{offsetof(I2, presentId2), VK_TRUE}});
    if (inj.wait2_on)
        turn_on(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_WAIT_2_FEATURES_KHR,
                reinterpret_cast<VkBaseOutStructure*>(&inj.wait2), "presentWait2",
                {{offsetof(W2, presentWait2), VK_TRUE}});
    if (inj.timing_on)
        turn_on(VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT,
                reinterpret_cast<VkBaseOutStructure*>(&inj.timing), "presentTiming",
                {{offsetof(PT, presentTiming), VK_TRUE},
                 {offsetof(PT, presentAtAbsoluteTime), q_timing.presentAtAbsoluteTime},
                 {offsetof(PT, presentAtRelativeTime), q_timing.presentAtRelativeTime}});
    inj.head = head;
}

// The layer's own queue (BUTTEREYE_FRAMES_QUEUE) and the global priority
// (BUTTEREYE_FRAMES_PRIORITY): the app's queue create infos, copied, with one queue more in the
// chosen family and a VkDeviceQueueGlobalPriorityCreateInfoKHR on the queue the mc work will use.
struct QueuePlan {
    std::vector<VkDeviceQueueCreateInfo> infos;
    std::vector<std::vector<float>> prios;
    VkDeviceQueueGlobalPriorityCreateInfoKHR gp[8];
    uint32_t ngp = 0;
    bool changed = false;
    uint32_t own_family = UINT32_MAX, own_index = 0;
    bool shared = false;         // the own queue is one the app made too (it takes every queue of the family)
    int priority = 0;            // asked for and chained
    const char* ext = nullptr;   // the priority extension to add (nullptr: none or already on)
    std::string asked;           // the app's queues, for the log

    // the retry after VK_ERROR_NOT_PERMITTED: the same queues at the default priority
    void drop_priority() {
        for (auto& qi : infos)
            for (uint32_t i = 0; i < ngp; ++i)
                if (qi.pNext == &gp[i]) qi.pNext = gp[i].pNext;
        ngp = 0;
        priority = 0;
    }
};

const char* priority_name(int p) {
    switch (p) {
        case VK_QUEUE_GLOBAL_PRIORITY_LOW_KHR: return "low";
        case VK_QUEUE_GLOBAL_PRIORITY_MEDIUM_KHR: return "medium";
        case VK_QUEUE_GLOBAL_PRIORITY_HIGH_KHR: return "high";
        case VK_QUEUE_GLOBAL_PRIORITY_REALTIME_KHR: return "realtime";
        default: return "default";
    }
}

void plan_queues(Instance* in, VkPhysicalDevice pd, const VkDeviceCreateInfo* ci, Injection& inj, QueuePlan& qp) {
    std::vector<VkQueueFamilyProperties> qf;
    if (in->t.GetPhysicalDeviceQueueFamilyProperties) {
        uint32_t nq = 0;
        in->t.GetPhysicalDeviceQueueFamilyProperties(pd, &nq, nullptr);
        qf.resize(nq);
        in->t.GetPhysicalDeviceQueueFamilyProperties(pd, &nq, qf.data());
    }
    qp.infos.assign(ci->pQueueCreateInfos, ci->pQueueCreateInfos + ci->queueCreateInfoCount);
    for (const auto& qi : qp.infos) {
        char b[64];
        std::snprintf(b, sizeof b, "%sfamily %u x%u%s", qp.asked.empty() ? "" : ", ", qi.queueFamilyIndex,
                      qi.queueCount, find_struct(qi.pNext, VK_STRUCTURE_TYPE_DEVICE_QUEUE_GLOBAL_PRIORITY_CREATE_INFO_KHR)
                                         ? " (own priority)"
                                         : "");
        qp.asked += b;
    }
    if (config().queue == kQueueApp && !config().priority) return;
    // the queue infos the priority goes on: the own queue's, else those of graphics families
    std::vector<size_t> targets;
    if (config().queue != kQueueApp) {
        uint32_t fam = UINT32_MAX;
        for (uint32_t f = 0; f < qf.size(); ++f) {
            const VkQueueFlags fl = qf[f].queueFlags;
            const bool fits = config().queue == kQueueCompute
                                  ? (fl & VK_QUEUE_COMPUTE_BIT) && !(fl & VK_QUEUE_GRAPHICS_BIT)
                                  : (fl & VK_QUEUE_GRAPHICS_BIT) && (fl & VK_QUEUE_COMPUTE_BIT);
            if (fits) {
                fam = f;
                break;
            }
        }
        if (fam == UINT32_MAX) {
            logf("device: no %s queue family for the layer's own queue; the mc work stays on the app's queue",
                 config().queue == kQueueCompute ? "compute-only" : "graphics");
        } else {
            size_t at = qp.infos.size();
            for (size_t i = 0; i < qp.infos.size(); ++i)
                if (qp.infos[i].queueFamilyIndex == fam && !(qp.infos[i].flags & VK_DEVICE_QUEUE_CREATE_PROTECTED_BIT))
                    at = i;
            qp.prios.reserve(qp.infos.size() + 1);
            if (at == qp.infos.size()) {
                qp.prios.push_back({1.0f});
                qp.infos.push_back({VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, nullptr, 0, fam, 1, qp.prios.back().data()});
                qp.own_family = fam;
                qp.own_index = 0;
                targets.push_back(at);
            } else if (qp.infos[at].queueCount < qf[fam].queueCount) {
                VkDeviceQueueCreateInfo& qi = qp.infos[at];
                qp.prios.emplace_back(qi.pQueuePriorities, qi.pQueuePriorities + qi.queueCount);
                qp.prios.back().push_back(1.0f);
                qp.own_family = fam;
                qp.own_index = qi.queueCount;
                qi.queueCount += 1;
                qi.pQueuePriorities = qp.prios.back().data();
                targets.push_back(at);
            } else if (qp.infos[at].queueCount > 1) {
                // the app takes every queue of the family (mpv makes them all and, with its default
                // --vulkan-queue-count=1, submits to the first only): the layer uses the last one
                qp.own_family = fam;
                qp.own_index = qp.infos[at].queueCount - 1;
                qp.shared = true;
                targets.push_back(at);
            } else {
                logf("device: the app takes the only queue of family %u; the mc work stays on the app's queue", fam);
            }
            qp.changed = qp.own_family != UINT32_MAX && !qp.shared;
        }
    }
    if (!config().priority) return;
    if (targets.empty())
        for (size_t i = 0; i < qp.infos.size(); ++i)
            if (qp.infos[i].queueFamilyIndex < qf.size() && (qf[qp.infos[i].queueFamilyIndex].queueFlags & VK_QUEUE_GRAPHICS_BIT))
                targets.push_back(i);
    // the extension, KHR or EXT
    uint32_t count = 0;
    in->t.EnumerateDeviceExtensionProperties(pd, nullptr, &count, nullptr);
    std::vector<VkExtensionProperties> avail(count);
    in->t.EnumerateDeviceExtensionProperties(pd, nullptr, &count, avail.data());
    auto offered = [&](const char* name) {
        for (auto& e : avail)
            if (std::strcmp(e.extensionName, name) == 0) return true;
        return false;
    };
    const char* const* names = ci->ppEnabledExtensionNames;
    const char* kKHR = "VK_KHR_global_priority";
    const char* kEXT = "VK_EXT_global_priority";
    const bool on = has_name(names, ci->enabledExtensionCount, kKHR) || has_name(names, ci->enabledExtensionCount, kEXT);
    if (!on) {
        if (offered(kKHR)) qp.ext = kKHR;
        else if (offered(kEXT)) qp.ext = kEXT;
        else {
            logf("device: no global priority extension; priority not set");
            return;
        }
        inj.added.push_back(qp.ext);
    }
    for (size_t i : targets) {
        VkDeviceQueueCreateInfo& qi = qp.infos[i];
        if (find_struct(qi.pNext, VK_STRUCTURE_TYPE_DEVICE_QUEUE_GLOBAL_PRIORITY_CREATE_INFO_KHR) || qp.ngp >= 8) continue;
        qp.gp[qp.ngp] = {VK_STRUCTURE_TYPE_DEVICE_QUEUE_GLOBAL_PRIORITY_CREATE_INFO_KHR, qi.pNext,
                         static_cast<VkQueueGlobalPriorityKHR>(config().priority)};
        qi.pNext = &qp.gp[qp.ngp++];
    }
    if (qp.ngp) {
        qp.priority = config().priority;
        qp.changed = true;
    }
}

VKAPI_ATTR VkResult VKAPI_CALL CreateDevice(VkPhysicalDevice pd, const VkDeviceCreateInfo* ci,
                                            const VkAllocationCallbacks* alloc, VkDevice* out) {
    Instance* in = find_instance(key(pd));
    auto* link = find_link<VkLayerDeviceCreateInfo>(
        ci->pNext, VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO, VK_LAYER_LINK_INFO);
    auto* cb = find_link<VkLayerDeviceCreateInfo>(
        ci->pNext, VK_STRUCTURE_TYPE_LOADER_DEVICE_CREATE_INFO, VK_LOADER_DATA_CALLBACK);
    if (!in || !link || !link->u.pLayerInfo) return VK_ERROR_INITIALIZATION_FAILED;
    PFN_vkGetInstanceProcAddr gipa = link->u.pLayerInfo->pfnNextGetInstanceProcAddr;
    PFN_vkGetDeviceProcAddr gdpa = link->u.pLayerInfo->pfnNextGetDeviceProcAddr;
    link->u.pLayerInfo = link->u.pLayerInfo->pNext;
    VkLayerDeviceLink* after = link->u.pLayerInfo;
    auto create = reinterpret_cast<PFN_vkCreateDevice>(gipa(in->handle, "vkCreateDevice"));
    if (!create) return VK_ERROR_INITIALIZATION_FAILED;

    VkPhysicalDeviceProperties props{};
    if (in->t.GetPhysicalDeviceProperties) in->t.GetPhysicalDeviceProperties(pd, &props);

    Injection inj;
    QueuePlan qp;
    if (in->marked && config().kmax >= 2) {
        plan_injection(in, pd, ci, inj);
        plan_queues(in, pd, ci, inj, qp);
    }

    VkResult r = VK_ERROR_INITIALIZATION_FAILED;
    bool injected = false, retried = false;
    if (!inj.added.empty() || !inj.saved.empty() || !inj.chained.empty() || qp.changed) {
        std::vector<const char*> exts(ci->ppEnabledExtensionNames,
                                      ci->ppEnabledExtensionNames + ci->enabledExtensionCount);
        exts.insert(exts.end(), inj.added.begin(), inj.added.end());
        VkDeviceCreateInfo c2 = *ci;
        c2.pNext = inj.head ? inj.head : ci->pNext;
        c2.enabledExtensionCount = static_cast<uint32_t>(exts.size());
        c2.ppEnabledExtensionNames = exts.data();
        if (qp.changed) {
            c2.queueCreateInfoCount = static_cast<uint32_t>(qp.infos.size());
            c2.pQueueCreateInfos = qp.infos.data();
        }
        r = create(pd, &c2, alloc, out);
        if (r == VK_ERROR_NOT_PERMITTED_KHR && qp.priority) {
            // a priority above the default needs a right the process may not have: the same
            // device at the default priority
            logf("device: global priority %s not permitted; making the device without it",
                 priority_name(qp.priority));
            qp.drop_priority();
            link->u.pLayerInfo = after;
            r = create(pd, &c2, alloc, out);
        }
        inj.restore();
        injected = r == VK_SUCCESS;
        if (!injected) {
            logf("device: create with %s failed (%d), retrying with the app's own create info;"
                 " this device stays pass-through",
                 join(inj.added).c_str(), r);
            link->u.pLayerInfo = after;
            retried = true;
        }
    }
    if (!injected) r = create(pd, ci, alloc, out);
    if (r != VK_SUCCESS) return r;

    auto* d = new Device;
    d->handle = *out;
    d->physical = pd;
    d->instance = in;
    d->t.GetDeviceProcAddr = gdpa;
#define BEF_X(fn) d->t.fn = reinterpret_cast<PFN_vk##fn>(gdpa(*out, "vk" #fn));
    BEF_DEVICE_FNS(BEF_X)
#undef BEF_X
    d->set_loader_data = cb ? cb->u.pfnSetDeviceLoaderData : nullptr;
    d->hooked = in->marked && !retried;
    if (d->hooked) {
        // for the blend's images and timestamps (S3)
        if (in->t.GetPhysicalDeviceMemoryProperties) in->t.GetPhysicalDeviceMemoryProperties(pd, &d->memory);
        d->timestamp_period = props.limits.timestampPeriod;
        if (in->t.GetPhysicalDeviceQueueFamilyProperties) {
            uint32_t nq = 0;
            in->t.GetPhysicalDeviceQueueFamilyProperties(pd, &nq, nullptr);
            std::vector<VkQueueFamilyProperties> qf(nq);
            in->t.GetPhysicalDeviceQueueFamilyProperties(pd, &nq, qf.data());
            for (auto& q : qf) d->timestamp_bits.push_back(q.timestampValidBits);
        }
    }
    if (injected && d->hooked) {
        d->priority = qp.priority;
        if (qp.own_family != UINT32_MAX && d->t.GetDeviceQueue) {
            VkQueue q = VK_NULL_HANDLE;
            d->t.GetDeviceQueue(*out, qp.own_family, qp.own_index, &q);
            // a queue the layer gets for itself needs the loader's dispatch pointer
            if (q && (!d->set_loader_data || d->set_loader_data(*out, q) == VK_SUCCESS)) {
                d->own_queue = q;
                d->own_family = qp.own_family;
            }
        }
    }
    if (injected) {
        d->swapchain_maint1 = inj.maint1_on;
        d->present_id2 = inj.id2_on;
        d->present_wait2 = inj.wait2_on;
        d->present_timing = inj.timing_on;
    }
    {
        std::lock_guard<std::mutex> g(g_lock);
        d->ordinal = ++g_device_count;
        g_devices[key(*out)] = d;
    }
    const bool sc = has_name(ci->ppEnabledExtensionNames, ci->enabledExtensionCount, kSwapchain);
    logf("device %u on instance %u: %s, %u extensions (swapchain %s), %s",
         d->ordinal, in->ordinal, props.deviceName, ci->enabledExtensionCount, sc ? "yes" : "no",
         d->hooked ? "hooked" : "pass-through");
    if (in->marked) {
        logf("device %u: app chain %s", d->ordinal, chain_types(ci->pNext).c_str());
        if (injected)
            logf("device %u: added %s; features merged into the app's structs: %s; chained: %s",
                 d->ordinal, join(inj.added).c_str(), join(inj.merged).c_str(),
                 join(inj.chained).c_str());
        logf("device %u: the app's queues: %s; the layer's own queue: %s; global priority: %s", d->ordinal,
             qp.asked.c_str(),
             d->own_queue ? (std::string("family ") + std::to_string(d->own_family) + " index " +
                             std::to_string(qp.own_index) + (qp.shared ? ", one the app made too" : ", added"))
                                .c_str()
                          : "none (the app's present queue)",
             d->priority ? priority_name(d->priority) : "not set");
    }
    return VK_SUCCESS;
}

VKAPI_ATTR void VKAPI_CALL DestroyDevice(VkDevice device, const VkAllocationCallbacks* alloc) {
    if (!device) return;
    Device* d = find_device(key(device));
    if (!d) return;
    device_gone(device);
    {
        std::lock_guard<std::mutex> g(g_lock);
        g_devices.erase(key(device));
        // queues of this device: their handles may be reused by a later device
        for (auto it = g_queue_family.begin(); it != g_queue_family.end();) {
            if (key(reinterpret_cast<void*>(it->first)) == key(device)) it = g_queue_family.erase(it);
            else ++it;
        }
    }
    d->t.DestroyDevice(device, alloc);
    delete d;
    log_flush();
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetDeviceProcAddr(VkDevice device, const char* name);

// device-level entry points hooked only on devices of a marked instance
PFN_vkVoidFunction swapchain_hook(const char* name) {
    if (!std::strcmp(name, "vkCreateSwapchainKHR")) return reinterpret_cast<PFN_vkVoidFunction>(CreateSwapchainKHR);
    if (!std::strcmp(name, "vkDestroySwapchainKHR")) return reinterpret_cast<PFN_vkVoidFunction>(DestroySwapchainKHR);
    if (!std::strcmp(name, "vkQueuePresentKHR")) return reinterpret_cast<PFN_vkVoidFunction>(QueuePresentKHR);
    if (!std::strcmp(name, "vkAcquireNextImageKHR")) return reinterpret_cast<PFN_vkVoidFunction>(AcquireNextImageKHR);
    if (!std::strcmp(name, "vkGetDeviceQueue")) return reinterpret_cast<PFN_vkVoidFunction>(GetDeviceQueue);
    if (!std::strcmp(name, "vkGetDeviceQueue2")) return reinterpret_cast<PFN_vkVoidFunction>(GetDeviceQueue2);
    return nullptr;
}

// instance-level entry points hooked only on a marked instance, and only with the test knob
PFN_vkVoidFunction format_hook(const char* name) {
    if (config().test_formats.empty()) return nullptr;
    if (!std::strcmp(name, "vkGetPhysicalDeviceSurfaceFormatsKHR"))
        return reinterpret_cast<PFN_vkVoidFunction>(GetPhysicalDeviceSurfaceFormatsKHR);
    if (!std::strcmp(name, "vkGetPhysicalDeviceSurfaceFormats2KHR"))
        return reinterpret_cast<PFN_vkVoidFunction>(GetPhysicalDeviceSurfaceFormats2KHR);
    return nullptr;
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetInstanceProcAddr(VkInstance instance, const char* name) {
    if (!std::strcmp(name, "vkGetInstanceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(GetInstanceProcAddr);
    if (!std::strcmp(name, "vkCreateInstance")) return reinterpret_cast<PFN_vkVoidFunction>(CreateInstance);
    if (!std::strcmp(name, "vkDestroyInstance")) return reinterpret_cast<PFN_vkVoidFunction>(DestroyInstance);
    if (!std::strcmp(name, "vkCreateDevice")) return reinterpret_cast<PFN_vkVoidFunction>(CreateDevice);
    if (!std::strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(GetDeviceProcAddr);
    if (!std::strcmp(name, "vkDestroyDevice")) return reinterpret_cast<PFN_vkVoidFunction>(DestroyDevice);
    if (!instance) return nullptr;
    Instance* in = find_instance(key(instance));
    if (!in) return nullptr;
    if (in->marked) {
        if (PFN_vkVoidFunction f = swapchain_hook(name)) return f;
        if (PFN_vkVoidFunction f = format_hook(name)) {
            // only where the next layer has the function too
            if (in->t.GetInstanceProcAddr(instance, name)) return f;
        }
    }
    return in->t.GetInstanceProcAddr(instance, name);
}

VKAPI_ATTR PFN_vkVoidFunction VKAPI_CALL GetDeviceProcAddr(VkDevice device, const char* name) {
    if (!std::strcmp(name, "vkGetDeviceProcAddr")) return reinterpret_cast<PFN_vkVoidFunction>(GetDeviceProcAddr);
    if (!std::strcmp(name, "vkDestroyDevice")) return reinterpret_cast<PFN_vkVoidFunction>(DestroyDevice);
    if (!device) return nullptr;
    Device* d = find_device(key(device));
    if (!d) return nullptr;
    if (d->hooked)
        if (PFN_vkVoidFunction f = swapchain_hook(name)) return f;
    return d->t.GetDeviceProcAddr(device, name);
}

}  // namespace

// ---------------------------------------------------------------- shared helpers

VKAPI_ATTR void VKAPI_CALL GetDeviceQueue(VkDevice device, uint32_t family, uint32_t index, VkQueue* out) {
    Device* d = find_device(key(device));
    if (!d) return;
    d->t.GetDeviceQueue(device, family, index, out);
    if (*out) {
        std::lock_guard<std::mutex> g(g_lock);
        g_queue_family[reinterpret_cast<uint64_t>(*out)] = family;
    }
}

VKAPI_ATTR void VKAPI_CALL GetDeviceQueue2(VkDevice device, const VkDeviceQueueInfo2* info, VkQueue* out) {
    Device* d = find_device(key(device));
    if (!d || !d->t.GetDeviceQueue2) return;
    d->t.GetDeviceQueue2(device, info, out);
    if (*out) {
        std::lock_guard<std::mutex> g(g_lock);
        g_queue_family[reinterpret_cast<uint64_t>(*out)] = info->queueFamilyIndex;
    }
}

namespace {

bool allowed(VkFormat f) {
    for (VkFormat a : config().test_formats)
        if (a == f) return true;
    return false;
}

// keeps the allowed entries of a format list; the usual two-call idiom on the app side
template <typename T, typename Get, typename FormatOf>
VkResult filter_formats(uint32_t* count, T* out, Get get, FormatOf fmt, T proto) {
    uint32_t n = 0;
    VkResult r = get(&n, nullptr);
    if (r != VK_SUCCESS && r != VK_INCOMPLETE) return r;
    std::vector<T> all(n, proto);
    r = get(&n, all.data());
    if (r != VK_SUCCESS && r != VK_INCOMPLETE) return r;
    all.resize(n);
    std::vector<T> keep;
    for (const T& e : all)
        if (allowed(fmt(e))) keep.push_back(e);
    if (keep.empty()) keep = all;  // nothing matched: leave the list alone
    if (!out) {
        *count = static_cast<uint32_t>(keep.size());
        return VK_SUCCESS;
    }
    const uint32_t m = *count < keep.size() ? *count : static_cast<uint32_t>(keep.size());
    for (uint32_t i = 0; i < m; ++i) {
        void* next = nullptr;
        if constexpr (std::is_same_v<T, VkSurfaceFormat2KHR>) next = out[i].pNext;
        out[i] = keep[i];
        if constexpr (std::is_same_v<T, VkSurfaceFormat2KHR>) out[i].pNext = next;
    }
    *count = m;
    return m < keep.size() ? VK_INCOMPLETE : VK_SUCCESS;
}

}  // namespace

VKAPI_ATTR VkResult VKAPI_CALL GetPhysicalDeviceSurfaceFormatsKHR(VkPhysicalDevice pd, VkSurfaceKHR surface,
                                                                  uint32_t* count, VkSurfaceFormatKHR* formats) {
    Instance* in = find_instance(key(pd));
    if (!in || !in->t.GetPhysicalDeviceSurfaceFormatsKHR) return VK_ERROR_INITIALIZATION_FAILED;
    auto get = [&](uint32_t* n, VkSurfaceFormatKHR* f) {
        return in->t.GetPhysicalDeviceSurfaceFormatsKHR(pd, surface, n, f);
    };
    return filter_formats<VkSurfaceFormatKHR>(count, formats, get, [](const VkSurfaceFormatKHR& e) { return e.format; },
                                              VkSurfaceFormatKHR{});
}

VKAPI_ATTR VkResult VKAPI_CALL GetPhysicalDeviceSurfaceFormats2KHR(VkPhysicalDevice pd,
                                                                   const VkPhysicalDeviceSurfaceInfo2KHR* info,
                                                                   uint32_t* count, VkSurfaceFormat2KHR* formats) {
    Instance* in = find_instance(key(pd));
    if (!in || !in->t.GetPhysicalDeviceSurfaceFormats2KHR) return VK_ERROR_INITIALIZATION_FAILED;
    auto get = [&](uint32_t* n, VkSurfaceFormat2KHR* f) {
        return in->t.GetPhysicalDeviceSurfaceFormats2KHR(pd, info, n, f);
    };
    return filter_formats<VkSurfaceFormat2KHR>(
        count, formats, get, [](const VkSurfaceFormat2KHR& e) { return e.surfaceFormat.format; },
        VkSurfaceFormat2KHR{VK_STRUCTURE_TYPE_SURFACE_FORMAT_2_KHR, nullptr, {}});
}

namespace {
const struct {
    VkFormat f;
    const char* name;
} kFormatNames[] = {
    {VK_FORMAT_B8G8R8A8_UNORM, "B8G8R8A8_UNORM"},
    {VK_FORMAT_B8G8R8A8_SRGB, "B8G8R8A8_SRGB"},
    {VK_FORMAT_R8G8B8A8_UNORM, "R8G8B8A8_UNORM"},
    {VK_FORMAT_R8G8B8A8_SRGB, "R8G8B8A8_SRGB"},
    {VK_FORMAT_A8B8G8R8_UNORM_PACK32, "A8B8G8R8_UNORM"},
    {VK_FORMAT_A8B8G8R8_SRGB_PACK32, "A8B8G8R8_SRGB"},
    {VK_FORMAT_A2R10G10B10_UNORM_PACK32, "A2R10G10B10_UNORM"},
    {VK_FORMAT_A2B10G10R10_UNORM_PACK32, "A2B10G10R10_UNORM"},
    {VK_FORMAT_R16G16B16A16_SFLOAT, "R16G16B16A16_SFLOAT"},
};
}  // namespace

const char* format_name(VkFormat f) {
    for (auto& e : kFormatNames)
        if (e.f == f) return e.name;
    return "other";
}

VkFormat format_by_name(const std::string& name) {
    for (auto& e : kFormatNames)
        if (name == e.name) return e.f;
    return VK_FORMAT_UNDEFINED;
}

uint32_t queue_family(VkQueue queue) {
    std::lock_guard<std::mutex> g(g_lock);
    auto it = g_queue_family.find(reinterpret_cast<uint64_t>(queue));
    return it == g_queue_family.end() ? UINT32_MAX : it->second;
}

const Config& config() {
    static const Config c = [] {
        Config c;
        if (const char* k = std::getenv("BUTTEREYE_FRAMES_KMAX")) c.kmax = std::atoi(k);
        if (const char* p = std::getenv("BUTTEREYE_FRAMES_LOG")) c.log_path = p;
        if (const char* q = std::getenv("BUTTEREYE_FRAMES_QUIET")) c.quiet = std::strcmp(q, "1") == 0;
        if (const char* t = std::getenv("BUTTEREYE_FRAMES_TIMING")) c.timing = std::strcmp(t, "0") != 0;
        if (const char* a = std::getenv("BUTTEREYE_FRAMES_ACQUIRE_US"))
            c.acquire_us = static_cast<uint32_t>(std::strtoul(a, nullptr, 10));
        if (c.kmax > kKLimit) c.kmax = kKLimit;
        if (const char* i = std::getenv("BUTTEREYE_FRAMES_IMAGES")) c.images = std::atoi(i);
        if (const char* q = std::getenv("BUTTEREYE_FRAMES_DEPTH")) c.depth = std::atoi(q);
        if (const char* h = std::getenv("BUTTEREYE_FRAMES_HOLD")) c.hold = std::atoi(h);
        if (c.images > c.kmax) c.images = c.kmax;
        // k: "off" or a number; it never exceeds kmax (no extra images without kmax)
        if (const char* f = std::getenv("BUTTEREYE_FRAMES")) {
            c.k = std::strcmp(f, "off") == 0 ? 1 : std::atoi(f);
            if (c.k < 1) c.k = 1;
            if (c.k > c.kmax) c.k = c.kmax < 1 ? 1 : c.kmax;
        }
        if (const char* m = std::getenv("BUTTEREYE_FRAMES_MODE"))
            c.mode = std::strcmp(m, "blend") == 0 ? kModeBlend : std::strcmp(m, "mc") == 0 ? kModeMc : kModeDup;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_SHOW")) c.show = std::strcmp(v, "1") == 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_DUMP")) c.dump_path = v;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_DUMP_AT")) c.dump_at = std::atoi(v);
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_ENGINE_TEST")) c.engine_test = std::strcmp(v, "1") == 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_TEST_CREATE_FAIL"))
            c.test_create_fail = std::strcmp(v, "1") == 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_SPLIT")) c.split = std::strcmp(v, "0") != 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_MC_THREAD")) c.mc_thread = std::strcmp(v, "0") != 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_QUEUE"))
            c.queue = std::strcmp(v, "compute") == 0    ? kQueueCompute
                      : std::strcmp(v, "graphics") == 0 ? kQueueGraphics
                                                        : kQueueApp;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_PRIORITY"))
            c.priority = std::strcmp(v, "low") == 0        ? VK_QUEUE_GLOBAL_PRIORITY_LOW_KHR
                         : std::strcmp(v, "medium") == 0   ? VK_QUEUE_GLOBAL_PRIORITY_MEDIUM_KHR
                         : std::strcmp(v, "high") == 0     ? VK_QUEUE_GLOBAL_PRIORITY_HIGH_KHR
                         : std::strcmp(v, "realtime") == 0 ? VK_QUEUE_GLOBAL_PRIORITY_REALTIME_KHR
                                                           : 0;
        if (const char* v = std::getenv("BUTTEREYE_FRAMES_TEST_FORMATS")) {
            std::string all(v);
            size_t at = 0;
            while (at <= all.size()) {
                const size_t end = all.find(',', at);
                const std::string one = all.substr(at, end == std::string::npos ? std::string::npos : end - at);
                const VkFormat f = format_by_name(one);
                if (f != VK_FORMAT_UNDEFINED) c.test_formats.push_back(f);
                if (end == std::string::npos) break;
                at = end + 1;
            }
        }
        if (const char* m = std::getenv("BUTTEREYE_FRAMES_SHM")) {
            c.shm_path = m;
        } else if (const char* x = std::getenv("XDG_RUNTIME_DIR")) {
            c.shm_path = std::string(x) + "/buttereye/frames-" + std::to_string(getpid());
        }
        return c;
    }();
    return c;
}

Device* find_device(void* k) {
    std::lock_guard<std::mutex> g(g_lock);
    auto it = g_devices.find(k);
    return it == g_devices.end() ? nullptr : it->second;
}

uint64_t now_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
}

void logf(const char* fmt, ...) {
    char buf[4096];
    va_list ap;
    va_start(ap, fmt);
    std::vsnprintf(buf, sizeof buf, fmt, ap);
    va_end(ap);
    if (!config().quiet) std::fprintf(stderr, "buttereye-frames: %s\n", buf);
    std::lock_guard<std::mutex> g(g_log_lock);
    if (FILE* f = log_file()) std::fprintf(f, "# %s\n", buf);
}

void log_row(const char* fmt, ...) {
    std::lock_guard<std::mutex> g(g_log_lock);
    FILE* f = log_file();
    if (!f) return;
    va_list ap;
    va_start(ap, fmt);
    std::vfprintf(f, fmt, ap);
    va_end(ap);
}

void log_flush() {
    std::lock_guard<std::mutex> g(g_log_lock);
    if (g_log) std::fflush(g_log);
}

const char* stype_name(VkStructureType s) {
    switch (s) {
#define BEF_S(x) case VK_STRUCTURE_TYPE_##x: return #x;
        BEF_S(LOADER_DEVICE_CREATE_INFO)
        BEF_S(LOADER_INSTANCE_CREATE_INFO)
        BEF_S(DEVICE_QUEUE_GLOBAL_PRIORITY_CREATE_INFO)
        BEF_S(PHYSICAL_DEVICE_FEATURES_2)
        BEF_S(PHYSICAL_DEVICE_VULKAN_1_1_FEATURES)
        BEF_S(PHYSICAL_DEVICE_VULKAN_1_2_FEATURES)
        BEF_S(PHYSICAL_DEVICE_VULKAN_1_3_FEATURES)
        BEF_S(PHYSICAL_DEVICE_VULKAN_1_4_FEATURES)
        BEF_S(PHYSICAL_DEVICE_SWAPCHAIN_MAINTENANCE_1_FEATURES_KHR)
        BEF_S(PHYSICAL_DEVICE_PRESENT_ID_2_FEATURES_KHR)
        BEF_S(PHYSICAL_DEVICE_PRESENT_WAIT_2_FEATURES_KHR)
        BEF_S(PHYSICAL_DEVICE_PRESENT_TIMING_FEATURES_EXT)
        BEF_S(PHYSICAL_DEVICE_PRESENT_ID_FEATURES_KHR)
        BEF_S(PHYSICAL_DEVICE_PRESENT_WAIT_FEATURES_KHR)
        BEF_S(IMAGE_FORMAT_LIST_CREATE_INFO)
        BEF_S(SWAPCHAIN_PRESENT_MODES_CREATE_INFO_KHR)
        BEF_S(SWAPCHAIN_PRESENT_SCALING_CREATE_INFO_KHR)
        BEF_S(SWAPCHAIN_PRESENT_FENCE_INFO_KHR)
        BEF_S(SWAPCHAIN_PRESENT_MODE_INFO_KHR)
        BEF_S(PRESENT_ID_KHR)
        BEF_S(PRESENT_ID_2_KHR)
        BEF_S(PRESENT_TIMINGS_INFO_EXT)
        BEF_S(PRESENT_REGIONS_KHR)
        BEF_S(DEVICE_GROUP_PRESENT_INFO_KHR)
        BEF_S(DEVICE_GROUP_SWAPCHAIN_CREATE_INFO_KHR)
        BEF_S(IMAGE_COMPRESSION_CONTROL_EXT)
        BEF_S(SWAPCHAIN_LATENCY_CREATE_INFO_NV)
        BEF_S(LATENCY_SUBMISSION_PRESENT_ID_NV)
#undef BEF_S
        default: {
            // one buffer per thread; only used for log lines
            static thread_local char buf[24];
            std::snprintf(buf, sizeof buf, "%d", static_cast<int>(s));
            return buf;
        }
    }
}

}  // namespace bef

extern "C" VKAPI_ATTR VkResult VKAPI_CALL
vkNegotiateLoaderLayerInterfaceVersion(VkNegotiateLayerInterface* v) {
    if (!v || v->sType != LAYER_NEGOTIATE_INTERFACE_STRUCT) return VK_ERROR_INITIALIZATION_FAILED;
    if (v->loaderLayerInterfaceVersion < 2) return VK_ERROR_INITIALIZATION_FAILED;
    v->loaderLayerInterfaceVersion = 2;
    v->pfnGetInstanceProcAddr = bef::GetInstanceProcAddr;
    v->pfnGetDeviceProcAddr = bef::GetDeviceProcAddr;
    v->pfnGetPhysicalDeviceProcAddr = nullptr;
    return VK_SUCCESS;
}
