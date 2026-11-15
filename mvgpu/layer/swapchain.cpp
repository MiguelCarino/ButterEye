// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames: swapchain and present hooks.
//
// S1: every present is timed (entry time, time in the next layer's vkQueuePresentKHR,
// the layer's own time) and a summary is logged when the swapchain goes away.
//
// S2, the paced duplicate path (BUTTEREYE_FRAMES_KMAX >= 2 and k >= 2): the swapchain
// is made with kmax more images, TRANSFER_SRC/DST usage and, where offered, present
// ids and present timing. Each app present of image i then becomes:
//   1. acquire j_1..j_{k-1}, each with the layer's own semaphore (finite timeout);
//   2. one submit on the app's present queue that waits on the app's semaphores and
//      the acquire semaphores, copies i into every j_m and signals one semaphore per
//      image that the presents wait on;
//   3. present j_1..j_{k-1}, then i (with the app's own pNext), each on its own call.
// Under FIFO each present holds the screen for one refresh, so the k presents show
// at the app's rate times k with no timer. An app present goes through unchanged
// when k is 1, on the first present, when the gap since the previous app present
// is over 1.5 times the expected one (mpv dropped, repeated or redrew a frame),
// when the control block's event sequence changed (pause, seek), when no image
// could be acquired in time, or when the app sends its own present ids.
// After its k presents the layer returns to the app only once at most `hold` of them
// (default k - 1) are still waiting for the screen (vkWaitForPresent2KHR). Without
// that, the added images let the app draw several presents ahead and the app's image
// reaches the screen up to kmax refreshes later than it needs to.
//
// S3, the blend (BUTTEREYE_FRAMES_MODE=blend, or mode 2 in the control block): the
// same submit also copies the app's image i into a history image, and once the layer
// holds the app's previous image too, one compute dispatch mixes the two at t = m/k
// into an output image whose layers are copied into the j_m (see blend.h). The order
// on screen is then: previous image, k-1 blends, image i. After any pass-through the
// history starts again, so the first k path present after it sends plain copies.
// Timestamp queries time the layer's GPU work per app present (G rows).
//
// S4, motion-compensated frames (BUTTEREYE_FRAMES_MODE=mc, or mode 3 in the control block):
// mvgpu's engine runs on the app's images (mc.h) and the k-1 added presents show in-between
// frames at t = m/k. Timestamps go to M rows. The work is split into submits (one call, one
// batch each): the pair's (copy in, ingest, pyramid, vectors, cut test) and one per in-between
// frame (select, render, copy out), so the first added present waits only for the pair and its
// own frame (BUTTEREYE_FRAMES_SPLIT=0: one submit, as in S4c). With an own queue
// (BUTTEREYE_FRAMES_QUEUE=compute|graphics) the compute work goes there and the copies stay on
// the app's queue, ordered by semaphores: app queue (copy in) -> own queue (pair, frames) ->
// app queue (copies out). The engine is made on a worker thread; until it is ready the added
// presents are blends. The cut test's result is read back once a slot's fence is done.
#include "layer.h"

#include "blend.h"
#include "mc.h"

#include <algorithm>
#include <atomic>
#include <thread>
#include <memory>
#include <cinttypes>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace bef {

namespace {

// why an app present went through unchanged (column "reason" of the P rows)
enum Reason : int {
    kFrames = 0,     // took the k path
    kOff = 1,        // k is 1
    kCadence = 2,    // the gap since the previous app present was too long
    kEvent = 3,      // the control block's event sequence changed
    kAcquire = 4,    // no extra image could be acquired in time
    kFirst = 5,      // first app present on this swapchain
    kSlotBusy = 6,   // the layer's previous work on this slot had not finished
    kInactive = 7,   // the swapchain is not set up for the k path
    kMulti = 8,      // a present of several swapchains
    kAppIds = 9,     // the app sends its own present ids
    kNoQueue = 10,   // the present queue's family is unknown
    kFailed = 11,    // a layer call failed; the swapchain stays pass-through
    kReasons = 12,
};
const char* const kReasonName[kReasons] = {"frames", "k=1", "cadence", "event", "acquire", "first",
                                           "slot busy", "inactive", "several swapchains",
                                           "app ids", "no queue", "failed"};

constexpr int kSlots = 8;               // app presents the layer can have in flight
constexpr uint32_t kTimingQueue = 64;   // present-timing results the driver keeps
constexpr int kHist = 8;                // screen-interval histogram, in refreshes (last: more)

struct Slot {
    VkCommandBuffer cb = VK_NULL_HANDLE;
    VkFence done = VK_NULL_HANDLE;           // the copy submit
    bool submitted = false;
    VkSemaphore acquired[kKLimit] = {};      // one per extra image
    VkFence presented[kKLimit] = {};         // present fences of the added presents
    uint32_t presented_used = 0;
    // S3: the blend's timestamps for this slot's submit, read once its fence is done
    bool timed = false;
    bool blended = false;
    bool mc = false;      // the work was mc_record's (M rows), else blend_record's (G rows)
    bool mc_made = false;  // it made in-between frames
    bool mc_split = false;  // the pair and each frame were submits of their own
    bool mc_own = false;    // the compute work went on the device's own queue
    bool cut_pending = false;  // the cut test's result is to be read once the fence is done
    // split and own-queue submits: one command buffer per frame (the app's family) and the own
    // queue's (the pair's, then one per frame), with the semaphores between the two queues
    VkCommandBuffer frame_cb[kKLimit - 1] = {};
    VkCommandBuffer own_cb[kKLimit] = {};
    VkSemaphore own_in = VK_NULL_HANDLE;           // the copy in is done (app queue -> own queue)
    VkSemaphore own_out[kKLimit - 1] = {};         // frame m is rendered (own queue -> app queue)
    uint32_t got = 0;
    uint64_t t_entry = 0;
};

struct Swapchain {
    VkDevice device = VK_NULL_HANDLE;
    Device* d = nullptr;
    VkSwapchainKHR handle = VK_NULL_HANDLE;
    uint32_t ordinal = 0;
    std::mutex m;

    // S1 stats: over every present this layer sends down
    uint64_t n = 0;
    uint64_t first_ns = 0, last_ns = 0;
    uint64_t not_success = 0;
    std::vector<uint32_t> next_ns, own_ns, gap_ns;
    std::string present_chain;

    // S2 set-up
    bool frames = false;        // the k path is possible on this swapchain
    bool failed = false;        // a layer call failed: pass-through from now on
    uint32_t extra = 0;         // images the layer added
    VkExtent2D extent{};
    std::vector<VkImage> images;
    // when the app's own acquire of each image returned (CLOCK_MONOTONIC ns)
    std::unique_ptr<std::atomic<uint64_t>[]> app_acquired;
    std::vector<VkSemaphore> ready;  // per image: signalled by the copy submit, waited by its present
    VkCommandPool pool = VK_NULL_HANDLE;
    uint32_t family = UINT32_MAX;
    Slot slots[kSlots];
    uint32_t slot_next = 0;
    bool maint1 = false;        // present fences on the added presents

    // present ids and timing
    bool ids = false, timing = false, wait2 = false;
    uint64_t next_id = 0;
    uint64_t domain_id = 0;
    VkTimeDomainKHR domain = VK_TIME_DOMAIN_DEVICE_KHR;
    VkPresentStageFlagsEXT stage = 0;
    uint64_t refresh_ns = 0;
    uint64_t refresh_try_ns = 0;
    uint32_t outstanding = 0;
    // stage time domain -> CLOCK_MONOTONIC: monotonic = stage time + offset
    bool calibrated = false;
    int64_t offset_ns = 0;
    uint64_t calibrate_try_ns = 0;
    std::unordered_map<uint64_t, uint64_t> app_ids;  // id of an app image -> app present time
    std::unordered_map<uint64_t, uint64_t> app_acq_ids;  // id of an app image -> the app's acquire time
    uint64_t last_shown_id = 0, last_shown_ns = 0;

    // S2 state and stats
    uint64_t last_app_ns = 0;
    bool have_event = false;
    uint32_t event_seq = 0;
    uint64_t app_presents = 0, frames_presents = 0, generated = 0;
    uint64_t reasons[kReasons] = {};
    uint64_t acquire_short = 0;  // k path with fewer extra images than asked
    uint64_t missed = 0, intervals = 0, not_shown = 0, latency_last = 0;
    uint64_t hist[kHist + 1] = {};
    std::vector<uint32_t> acquire_ns, latency_us, latency_acq_us, app_gap_ns, depth_wait_ns;
    uint64_t depth_timeouts = 0, hold_timeouts = 0;
    std::vector<uint32_t> hold_wait_ns;
    uint32_t k_last = 1;

    // S3: the blend
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkColorSpaceKHR colour_space = VK_COLOR_SPACE_SRGB_NONLINEAR_KHR;
    uint32_t array_layers = 1;
    Blend blend;
    bool blend_ready = false, blend_failed = false;
    VkQueue blend_queue = VK_NULL_HANDLE;
    uint64_t blended = 0, blend_first = 0;  // k path presents with blends / with copies (no history yet)
    bool dumped = false;
    // GPU time per app present, ns: copy into H, the dispatch, the copies out, all of it
    std::vector<uint32_t> gpu_ingest_ns, gpu_blend_ns, gpu_out_ns, gpu_total_ns;
    int last_mode = -1;

    // S4: motion-compensated frames
    std::unique_ptr<Mc> mc;  // the engine in use (null: none yet)
    bool mc_ready = false, mc_failed = false;
    McSettings mc_set;  // what mc was made with
    uint64_t mc_made = 0, mc_first = 0, mc_cuts = 0, mc_cut_known = 0;
    // the engine being made on a worker thread
    struct McJob {
        std::thread th;
        std::atomic<bool> done{false};
        std::unique_ptr<Mc> mc;
        McSettings set;
        bool ok = false;
        std::string why;
        double ms = 0;
        bool drain_failed = false;  // made, but the old engine's work had not finished (logged once)
    };
    std::unique_ptr<McJob> mc_job;
    McSettings mc_tried;          // the settings of the last engine that could not be made
    bool mc_tried_failed = false;
    uint64_t mc_jobs = 0;
    double mc_make_ms = 0;  // the last engine's making time
    McRect mc_rect;         // the last engine's rectangle and size (for the summary)
    int mc_sw = 0, mc_sh = 0;
    // GPU ns per app present that made frames: copy in, ingest + pyramid, vectors + cut,
    // in-between frames (select + render), copies out, all of it, the largest single submit
    std::vector<uint32_t> mc_copy_ns, mc_ingest_ns, mc_vectors_ns, mc_frames_ns, mc_out_ns, mc_total_ns, mc_largest_ns;
    std::vector<uint32_t> mc_busy_ns;  // the sum of the submits' own times
    VkCommandPool own_pool = VK_NULL_HANDLE;
    // the k path's fences and semaphores, made on a thread from swapchain creation on
    std::thread sync_th;
    bool sync_ok = false;
    double sync_ms = 0;
};

std::mutex g_sc_lock;
std::unordered_map<uint64_t, Swapchain*> g_sc;
uint32_t g_sc_count = 0;

uint64_t h64(VkSwapchainKHR s) { return (uint64_t)(uintptr_t)s; }

const char* mode_name(VkPresentModeKHR m) {
    switch (m) {
        case VK_PRESENT_MODE_IMMEDIATE_KHR: return "IMMEDIATE";
        case VK_PRESENT_MODE_MAILBOX_KHR: return "MAILBOX";
        case VK_PRESENT_MODE_FIFO_KHR: return "FIFO";
        case VK_PRESENT_MODE_FIFO_RELAXED_KHR: return "FIFO_RELAXED";
        case VK_PRESENT_MODE_FIFO_LATEST_READY_KHR: return "FIFO_LATEST_READY";
        default: return "other";
    }
}

const char* colorspace_name(VkColorSpaceKHR c) {
    switch (c) {
        case VK_COLOR_SPACE_SRGB_NONLINEAR_KHR: return "SRGB_NONLINEAR";
        case VK_COLOR_SPACE_PASS_THROUGH_EXT: return "PASS_THROUGH";
        case VK_COLOR_SPACE_HDR10_ST2084_EXT: return "HDR10_ST2084";
        case VK_COLOR_SPACE_EXTENDED_SRGB_LINEAR_EXT: return "EXTENDED_SRGB_LINEAR";
        case VK_COLOR_SPACE_BT709_NONLINEAR_EXT: return "BT709_NONLINEAR";
        default: return "other";
    }
}

const char* domain_name(VkTimeDomainKHR t) {
    switch (t) {
        case VK_TIME_DOMAIN_DEVICE_KHR: return "DEVICE";
        case VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR: return "CLOCK_MONOTONIC";
        case VK_TIME_DOMAIN_CLOCK_MONOTONIC_RAW_KHR: return "CLOCK_MONOTONIC_RAW";
        case VK_TIME_DOMAIN_PRESENT_STAGE_LOCAL_EXT: return "PRESENT_STAGE_LOCAL";
        case VK_TIME_DOMAIN_SWAPCHAIN_LOCAL_EXT: return "SWAPCHAIN_LOCAL";
        default: return "other";
    }
}

std::string chain_types(const void* chain) {
    std::string s;
    for (auto* p = static_cast<const VkBaseInStructure*>(chain); p; p = p->pNext) {
        if (!s.empty()) s += ' ';
        s += stype_name(p->sType);
    }
    return s.empty() ? "none" : s;
}

const VkBaseInStructure* find_in(const void* chain, VkStructureType s) {
    for (auto* p = static_cast<const VkBaseInStructure*>(chain); p; p = p->pNext)
        if (p->sType == s) return p;
    return nullptr;
}

std::string usage_names(VkImageUsageFlags u) {
    static const struct { VkImageUsageFlags bit; const char* name; } bits[] = {
        {VK_IMAGE_USAGE_TRANSFER_SRC_BIT, "TRANSFER_SRC"},
        {VK_IMAGE_USAGE_TRANSFER_DST_BIT, "TRANSFER_DST"},
        {VK_IMAGE_USAGE_SAMPLED_BIT, "SAMPLED"},
        {VK_IMAGE_USAGE_STORAGE_BIT, "STORAGE"},
        {VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT, "COLOR_ATTACHMENT"},
        {VK_IMAGE_USAGE_INPUT_ATTACHMENT_BIT, "INPUT_ATTACHMENT"},
    };
    std::string s;
    for (auto& b : bits) {
        if (u & b.bit) {
            if (!s.empty()) s += '|';
            s += b.name;
            u &= ~b.bit;
        }
    }
    if (u) {
        char x[24];
        std::snprintf(x, sizeof x, "%s0x%x", s.empty() ? "" : "|", u);
        s += x;
    }
    return s;
}

// p-th percentile (0..100) of a copy of v, nearest rank
double pct(std::vector<uint32_t> v, double p) {
    if (v.empty()) return 0.0;
    size_t i = static_cast<size_t>(p / 100.0 * static_cast<double>(v.size() - 1) + 0.5);
    std::nth_element(v.begin(), v.begin() + static_cast<long>(i), v.end());
    return v[i];
}

uint32_t clamp32(uint64_t v) { return v > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(v); }

void summarize(Swapchain* s) {
    if (s->n == 0) {
        logf("swapchain %u: no presents", s->ordinal);
        return;
    }
    const double span = static_cast<double>(s->last_ns - s->first_ns) / 1e9;
    const double rate = span > 0 ? static_cast<double>(s->n - 1) / span : 0.0;
    const double gap50 = pct(s->gap_ns, 50);
    uint64_t long_gaps = 0;
    for (uint32_t g : s->gap_ns)
        if (g > 1.5 * gap50) ++long_gaps;
    logf("swapchain %u: %" PRIu64 " presents in %.2f s = %.2f/s; gap ms p1 %.3f p50 %.3f p99 %.3f max %.3f,"
         " %" PRIu64 " gaps over 1.5x the median; in the next layer's present us p50 %.1f p99 %.1f max %.1f;"
         " layer's own us p50 %.2f p99 %.2f max %.2f; %" PRIu64 " results not VK_SUCCESS",
         s->ordinal, s->n, span, rate, pct(s->gap_ns, 1) / 1e6, gap50 / 1e6, pct(s->gap_ns, 99) / 1e6,
         pct(s->gap_ns, 100) / 1e6, long_gaps, pct(s->next_ns, 50) / 1e3, pct(s->next_ns, 99) / 1e3,
         pct(s->next_ns, 100) / 1e3, pct(s->own_ns, 50) / 1e3, pct(s->own_ns, 99) / 1e3,
         pct(s->own_ns, 100) / 1e3, s->not_success);
    if (!s->frames && s->app_presents == 0) return;
    std::string why;
    for (int r = 1; r < kReasons; ++r) {
        if (!s->reasons[r]) continue;
        char b[48];
        std::snprintf(b, sizeof b, "%s%s %" PRIu64, why.empty() ? "" : ", ", kReasonName[r], s->reasons[r]);
        why += b;
    }
    logf("swapchain %u: %" PRIu64 " app presents, %" PRIu64 " took the k path (%" PRIu64
         " with fewer images), %" PRIu64 " presents added; passed through: %s; acquire us p50 %.1f p99 %.1f"
         " max %.1f",
         s->ordinal, s->app_presents, s->frames_presents, s->acquire_short, s->generated,
         why.empty() ? "none" : why.c_str(), pct(s->acquire_ns, 50) / 1e3, pct(s->acquire_ns, 99) / 1e3,
         pct(s->acquire_ns, 100) / 1e3);
    if (!s->hold_wait_ns.empty())
        logf("swapchain %u: hold (setting %d): wait us p50 %.1f p99 %.1f max %.1f, %" PRIu64 " timeouts", s->ordinal,
             config().hold, pct(s->hold_wait_ns, 50) / 1e3, pct(s->hold_wait_ns, 99) / 1e3,
             pct(s->hold_wait_ns, 100) / 1e3, s->hold_timeouts);
    if (!s->depth_wait_ns.empty())
        logf("swapchain %u: depth limit %d: wait us p50 %.1f p99 %.1f max %.1f, %" PRIu64 " timeouts", s->ordinal,
             config().depth, pct(s->depth_wait_ns, 50) / 1e3, pct(s->depth_wait_ns, 99) / 1e3,
             pct(s->depth_wait_ns, 100) / 1e3, s->depth_timeouts);
    if (s->blended || s->blend_first || s->blend_failed)
        logf("swapchain %u: blend %s: %" PRIu64 " app presents blended, %" PRIu64 " sent copies (no previous"
             " image yet); GPU per app present us p50 %.1f p99 %.1f max %.1f (copy in p50 %.1f, dispatch p50 %.1f"
             " p99 %.1f, copies out p50 %.1f) over %zu timed presents",
             s->ordinal, s->blend_failed ? "failed" : "on", s->blended, s->blend_first, pct(s->gpu_total_ns, 50) / 1e3,
             pct(s->gpu_total_ns, 99) / 1e3, pct(s->gpu_total_ns, 100) / 1e3, pct(s->gpu_ingest_ns, 50) / 1e3,
             pct(s->gpu_blend_ns, 50) / 1e3, pct(s->gpu_blend_ns, 99) / 1e3, pct(s->gpu_out_ns, 50) / 1e3,
             s->gpu_total_ns.size());
    if (s->mc_made || s->mc_first || s->mc_failed) {
        logf("swapchain %u: mc %s: engine %dx%d over %d,%d %dx%d, %s, %s%s; %" PRIu64 " app presents made in-between"
             " frames, %" PRIu64 " sent copies (first of a run), %" PRIu64 " cuts in %" PRIu64 " tested pairs; GPU per"
             " app present ms p50 %.3f p99 %.3f max %.3f (copy in %.3f, ingest %.3f, vectors+cut %.3f, frames %.3f,"
             " copies out %.3f at p50), largest submit ms p50 %.3f p99 %.3f max %.3f, over %zu timed presents;"
             " %" PRIu64 " engines made, the last in %.1f ms",
             s->ordinal, s->mc_failed ? "failed" : "on", s->mc_sw, s->mc_sh, s->mc_rect.x, s->mc_rect.y, s->mc_rect.w,
             s->mc_rect.h, s->mc_set.pq ? "PQ" : "SDR",
             config().split || s->d->own_queue ? "split submits" : "one submit",
             s->d->own_queue ? ", own queue" : "", s->mc_made, s->mc_first, s->mc_cuts, s->mc_cut_known,
             pct(s->mc_total_ns, 50) / 1e6, pct(s->mc_total_ns, 99) / 1e6, pct(s->mc_total_ns, 100) / 1e6,
             pct(s->mc_copy_ns, 50) / 1e6, pct(s->mc_ingest_ns, 50) / 1e6, pct(s->mc_vectors_ns, 50) / 1e6,
             pct(s->mc_frames_ns, 50) / 1e6, pct(s->mc_out_ns, 50) / 1e6, pct(s->mc_largest_ns, 50) / 1e6,
             pct(s->mc_largest_ns, 99) / 1e6, pct(s->mc_largest_ns, 100) / 1e6, s->mc_total_ns.size(), s->mc_jobs,
             s->mc_make_ms);
    }
    if (s->intervals || s->not_shown) {
        std::string h;
        for (int i = 0; i <= kHist; ++i) {
            char b[32];
            std::snprintf(b, sizeof b, "%s%d%s:%" PRIu64, h.empty() ? "" : " ", i, i == kHist ? "+" : "",
                          s->hist[i]);
            h += b;
        }
        logf("swapchain %u: present timing: refresh %.3f ms, %" PRIu64 " screen intervals (refreshes %s),"
             " %" PRIu64 " missed refreshes, %" PRIu64 " presents never shown; app present to first pixel"
             " of its image ms p50 %.2f p99 %.2f; app acquire to first pixel ms p50 %.2f p99 %.2f",
             s->ordinal, s->refresh_ns / 1e6, s->intervals, h.c_str(), s->missed, s->not_shown,
             pct(s->latency_us, 50) / 1e3, pct(s->latency_us, 99) / 1e3, pct(s->latency_acq_us, 50) / 1e3,
             pct(s->latency_acq_us, 99) / 1e3);
    }
}

Swapchain* new_swapchain(VkDevice device, Device* d) {
    auto* s = new Swapchain;
    s->device = device;
    s->d = d;
    s->next_ns.reserve(1 << 16);
    s->own_ns.reserve(1 << 16);
    s->gap_ns.reserve(1 << 16);
    return s;
}

// the layer's objects of one swapchain; called once nothing of the layer's is pending
void free_objects(Swapchain* s) {
    if (s->sync_th.joinable()) s->sync_th.join();
    const DeviceTable& t = s->d->t;
    VkDevice dev = s->device;
    for (Slot& sl : s->slots) {
        for (int m = 0; m < kKLimit; ++m) {
            if (sl.acquired[m]) t.DestroySemaphore(dev, sl.acquired[m], nullptr);
            if (sl.presented[m]) t.DestroyFence(dev, sl.presented[m], nullptr);
        }
        if (sl.done) t.DestroyFence(dev, sl.done, nullptr);
        if (sl.own_in) t.DestroySemaphore(dev, sl.own_in, nullptr);
        for (VkSemaphore x : sl.own_out)
            if (x) t.DestroySemaphore(dev, x, nullptr);
    }
    if (s->pool) t.DestroyCommandPool(dev, s->pool, nullptr);  // frees the command buffers
    if (s->own_pool) t.DestroyCommandPool(dev, s->own_pool, nullptr);
    for (VkSemaphore r : s->ready)
        if (r) t.DestroySemaphore(dev, r, nullptr);
    s->ready.clear();
    if (s->mc_job) {
        // the worker may still be making an engine for the blend's images
        if (s->mc_job->th.joinable()) s->mc_job->th.join();
        logf("swapchain %u: the engine being made (%s, %.1f ms) was not used: the swapchain went away", s->ordinal,
             s->mc_job->ok ? "made" : "failed", s->mc_job->ms);
        if (s->mc_job->ok) mc_destroy(*s->mc_job->mc);
        s->mc_job.reset();
    }
    if (s->mc_ready) mc_destroy(*s->mc);
    s->mc.reset();
    s->mc_ready = false;
    if (s->blend_ready) blend_destroy(s->blend);
    s->blend_ready = false;
}

// the GPU times of a slot whose submit has finished (G and M rows), and its cut test
void collect_gpu(Swapchain* s, Slot& sl) {
    const uint32_t qslot = static_cast<uint32_t>(&sl - s->slots);
    int cut = -1;
    float cut_d = -1.0f;
    if (sl.cut_pending) {
        sl.cut_pending = false;
        if (s->mc_ready && (cut = mc_cut(*s->mc, qslot, &cut_d)) >= 0) {
            ++s->mc_cut_known;
            if (cut == 1) ++s->mc_cuts;
        }
    }
    if (!sl.timed) return;
    sl.timed = false;
    if (sl.mc) {
        McTimes x;
        if (!s->mc_ready || !mc_times(*s->mc, qslot, sl.mc_split, &x)) return;
        const uint64_t busy = static_cast<uint64_t>(x.pair + x.frame[0] + x.frame[1] + x.frame[2]);
        if (sl.mc_made) {
            s->mc_copy_ns.push_back(clamp32(static_cast<uint64_t>(x.copy_in)));
            s->mc_ingest_ns.push_back(clamp32(static_cast<uint64_t>(x.ingest)));
            s->mc_vectors_ns.push_back(clamp32(static_cast<uint64_t>(x.vectors)));
            s->mc_frames_ns.push_back(clamp32(static_cast<uint64_t>(x.frames)));
            s->mc_out_ns.push_back(clamp32(static_cast<uint64_t>(x.out)));
            s->mc_total_ns.push_back(clamp32(static_cast<uint64_t>(x.total)));
            s->mc_largest_ns.push_back(clamp32(static_cast<uint64_t>(x.largest)));
            s->mc_busy_ns.push_back(clamp32(busy));
        }
        log_row("M,%u,%" PRIu64 ",%u,%d,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%.0f,%d,%d,%.5f\n", s->ordinal,
                sl.t_entry, sl.got, sl.mc_made ? 1 : 0, x.copy_in, x.ingest, x.vectors, x.frames, x.out, x.total,
                x.pair, x.frame[0], x.frame[1], x.frame[2], x.largest, (sl.mc_split ? 1 : 0) | (sl.mc_own ? 2 : 0),
                cut, static_cast<double>(cut_d));
        return;
    }
    double ns[kStamps];
    if (!blend_times(s->blend, qslot, ns)) return;
    const uint64_t ingest = static_cast<uint64_t>(ns[1]), total = static_cast<uint64_t>(ns[3]);
    const uint64_t disp = static_cast<uint64_t>(ns[2] - ns[1]), out = static_cast<uint64_t>(ns[3] - ns[2]);
    if (sl.blended) {
        s->gpu_ingest_ns.push_back(clamp32(ingest));
        s->gpu_blend_ns.push_back(clamp32(disp));
        s->gpu_out_ns.push_back(clamp32(out));
        s->gpu_total_ns.push_back(clamp32(total));
    }
    log_row("G,%u,%" PRIu64 ",%u,%d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n", s->ordinal, sl.t_entry, sl.got,
            sl.blended ? 1 : 0, ingest, disp, out, total);
}

// wait for everything of the layer's that may still use the swapchain's objects. The fences
// waited are reset (the slots are used again after an engine change), and a slot whose wait
// failed keeps its state, so its fences are waited again before reuse; false when one failed
bool drain(Swapchain* s) {
    const DeviceTable& t = s->d->t;
    bool all = true;
    for (Slot& sl : s->slots) {
        if (sl.submitted) {
            if (t.WaitForFences(s->device, 1, &sl.done, VK_TRUE, 1000000000ull) == VK_SUCCESS) {
                collect_gpu(s, sl);
                t.ResetFences(s->device, 1, &sl.done);
                sl.submitted = false;
            } else {
                all = false;
            }
        }
        if (sl.presented_used) {
            if (t.WaitForFences(s->device, sl.presented_used, sl.presented, VK_TRUE, 1000000000ull) == VK_SUCCESS) {
                t.ResetFences(s->device, sl.presented_used, sl.presented);
                sl.presented_used = 0;
            } else {
                all = false;
            }
        }
    }
    return all;
}

// the mc settings from the environment, read once
const McSettings& mc_settings() {
    static const McSettings env = mc_settings_from_env();
    return env;
}

// set the swapchain up for the k path once a present shows its queue family
// the fences and semaphores of the k path. They need no queue family, so they are made on a
// thread from swapchain creation on: on this driver a fence takes about a millisecond to make,
// and the 20-odd of them made the first k path present 15-35 ms late.
bool make_sync(Swapchain* s) {
    const DeviceTable& t = s->d->t;
    VkDevice dev = s->device;
    VkSemaphoreCreateInfo semi{VK_STRUCTURE_TYPE_SEMAPHORE_CREATE_INFO, nullptr, 0};
    VkFenceCreateInfo fi{VK_STRUCTURE_TYPE_FENCE_CREATE_INFO, nullptr, 0};
    const int extra = config().kmax - 1;
    const bool own_queue = s->d->own_queue != VK_NULL_HANDLE;
    for (int i = 0; i < kSlots; ++i) {
        Slot& sl = s->slots[i];
        if (t.CreateFence(dev, &fi, nullptr, &sl.done) != VK_SUCCESS) return false;
        for (int m = 0; m < extra; ++m) {
            if (t.CreateSemaphore(dev, &semi, nullptr, &sl.acquired[m]) != VK_SUCCESS) return false;
            if (s->maint1 && t.CreateFence(dev, &fi, nullptr, &sl.presented[m]) != VK_SUCCESS) return false;
        }
        if (own_queue) {
            if (t.CreateSemaphore(dev, &semi, nullptr, &sl.own_in) != VK_SUCCESS) return false;
            for (int m = 0; m < kKLimit - 1; ++m)
                if (t.CreateSemaphore(dev, &semi, nullptr, &sl.own_out[m]) != VK_SUCCESS) return false;
        }
    }
    s->ready.assign(s->images.size(), VK_NULL_HANDLE);
    for (auto& r : s->ready)
        if (t.CreateSemaphore(dev, &semi, nullptr, &r) != VK_SUCCESS) return false;
    return true;
}

// with blend_too, the blend's images and pipeline too (made in 1-25 ms here): for a swapchain
// that starts in blend or mc mode, when the layer has no queue of its own (whose family the
// images would have to be shared with). The queue family is not known yet; the blend uses it
// only to check that the family has timestamps, and every family here has them.
void start_sync(Swapchain* s, bool blend_too) {
    s->sync_th = std::thread([s, blend_too] {
        const uint64_t t0 = now_ns();
        s->sync_ok = make_sync(s);
        s->sync_ms = static_cast<double>(now_ns() - t0) / 1e6;
        if (!s->sync_ok || !blend_too || s->array_layers != 1) return;
        const std::vector<uint32_t>& tb = s->d->timestamp_bits;
        uint32_t fam = 0;
        while (fam + 1 < tb.size() && !tb[fam]) ++fam;
        std::string why;
        const uint64_t tb0 = now_ns();
        if (blend_create(s->blend, s->d, s->device, s->extent, s->format, static_cast<uint32_t>(config().kmax - 1), fam,
                         kSlots, &why)) {
            s->blend_ready = true;
            logf("swapchain %u: blend ready in %.1f ms (made with the swapchain, on a thread): %ux%u, format %s,"
                 " %u bits a channel, red %s, %u in-between layers, %.1f MB of images, GPU timestamps %s",
                 s->ordinal, (now_ns() - tb0) / 1e6, s->extent.width, s->extent.height, format_name(s->format),
                 s->blend.fl.bits, s->blend.fl.red_low ? "low" : "high", s->blend.layers, s->blend.bytes / 1048576.0,
                 s->blend.queries ? "on" : "off");
        }
        // on failure the first present that asks for the blend tries again and logs why
    });
}

// waits for start_sync's thread, once; false when its objects could not all be made
bool join_sync(Swapchain* s) {
    if (s->sync_th.joinable()) {
        const uint64_t t0 = now_ns();
        s->sync_th.join();
        const double waited = static_cast<double>(now_ns() - t0) / 1e6;
        logf("swapchain %u: fences and semaphores made in %.1f ms on a thread (%s; waited %.1f ms for it)", s->ordinal,
             s->sync_ms, s->sync_ok ? "ok" : "FAILED", waited);
    }
    return s->sync_ok;
}

// set the swapchain up for the k path once a present shows its queue family
bool ensure_objects(Swapchain* s, uint32_t family) {
    if (s->pool) return s->family == family;
    if (!join_sync(s)) return false;
    const DeviceTable& t = s->d->t;
    VkDevice dev = s->device;
    s->family = family;
    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr,
                                VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, family};
    if (t.CreateCommandPool(dev, &pci, nullptr, &s->pool) != VK_SUCCESS) return false;
    // per slot: the submit's command buffer and one per in-between frame (split submits)
    constexpr uint32_t kPer = kKLimit;
    VkCommandBuffer cbs[kSlots * kPer];
    VkCommandBufferAllocateInfo cai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, s->pool,
                                    VK_COMMAND_BUFFER_LEVEL_PRIMARY, kSlots * kPer};
    if (t.AllocateCommandBuffers(dev, &cai, cbs) != VK_SUCCESS) return false;
    // the own queue's: the pair's and one per frame
    VkCommandBuffer own[kSlots * kKLimit] = {};
    const bool own_queue = s->d->own_queue != VK_NULL_HANDLE;
    if (own_queue) {
        VkCommandPoolCreateInfo opi{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, nullptr,
                                    VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT, s->d->own_family};
        if (t.CreateCommandPool(dev, &opi, nullptr, &s->own_pool) != VK_SUCCESS) return false;
        VkCommandBufferAllocateInfo oai{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, nullptr, s->own_pool,
                                        VK_COMMAND_BUFFER_LEVEL_PRIMARY, kSlots * kKLimit};
        if (t.AllocateCommandBuffers(dev, &oai, own) != VK_SUCCESS) return false;
    }
    for (int i = 0; i < kSlots; ++i) {
        Slot& sl = s->slots[i];
        sl.cb = cbs[i * kPer];
        for (uint32_t m = 0; m + 1 < kPer; ++m) sl.frame_cb[m] = cbs[i * kPer + 1 + m];
        if (own_queue)
            for (int m = 0; m < kKLimit; ++m) sl.own_cb[m] = own[i * kKLimit + m];
        // command buffers made by a layer need the loader's dispatch pointer
        if (s->d->set_loader_data) {
            for (uint32_t m = 0; m < kPer; ++m)
                if (s->d->set_loader_data(dev, cbs[i * kPer + m]) != VK_SUCCESS) return false;
            if (own_queue)
                for (int m = 0; m < kKLimit; ++m)
                    if (s->d->set_loader_data(dev, sl.own_cb[m]) != VK_SUCCESS) return false;
        }
    }
    return true;
}

void refresh_duration(Swapchain* s, uint64_t now) {
    if (s->refresh_ns || !s->timing || now < s->refresh_try_ns) return;
    s->refresh_try_ns = now + 100000000ull;
    VkSwapchainTimingPropertiesEXT tp{VK_STRUCTURE_TYPE_SWAPCHAIN_TIMING_PROPERTIES_EXT, nullptr, 0, 0};
    uint64_t counter = 0;
    if (s->d->t.GetSwapchainTimingPropertiesEXT(s->device, s->handle, &tp, &counter) == VK_SUCCESS &&
        tp.refreshDuration) {
        s->refresh_ns = tp.refreshDuration;
        logf("swapchain %u: refresh duration %.4f ms, interval %.4f ms", s->ordinal, tp.refreshDuration / 1e6,
             tp.refreshInterval / 1e6);
    }
}

// the offset from the present-stage time domain to CLOCK_MONOTONIC, once a second
void calibrate(Swapchain* s, uint64_t now) {
    if (!s->timing || now < s->calibrate_try_ns) return;
    s->calibrate_try_ns = now + 1000000000ull;
    if (s->domain == VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR) {
        s->calibrated = true;
        s->offset_ns = 0;
        return;
    }
    if (!s->d->t.GetCalibratedTimestampsKHR) return;
    VkSwapchainCalibratedTimestampInfoEXT sci{VK_STRUCTURE_TYPE_SWAPCHAIN_CALIBRATED_TIMESTAMP_INFO_EXT, nullptr,
                                              s->handle, s->stage, s->domain_id};
    VkCalibratedTimestampInfoKHR ci[2] = {
        {VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR, &sci, s->domain},
        {VK_STRUCTURE_TYPE_CALIBRATED_TIMESTAMP_INFO_KHR, nullptr, VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR}};
    uint64_t ts[2] = {0, 0}, dev = 0;
    const VkResult r = s->d->t.GetCalibratedTimestampsKHR(s->device, 2, ci, ts, &dev);
    if (r != VK_SUCCESS) {
        if (!s->calibrated) logf("swapchain %u: calibrated timestamps failed (%d)", s->ordinal, r);
        s->calibrate_try_ns = now + 60000000000ull;
        return;
    }
    s->offset_ns = static_cast<int64_t>(ts[1]) - static_cast<int64_t>(ts[0]);
    s->calibrated = true;
    log_row("C,%u,%" PRIu64 ",%" PRIu64 ",%" PRIu64 "\n", s->ordinal, ts[0], ts[1], dev);
}

// read the finished present-timing results: T rows, screen intervals, latency
void collect_timing(Swapchain* s) {
    if (!s->timing || !s->outstanding) return;
    VkPresentStageTimeEXT st[kTimingQueue][2];
    VkPastPresentationTimingEXT pt[kTimingQueue];
    for (uint32_t round = 0; round < 2; ++round) {
        for (uint32_t x = 0; x < kTimingQueue; ++x) {
            pt[x] = {VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_EXT};
            pt[x].presentStageCount = 2;
            pt[x].pPresentStages = st[x];
        }
        VkPastPresentationTimingInfoEXT pi{VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_INFO_EXT, nullptr, 0,
                                           s->handle};
        VkPastPresentationTimingPropertiesEXT pp{VK_STRUCTURE_TYPE_PAST_PRESENTATION_TIMING_PROPERTIES_EXT};
        pp.presentationTimingCount = kTimingQueue;
        pp.pPresentationTimings = pt;
        const VkResult r = s->d->t.GetPastPresentationTimingEXT(s->device, &pi, &pp);
        if (r != VK_SUCCESS && r != VK_INCOMPLETE) return;
        for (uint32_t x = 0; x < pp.presentationTimingCount; ++x) {
            const VkPastPresentationTimingEXT& p = pt[x];
            uint64_t t = 0;
            for (uint32_t k = 0; k < p.presentStageCount && k < 2; ++k)
                if (p.pPresentStages[k].stage == s->stage) t = p.pPresentStages[k].time;
            if (s->outstanding) --s->outstanding;
            log_row("T,%u,%" PRIu64 ",%" PRIu64 ",%d,%u\n", s->ordinal, p.presentId, t,
                    static_cast<int>(p.timeDomain), p.reportComplete);
            if (!t) {
                ++s->not_shown;
                s->app_ids.erase(p.presentId);
                continue;
            }
            if (s->last_shown_id && p.presentId > s->last_shown_id && t > s->last_shown_ns && s->refresh_ns) {
                const double r = static_cast<double>(t - s->last_shown_ns) / static_cast<double>(s->refresh_ns);
                const uint64_t refreshes = static_cast<uint64_t>(r + 0.5);
                // only consecutive presents tell a missed refresh apart from a gap in the app
                if (p.presentId == s->last_shown_id + 1) {
                    ++s->intervals;
                    ++s->hist[refreshes > kHist ? kHist : refreshes];
                    if (refreshes > 1) s->missed += refreshes - 1;
                }
            }
            s->last_shown_id = p.presentId;
            s->last_shown_ns = t;
            auto it = s->app_ids.find(p.presentId);
            if (it != s->app_ids.end()) {
                const int64_t mono = static_cast<int64_t>(t) + s->offset_ns;
                if (s->calibrated && mono > static_cast<int64_t>(it->second)) {
                    const uint64_t lat = static_cast<uint64_t>(mono) - it->second;
                    s->latency_last = lat;
                    s->latency_us.push_back(clamp32(lat / 1000));
                }
                auto ia = s->app_acq_ids.find(p.presentId);
                if (ia != s->app_acq_ids.end()) {
                    if (s->calibrated && ia->second && mono > static_cast<int64_t>(ia->second))
                        s->latency_acq_us.push_back(clamp32((static_cast<uint64_t>(mono) - ia->second) / 1000));
                    s->app_acq_ids.erase(ia);
                }
                s->app_ids.erase(it);
            }
        }
        if (r != VK_INCOMPLETE) break;
    }
    if (s->app_ids.size() > 4 * kTimingQueue) s->app_ids.clear();  // results that never came
    if (s->app_acq_ids.size() > 4 * kTimingQueue) s->app_acq_ids.clear();
}

// the pNext structs the layer puts in front of a present's chain
struct PresentExtras {
    uint64_t id = 0;
    VkPresentId2KHR id2{VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR, nullptr, 1, &id};
    VkPresentTimingInfoEXT ti{VK_STRUCTURE_TYPE_PRESENT_TIMING_INFO_EXT};
    VkPresentTimingsInfoEXT tis{VK_STRUCTURE_TYPE_PRESENT_TIMINGS_INFO_EXT, nullptr, 1, &ti};

    // returns the new head of the chain
    const void* attach(Swapchain* s, const void* chain, bool timed) {
        if (!s->ids) return chain;
        id = ++s->next_id;
        const void* head = chain;
        if (timed) {
            ti.timeDomainId = s->domain_id;
            ti.presentStageQueries = s->stage;
            tis.pNext = head;
            head = &tis;
            ++s->outstanding;
        }
        id2.pNext = head;
        return &id2;
    }
};

// the expected gap between app presents at k: k refreshes, or (refresh not known yet)
// the median of the last 15 gaps; UINT64_MAX/4 while neither is known
uint64_t expected_gap(Swapchain* s, uint32_t k) {
    if (s->refresh_ns) return k * s->refresh_ns;
    const size_t n = s->app_gap_ns.size();
    if (n < 15) return UINT64_MAX / 4;
    std::vector<uint32_t> last(s->app_gap_ns.end() - 15, s->app_gap_ns.end());
    return static_cast<uint64_t>(pct(last, 50));
}

bool want_timing(Swapchain* s) { return s->timing && s->outstanding + 1 < kTimingQueue; }

}  // namespace

VKAPI_ATTR VkResult VKAPI_CALL CreateSwapchainKHR(VkDevice device, const VkSwapchainCreateInfoKHR* ci,
                                                  const VkAllocationCallbacks* alloc, VkSwapchainKHR* out) {
    Device* d = find_device(key(device));
    if (!d) return VK_ERROR_INITIALIZATION_FAILED;
    if (!d->hooked) return d->t.CreateSwapchainKHR(device, ci, alloc, out);

    const int kmax = config().kmax;
    VkSwapchainCreateInfoKHR c2 = *ci;
    std::string why_not;
    bool frames = false, ids = false, timing = false, wait2 = false;
    uint32_t extra = 0;
    VkPresentStageFlagsEXT stages = 0;
    if (kmax < 2) {
        why_not = "kmax < 2";
    } else if (ci->presentMode != VK_PRESENT_MODE_FIFO_KHR) {
        why_not = "not FIFO";
    } else if (!d->instance || !d->instance->t.GetPhysicalDeviceSurfaceCapabilitiesKHR) {
        why_not = "no surface caps";
    } else {
        VkSurfaceCapabilitiesKHR caps{};
        d->instance->t.GetPhysicalDeviceSurfaceCapabilitiesKHR(d->physical, ci->surface, &caps);
        const VkImageUsageFlags need = VK_IMAGE_USAGE_TRANSFER_SRC_BIT | VK_IMAGE_USAGE_TRANSFER_DST_BIT;
        const uint32_t add = static_cast<uint32_t>(config().images < 0 ? kmax : config().images);
        uint32_t want = ci->minImageCount + add;
        if (caps.maxImageCount && want > caps.maxImageCount) want = caps.maxImageCount;
        extra = want - ci->minImageCount;
        if ((caps.supportedUsageFlags & need) != need) {
            why_not = "no transfer usage";
        } else if (extra < add) {
            why_not = "image limit";
        } else {
            frames = true;
            c2.minImageCount = want;
            c2.imageUsage |= need;
            // present ids and timing, when the device, the surface and the swapchain allow
            if (d->present_id2 && d->instance->surface_caps2 &&
                d->instance->t.GetPhysicalDeviceSurfaceCapabilities2KHR) {
                VkSurfaceCapabilitiesPresentWait2KHR wcaps{VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_PRESENT_WAIT_2_KHR,
                                                           nullptr, VK_FALSE};
                VkPresentTimingSurfaceCapabilitiesEXT tcaps{
                    VK_STRUCTURE_TYPE_PRESENT_TIMING_SURFACE_CAPABILITIES_EXT};
                tcaps.pNext = d->present_wait2 ? &wcaps : nullptr;
                VkSurfaceCapabilitiesPresentId2KHR idcaps{VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_PRESENT_ID_2_KHR,
                                                          d->present_timing ? static_cast<void*>(&tcaps)
                                                          : d->present_wait2 ? static_cast<void*>(&wcaps)
                                                                             : nullptr,
                                                          VK_FALSE};
                VkSurfaceCapabilities2KHR c2caps{VK_STRUCTURE_TYPE_SURFACE_CAPABILITIES_2_KHR, &idcaps, {}};
                VkPhysicalDeviceSurfaceInfo2KHR si{VK_STRUCTURE_TYPE_PHYSICAL_DEVICE_SURFACE_INFO_2_KHR, nullptr,
                                                   ci->surface};
                if (d->instance->t.GetPhysicalDeviceSurfaceCapabilities2KHR(d->physical, &si, &c2caps) ==
                    VK_SUCCESS) {
                    ids = idcaps.presentId2Supported;
                    timing = ids && d->present_timing && config().timing && tcaps.presentTimingSupported &&
                             d->t.SetSwapchainPresentTimingQueueSizeEXT && d->t.GetPastPresentationTimingEXT;
                    stages = tcaps.presentStageQueries;
                    wait2 = ids && d->present_wait2 && wcaps.presentWait2Supported && d->t.WaitForPresent2KHR;
                }
            }
            if (ids) c2.flags |= VK_SWAPCHAIN_CREATE_PRESENT_ID_2_BIT_KHR;
            if (wait2) c2.flags |= VK_SWAPCHAIN_CREATE_PRESENT_WAIT_2_BIT_KHR;
            if (timing) c2.flags |= VK_SWAPCHAIN_CREATE_PRESENT_TIMING_BIT_EXT;
        }
    }

    VkResult r = d->t.CreateSwapchainKHR(device, &c2, alloc, out);
    if (r == VK_SUCCESS && frames && ci->oldSwapchain && config().test_create_fail) {
        // test knob (BUTTEREYE_FRAMES_TEST_CREATE_FAIL): the old swapchain is retired now, as after a failure
        d->t.DestroySwapchainKHR(device, *out, alloc);
        *out = VK_NULL_HANDLE;
        r = VK_ERROR_OUT_OF_DEVICE_MEMORY;
    }
    if (r != VK_SUCCESS && frames) {
        logf("swapchain: create with %u images and the layer's usage failed (%d), retrying as the app asked",
             c2.minImageCount, r);
        frames = ids = timing = wait2 = false;
        why_not = "create failed";
        // the failed create retired the app's old swapchain all the same (vkCreateSwapchainKHR),
        // and a retired one must not be named again: the retry replaces nothing
        VkSwapchainCreateInfoKHR c3 = *ci;
        c3.oldSwapchain = VK_NULL_HANDLE;
        r = d->t.CreateSwapchainKHR(device, &c3, alloc, out);
    }
    uint32_t nimages = 0;
    Swapchain* s = nullptr;
    if (r == VK_SUCCESS) {
        s = new_swapchain(device, d);
        s->handle = *out;
        d->t.GetSwapchainImagesKHR(device, *out, &nimages, nullptr);
        s->images.resize(nimages);
        d->t.GetSwapchainImagesKHR(device, *out, &nimages, s->images.data());
        s->app_acquired.reset(new std::atomic<uint64_t>[nimages]);
        for (uint32_t x = 0; x < nimages; ++x) s->app_acquired[x] = 0;
        s->frames = frames;
        s->extra = frames ? extra : 0;
        s->extent = ci->imageExtent;
        s->format = ci->imageFormat;
        s->colour_space = ci->imageColorSpace;
        s->array_layers = ci->imageArrayLayers;
        s->maint1 = d->swapchain_maint1;
        s->ids = ids;
        s->wait2 = wait2;
        if (timing) {
            // the stage to ask for: first pixel out, else the latest one offered
            VkPresentStageFlagsEXT stage = 0;
            if (stages & VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT)
                stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_OUT_BIT_EXT;
            else if (stages & VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT)
                stage = VK_PRESENT_STAGE_IMAGE_FIRST_PIXEL_VISIBLE_BIT_EXT;
            else if (stages & VK_PRESENT_STAGE_REQUEST_DEQUEUED_BIT_EXT)
                stage = VK_PRESENT_STAGE_REQUEST_DEQUEUED_BIT_EXT;
            else if (stages & VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT)
                stage = VK_PRESENT_STAGE_QUEUE_OPERATIONS_END_BIT_EXT;
            uint32_t nd = 0;
            uint64_t counter = 0;
            VkSwapchainTimeDomainPropertiesEXT dp{VK_STRUCTURE_TYPE_SWAPCHAIN_TIME_DOMAIN_PROPERTIES_EXT};
            std::vector<VkTimeDomainKHR> doms;
            std::vector<uint64_t> dom_ids;
            if (stage && d->t.GetSwapchainTimeDomainPropertiesEXT &&
                d->t.GetSwapchainTimeDomainPropertiesEXT(device, *out, &dp, &counter) == VK_SUCCESS &&
                (nd = dp.timeDomainCount) > 0) {
                doms.resize(nd);
                dom_ids.resize(nd);
                dp.pTimeDomains = doms.data();
                dp.pTimeDomainIds = dom_ids.data();
                if (d->t.GetSwapchainTimeDomainPropertiesEXT(device, *out, &dp, &counter) != VK_SUCCESS) nd = 0;
                else nd = dp.timeDomainCount;
            }
            std::string dl;
            int pick = -1;
            for (uint32_t i = 0; i < nd; ++i) {
                dl += (dl.empty() ? "" : " ") + std::string(domain_name(doms[i])) + "#" +
                      std::to_string(dom_ids[i]);
                if (doms[i] == VK_TIME_DOMAIN_CLOCK_MONOTONIC_KHR && pick < 0) pick = static_cast<int>(i);
            }
            if (pick < 0 && nd) pick = 0;
            if (pick >= 0 &&
                d->t.SetSwapchainPresentTimingQueueSizeEXT(device, *out, kTimingQueue) == VK_SUCCESS) {
                s->timing = true;
                s->stage = stage;
                s->domain = doms[static_cast<size_t>(pick)];
                s->domain_id = dom_ids[static_cast<size_t>(pick)];
            }
            logf("present timing: stages offered 0x%x, asked 0x%x; time domains %s; using %s", stages, stage,
                 dl.empty() ? "none" : dl.c_str(), s->timing ? domain_name(s->domain) : "nothing (timing off)");
        }
        std::unique_lock<std::mutex> g(g_sc_lock);
        s->ordinal = ++g_sc_count;
        g.unlock();
        if (frames) {
            const ShmControl ctl = shm_read();
            const int mode = ctl.present && ctl.mode >= 0 ? ctl.mode : config().mode;
            start_sync(s, (mode == kModeBlend || mode == kModeMc) && !d->own_queue);
        }
        g.lock();
        g_sc[h64(*out)] = s;
    }
    logf("swapchain %u on device %u: %s %ux%u, format %s (%d), colour space %s (%d), min images %u"
         " (got %u), mode %s, usage %s, flags 0x%x, alpha 0x%x, transform 0x%x, clipped %u,"
         " replaces an old one %s, chain %s -> %d",
         s ? s->ordinal : 0, d->ordinal, r == VK_SUCCESS ? "created" : "FAILED", ci->imageExtent.width,
         ci->imageExtent.height, format_name(ci->imageFormat), ci->imageFormat,
         colorspace_name(ci->imageColorSpace), ci->imageColorSpace, ci->minImageCount, nimages,
         mode_name(ci->presentMode), usage_names(ci->imageUsage).c_str(), ci->flags,
         ci->compositeAlpha, ci->preTransform, ci->clipped, ci->oldSwapchain ? "yes" : "no",
         chain_types(ci->pNext).c_str(), r);
    if (s && kmax >= 2)
        logf("swapchain %u: k path %s%s; %u images added, present ids %s, timing %s, present wait %s,"
             " depth limit %d, hold %d",
             s->ordinal, frames ? "ready" : "off: ", frames ? "" : why_not.c_str(), s->extra, s->ids ? "on" : "off",
             s->timing ? "on" : "off", s->wait2 ? "on" : "off", s->wait2 ? config().depth : 0,
             s->wait2 ? config().hold : 0);
    return r;
}

VKAPI_ATTR void VKAPI_CALL DestroySwapchainKHR(VkDevice device, VkSwapchainKHR swapchain,
                                               const VkAllocationCallbacks* alloc) {
    Device* d = find_device(key(device));
    if (!d) return;
    Swapchain* s = nullptr;
    if (swapchain) {
        std::lock_guard<std::mutex> g(g_sc_lock);
        auto it = g_sc.find(h64(swapchain));
        if (it != g_sc.end()) {
            s = it->second;
            g_sc.erase(it);
        }
    }
    if (s) {
        std::lock_guard<std::mutex> g(s->m);
        drain(s);
        collect_timing(s);
    }
    d->t.DestroySwapchainKHR(device, swapchain, alloc);
    if (s) {
        free_objects(s);
        summarize(s);
        delete s;
        log_flush();
    }
}

void device_gone(VkDevice device) {
    std::vector<Swapchain*> left;
    {
        std::lock_guard<std::mutex> g(g_sc_lock);
        for (auto it = g_sc.begin(); it != g_sc.end();) {
            if (it->second->device == device) {
                left.push_back(it->second);
                it = g_sc.erase(it);
            } else {
                ++it;
            }
        }
    }
    for (Swapchain* s : left) {
        drain(s);
        free_objects(s);
        summarize(s);
        delete s;
    }
}

namespace {

// one present to the next layer, timed; adds a P row and the S1 stats
VkResult present_down(Swapchain* s, VkQueue queue, const VkPresentInfoKHR* pi, uint64_t t_entry, char kind,
                      uint64_t id, uint64_t acquire_ns, int reason, uint64_t& own_start) {
    const uint64_t t1 = now_ns();
    const VkResult r = s->d->t.QueuePresentKHR(queue, pi);
    const uint64_t t2 = now_ns();
    const VkResult ri = pi->pResults ? pi->pResults[0] : r;
    if (s->n > 0) s->gap_ns.push_back(clamp32(t1 - s->last_ns));
    else s->first_ns = t1;
    s->last_ns = t1;
    ++s->n;
    if (ri != VK_SUCCESS) ++s->not_success;
    s->next_ns.push_back(clamp32(t2 - t1));
    const uint32_t own = clamp32(t1 - own_start);
    s->own_ns.push_back(own);
    uint64_t since_acquire = 0;
    // the app's presents only ('p', 'a'): the added ones ('d', 'g', 'm') show images the layer
    // acquired itself, whose app acquire time is from some earlier frame
    if (kind == 'p' || kind == 'a') {
        const uint64_t a = s->app_acquired[pi->pImageIndices[0]].load(std::memory_order_relaxed);
        if (a && a < t_entry) since_acquire = t_entry - a;
        if (id && a) s->app_acq_ids[id] = a;
    }
    log_row("P,%" PRIu64 ",%u,%u,%c,%" PRIu64 ",%u,%u,%u,%d,%d,%u\n", t_entry, s->ordinal, pi->pImageIndices[0],
            kind, id, clamp32(t2 - t1), own, clamp32(acquire_ns), ri, reason, clamp32(since_acquire));
    own_start = now_ns();
    return r;
}

// the app's present unchanged, plus the layer's present id and timing when on
VkResult present_app(Swapchain* s, VkQueue queue, const VkPresentInfoKHR* info, uint64_t t0, int reason,
                     uint64_t& own_start) {
    ++s->reasons[reason];
    PresentExtras ex;
    VkPresentInfoKHR pi = *info;
    if (reason != kAppIds) pi.pNext = ex.attach(s, info->pNext, want_timing(s));  // s->ids is off with app ids
    if (ex.id) s->app_ids[ex.id] = t0;
    return present_down(s, queue, &pi, t0, 'p', ex.id, 0, reason, own_start);
}

VkResult worse(VkResult a, VkResult b) {
    if (a < 0) return a;
    if (b < 0 || b == VK_SUBOPTIMAL_KHR) return a == VK_SUCCESS ? VK_SUBOPTIMAL_KHR : a;
    return a;
}

void barrier(VkImageMemoryBarrier& b, VkImage image, VkImageLayout from, VkImageLayout to, VkAccessFlags src,
             VkAccessFlags dst) {
    b = {VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    b.srcAccessMask = src;
    b.dstAccessMask = dst;
    b.oldLayout = from;
    b.newLayout = to;
    b.srcQueueFamilyIndex = b.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    b.image = image;
    b.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
}

bool same_place(const McSettings& a, const McSettings& b) {
    return a.rect.x == b.rect.x && a.rect.y == b.rect.y && a.rect.w == b.rect.w && a.rect.h == b.rect.h &&
           a.source_h == b.source_h && a.pq == b.pq;
}

// starts making an engine for `want` on a worker thread
void mc_start(Swapchain* s, const McSettings& want, uint32_t family) {
    auto job = std::make_unique<Swapchain::McJob>();
    job->set = want;
    job->mc = std::make_unique<Mc>();
    Swapchain::McJob* j = job.get();
    const Blend* b = &s->blend;
    s->mc_job = std::move(job);
    j->th = std::thread([j, b, family] {
        const uint64_t t0 = now_ns();
        j->ok = mc_create(*j->mc, *b, j->set, family, kSlots, &j->why);
        j->ms = static_cast<double>(now_ns() - t0) / 1e6;
        j->done.store(true, std::memory_order_release);
    });
}

// a finished engine (or, with wait, the one being made) goes into use
void mc_poll(Swapchain* s, bool wait) {
    Swapchain::McJob* j = s->mc_job.get();
    if (!j || (!wait && !j->done.load(std::memory_order_acquire))) return;
    if (j->th.joinable()) j->th.join();
    if (j->ok) {
        if (s->mc_ready) {
            // nothing of the old engine's may still run: while some of its work is not done,
            // the old engine stays in use and the new one waits for a later present
            if (!drain(s)) {
                if (!j->drain_failed)
                    logf("swapchain %u: the old engine's work is not done after 1 s; the new engine waits",
                         s->ordinal);
                j->drain_failed = true;
                return;
            }
            mc_destroy(*s->mc);
        }
        s->mc = std::move(j->mc);
        s->mc_set = j->set;
        s->mc_ready = true;
        s->mc_tried_failed = false;
        s->mc_make_ms = j->ms;
        ++s->mc_jobs;
        blend_forget(s->blend);  // the engine's run starts again
        const Mc& m = *s->mc;
        s->mc_rect = m.rect;
        s->mc_sw = m.sw;
        s->mc_sh = m.sh;
        logf("swapchain %u: mc ready in %.1f ms (%s): engine %dx%d over the rectangle %d,%d %dx%d (%s), %.1f MB"
             " of engine buffers, GPU timestamps %s, %s",
             s->ordinal, j->ms, config().mc_thread ? "worker thread" : "inside the present", m.sw, m.sh, m.rect.x,
             m.rect.y, m.rect.w, m.rect.h, m.set.pq ? "PQ, BT.2020 luma" : "SDR, BT.709 luma",
             static_cast<double>(m.eng->memory_bytes()) / 1048576.0, m.queries ? "on" : "off",
             s->d->own_queue ? "own queue" : config().split ? "split submits" : "one submit");
    } else {
        logf("swapchain %u: no mc (%s); the added presents stay %s", s->ordinal, j->why.c_str(),
             s->mc_ready ? "the old engine's" : "blends");
        s->mc_tried = j->set;
        s->mc_tried_failed = true;
        if (!s->mc_ready) s->mc_failed = true;
    }
    s->mc_job.reset();
}

// layout changes of the app's image and the targets around the layer's copies
void app_to(const DeviceTable& t, VkCommandBuffer cb, VkImage app, bool to_src) {
    VkImageMemoryBarrier b;
    if (to_src) {
        barrier(b, app, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, 0,
                VK_ACCESS_TRANSFER_READ_BIT);
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                             nullptr, 1, &b);
    } else {
        barrier(b, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, 0, 0);
        t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, nullptr,
                             0, nullptr, 1, &b);
    }
}
void targets_to(const DeviceTable& t, VkCommandBuffer cb, const VkImage* img, uint32_t n, bool to_dst) {
    VkImageMemoryBarrier b[kKLimit];
    for (uint32_t m = 0; m < n; ++m) {
        if (to_dst)
            barrier(b[m], img[m], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                    VK_ACCESS_TRANSFER_WRITE_BIT);
        else
            barrier(b[m], img[m], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
                    VK_ACCESS_TRANSFER_WRITE_BIT, 0);
    }
    t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT,
                         to_dst ? VK_PIPELINE_STAGE_TRANSFER_BIT : VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0, nullptr,
                         0, nullptr, n, b);
}

// S4: records and submits one app present's mc work (see the top of this file). The app's
// semaphores are consumed and ready[i], ready[j_m] signalled whenever this returns VK_SUCCESS;
// *partial is set when some of it was submitted before a later submit failed.
VkResult submit_mc(Swapchain* s, Slot& sl, VkQueue queue, const VkPresentInfoKHR* info, const uint32_t* j,
                   uint32_t got, uint32_t k, bool* made_out, bool* partial) {
    const DeviceTable& t = s->d->t;
    Mc& mc = *s->mc;
    Blend& b = s->blend;
    const uint32_t i = info->pImageIndices[0];
    const uint32_t qslot = static_cast<uint32_t>(&sl - s->slots);
    const bool own = s->d->own_queue != VK_NULL_HANDLE;
    const bool split = own || config().split;
    const bool show = config().show;
    const VkImage app = s->images[i];
    VkImage targets[kKLimit];
    for (uint32_t m = 0; m < got; ++m) targets[m] = s->images[j[m]];
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
    bool ok = true;
    auto begin = [&](VkCommandBuffer cb) {
        ok = ok && t.ResetCommandBuffer(cb, 0) == VK_SUCCESS && t.BeginCommandBuffer(cb, &bi) == VK_SUCCESS;
        return ok;
    };
    auto end = [&](VkCommandBuffer cb) { ok = ok && t.EndCommandBuffer(cb) == VK_SUCCESS; };
    auto copies = [&](VkCommandBuffer cb) {
        VkImageCopy r{};
        r.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
        r.dstSubresource = r.srcSubresource;
        r.extent = {s->extent.width, s->extent.height, 1};
        for (uint32_t m = 0; m < got; ++m)
            t.CmdCopyImage(cb, app, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, targets[m],
                           VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &r);
    };
    sl.mc = true;
    sl.mc_split = split;
    sl.mc_own = own;
    sl.got = got;
    *partial = false;
    const VkPipelineStageFlags tr = VK_PIPELINE_STAGE_TRANSFER_BIT;
    const std::vector<VkSemaphore> app_waits(info->pWaitSemaphores, info->pWaitSemaphores + info->waitSemaphoreCount);
    bool made = false;
    VkResult r = VK_SUCCESS;

    if (!split) {
        // S4c: everything in one submit
        const VkCommandBuffer cb = sl.cb;
        if (begin(cb)) {
            app_to(t, cb, app, true);
            targets_to(t, cb, targets, got, true);
            made = mc_record(mc, b, cb, app, targets, got, k, show, qslot);
            app_to(t, cb, app, false);
            targets_to(t, cb, targets, got, false);
            end(cb);
        }
        std::vector<VkSemaphore> waits = app_waits;
        for (uint32_t m = 0; m < got; ++m) waits.push_back(sl.acquired[m]);
        const std::vector<VkPipelineStageFlags> st(waits.size(), tr);
        VkSemaphore sig[kKLimit + 1];
        for (uint32_t m = 0; m < got; ++m) sig[m] = s->ready[j[m]];
        sig[got] = s->ready[i];
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, static_cast<uint32_t>(waits.size()), waits.data(),
                        st.data(), 1, &cb, got + 1, sig};
        r = ok ? t.QueueSubmit(queue, 1, &si, sl.done) : VK_ERROR_UNKNOWN;
    } else if (!own) {
        // split on the app's queue: the pair's batch, then one batch per frame, in one call
        const VkCommandBuffer cb0 = sl.cb;
        if (begin(cb0)) {
            app_to(t, cb0, app, true);
            made = mc_record_pair(mc, b, cb0, app, got, qslot);
            if (!made) {
                targets_to(t, cb0, targets, got, true);
                copies(cb0);
                targets_to(t, cb0, targets, got, false);
            }
            app_to(t, cb0, app, false);
            end(cb0);
        }
        if (made)
            for (uint32_t m = 1; m <= got; ++m) {
                const VkCommandBuffer cb = sl.frame_cb[m - 1];
                if (!begin(cb)) break;
                targets_to(t, cb, &targets[m - 1], 1, true);
                mc_record_frame(mc, b, cb, m, k, targets[m - 1], show, qslot);
                targets_to(t, cb, &targets[m - 1], 1, false);
                end(cb);
            }
        mc_record_done(mc, qslot, made ? got : 0);
        std::vector<VkSemaphore> w0 = app_waits;
        VkSemaphore sig0[kKLimit + 1];
        uint32_t nsig0 = 0;
        sig0[nsig0++] = s->ready[i];
        if (!made)
            for (uint32_t m = 0; m < got; ++m) {
                w0.push_back(sl.acquired[m]);
                sig0[nsig0++] = s->ready[j[m]];
            }
        const std::vector<VkPipelineStageFlags> st0(w0.size(), tr);
        VkSubmitInfo si[kKLimit];
        si[0] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, static_cast<uint32_t>(w0.size()), w0.data(), st0.data(), 1,
                 &cb0, nsig0, sig0};
        uint32_t n = 1;
        if (made)
            for (uint32_t m = 1; m <= got; ++m, ++n)
                si[n] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 1, &sl.acquired[m - 1], &tr, 1, &sl.frame_cb[m - 1],
                         1, &s->ready[j[m - 1]]};
        r = ok ? t.QueueSubmit(queue, n, si, sl.done) : VK_ERROR_UNKNOWN;
    } else {
        // own queue: copy in on the app's queue (A), the compute work on the own queue (B), the
        // copies out on the app's queue (C); C's fence covers all three (C waits on B's last signal)
        const VkCommandBuffer ca = sl.cb;
        if (begin(ca)) {
            app_to(t, ca, app, true);
            mc_record_copy_in(mc, b, ca, app, qslot);
            made = mc.slot >= 0 && b.have == 2 && got > 0;
            if (!made) {
                targets_to(t, ca, targets, got, true);
                copies(ca);
                targets_to(t, ca, targets, got, false);
            }
            app_to(t, ca, app, false);
            end(ca);
        }
        if (begin(sl.own_cb[0])) {
            const bool made2 = mc_record_pair(mc, b, sl.own_cb[0], app, got, qslot, false);
            if (made2 != made) ok = false;  // cannot happen: both read the same state
            end(sl.own_cb[0]);
        }
        if (made)
            for (uint32_t m = 1; m <= got; ++m) {
                if (begin(sl.own_cb[m])) {
                    mc_record_frame(mc, b, sl.own_cb[m], m, k, VK_NULL_HANDLE, show, qslot, true, false);
                    end(sl.own_cb[m]);
                }
                const VkCommandBuffer cb = sl.frame_cb[m - 1];
                if (begin(cb)) {
                    targets_to(t, cb, &targets[m - 1], 1, true);
                    mc_record_frame(mc, b, cb, m, k, targets[m - 1], show, qslot, false, true);
                    targets_to(t, cb, &targets[m - 1], 1, false);
                    end(cb);
                }
            }
        mc_record_done(mc, qslot, made ? got : 0);
        if (!ok) return VK_ERROR_UNKNOWN;
        // A
        std::vector<VkSemaphore> wa = app_waits;
        VkSemaphore sa[kKLimit + 2];
        uint32_t nsa = 0;
        sa[nsa++] = s->ready[i];
        sa[nsa++] = sl.own_in;
        if (!made)
            for (uint32_t m = 0; m < got; ++m) {
                wa.push_back(sl.acquired[m]);
                sa[nsa++] = s->ready[j[m]];
            }
        const std::vector<VkPipelineStageFlags> sta(wa.size(), tr);
        VkSubmitInfo a{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, static_cast<uint32_t>(wa.size()), wa.data(), sta.data(),
                       1, &ca, nsa, sa};
        r = t.QueueSubmit(queue, 1, &a, VK_NULL_HANDLE);
        if (r != VK_SUCCESS) return r;
        *partial = true;
        // B
        const VkPipelineStageFlags work = VK_PIPELINE_STAGE_TRANSFER_BIT | VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT;
        VkSubmitInfo bs[kKLimit];
        bs[0] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 1, &sl.own_in, &work, 1, &sl.own_cb[0], made ? 0u : 1u,
                 &sl.own_out[0]};
        uint32_t nb = 1;
        if (made)
            for (uint32_t m = 1; m <= got; ++m, ++nb)
                bs[nb] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 0, nullptr, nullptr, 1, &sl.own_cb[m], 1,
                          &sl.own_out[m - 1]};
        {
            std::lock_guard<std::mutex> g(s->d->own_lock);
            r = t.QueueSubmit(s->d->own_queue, nb, bs, VK_NULL_HANDLE);
        }
        if (r != VK_SUCCESS) return r;
        // C
        VkSubmitInfo cs[kKLimit];
        VkSemaphore cw[kKLimit][2];
        const VkPipelineStageFlags cst[2] = {tr, tr};
        const VkPipelineStageFlags all = VK_PIPELINE_STAGE_ALL_COMMANDS_BIT;
        uint32_t nc = 0;
        if (made) {
            for (uint32_t m = 1; m <= got; ++m, ++nc) {
                cw[nc][0] = sl.own_out[m - 1];
                cw[nc][1] = sl.acquired[m - 1];
                cs[nc] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 2, cw[nc], cst, 1, &sl.frame_cb[m - 1], 1,
                          &s->ready[j[m - 1]]};
            }
        } else {
            cs[nc++] = {VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, 1, &sl.own_out[0], &all, 0, nullptr, 0, nullptr};
        }
        r = t.QueueSubmit(queue, nc, cs, sl.done);
        if (r != VK_SUCCESS) return r;
        *partial = false;
    }
    if (!ok && r == VK_SUCCESS) r = VK_ERROR_UNKNOWN;
    *made_out = made;
    sl.mc_made = made;
    sl.timed = mc.queries != VK_NULL_HANDLE;
    sl.cut_pending = made;
    return r;
}

// the k path; returns false (nothing sent, nothing consumed) when the app present must
// go through unchanged, with the reason in *reason
bool present_frames(Swapchain* s, VkQueue queue, const VkPresentInfoKHR* info, uint32_t k, int mode, uint64_t t0,
                    uint64_t& own_start, int* reason, VkResult* result) {
    const DeviceTable& t = s->d->t;
    VkDevice dev = s->device;
    const uint32_t family = queue_family(queue);
    if (family == UINT32_MAX) {
        *reason = kNoQueue;
        return false;
    }
    const uint64_t tf0 = now_ns();
    const bool first_k = s->pool == VK_NULL_HANDLE;  // the first k path present: its set-up is timed
    bool blend_here = false;
    if (!ensure_objects(s, family)) {
        logf("swapchain %u: could not make the layer's objects; pass-through from now on", s->ordinal);
        s->failed = true;
        *reason = kFailed;
        return false;
    }
    const uint64_t tf1 = now_ns();
    Slot& sl = s->slots[s->slot_next];
    if (sl.submitted) {
        if (t.WaitForFences(dev, 1, &sl.done, VK_TRUE, 50000000ull) != VK_SUCCESS) {
            *reason = kSlotBusy;
            return false;
        }
        collect_gpu(s, sl);
        t.ResetFences(dev, 1, &sl.done);
        sl.submitted = false;
    }
    // a change of mode: the history (and the engine's run) start again
    if (mode != s->last_mode && s->blend_ready) blend_forget(s->blend);
    s->last_mode = mode;
    // S3: the blend's objects, made on the first present that asks for it (mc uses them too)
    bool use_blend = false;
    if ((mode == kModeBlend || mode == kModeMc) && !s->blend_failed) {
        if (!s->blend_ready) {
            std::string why;
            const uint64_t tb = now_ns();
            if (s->array_layers != 1) {
                why = "the swapchain has several array layers";
            } else if (blend_create(s->blend, s->d, dev, s->extent, s->format,
                                    static_cast<uint32_t>(config().kmax - 1), family, kSlots, &why,
                                    s->d->own_queue ? s->d->own_family : UINT32_MAX)) {
                s->blend_ready = true;
                blend_here = true;
                s->blend_queue = queue;
                logf("swapchain %u: blend ready in %.1f ms: %ux%u, format %s, %u bits a channel, red %s, %u in-between"
                     " layers, %.1f MB of images%s, GPU timestamps %s",
                     s->ordinal, (now_ns() - tb) / 1e6, s->extent.width, s->extent.height, format_name(s->format),
                     s->blend.fl.bits, s->blend.fl.red_low ? "low" : "high", s->blend.layers,
                     s->blend.bytes / 1048576.0, s->blend.nshare == 2 ? " (shared with the own queue's family)" : "",
                     s->blend.queries ? "on" : "off");
            }
            if (!s->blend_ready) {
                s->blend_failed = true;
                logf("swapchain %u: no blend (%s); the added presents stay copies", s->ordinal, why.c_str());
            }
        }
        if (s->blend_ready) {
            use_blend = true;
            if (queue != s->blend_queue) {
                // the history was written on another queue: nothing orders it with this one
                blend_forget(s->blend);
                s->blend_queue = queue;
            }
        }
    } else if (s->blend_ready) {
        blend_forget(s->blend);  // duplicates for now: the history goes stale
    }
    // S4: the engine. It is made on a worker thread (BUTTEREYE_FRAMES_MC_THREAD=0: inside this
    // present, as in S4c) when a present first asks for mc, and again when the video rectangle or
    // the source height in the control block changes; until it is ready the added presents stay
    // blends, or the old engine's frames.
    bool use_mc = false;
    if (use_blend && mode == kModeMc && !s->mc_failed) {
        McSettings want = mc_settings();
        want.pq = s->colour_space == VK_COLOR_SPACE_HDR10_ST2084_EXT;
        const ShmControl ctl = shm_read();
        if (ctl.present && ctl.rect_w > 0 && ctl.rect_h > 0) want.rect = {ctl.rect_x, ctl.rect_y, ctl.rect_w, ctl.rect_h};
        if (ctl.present && ctl.source_h > 0) want.source_h = ctl.source_h;
        mc_poll(s, false);
        const bool same = s->mc_ready && same_place(want, s->mc_set);
        const bool tried = s->mc_tried_failed && same_place(want, s->mc_tried);
        if (!same && !tried && !s->mc_job) {
            mc_start(s, want, s->d->own_queue ? s->d->own_family : family);
            if (!config().mc_thread) mc_poll(s, true);
        }
        use_mc = s->mc_ready;
    }
    if (sl.presented_used) {
        if (t.WaitForFences(dev, sl.presented_used, sl.presented, VK_TRUE, 50000000ull) != VK_SUCCESS) {
            *reason = kSlotBusy;
            return false;
        }
        t.ResetFences(dev, sl.presented_used, sl.presented);
        sl.presented_used = 0;
    }

    // 0. depth limit: wait until few enough presents are left waiting for the screen
    const uint64_t tw = now_ns();
    if (config().depth > 0 && s->wait2 && s->next_id + k > static_cast<uint64_t>(config().depth)) {
        const uint64_t target = s->next_id + k - static_cast<uint64_t>(config().depth);
        const uint64_t limit = s->refresh_ns ? s->refresh_ns * 3 : 25000000ull;
        VkPresentWait2InfoKHR wi{VK_STRUCTURE_TYPE_PRESENT_WAIT_2_INFO_KHR, nullptr, target, limit};
        const VkResult wr = t.WaitForPresent2KHR(dev, s->handle, &wi);
        const uint64_t tr = now_ns();
        if (wr == VK_TIMEOUT) ++s->depth_timeouts;
        s->depth_wait_ns.push_back(clamp32(tr - tw));
        log_row("W,%u,d,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%d\n", s->ordinal, target, tw, tr, wr);
    }

    // 1. the extra images
    const uint64_t timeout = config().acquire_us ? config().acquire_us * 1000ull
                             : s->refresh_ns   ? s->refresh_ns * 5 / 2
                                               : 20000000ull;
    const uint64_t ta = now_ns();
    uint32_t j[kKLimit];
    uint32_t got = 0;
    VkResult acquire_result = VK_SUCCESS;
    for (uint32_t m = 0; m + 1 < k; ++m) {
        const VkResult r = t.AcquireNextImageKHR(dev, s->handle, timeout, sl.acquired[m], VK_NULL_HANDLE, &j[m]);
        if (r == VK_SUCCESS || r == VK_SUBOPTIMAL_KHR) {
            ++got;
            if (r == VK_SUBOPTIMAL_KHR) acquire_result = r;
        } else {
            acquire_result = r;
            break;
        }
    }
    const uint64_t acquire_ns = now_ns() - ta;
    s->acquire_ns.push_back(clamp32(acquire_ns));
    own_start += now_ns() - tw;  // the layer's own time leaves the wait out
    if (got == 0) {
        *reason = kAcquire;
        return false;
    }
    if (got + 1 < k) ++s->acquire_short;

    // 2. the submit(s): copy i into every j_m, or the blend's or the engine's frames
    const uint64_t tf2 = now_ns();
    const uint32_t i = info->pImageIndices[0];
    VkResult sr = VK_SUCCESS;
    bool partial = false, mc_made_now = false;
    sl.blended = sl.timed = sl.mc = sl.mc_made = sl.cut_pending = false;
    sl.t_entry = t0;
    if (use_mc) {
        sr = submit_mc(s, sl, queue, info, j, got, k, &mc_made_now, &partial);
    } else {
        const VkCommandBuffer cb = sl.cb;
        VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO, nullptr,
                                    VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT, nullptr};
        bool ok = t.ResetCommandBuffer(cb, 0) == VK_SUCCESS && t.BeginCommandBuffer(cb, &bi) == VK_SUCCESS;
        if (ok) {
            VkImageMemoryBarrier b[kKLimit + 1];
            barrier(b[0], s->images[i], VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, 0,
                    VK_ACCESS_TRANSFER_READ_BIT);
            for (uint32_t m = 0; m < got; ++m)
                barrier(b[m + 1], s->images[j[m]], VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 0,
                        VK_ACCESS_TRANSFER_WRITE_BIT);
            t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr, 0,
                                 nullptr, got + 1, b);
            if (use_blend) {
                VkImage targets[kKLimit];
                for (uint32_t m = 0; m < got; ++m) targets[m] = s->images[j[m]];
                const uint32_t qslot = static_cast<uint32_t>(&sl - s->slots);
                sl.blended = blend_record(s->blend, cb, s->images[i], targets, got, k, config().show, qslot);
                sl.timed = s->blend.queries != VK_NULL_HANDLE;
                sl.got = got;
                sl.t_entry = t0;
            } else {
                VkImageCopy region{};
                region.srcSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
                region.dstSubresource = region.srcSubresource;
                region.extent = {s->extent.width, s->extent.height, 1};
                for (uint32_t m = 0; m < got; ++m)
                    t.CmdCopyImage(cb, s->images[i], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, s->images[j[m]],
                                   VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);
            }
            barrier(b[0], s->images[i], VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR, 0, 0);
            for (uint32_t m = 0; m < got; ++m)
                barrier(b[m + 1], s->images[j[m]], VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, VK_IMAGE_LAYOUT_PRESENT_SRC_KHR,
                        VK_ACCESS_TRANSFER_WRITE_BIT, 0);
            t.CmdPipelineBarrier(cb, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT, 0, 0,
                                 nullptr, 0, nullptr, got + 1, b);
            ok = t.EndCommandBuffer(cb) == VK_SUCCESS;
        }
        // the app's semaphores and the acquire semaphores, all waited before the copies
        std::vector<VkSemaphore> waits(info->pWaitSemaphores, info->pWaitSemaphores + info->waitSemaphoreCount);
        for (uint32_t m = 0; m < got; ++m) waits.push_back(sl.acquired[m]);
        const std::vector<VkPipelineStageFlags> stages(waits.size(), VK_PIPELINE_STAGE_TRANSFER_BIT);
        VkSemaphore signals[kKLimit + 1];
        for (uint32_t m = 0; m < got; ++m) signals[m] = s->ready[j[m]];
        signals[got] = s->ready[i];
        VkSubmitInfo si{VK_STRUCTURE_TYPE_SUBMIT_INFO, nullptr, static_cast<uint32_t>(waits.size()), waits.data(),
                        stages.data(), 1, &cb, got + 1, signals};
        sr = ok ? t.QueueSubmit(queue, 1, &si, sl.done) : VK_ERROR_UNKNOWN;
    }
    if (sr != VK_SUCCESS) {
        // nothing of the submit happened; the acquired images stay with the layer, so the
        // swapchain is left to the app from now on (libplacebo recreates on errors). After a
        // partial own-queue submit the app's semaphores are spent: the error goes to the app.
        logf("swapchain %u: the copy submit failed (%d%s); pass-through from now on", s->ordinal, sr,
             partial ? ", partly submitted" : "");
        s->failed = true;
        sl.timed = sl.cut_pending = false;
        if (s->blend_ready) blend_forget(s->blend);
        if (sr == VK_ERROR_DEVICE_LOST || partial) {
            *result = sr;
            return true;
        }
        *reason = kFailed;
        return false;
    }
    if (first_k)
        logf("swapchain %u: the first k path present: objects %.1f ms, set-up to the acquire %.1f ms (the blend %s),"
             " acquire %.1f ms, record and submit %.1f ms",
             s->ordinal, (tf1 - tf0) / 1e6, (ta - tf1) / 1e6, blend_here ? "made here" : s->blend_ready ? "made before" : "none",
             acquire_ns / 1e6, (now_ns() - tf2) / 1e6);
    sl.submitted = true;
    s->slot_next = (s->slot_next + 1) % kSlots;
    const bool blended = use_blend && !use_mc && sl.blended;
    if (use_blend && !use_mc) ++(blended ? s->blended : s->blend_first);
    const bool made = use_mc && mc_made_now;
    if (use_mc) ++(made ? s->mc_made : s->mc_first);

    // 3. the copies, then the app's image
    const auto* mode_info = reinterpret_cast<const VkSwapchainPresentModeInfoKHR*>(
        find_in(info->pNext, VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_MODE_INFO_KHR));
    VkResult res = VK_SUCCESS;
    for (uint32_t m = 0; m < got; ++m) {
        const void* chain = nullptr;
        VkSwapchainPresentModeInfoKHR mi{};
        if (mode_info) {
            mi = *mode_info;
            mi.pNext = chain;
            chain = &mi;
        }
        VkSwapchainPresentFenceInfoKHR fi{VK_STRUCTURE_TYPE_SWAPCHAIN_PRESENT_FENCE_INFO_KHR, chain, 1,
                                          &sl.presented[m]};
        if (s->maint1) {
            chain = &fi;
            sl.presented_used = m + 1;
        }
        PresentExtras ex;
        chain = ex.attach(s, chain, want_timing(s));
        VkPresentInfoKHR pi{VK_STRUCTURE_TYPE_PRESENT_INFO_KHR, chain, 1, &s->ready[j[m]], 1, &s->handle, &j[m],
                            nullptr};
        const VkResult r = present_down(s, queue, &pi, t0, made ? 'm' : blended ? 'g' : 'd', ex.id, acquire_ns, kFrames, own_start);
        ++s->generated;
        res = worse(res, r);
        if (r == VK_ERROR_DEVICE_LOST) {
            *result = r;
            return true;
        }
    }
    PresentExtras ex;
    VkPresentInfoKHR pi = *info;
    pi.waitSemaphoreCount = 1;
    pi.pWaitSemaphores = &s->ready[i];
    pi.pNext = ex.attach(s, info->pNext, want_timing(s));
    if (ex.id) s->app_ids[ex.id] = t0;
    const VkResult r = present_down(s, queue, &pi, t0, 'a', ex.id, acquire_ns, kFrames, own_start);
    ++s->frames_presents;
    ++s->reasons[kFrames];
    if (config().engine_test && !s->d->engine_tested) {
        s->d->engine_tested = true;
        engine_test(s->d, queue, family);
    }
    // the check dump: the images of one blended present, written before the app goes on
    // (S4: of one present with motion-compensated frames, which are in the same images)
    const uint64_t dump_count = made ? s->mc_made : s->blended;
    if ((blended || made) && !s->dumped && !config().dump_path.empty() &&
        dump_count == static_cast<uint64_t>(config().dump_at)) {
        s->dumped = true;
        const bool ok = blend_dump(s->blend, queue, family, got, k, config().show, s->colour_space, config().dump_path);
        logf("swapchain %u: dump of %s present %" PRIu64 " %s", s->ordinal, made ? "mc" : "blended", dump_count,
             ok ? "written" : "FAILED");
    }
    // hold: return to the app only once at most `hold` presents are left waiting, so
    // that the app draws its next frame just before it is needed
    const uint64_t hold = config().hold < 0 ? got : static_cast<uint64_t>(config().hold);
    if (hold > 0 && s->wait2 && r >= 0 && s->next_id > hold) {
        const uint64_t target = s->next_id - hold;
        const uint64_t limit = s->refresh_ns ? s->refresh_ns * (k + 1) : 40000000ull;
        VkPresentWait2InfoKHR wi{VK_STRUCTURE_TYPE_PRESENT_WAIT_2_INFO_KHR, nullptr, target, limit};
        const uint64_t th = now_ns();
        const VkResult wr = t.WaitForPresent2KHR(dev, s->handle, &wi);
        const uint64_t tr = now_ns();
        if (wr == VK_TIMEOUT) ++s->hold_timeouts;
        s->hold_wait_ns.push_back(clamp32(tr - th));
        log_row("W,%u,h,%" PRIu64 ",%" PRIu64 ",%" PRIu64 ",%d\n", s->ordinal, target, th, tr, wr);
    }
    *result = worse(r, worse(res, acquire_result == VK_SUBOPTIMAL_KHR ? VK_SUBOPTIMAL_KHR : VK_SUCCESS));
    return true;
}

Swapchain* find_swapchain(VkSwapchainKHR h) {
    std::lock_guard<std::mutex> g(g_sc_lock);
    auto it = g_sc.find(h64(h));
    return it == g_sc.end() ? nullptr : it->second;
}

// the S1 path: several swapchains in one call, or a swapchain the layer does not know
VkResult present_many(Device* d, VkQueue queue, const VkPresentInfoKHR* info, uint64_t t0) {
    const uint64_t t1 = now_ns();
    const VkResult r = d->t.QueuePresentKHR(queue, info);
    const uint64_t t2 = now_ns();
    std::lock_guard<std::mutex> g(g_sc_lock);
    for (uint32_t i = 0; i < info->swapchainCount; ++i) {
        auto it = g_sc.find(h64(info->pSwapchains[i]));
        if (it == g_sc.end()) continue;
        Swapchain* s = it->second;
        const VkResult ri = info->pResults ? info->pResults[i] : r;
        if (s->n > 0) s->gap_ns.push_back(clamp32(t0 - s->last_ns));
        else s->first_ns = t0;
        s->last_ns = t0;
        ++s->n;
        ++s->app_presents;
        ++s->reasons[kMulti];
        if (ri != VK_SUCCESS) ++s->not_success;
        s->next_ns.push_back(clamp32(t2 - t1));
        s->own_ns.push_back(clamp32(t1 - t0));
        log_row("P,%" PRIu64 ",%u,%u,p,0,%u,%u,0,%d,%d,0\n", t0, s->ordinal, info->pImageIndices[i],
                clamp32(t2 - t1), clamp32(t1 - t0), ri, static_cast<int>(kMulti));
    }
    return r;
}

}  // namespace

VKAPI_ATTR VkResult VKAPI_CALL AcquireNextImageKHR(VkDevice device, VkSwapchainKHR swapchain, uint64_t timeout,
                                                   VkSemaphore semaphore, VkFence fence, uint32_t* index) {
    Device* d = find_device(key(device));
    if (!d) return VK_ERROR_DEVICE_LOST;
    const VkResult r = d->t.AcquireNextImageKHR(device, swapchain, timeout, semaphore, fence, index);
    if (r == VK_SUCCESS || r == VK_SUBOPTIMAL_KHR) {
        // no swapchain lock: the present may hold it while it waits
        Swapchain* s = find_swapchain(swapchain);
        if (s && *index < s->images.size()) s->app_acquired[*index].store(now_ns(), std::memory_order_relaxed);
    }
    return r;
}

VKAPI_ATTR VkResult VKAPI_CALL QueuePresentKHR(VkQueue queue, const VkPresentInfoKHR* info) {
    const uint64_t t0 = now_ns();
    Device* d = find_device(key(queue));
    if (!d) return VK_ERROR_DEVICE_LOST;  // no way to reach the next layer
    if (!d->hooked) return d->t.QueuePresentKHR(queue, info);
    Swapchain* s = nullptr;
    if (info->swapchainCount == 1) {
        std::lock_guard<std::mutex> g(g_sc_lock);
        auto it = g_sc.find(h64(info->pSwapchains[0]));
        if (it != g_sc.end()) s = it->second;
    }
    if (!s) return present_many(d, queue, info, t0);

    std::lock_guard<std::mutex> g(s->m);
    uint64_t own_start = t0;
    ++s->app_presents;
    if (s->n == 0 || (s->app_presents & 1023) == 0) {
        std::string c = chain_types(info->pNext);
        if (c != s->present_chain) {
            s->present_chain = c;
            logf("swapchain %u: present chain %s, %u wait semaphores, results array %s", s->ordinal, c.c_str(),
                 info->waitSemaphoreCount, info->pResults ? "yes" : "no");
        }
    }
    refresh_duration(s, t0);
    calibrate(s, t0);
    collect_timing(s);

    // k for this present: the control block wins over the environment
    const ShmControl ctl = config().kmax >= 2 ? shm_read() : ShmControl{};
    int kk = config().k;
    if (ctl.present && ctl.k > 0) kk = std::min(ctl.k, config().kmax);
    const int mode = ctl.present && ctl.mode >= 0 ? ctl.mode : config().mode;
    uint32_t k = kk < 1 ? 1u : static_cast<uint32_t>(kk);
    if (k > s->extra + 1) k = s->extra + 1;
    const bool event = ctl.present && s->have_event && ctl.event_seq != s->event_seq;
    if (ctl.present) {
        s->have_event = true;
        s->event_seq = ctl.event_seq;
    }
    const uint64_t gap = s->last_app_ns ? t0 - s->last_app_ns : 0;
    s->last_app_ns = t0;
    const uint32_t k_prev = s->k_last;
    s->k_last = k;

    // the app's own present ids: the layer then adds no id of its own, at any k (two present-id
    // structs in one chain are invalid, and the app's ids must stay its own)
    const bool app_ids = find_in(info->pNext, VK_STRUCTURE_TYPE_PRESENT_ID_2_KHR) ||
                         find_in(info->pNext, VK_STRUCTURE_TYPE_PRESENT_ID_KHR);
    if (app_ids && (s->ids || s->timing)) {
        logf("swapchain %u: the app sends its own present ids; the layer's ids and timing are off", s->ordinal);
        s->timing = s->ids = false;
    }
    int reason = kFrames;
    if (!s->frames || s->failed) reason = kInactive;
    else if (app_ids) reason = kAppIds;
    else if (k <= 1) reason = kOff;
    else if (event) reason = kEvent;
    else if (!gap) reason = kFirst;
    else if (gap * 2 > 3 * expected_gap(s, std::max(k, k_prev)))
        reason = kCadence;

    if (gap) s->app_gap_ns.push_back(clamp32(gap));
    VkResult result = VK_SUCCESS;
    if (reason == kFrames && present_frames(s, queue, info, k, mode, t0, own_start, &reason, &result)) {
        // sent
    } else {
        result = present_app(s, queue, info, t0, reason, own_start);
        if (reason != kInactive) s->k_last = 1;
        // the next blend needs two images in a row (while the sync thread may still be making the
        // blend, its history is empty anyway)
        if (!s->sync_th.joinable() && s->blend_ready) blend_forget(s->blend);
    }

    ShmStats st{};
    st.refresh_ns = s->refresh_ns;
    st.app_presents = s->app_presents;
    st.frames_presents = s->frames_presents;
    st.passed = s->app_presents - s->frames_presents;
    st.generated = s->generated;
    st.missed_refreshes = s->missed;
    st.timed_intervals = s->intervals;
    st.latency_ns_last = s->latency_last;
    st.blended = s->blended;
    st.mc_made = s->mc_made;
    st.mc_cuts = s->mc_cuts;
    st.k_active = s->k_last;
    shm_write_stats(st);
    return result;
}

}  // namespace bef
