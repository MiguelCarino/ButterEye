// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames, stage S4: motion-compensated in-between frames
// (BUTTEREYE_FRAMES_MODE=mc, or mode 3 in the control block).
//
// It works on the blend's images (blend.h): the history images H[0..1] that hold the app's
// last two images, and Out, whose layers are copied into the added swapchain images. Per app
// present:
//   1. the app's image is copied into H (as for the blend);
//   2. ingest.comp turns it into the engine's luma at source-equivalent scale over the video
//      rectangle, and the engine builds its pyramid and mean (record_pyramid);
//   3. with the previous image ingested too: the engine's vectors for the pair and cut.comp's
//      scene-cut test (its result is copied out for the cut counter);
//   4. per in-between frame m: the engine's field at t = m/k (record_select), render_rgb.comp
//      into Out's layer m - 1, and that layer's copy into the added swapchain image m.
// Steps 1-3 are the pair's work (mc_record_pair), step 4 is one frame's (mc_record_frame), so
// the caller can put each in a submit of its own; mc_record puts all of it in one command buffer.
// The engine is mvgpu's (src/engine), with gdc reduced on the GPU and its Vulkan calls
// through a table filled from the next layer. Nothing is read back but the cut test's result.
#pragma once

#include "blend.h"
#include "engine.h"

#include <memory>
#include <string>
#include <vector>

namespace bef {

// timestamps per app present: [0] the pair's start, [1] after the copy into H, [2] after ingest
// and pyramid, [3] after the vectors and the cut test (the pair's end); then for in-between frame
// m (1..kKLimit - 1) at 4 + 3 (m - 1): its start, after select and render, after its copy out
constexpr uint32_t kMcPairStamps = 4;
constexpr uint32_t kMcFrameStamps = 3;
constexpr uint32_t kMcStamps = kMcPairStamps + kMcFrameStamps * (kKLimit - 1);

// the GPU times of one app present's work, ns
struct McTimes {
    double copy_in = 0, ingest = 0, vectors = 0;  // the pair's work, by step
    double frames = 0, out = 0;                   // select + render, and the copies out, summed over the frames
    double total = 0;                             // first stamp to last (gaps between submits included)
    double pair = 0;                              // the pair's work, start to end
    double frame[kKLimit - 1] = {};               // each frame's work, start to end
    double largest = 0;                           // the largest single submit's work (all of it when unsplit)
};

struct McRect {
    int x = 0, y = 0, w = 0, h = 0;
};

struct McSettings {
    McRect rect;          // the video rectangle; w == 0: the whole image
    int source_h = 0;     // the video's height, when known (0: not known)
    int max_h = 1080;     // the engine never searches at more lines than this
    bool pq = false;      // HDR10 (PQ, BT.2020) swapchain
    float still = 0.004f; // the static-pixel rule's limit (share of full scale); 0: off
    float cut_hard = 0.30f, cut_lo = 0.02f, cut_ratio = 5.0f, cut_floor = 0.003f;
    bool cut_force = false;  // every pair is a cut (a test knob)
    mvgpu::Tuning tuning;
};

// the layer's settings from the environment: BUTTEREYE_FRAMES_RECT=x,y,w,h,
// BUTTEREYE_FRAMES_SRC_H, BUTTEREYE_FRAMES_STILL, BUTTEREYE_FRAMES_CUT=hard,lo,ratio,floor
// (BUTTEREYE_FRAMES_CUT=force: every pair), and the engine's MVGPU_* tuning
McSettings mc_settings_from_env();

struct Mc {
    Device* d = nullptr;
    VkDevice dev = VK_NULL_HANDLE;
    McSettings set;
    McRect rect;      // the rectangle in use (clamped to the image)
    int sw = 0, sh = 0;  // the engine's size
    mvgpu::VkFns fns;
    std::unique_ptr<mvgpu::Engine> eng;
    std::unique_ptr<mvgpu::Memory> mem;
    mvgpu::Buffer state;  // cut.comp's
    mvgpu::Buffer cuts;   // host-visible: per query slot, cut.comp's d and cut flag (8 bytes)
    VkDescriptorSetLayout dsl[3] = {};  // ingest, cut, render
    VkPipelineLayout pl[3] = {};
    VkPipeline pipe[3] = {};
    VkDescriptorPool dpool = VK_NULL_HANDLE;
    VkDescriptorSet set_in[2][mvgpu::Engine::SLOTS] = {};  // [H index][engine slot]
    VkDescriptorSet set_cut[mvgpu::Engine::SLOTS] = {};    // [pair's first slot]
    VkDescriptorSet set_render[2] = {};                    // [H index of the current image]
    VkQueryPool queries = VK_NULL_HANDLE;
    uint32_t query_slots = 0;
    uint64_t bytes = 0;  // device memory of the engine and the state (not the blend's images)
    std::vector<uint8_t> slot_frames;  // per query slot: in-between frames recorded (0: none, the first of a run)
    // the run: the engine slot holding H[cur]'s luma (-1: none), pairs made in a row, the
    // cut ring
    int slot = -1;
    int pairs = 0;
    int ring = 0, valid = 0;
};

// makes everything for the blend's images (b must be ready); on failure frees what was made
bool mc_create(Mc& mc, const Blend& b, const McSettings& set, uint32_t family, uint32_t query_slots,
               std::string* why);
void mc_destroy(Mc& mc);

// Records one app present's work (see the top of this file) after the caller's barrier that
// put `app` in TRANSFER_SRC_OPTIMAL and every target in TRANSFER_DST_OPTIMAL, all in one command
// buffer. Returns true when the targets got in-between frames, false when they got copies of
// `app` (the first image of a run).
bool mc_record(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, const VkImage* targets, uint32_t got, uint32_t k,
               bool show, uint32_t qslot);

// The same work in parts. mc_record_pair: steps 1-3, after the caller's barrier that put `app`
// in TRANSFER_SRC_OPTIMAL (with copy_in false, step 1 is left out: the caller copies `app` into
// mc_copy_in_target(b) itself, on whatever queue it likes, before this command buffer runs).
// Returns true when in-between frames follow (then call mc_record_frame for m = 1..got), false
// for the first image of a run (the caller copies `app` into the targets).
bool mc_record_pair(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, uint32_t got, uint32_t qslot,
                    bool copy_in = true);
// the H image the next copy in writes, and the bookkeeping of that copy (mc_record_pair with
// copy_in false); the caller records the copy itself with mc_record_copy_in
uint32_t mc_copy_in_target(const Blend& b);
void mc_record_copy_in(Mc& mc, Blend& b, VkCommandBuffer cb, VkImage app, uint32_t qslot);
// Step 4 for frame m (1..got): select and render into Out's layer m - 1 (with render true), then
// (with copy_out true) that layer's copy into `target`, which the caller has put in
// TRANSFER_DST_OPTIMAL. Starts with a barrier, so it may begin a command buffer of its own.
void mc_record_frame(Mc& mc, Blend& b, VkCommandBuffer cb, uint32_t m, uint32_t k, VkImage target, bool show,
                     uint32_t qslot, bool render = true, bool copy_out = true);
// marks the end of the present's recording: how many frames the query slot holds
void mc_record_done(Mc& mc, uint32_t qslot, uint32_t frames);

// the query slot's GPU times (false when not available); `split`: the pair and each frame were
// submits of their own
bool mc_times(Mc& mc, uint32_t qslot, bool split, McTimes* out);
// the query slot's cut test: 1 a cut, 0 none, -1 not known (no pair, no readback); d when known
int mc_cut(Mc& mc, uint32_t qslot, float* d);

}  // namespace bef
