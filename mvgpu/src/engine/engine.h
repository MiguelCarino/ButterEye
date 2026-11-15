// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// The engine: mvgpu's GPU work, shared by the VapourSynth plugin (src/mvgpu.cpp)
// and the present layer (layer/). It owns the buffers, pipelines and descriptor
// sets on a device someone else created, and records into command buffers it is
// handed; it never submits, waits or creates an instance. Every Vulkan call goes
// through a VkFns table (vkfns.h).
//
// Per source frame in slot s: record_load (planes unpacked from raw_in, luma
// pyramid). Per pair (slot a, slot a + 1): record_vectors. Per in-between frame:
// record_select (the per-pixel field at t) and record_render (the planes at t,
// packed into raw_out). Callers put a barrier() between their own transfers and
// these, as the plugin does.
//
// The brightness difference between two frames (gdc) is taken out of every match.
// The plugin measures each frame's mean luma on the CPU (set_mean) and passes gdc
// as a push constant. With gdc_from_buffer the mean is reduced on the GPU in
// record_load and the shaders read gdc from a buffer (specialization constant
// GDC_FROM_BUFFER), for the layer, which has no CPU copy of the frame.
#pragma once

#include <vulkan/vulkan.h>

#include <array>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>
#include <string>

#include "vkfns.h"

namespace mvgpu {

constexpr int BLOCK = 8;
constexpr int LEVELS = 4;
constexpr int COARSE_RADIUS = 8;
// candidates a finer level refines around: the parent block's vector, its 4
// neighbours' and no motion (search.comp)
constexpr int NPRED = 6;

// a failed Vulkan call; ``result`` tells a lost device from a transient failure
struct VkError : std::runtime_error {
    VkResult result;
    VkError(const std::string& what, VkResult r) : std::runtime_error(what), result(r) {}
};

inline void check(VkResult r, const char* what) {
    if (r != VK_SUCCESS) throw VkError(std::string(what) + " failed (" + std::to_string(r) + ")", r);
}

struct Buffer {
    VkBuffer buf = VK_NULL_HANDLE;
    VkDeviceMemory mem = VK_NULL_HANDLE;
    VkDeviceSize size = 0;
    void* map = nullptr;
};

// buffers on one device, through the table
class Memory {
public:
    Memory(const VkFns& fns, VkDevice device, const VkPhysicalDeviceMemoryProperties& props)
        : vk(fns), dev(device), memprops(props) {}
    uint32_t find_mem(uint32_t bits, VkMemoryPropertyFlags want) const;
    Buffer make(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags props);
    void free(Buffer& b);

private:
    const VkFns& vk;
    VkDevice dev;
    VkPhysicalDeviceMemoryProperties memprops;
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

// The settings read from the environment (tuning and comparisons).
struct Tuning {
    // MVGPU_TRI=1: the frame before the pair and the frame after it are read
    // too. Off unless asked for: on drawn scenes it helps, on real fast motion
    // it turns a soft smear into hard-edged flecks and the error against the
    // real frame rises by a tenth (docs/spikes/m0t.md). Where the vectors of the
    // pair before and the pair after differ by more than tri_pre px at a place,
    // and neither they nor the vector chosen explain A and C over a patch
    // (mean difference above tri_gate), the content is in one frame only: what
    // A and the frame before agree on (within tri_thr, and tri_ratio of the
    // other side's error) is about to be covered and is taken from A; what C
    // and the frame after agree on was just uncovered and is taken from C. Not
    // where the other motion explains the pixel, or one tri_reach px along the
    // motion, within tri_fine (the object's soft edge). A neighbouring pair's
    // vector is also tried where the best one leaves more than tri_bad.
    // MVGPU_TRI_T="gate,thr,ratio,pre,bad,fine,reach"
    bool tri = false;
    // MVGPU_SHOW=1|2|3: draw the side taken, the mismatch or the vector's length (render.comp)
    int show = 0;
    // MVGPU_TRI_DBG=4: no neighbouring pair's vector; 8: no strips (for comparisons)
    int tri_dbg = 0;
    float tri_gate = 0.03f, tri_thr = 0.03f, tri_ratio = 0.5f, tri_pre = 0.75f, tri_bad = 0.03f, tri_fine = 0.02f,
          tri_reach = 3.0f;
    // MVGPU_EDGE=1: a found object's vector is offered to the pixels around
    // its blocks (select.comp). Off: where the source-frame search matched a bar
    // to its neighbour, it draws the whole phantom bar (docs/spikes/m0t.md).
    // MVGPU_EDGE_T="thr,act"
    bool edge = false;
    float edge_thr = 0.02f, edge_act = 0.03f;
    // MVGPU_UNI=0: without them (for comparisons). The window is +-uni_radius px
    // of motion; a block is searched when the frames differ there by uni_flag a
    // pixel under the in-between vector, and found when a place matches within
    // uni_abs a pixel and uni_rel of that difference; MVGPU_UNI_T="flag,abs,rel"
    bool uni = true;
    int uni_radius = 64;
    float uni_flag = 0.04f, uni_abs = 0.02f, uni_rel = 0.4f;
    // how far (px) from a found block's half-way centre an in-between block takes its vector
    float uni_land = 4.0f;
    // MVGPU_FINE=0: vectors per 8x8 block only (for comparisons)
    bool fine = true;
    // MVGPU_SUBPEL=0: no 1/2 px refinement of the final motion (for comparisons)
    int subpel_steps = 1;
    // MVGPU_MEDIAN=0: the plain component median at the full-size levels too (for
    // comparisons); otherwise a vector median kept only where it matches the block
    // about as well: SAD within med_rel of the block's own plus med_abs a pixel
    bool checked_median = true;
    float med_rel = 0.5f, med_abs = 0.008f;
    // The vectors of the pair before are tried as a candidate (0, as shipped:
    // a little better on real footage, docs/spikes/m0t.md). A cost for leaving
    // them, a pixel of the block for each pixel of difference up to 2
    // (MVGPU_TEMPORAL=0.002), lost quality; negative turns them off
    float tpen = 0.0f;
    // the credit a match gets for the detail in what matched (search.comp), as a
    // share of the patches' deviation; MVGPU_ACT overrides (tuning, 0 = off)
    float act = 0.5f;
    // MVGPU_SELECT=0: the old bilinear block vectors (for comparisons)
    bool select_vectors = true;
    // mean |A - C| (0-1 luma) where blending starts to give way to the nearer
    // frame, and where it has fully; MVGPU_OCC="lo,hi" overrides (tuning).
    // Three times what they were (0.04, 0.12), and the fade below 2.5 times
    // (0.02, 0.06): on 169 moments of real 50/60 fps footage both did more
    // harm than good at the old values (worst areas 0.150 -> 0.180 with them
    // out of the way), and nothing on the drawn scenes needed them
    float occ_lo = 0.12f, occ_hi = 0.36f;
    // mean mismatch over an area (render.comp) where the in-between frame fades
    // to the plain blend (MVGPU_FAIL="lo,hi"); per-pixel margin a block vector
    // needs over the smooth one (MVGPU_SELM)
    float fail_lo = 0.05f, fail_hi = 0.15f, sel_margin = 0.01f;

    static Tuning from_env();
};

class Engine {
public:
    static constexpr int SLOTS = 4;
    // throws when this device cannot run the engine at w x h (before anything is made)
    static void check_limits(const VkPhysicalDeviceLimits& limits, int w, int h);
    Engine(const VkFns& fns, VkDevice dev, const VkPhysicalDeviceMemoryProperties& memprops,
           const VkPhysicalDeviceLimits& limits, int w, int h, int bits, const Tuning& tuning,
           bool gdc_from_buffer = false);
    ~Engine();
    Engine(const Engine&) = delete;
    Engine& operator=(const Engine&) = delete;

    const Tuning& tuning() const { return tune; }
    bool gdc_from_buffer() const { return gdc_buf; }
    // the packed planes in (record_load reads them) and out (record_render writes them)
    const Buffer& raw_input() const { return raw_in; }
    const Buffer& raw_output() const { return raw_out; }
    uint32_t raw_size() const { return raw_bytes; }
    const Plane& plane(int i) const { return planes[i]; }
    // device memory of the engine's buffers
    uint64_t memory_bytes() const;

    // slot s's mean luma (0-1), measured by the caller (push-constant mode)
    void set_mean(int s, float m) { smean[s] = m; }
    // record a copy of the four slots' means into the GPU's means buffer (gdc_from_buffer;
    // for checks that the buffer path matches the push constants)
    void record_set_means(VkCommandBuffer cmd, const float (&m)[SLOTS]);
    // read back through a host buffer: the GPU means (gdc_from_buffer)
    const Buffer& means_buffer() const { return means; }

    // the memory dependency every step ends with (shader and transfer writes before
    // shader and transfer reads and writes)
    void barrier(VkCommandBuffer cmd) const;
    // raw_in -> slot s (planes as floats), its luma pyramid and (gdc_from_buffer) its mean
    void record_load(VkCommandBuffer cmd, int s);
    // slot s's luma pyramid and (gdc_from_buffer) its mean, from the luma plane already in
    // the slot: the second half of record_load, for a caller that writes the luma itself
    // (the present layer's RGB ingest). Starts with the copy of the luma: put a barrier()
    // between the caller's writes and this.
    void record_pyramid(VkCommandBuffer cmd, int s);

    // the engine's buffers, for a caller that adds passes of its own (the present layer)
    int width() const { return w; }
    int height() const { return h; }
    const Buffer& slot_buffer(int s) const { return slot[s]; }  // planes as floats, luma at 0
    const Buffer& pyramid(int s) const { return pyr[s]; }
    const Buffer& field_buffer() const { return field; }  // per pixel (u.x, u.y, mismatch, side)
    uint32_t level_offset(int l) const { return loff[l]; }
    int level_width(int l) const { return lw[l]; }
    int level_height(int l) const { return lh[l]; }
    // the vectors of pair (a, a + 1); follows: the pair searched before it is the pair before it
    void record_vectors(VkCommandBuffer cmd, int a, bool follows);
    // the per-pixel field of pair a at t; has_prev, has_next: slots a - 1 and a + 2 hold
    // the frames around the pair, in the same shot, with the vectors of their pairs
    void record_select(VkCommandBuffer cmd, int a, float t, bool has_prev, bool has_next);
    // pair a rendered at t from the field, packed into raw_out
    void record_render(VkCommandBuffer cmd, int a, float t);

private:
    VkFns vk;  // a copy: the caller's table need not outlive the engine
    VkDevice dev;
    Memory mem;
    VkPhysicalDeviceLimits limits{};
    Tuning tune;
    bool gdc_buf;

    int w, h, bytes, bits;
    std::array<Plane, 3> planes{};
    uint32_t floats = 0, raw_bytes = 0;
    std::array<int, LEVELS> lw{}, lh{}, bw{}, bh{};
    std::array<uint32_t, LEVELS> loff{};
    uint32_t pyr_floats = 0;
    int bw4 = 0, bh4 = 0;

    Buffer raw_in, raw_out, out, field;
    std::array<Buffer, SLOTS> slot, pyr;
    std::array<Buffer, LEVELS> vraw, vec;
    Buffer vraw4, vec4;  // 4x4 refinement at full size
    Buffer vsub;         // the final half-vectors as floats, refined to 1/4 px (subpel.comp)
    // vectors searched from each source frame for the blocks the in-between
    // vectors leave unexplained (uni.comp), and vsub with them carried to the
    // in-between frame (proj.comp): what select.comp reads
    // Those are kept for each of the last four pairs (by the slot of the pair's
    // first frame): the render of a pair also reads the pair before and after
    Buffer ufwd, ubwd;
    std::array<Buffer, SLOTS> vfin, vsd;
    // gdc for the shaders that read it from a buffer: (A - C, A - before, C - after);
    // bound in both modes, written and read only with gdc_from_buffer
    Buffer gdcv;
    // gdc_from_buffer: each slot's mean luma and the per-row sums it is reduced from
    Buffer means, mean_rows;
    // each slot's mean luma (0-1): the pair's global brightness difference is
    // taken out of every match (fades, flashes, a stage lighting up)
    std::array<float, SLOTS> smean{};

    VkDescriptorPool dpool = VK_NULL_HANDLE;
    Pipe p_unpack, p_down2, p_search_coarse, p_search_fine, p_search_full, p_search4, p_median, p_subpel, p_uni,
        p_proj, p_select, p_render, p_pack, p_mean;
    std::array<VkDescriptorSet, SLOTS> ds_uni_f{}, ds_uni_b{}, ds_proj{};
    std::array<VkDescriptorSet, SLOTS> ds_median4{};
    std::array<VkDescriptorSet, SLOTS> ds_search4{}, ds_subpel{}, ds_subpel4{};
    std::array<VkDescriptorSet, SLOTS> ds_select{};
    std::array<VkDescriptorSet, SLOTS> ds_unpack{}, ds_down2{};
    std::array<std::array<VkDescriptorSet, LEVELS>, SLOTS> ds_search{};  // [order][level]
    std::array<std::array<VkDescriptorSet, LEVELS>, SLOTS> ds_median{};  // [order][level]
    std::array<VkDescriptorSet, SLOTS> ds_render{}, ds_mean{};
    VkDescriptorSet ds_pack{};

    void init();
    void destroy();
    void dispatch1d(VkCommandBuffer cmd, uint32_t groups) const;
    // gdc: the shader reads gdc (one more binding, and GDC_FROM_BUFFER when gdc_buf)
    Pipe pipeline(const uint32_t* code, size_t bytes, int bindings, uint32_t push, uint32_t wg = 0,
                  uint32_t kids = 1, bool gdc = false);
    VkDescriptorSet set(const Pipe& p, std::initializer_list<const Buffer*> bufs);
    void push(VkCommandBuffer cmd, const Pipe& p, VkDescriptorSet s, const void* pc, uint32_t size) const;
    // gdc_from_buffer: gdcv for pair a from the means
    void record_gdc(VkCommandBuffer cmd, int a);
};

}  // namespace mvgpu
