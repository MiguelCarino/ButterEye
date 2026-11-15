// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (C) 2026 The ButterEye contributors
//
// VK_LAYER_BUTTEREYE_frames: the control block shared with ButterEye.
//
// A 256-byte file ButterEye creates before it starts mpv, at $BUTTEREYE_FRAMES_SHM or
// $XDG_RUNTIME_DIR/buttereye/frames-<mpv pid>. The layer never creates it; while it is
// missing the layer keeps its environment settings and looks again once a second.
// Both halves are seqlocks: the writer makes its counter odd, writes, makes it even.
//
//   offset  type  writer     field
//    0      u32   ButterEye  magic 0x31464542 ("BEF1")
//    4      u32   ButterEye  version (1)
//    8      u32   ButterEye  size of the block (>= 256)
//   12      u32   ButterEye  control sequence (seqlock)
//   16      i32   ButterEye  k (0: keep BUTTEREYE_FRAMES; 1: off; 2..kmax)
//   20      u32   ButterEye  mode (0: keep BUTTEREYE_FRAMES_MODE; 1: copies; 2: blend;
//                            3: motion-compensated)
//   24      u32   ButterEye  event sequence, bumped on pause, seek, playback-restart, speed
//   28      i32   ButterEye  video rectangle x, in swapchain pixels
//   32      i32   ButterEye  video rectangle y
//   36      i32   ButterEye  video rectangle width (0: the whole image)
//   40      i32   ButterEye  video rectangle height
//   44      i32   ButterEye  the video's height in lines (0: not known)
//   48..63        ButterEye  reserved
//   64      u32   layer      stats sequence (seqlock)
//   68      u32   layer      k in use on the last present
//   72      u64   layer      refresh duration, ns (0: not known yet)
//   80      u64   layer      app presents
//   88      u64   layer      app presents that took the k path
//   96      u64   layer      app presents passed through unchanged
//  104      u64   layer      presents the layer added
//  112      u64   layer      missed refreshes seen in present timing
//  120      u64   layer      screen intervals seen in present timing
//  128      u64   layer      app present to first pixel of its image, last value, ns
//  136      u64   layer      app presents whose added frames were blends
//  144      u64   layer      app presents whose added frames were motion-compensated
//  152      u64   layer      of those, pairs the GPU cut test called a scene cut (their frames
//                            repeat the earlier image); counted once the GPU is done with them
#include "layer.h"

#include <fcntl.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#include <cstring>

namespace bef {

namespace {

constexpr uint32_t kMagic = 0x31464542u;
constexpr uint32_t kVersion = 1;
constexpr size_t kSize = 256;

std::mutex g_shm_lock;
unsigned char* g_map = nullptr;
uint64_t g_next_try_ns = 0;
bool g_bad_logged = false;

template <typename T>
T* at(size_t off) {
    return reinterpret_cast<T*>(g_map + off);
}

// called with g_shm_lock held
bool mapped() {
    if (g_map) return true;
    const std::string& path = config().shm_path;
    if (path.empty()) return false;
    const uint64_t now = now_ns();
    if (now < g_next_try_ns) return false;
    g_next_try_ns = now + 1000000000ull;
    int fd = open(path.c_str(), O_RDWR | O_CLOEXEC);
    if (fd < 0) return false;
    struct stat st;
    void* m = MAP_FAILED;
    if (fstat(fd, &st) == 0 && st.st_size >= static_cast<off_t>(kSize))
        m = mmap(nullptr, kSize, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (m == MAP_FAILED) return false;
    auto* p = static_cast<unsigned char*>(m);
    uint32_t magic, version;
    std::memcpy(&magic, p, 4);
    std::memcpy(&version, p + 4, 4);
    if (magic != kMagic || version != kVersion) {
        munmap(m, kSize);
        if (!g_bad_logged) logf("control block %s: wrong magic or version, ignored", path.c_str());
        g_bad_logged = true;
        return false;
    }
    g_map = p;
    logf("control block %s mapped", path.c_str());
    return true;
}

}  // namespace

ShmControl shm_read() {
    std::lock_guard<std::mutex> g(g_shm_lock);
    ShmControl c;
    if (!mapped()) return c;
    uint32_t* seq = at<uint32_t>(12);
    for (int tries = 0; tries < 8; ++tries) {
        const uint32_t a = __atomic_load_n(seq, __ATOMIC_ACQUIRE);
        if (a & 1u) continue;
        const int k = __atomic_load_n(at<int32_t>(16), __ATOMIC_RELAXED);
        const uint32_t mode = __atomic_load_n(at<uint32_t>(20), __ATOMIC_RELAXED);
        const uint32_t ev = __atomic_load_n(at<uint32_t>(24), __ATOMIC_RELAXED);
        int32_t r[5];
        for (int i = 0; i < 5; ++i) r[i] = __atomic_load_n(at<int32_t>(28 + 4 * i), __ATOMIC_RELAXED);
        __atomic_thread_fence(__ATOMIC_ACQUIRE);
        if (__atomic_load_n(seq, __ATOMIC_RELAXED) != a) continue;
        c.present = true;
        c.k = k;
        c.mode = mode == 1 ? kModeDup : mode == 2 ? kModeBlend : mode == 3 ? kModeMc : -1;
        c.event_seq = ev;
        c.rect_x = r[0];
        c.rect_y = r[1];
        c.rect_w = r[2];
        c.rect_h = r[3];
        c.source_h = r[4];
        return c;
    }
    return c;  // the writer is busy: keep the previous settings this time
}

void shm_write_stats(const ShmStats& s) {
    std::lock_guard<std::mutex> g(g_shm_lock);
    if (!g_map) return;
    uint32_t* seq = at<uint32_t>(64);
    const uint32_t v = __atomic_load_n(seq, __ATOMIC_RELAXED);
    __atomic_store_n(seq, v + 1, __ATOMIC_RELAXED);
    __atomic_thread_fence(__ATOMIC_RELEASE);
    __atomic_store_n(at<uint32_t>(68), s.k_active, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(72), s.refresh_ns, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(80), s.app_presents, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(88), s.frames_presents, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(96), s.passed, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(104), s.generated, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(112), s.missed_refreshes, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(120), s.timed_intervals, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(128), s.latency_ns_last, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(136), s.blended, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(144), s.mc_made, __ATOMIC_RELAXED);
    __atomic_store_n(at<uint64_t>(152), s.mc_cuts, __ATOMIC_RELAXED);
    __atomic_store_n(seq, v + 2, __ATOMIC_RELEASE);
}

}  // namespace bef
