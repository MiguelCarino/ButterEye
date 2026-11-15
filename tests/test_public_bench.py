# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The public benchmark's manifest, downloads and decoding (tools/perf/public_bench.py).

No network and no GPU: downloads are served by a local HTTP server, footage is
made up in the test."""

from __future__ import annotations

import copy
import hashlib
import http.server
import json
import struct
import threading
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from tools.perf import public_bench as pb

# ---------------------------------------------------------------- manifest


def test_the_committed_manifest_is_valid() -> None:
    m = pb.load_manifest()
    assert pb.validate(m) == []
    assert json.loads(pb.dump_manifest(m)) == m


def test_licences_keep_non_commercial_footage_out_of_headline_and_images() -> None:
    m = pb.load_manifest()
    for name, st in m["sets"].items():
        if "NC" in st["licence"]:
            assert st["nc"] and not st["headline"] and not st["images_ok"], name
    assert {n for n, st in m["sets"].items() if st["images_ok"]} <= {"bbb", "svt22", "cel"}
    assert {n for n, st in m["sets"].items() if st["headline"]} == {"bbb", "svt22", "elfuente"}
    text = pb.MANIFEST.read_text().lower()
    assert "/home/" not in text


def _moment(**kw: Any) -> dict[str, Any]:
    base = {"footage": "x", "f": 10, "s": 2, "m": 2, "tier": "2x", "decoded_sha256": None}
    return base | kw


def test_windows_cover_every_source_and_in_between() -> None:
    assert pb.window(_moment()) == list(range(10, 19))  # 5 sources, 4s+1 frames
    assert len(pb.window(_moment(s=4))) == 17
    assert pb.window(_moment(s=6, m=6, tier="display")) == list(range(10, 29))  # 3s+1
    assert len(pb.window(_moment(s=5, m=5, tier="display"))) == 16


def test_slot_positions_spread_and_stay_inside() -> None:
    assert pb.slot_positions(600, 3, 18) == [120, 300, 480]
    assert pb.slot_positions(1000, 1, 8) == [500]
    assert pb.slot_positions(20, 1, 18) == [1]  # clamped so the window fits


def _tiny_manifest() -> dict[str, Any]:
    m = pb.load_manifest()
    return {
        "schema": 1,
        "set_version": 1,
        "sets": {
            "open": copy.deepcopy(m["sets"]["elfuente"]),
            "nc": copy.deepcopy(m["sets"]["uvg"]),
        },
        "footage": [],
        "moments": [],
    }


def test_validate_names_what_is_wrong() -> None:
    m = _tiny_manifest()
    m["footage"].append(
        {"id": "a", "set": "open", "fetch": "whole", "urls": ["https://e/a"], "frames_total": 20}
    )
    m["moments"] = [
        _moment(footage="a", f=0, s=4, m=3),
        _moment(footage="a", f=15),
        _moment(footage="a", f=0, s=6, m=6, tier="display"),
        _moment(footage="zz"),
    ]
    m["sets"]["nc"]["headline"] = True
    errs = "\n".join(pb.validate(m))
    assert "m=3 must divide stride s=4" in errs
    assert "outside the footage" in errs  # 15+8 >= 20
    assert "unknown footage" in errs
    assert "never headline" in errs


def test_slots_store_the_union_of_windows() -> None:
    m = _tiny_manifest()
    m["moments"] = [
        _moment(footage="a", f=0, s=4, m=2),
        _moment(footage="a", f=0, s=6, m=6, tier="display"),
        _moment(footage="a", f=40),
    ]
    assert pb.slots(m, "a") == {0: list(range(19)), 40: list(range(40, 49))}
    assert pb.needed_frames(m, "a") == list(range(19)) + list(range(40, 49))


def test_non_commercial_footage_needs_accept_nc() -> None:
    m = _tiny_manifest()
    m["footage"] = [{"id": "a", "set": "open"}, {"id": "b", "set": "nc"}]
    assert [f["id"] for f in pb._select(m, None, False, "core")] == ["a"]
    assert [f["id"] for f in pb._select(m, None, True, "core")] == ["a", "b"]
    assert pb._nc_skipped(m, None, False) == ["nc"]
    assert pb._nc_skipped(m, "a", False) == []


def test_select_filter_uses_ranges() -> None:
    assert pb.select_filter([3, 4, 5, 9, 11, 12]) == (
        "select='between(n,3,5)+between(n,9,9)+between(n,11,12)'"
    )


# ---------------------------------------------------------------- pixels


def test_box_rounds_half_up_and_ten_bit_rounds_once() -> None:
    a = np.array([[0, 1], [1, 0]], dtype=np.uint8)  # mean 0.5 -> 1
    assert pb.box8(np, a)[0, 0] == 1
    a = np.array([[255, 255], [255, 254]], dtype=np.uint8)
    assert pb.box8(np, a)[0, 0] == 255
    t = np.array([[1023, 1023], [1023, 1023]], dtype=np.uint16)
    assert pb.box10to8(np, t)[0, 0] == 255  # 4092+8 >> 4 = 256, clamped
    t = np.array([[512, 512], [512, 512]], dtype=np.uint16)
    assert pb.box10to8(np, t)[0, 0] == 128
    t = np.array([[6, 6], [6, 6]], dtype=np.uint16)  # 1.5 in 8 bits -> 2
    assert pb.box10to8(np, t)[0, 0] == 2


def test_crop_is_centred() -> None:
    a = np.arange(10 * 12).reshape(10, 12)
    c = pb.crop_centre(a, 8, 6)
    assert c.shape == (6, 8) and c[0, 0] == a[2, 2]
    with pytest.raises(pb.BenchError):
        pb.crop_centre(a, 14, 6)


def test_acescc_and_display_mapping() -> None:
    mid = (np.log2(0.18) + 9.72) / 17.52
    assert pb.acescc_decode(np, mid) == pytest.approx(0.18)
    assert abs(float(pb.acescc_decode(np, (np.log2(2.0**-16) + 9.72) / 17.52))) < 1e-6
    assert float(pb.acescc_decode(np, 2.0)) == 65504.0
    assert pb.tone(np, np.array(0.0)) == 0.0 and pb.tone(np, np.array(4.0)) == 1.0
    assert float(pb.tone(np, np.array(-1.0))) == 0.0
    one = np.ones((2, 2))
    y, u, v = pb.rgb_to_ycbcr709(np, one, one, one)
    assert y[0, 0] == 235 and u[0, 0] == 128 and v[0, 0] == 128
    y, u, v = pb.rgb_to_ycbcr709(np, 0 * one, 0 * one, 0 * one)
    assert y[0, 0] == 16
    y, u, v = pb.rgb_to_ycbcr709(np, one, 0 * one, 0 * one)  # red: Cr at its top
    assert v[0, 0] == 240


# ---------------------------------------------------------------- EXR


def _exr(tmp: Path, chans: dict[str, Any], comp: int) -> Path:
    """A minimal scanline OpenEXR writer (half channels), enough to test the reader."""
    h, w = next(iter(chans.values())).shape
    names = sorted(chans)

    def attr(name: str, typ: str, val: bytes) -> bytes:
        return name.encode() + b"\0" + typ.encode() + b"\0" + struct.pack("<i", len(val)) + val

    chl = b"".join(n.encode() + b"\0" + struct.pack("<iB3xii", 1, 0, 1, 1) for n in names) + b"\0"
    hdr = struct.pack("<ii", 20000630, 2)
    hdr += attr("channels", "chlist", chl)
    hdr += attr("compression", "compression", bytes([comp]))
    hdr += attr("dataWindow", "box2i", struct.pack("<iiii", 0, 0, w - 1, h - 1))
    hdr += attr("displayWindow", "box2i", struct.pack("<iiii", 0, 0, w - 1, h - 1))
    hdr += attr("lineOrder", "lineOrder", b"\0") + b"\0"
    lines = 16 if comp == 3 else 1
    chunks = []
    for y0 in range(0, h, lines):
        raw = b"".join(
            chans[n][y].astype("<f2").tobytes()
            for y in range(y0, min(h, y0 + lines))
            for n in names
        )
        if comp:
            b = np.frombuffer(raw, np.uint8)
            half = (b.size + 1) // 2
            inter = np.empty_like(b)
            inter[:half], inter[half:] = b[0::2], b[1::2]
            d = inter.copy()
            d[1:] = (inter[1:].astype(np.int16) - inter[:-1].astype(np.int16) + 128) % 256
            data = zlib.compress(d.tobytes())
            if len(data) >= len(raw):  # as OpenEXR: kept raw when it does not shrink
                data = raw
        else:
            data = raw
        chunks.append(struct.pack("<ii", y0, len(data)) + data)
    table_at = len(hdr)
    pos = table_at + 8 * len(chunks)
    table = b""
    for c in chunks:
        table += struct.pack("<Q", pos)
        pos += len(c)
    path = tmp / f"t{comp}.exr"
    path.write_bytes(hdr + table + b"".join(chunks))
    return path


@pytest.mark.parametrize("comp", [0, 3])
def test_exr_reader_gets_back_what_was_written(tmp_path: Path, comp: int) -> None:
    rng = np.random.default_rng(1)
    chans = {c: rng.uniform(-0.36, 1.47, (20, 24)).astype(np.float16) for c in "BGR"}
    got = pb.read_exr(np, _exr(tmp_path, chans, comp))
    for c in "BGR":
        assert np.array_equal(got[c], chans[c].astype(np.float32))


# ---------------------------------------------------------------- downloads


class _Handler(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    ignore_range = False
    log: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self.log.append(f"{self.path} {self.headers.get('Range')}")
        body = self.files.get(self.path)
        if body is None:
            self.send_error(404)
            return
        rng = self.headers.get("Range")
        if rng and not self.ignore_range:
            a, b = rng.removeprefix("bytes=").split("-")
            start, end = int(a), int(b) if b else len(body) - 1
            if start >= len(body):
                self.send_error(416)
                return
            part = body[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(body)}")
        else:
            part = body
            self.send_response(200)
        self.send_header("Content-Length", str(len(part)))
        self.end_headers()
        self.wfile.write(part)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.files, _Handler.log, _Handler.ignore_range = {}, [], False
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def test_fetch_resumes_a_part_file(server: str, tmp_path: Path) -> None:
    body = bytes(range(256)) * 50
    _Handler.files["/a.bin"] = body
    dest = tmp_path / "a.bin"
    (tmp_path / "a.bin.part").write_bytes(body[:1000])
    got = pb.fetch_file([f"{server}/a.bin"], dest, size=len(body), sha=_sha(body))
    assert got == _sha(body) and dest.read_bytes() == body
    assert _Handler.log == ["/a.bin bytes=1000-"]
    assert not (tmp_path / "a.bin.part").exists()


def test_fetch_starts_over_when_the_server_ignores_range(server: str, tmp_path: Path) -> None:
    body = b"x" * 3000
    _Handler.files["/a.bin"] = body
    _Handler.ignore_range = True
    (tmp_path / "a.bin.part").write_bytes(b"y" * 100)
    pb.fetch_file([f"{server}/a.bin"], tmp_path / "a.bin", size=3000, sha=_sha(body))
    assert (tmp_path / "a.bin").read_bytes() == body


def test_fetch_refuses_a_wrong_hash_and_tries_the_mirror(server: str, tmp_path: Path) -> None:
    good, bad = b"good" * 100, b"evil" * 100
    _Handler.files["/bad/a"] = bad
    _Handler.files["/good/a"] = good
    dest = tmp_path / "a"
    pb.fetch_file(
        [f"{server}/missing/a", f"{server}/bad/a", f"{server}/good/a"],
        dest,
        size=len(good),
        sha=_sha(good),
        retries=1,
    )
    assert dest.read_bytes() == good
    with pytest.raises(pb.BenchError, match="every source failed"):
        pb.fetch_file([f"{server}/bad/a"], tmp_path / "b", size=len(bad), sha=_sha(good), retries=1)
    assert not (tmp_path / "b").exists() and not (tmp_path / "b.part").exists()


def test_fetch_rehashes_a_kept_file(server: str, tmp_path: Path) -> None:
    dest = tmp_path / "a"
    dest.write_bytes(b"old")
    _Handler.files["/a"] = b"new"
    pb.fetch_file([f"{server}/a"], dest, size=3, sha=_sha(b"old"))
    assert _Handler.log == []
    pb.fetch_file([f"{server}/a"], dest, size=3, sha=_sha(b"new"))
    assert dest.read_bytes() == b"new"


# ---------------------------------------------------------------- y4m footage end to end

W, H = 40, 20  # made-up footage size; output is cropped to 16x8


def _y4m(n_frames: int) -> tuple[bytes, list[np.ndarray]]:
    rng = np.random.default_rng(7)
    head = f"YUV4MPEG2 W{W} H{H} F60:1 Ip A1:1 C420p10\n".encode()
    frames, body = [], b""
    for _ in range(n_frames):
        a = rng.integers(0, 1024, W * H * 3 // 2, dtype=np.uint16)
        frames.append(a)
        body += b"FRAME\n" + a.astype("<u2").tobytes()
    return head + body, frames


def _y4m_manifest(server: str, n_frames: int, hard: bool) -> dict[str, Any]:
    m = _tiny_manifest()
    m["sets"]["open"]["decoded_hash"] = "hard" if hard else "soft"
    m["footage"] = [
        {
            "id": "fake",
            "set": "open",
            "fetch": "y4m-range",
            "urls": [f"{server}/f.y4m"],
            "header_bytes": len(f"YUV4MPEG2 W{W} H{H} F60:1 Ip A1:1 C420p10\n"),
            "frame_bytes": 6 + W * H * 3,
            "width": W,
            "height": H,
            "frames_total": n_frames,
            "prepare": {"decode": "y4m"},
            "frames": {},
        }
    ]
    m["moments"] = [_moment(footage="fake", f=1), _moment(footage="fake", f=12)]
    return m


@pytest.fixture
def small_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(pb, "OUT_W", 16)
    monkeypatch.setattr(pb, "OUT_H", 8)
    monkeypatch.setenv("BUTTEREYE_BENCH_CACHE", str(tmp_path / "cache"))
    return tmp_path / "cache"


def test_y4m_frames_fetch_by_range_pin_and_prepare(server: str, small_out: Path) -> None:
    data, frames = _y4m(24)
    _Handler.files["/f.y4m"] = data
    m = _y4m_manifest(server, 24, hard=True)
    assert pb.validate(m) == []
    fp = m["footage"][0]
    s = pb.fetch_footage(m, fp, small_out, pin=True)
    assert s["files"] == 18 and s["unpinned"] == 18
    assert sorted(map(int, fp["frames"])) == list(range(1, 10)) + list(range(12, 21))
    assert all(
        r.endswith(f"-{int(r.split('=')[1].split('-')[0]) + fp['frame_bytes'] - 1}")
        for r in (ln.split()[1] for ln in _Handler.log)
    )

    s = pb.prepare_footage(np, m, fp, small_out, limit=None, threads=1, pin=True)
    assert s["written"] == 2 and s["pinned"] == 2
    with np.load(pb.moment_path(small_out, "fake", 12)) as d:
        assert d["y"].shape == (9, 8, 16) and d["u"].shape == (9, 4, 8)
        assert list(d["index"]) == list(range(12, 21))
        # frame 12, luma: boxed to 20x10, then the centre 16x8
        src = frames[12][: W * H].reshape(H, W).astype(np.uint32)
        boxed = (src.reshape(H // 2, 2, W // 2, 2).sum(axis=(1, 3)) + 8) >> 4
        assert np.array_equal(d["y"][0], np.minimum(boxed, 255)[1:9, 2:18])

    # kept files are reused, unless the way they were made changed
    s = pb.prepare_footage(np, m, fp, small_out, limit=None, threads=1, pin=False)
    assert s["written"] == 0 and s["moments"] == 2 and s["warnings"] == []
    fp["prepare"]["note"] = "other recipe"
    s = pb.prepare_footage(np, m, fp, small_out, limit=1, threads=1, pin=False)
    assert s["written"] == 1 and s["moments"] == 1

    # a decoder that gives other pixels is caught (hard) and the moment removed
    m["moments"][1]["decoded_sha256"] = "0" * 64
    pb.moment_path(small_out, "fake", 12).unlink()
    with pytest.raises(pb.BenchError, match="differ from the pinned hash"):
        pb.prepare_footage(np, m, fp, small_out, limit=None, threads=1, pin=False)
    assert not pb.moment_path(small_out, "fake", 12).exists()

    # soft footage only warns
    m["sets"]["open"]["decoded_hash"] = "soft"
    s = pb.prepare_footage(np, m, fp, small_out, limit=None, threads=1, pin=False)
    assert len(s["warnings"]) == 1


def test_a_wrong_frame_offset_is_refused(server: str, small_out: Path) -> None:
    data, _ = _y4m(24)
    _Handler.files["/f.y4m"] = data
    m = _y4m_manifest(server, 24, hard=True)
    fp = m["footage"][0]
    fp["header_bytes"] += 2
    pb.fetch_footage(m, fp, small_out, pin=False)
    with pytest.raises(pb.BenchError, match="does not start with FRAME"):
        pb.prepare_footage(np, m, fp, small_out, limit=1, threads=1, pin=False)


def test_missing_decoders_are_named(monkeypatch: pytest.MonkeyPatch) -> None:
    class Done:
        stdout = " V....D h264   H.264\n V....D exr    OpenEXR\n"

    monkeypatch.setattr(pb.shutil, "which", lambda _: "/usr/bin/ffmpeg")
    monkeypatch.setattr(pb.subprocess, "run", lambda *a, **k: Done())
    pb.check_decoders({"h264"})
    with pytest.raises(pb.BenchError, match="no hevc decoder"):
        pb.check_decoders({"h264", "hevc"})


# ---------------------------------------------------------------- run: moments


def test_scored_outputs_are_the_middle_in_betweens() -> None:
    assert pb.scored_outputs("2x", 5, 2) == [3, 5]  # pairs (1,2) and (2,3)
    assert pb.scored_outputs("2x", 9, 2) == [7, 9]  # cel: pairs (3,4) and (4,5)
    assert pb.scored_outputs("display", 4, 5) == [6, 7, 8, 9]  # pair (1,2) at x5
    assert pb.scored_outputs("display", 4, 6) == [7, 8, 9, 10, 11]


def _fake_moment_file(root: Path, footage: str, first: int, frames: list[int]) -> None:
    """A prepared moment whose every luma sample is its native frame number."""
    path = pb.moment_path(root, footage, first)
    path.parent.mkdir(parents=True, exist_ok=True)
    y = np.stack([np.full((4, 6), n % 256, np.uint8) for n in frames])
    u = np.stack([np.full((2, 3), 128, np.uint8) for _ in frames])
    np.savez_compressed(
        path, y=y, u=u, v=u.copy(), index=np.array(frames), meta=np.array(json.dumps({}))
    )


def _run_manifest(decoded: str = "hard") -> dict[str, Any]:
    m = _tiny_manifest()
    m["sets"]["open"]["decoded_hash"] = decoded
    m["footage"] = [
        {"id": "a", "set": "open", "prepare": {"decode": "mp4"}, "frames_total": 200},
        {"id": "b", "set": "nc", "prepare": {"decode": "hevc"}, "frames_total": 200},
    ]
    return m


def test_a_moment_loads_its_sources_and_truths(tmp_path: Path) -> None:
    m = _run_manifest()
    two = _moment(footage="a", f=10, s=4)  # sources 10,14,..,26; truths 16, 20
    disp = _moment(footage="a", f=10, s=5, m=5, tier="display")  # 10,15,20,25; 16..19
    _fake_moment_file(tmp_path, "a", 10, sorted(set(pb.window(two)) | set(pb.window(disp))))
    mo = pb.load_moment(np, m, two, tmp_path)
    assert [int(s[0][0, 0]) for s in mo.sources] == [10, 14, 18, 22, 26]
    assert [(o, int(t[0][0, 0])) for o, t in mo.truths] == [(3, 16), (5, 20)]
    assert (mo.group, mo.table, mo.chroma_loc, mo.images_ok) == (
        "live60",
        "headline",
        "left",
        False,
    )
    mo = pb.load_moment(np, m, disp, tmp_path)
    assert [int(s[0][0, 0]) for s in mo.sources] == [10, 15, 20, 25]
    assert [(o, int(t[0][0, 0])) for o, t in mo.truths] == [(6, 16), (7, 17), (8, 18), (9, 19)]


def test_a_moment_that_does_not_match_its_hash_is_refused(tmp_path: Path) -> None:
    mo = _moment(footage="a", f=10, s=2, decoded_sha256="0" * 64)
    _fake_moment_file(tmp_path, "a", 10, pb.window(mo))
    with pytest.raises(pb.BenchError, match="differ from the pinned hash"):
        pb.load_moment(np, _run_manifest("hard"), mo, tmp_path)
    got = pb.load_moment(np, _run_manifest("soft"), mo, tmp_path)
    assert len(got.warnings) == 1
    with pytest.raises(pb.BenchError, match="not prepared"):
        pb.load_moment(np, _run_manifest(), _moment(footage="a", f=50), tmp_path)


def test_blend_mixes_the_frames_around_each_in_between(tmp_path: Path) -> None:
    mo = _moment(footage="a", f=0, s=5, m=5, tier="display")
    _fake_moment_file(tmp_path, "a", 0, pb.window(mo))
    got = pb.blend_frames(np, pb.load_moment(np, _run_manifest(), mo, tmp_path))
    # sources 5 and 10 around outputs 6..9 (k/5 of the way): 6, 7, 8, 9
    assert [int(g[0][0, 0]) for g in got] == [6, 7, 8, 9]


def test_plan_leaves_non_commercial_footage_out_unless_accepted() -> None:
    m = _run_manifest()
    m["moments"] = [_moment(footage="a", f=k * 20) for k in range(3)] + [
        _moment(footage="b", f=0, s=4)
    ]

    def args(**kw: Any) -> Any:
        base = {"tier": "all", "only": None, "accept_nc": False, "limit": None}
        return type("A", (), base | kw)()

    picked, cel, notes = pb.plan_moments(m, args())
    assert [x["footage"] for x in picked] == ["a", "a", "a"]
    assert len(cel) == len(pb.CEL_SCENES) * 2 and "--accept-nc" in notes[0]
    picked, cel, _ = pb.plan_moments(m, args(accept_nc=True, limit=1, tier="2x"))
    assert [x["footage"] for x in picked] == ["a", "b"]
    assert len(cel) == len(pb.CEL_SCENES)  # one variant per scene
    picked, cel, _ = pb.plan_moments(m, args(only="cel-ball"))
    assert picked == [] and cel == [("ball", "ones"), ("ball", "twos")]
    picked, cel, _ = pb.plan_moments(m, args(tier="display"))
    assert cel == []


# ---------------------------------------------------------------- run: cel scenes


@pytest.mark.parametrize("scene", pb.CEL_SCENES)
def test_cel_scenes_are_drawn_the_same_every_time(scene: str) -> None:
    a = pb.cel_frame(np, scene, 3.5, 192, 108)
    b = pb.cel_frame(np, scene, 3.5, 192, 108)
    assert all(np.array_equal(x, y) for x, y in zip(a, b, strict=True))
    assert a[0].shape == (108, 192) and a[1].shape == (54, 96)
    later = pb.cel_frame(np, scene, 4.0, 192, 108)
    assert not np.array_equal(a[0], later[0])  # every scene moves (title after its hold)


def test_cel_motion_is_continuous_below_a_pixel() -> None:
    """Edges are covered analytically, so a tenth of a frame moves the picture
    a little, not by whole pixels or not at all."""
    a = pb.cel_frame(np, "ball", 4.0, 384, 216)[0].astype(np.int16)
    b = pb.cel_frame(np, "ball", 4.1, 384, 216)[0].astype(np.int16)
    c = pb.cel_frame(np, "ball", 5.0, 384, 216)[0].astype(np.int16)
    small, large = np.abs(b - a).sum(), np.abs(c - a).sum()
    assert 0 < small < large / 4


def test_cel_on_twos_holds_drawings_and_title_holds_still_first() -> None:
    ones = pb.cel_moment(np, None, "dash", "ones", 192, 108)
    twos = pb.cel_moment(np, None, "dash", "twos", 192, 108)
    same = [np.array_equal(twos.sources[j][0], twos.sources[j + 1][0]) for j in range(8)]
    assert same == [True, False] * 4
    assert not any(np.array_equal(ones.sources[j][0], ones.sources[j + 1][0]) for j in range(8))
    # the truth is the continuous motion, the same for both variants
    assert [o for o, _ in twos.truths] == [7, 9]
    for (_, a), (_, b) in zip(ones.truths, twos.truths, strict=True):
        assert np.array_equal(a[0], b[0])
    assert np.array_equal(ones.sources[4][0], twos.sources[4][0])
    title = pb.cel_moment(np, None, "title", "ones", 192, 108)
    still = [np.array_equal(title.sources[j][0], title.sources[j + 1][0]) for j in range(8)]
    assert still[:3] == [True, True, True] and not any(still[3:])  # a 4-frame hold


def test_cel_moments_are_kept_and_reused(tmp_path: Path) -> None:
    a = pb.cel_moment(np, tmp_path, "zoom", "twos", 192, 108)
    path = tmp_path / pb.MOMENTS_DIR / "cel-zoom-twos.npz"
    assert path.exists()
    b = pb.cel_moment(np, tmp_path, "zoom", "twos", 192, 108)
    assert np.array_equal(a.truths[1][1][0], b.truths[1][1][0])
    c = pb.cel_moment(np, tmp_path, "zoom", "twos", 96, 54)  # another recipe: drawn again
    assert c.sources[0][0].shape == (54, 96)


# ---------------------------------------------------------------- run: scores and tables


def _meta(group: str, table: str, tier: str = "2x", s: int = 2, m: int = 2) -> dict[str, Any]:
    return {"group": group, "table": table, "tier": tier, "s": s, "m": m}


def _cell(worst: float, mse: float, flicker: float = 1.0) -> dict[str, float]:
    return {"worst": worst, "flicker": flicker, "mean": 0.9, "psnr": 30.0, "mse": mse}


def test_headline_weighs_each_group_the_same_and_keeps_2d_and_film_step_apart() -> None:
    moments = {
        "cg1": _meta("cg", "headline"),
        "cg2": _meta("cg", "headline"),
        "cg3": _meta("cg", "headline"),
        "l50": _meta("live50", "headline"),
        "cel": _meta("2d", "2d"),
        "uvg": _meta("live120", "film-step", s=4),
        "uvg5": _meta("live120", "film-step", "display", 5, 5),
        "cg6": _meta("cg", "headline", "display", 6, 6),
    }
    results = {
        "cg1": {"mvgpu": _cell(0.6, 10), "x": _cell(0.7, 5)},
        "cg2": {"mvgpu": _cell(0.6, 10), "x": _cell(0.7, 5)},
        "cg3": {"mvgpu": _cell(0.6, 10), "x": {"error": "boom"}},
        "l50": {"mvgpu": _cell(0.8, 30), "x": _cell(0.9, 30)},
        "cel": {"mvgpu": _cell(0.1, 99), "x": _cell(0.1, 99)},
        "uvg": {"mvgpu": _cell(0.2, 99), "x": _cell(0.2, 99)},
        "uvg5": {"mvgpu": _cell(0.3, 99), "x": _cell(0.3, 99)},
        "cg6": {"mvgpu": _cell(0.5, 40), "x": _cell(0.5, 20)},
    }
    s = pb.summarize(results, moments, ["mvgpu", "x"])
    assert set(s) == {"2x-headline", "display-headline", "2d", "film-step"}
    head = s["2x-headline"]["headline"]["cells"]
    assert head["mvgpu"]["worst"] == pytest.approx((0.6 + 0.8) / 2)  # not (3*0.6+0.8)/4
    assert head["x"]["worst"] == pytest.approx((0.7 + 0.9) / 2)
    assert s["2x-headline"]["rows"]["cg"]["cells"]["x"]["n"] == 2  # the error is left out
    assert s["2x-headline"]["headline"]["mse_vs_mvgpu"]["x"] == pytest.approx(-12.5)
    assert set(s["film-step"]["rows"]) == {"live120 2x, s=4", "live120 x5"}
    assert s["film-step"]["headline"] is None and s["2d"]["headline"] is None
    assert s["display-headline"]["rows"]["cg x6"]["mse_vs_mvgpu"]["x"] == -50.0
    # a group with two multipliers in the display tier still weighs as one group
    moments |= {
        "l5": _meta("live50", "headline", "display", 5, 5),
        "l6": _meta("live50", "headline", "display", 6, 6),
    }
    results |= {
        "l5": {"mvgpu": _cell(0.1, 10), "x": _cell(0.1, 10)},
        "l6": {"mvgpu": _cell(0.3, 10), "x": _cell(0.3, 10)},
    }
    disp = pb.summarize(results, moments, ["mvgpu", "x"])["display-headline"]
    assert set(disp["rows"]) == {"cg x6", "live50 x5", "live50 x6"}
    assert disp["headline"]["cells"]["mvgpu"]["worst"] == pytest.approx((0.5 + 0.2) / 2)
    doc = {
        "configs": {"mvgpu": {}, "x": {}},
        "summary": s,
        "software": {"buttereye": "0", "git_rev": "abc"},
        "machine": {},
        "skipped": {"y": "not installed"},
    }
    text = pb.markdown(doc)
    # x failed on cg3, so both configurations are averaged over cg1 and cg2
    assert "| cg | 2/3 | 0.600 / 1.00 | 0.700 / 1.00 |" in text
    assert pb.SHORT_NOTE in text
    assert "**headline**" in text and "Not run: y: not installed" in text
    assert (
        text.index("2x tier")
        < text.index("Display tier")
        < text.index("2D")
        < text.index("Film step")
    )


def test_an_engine_failing_on_hard_moments_does_not_flatter_it() -> None:
    """Figures compare every configuration over the same moments: a moment one
    engine failed on is left out for all, and such a result is not published."""
    moments = {f"{g}{i}": _meta(g, "headline") for g in ("cg", "live50") for i in range(4)}
    hard = {"cg3", "live503"}
    results: dict[str, dict[str, Any]] = {
        mid: {
            "mvgpu": _cell(0.5 if mid in hard else 0.9, 5.0 if mid in hard else 1.0),
            "rife-trt-4.26": (
                {"error": "RuntimeError: engine failed"} if mid in hard else _cell(0.9, 1.0)
            ),
        }
        for mid in moments
    }
    s = pb.summarize(results, moments, ["mvgpu", "rife-trt-4.26"])["2x-headline"]
    # both engines scored the same on every moment they both finished
    assert s["headline"]["mse_vs_mvgpu"] == {"mvgpu": 0.0, "rife-trt-4.26": 0.0}
    assert s["headline"]["cells"]["mvgpu"] == s["headline"]["cells"]["rife-trt-4.26"]
    for row in s["rows"].values():
        assert row["moments"] == 4
        assert {c: v["n"] for c, v in row["cells"].items()} == {"mvgpu": 3, "rife-trt-4.26": 3}
        assert row["mse_vs_mvgpu"]["rife-trt-4.26"] == 0.0
    # an engine that failed every moment of a row has no cell there and no headline
    worse = {mid: dict(r) for mid, r in results.items()}
    for mid in ("cg0", "cg1", "cg2"):
        worse[mid]["rife-trt-4.26"] = {"error": "boom"}
    w = pb.summarize(worse, moments, ["mvgpu", "rife-trt-4.26"])["2x-headline"]
    assert set(w["rows"]["cg"]["cells"]) == {"mvgpu"}
    assert w["rows"]["cg"]["cells"]["mvgpu"]["n"] == 4
    assert set(w["headline"]["cells"]) == {"mvgpu"}
    doc = {
        "date": "2026-11-14",
        "configs": {"mvgpu": {}, "rife-trt-4.26": {}},
        "moments": moments,
        "results": {"2x": results},
        "partial": False,
        "skipped": {},
        "software": {"buttereye": "0", "git_rev": "abc"},
        "machine": {},
        "summary": s,
    }
    errs = "\n".join(pb.publish_problems(doc))
    assert "engines failed on some moments" in errs and "rife-trt-4.26 on 2" in errs
    text = pb.markdown(doc | {"summary": {"2x-headline": s}})
    assert "| cg | 3/4 |" in text and pb.SHORT_NOTE in text
    # a result written by an older summarize gets its tables made anew
    stale = doc | {"summary": {"2x-headline": {"rows": {}, "headline": None}}}
    assert pb.merge_results([stale])["summary"]["2x-headline"] == s
    assert stale["summary"]["2x-headline"]["headline"] is None  # the input is left alone


def test_publish_refuses_partial_runs_unknown_configs_and_home_paths() -> None:
    home = str(Path.home())
    doc = {
        "configs": {"blend": {}, "mvgpu": {}},
        "results": {"2x": {"m1": {"blend": {}, "mvgpu": {}}}},
        "partial": False,
        "skipped": {},
        "software": {"plugin_dir": "/usr/lib64/buttereye/vapoursynth"},
    }
    assert pb.publish_problems(doc) == []
    bad = copy.deepcopy(doc)
    bad["configs"]["reference"] = {}
    bad["results"]["2x"]["m1"]["reference"] = {}
    bad["partial"] = True
    bad["software"]["engine"] = home + "/.cache/buttereye/engines/x"
    errs = "\n".join(pb.publish_problems(bad))
    assert "not one of the named" in errs and "third-party reference" in errs
    assert "part of the set" in errs and "home folder" in errs
    skipped = copy.deepcopy(doc) | {"skipped": {"mvgpu": "engine build failed"}}
    assert "were skipped: mvgpu" in "\n".join(pb.publish_problems(skipped))
    assert pb.publish_problems(skipped | {"skipped_allowed": ["mvgpu"]}) == []
    assert pb.publish_problems(pb.redact(bad["software"], home) | {}) == []
    assert pb.redact({"a": [home + "/x"]}, home) == {"a": ["~/x"]}


def test_images_only_for_footage_that_allows_them(tmp_path: Path) -> None:
    m = pb.load_manifest()
    y = np.full((4, 6), 120, np.uint8)
    uv = np.full((2, 3), 128, np.uint8)
    pb.write_dump(np, tmp_path, "cg-mvgpu-m", (y, uv, uv), (y + 8, uv, uv), m["sets"]["bbb"])
    assert "Blender Foundation" in (tmp_path / "cg-mvgpu-m.txt").read_text()
    png = (tmp_path / "cg-mvgpu-m-error.png").read_bytes()
    assert png.startswith(b"\x89PNG") and b"IEND" in png
    raw = zlib.decompress(png[png.index(b"IDAT") + 4 :])
    assert raw[:7] == b"\x00" + bytes([64]) * 6  # filter byte, then 8 x 8 levels
    for name in ("uvg", "elfuente"):
        with pytest.raises(pb.BenchError, match="numbers only"):
            pb.write_dump(np, tmp_path, "x", (y, uv, uv), (y, uv, uv), m["sets"][name])


def test_truth_scores_report_squared_error_and_sort_any_set_name() -> None:
    truth = pb._truth()
    a = np.full((32, 32), 100, np.uint8)
    assert truth.score(np, a, a + 2)["mse"] == 4.0
    names = ["clip10@1.0.npz", "bbb@234.npz", "clip2@3.5.npz", "bbb@1234.npz", "clip2@10.0.npz"]
    got = [p.name for p in sorted(map(Path, names), key=truth.set_key)]
    assert got == ["bbb@234.npz", "bbb@1234.npz", "clip2@3.5.npz", "clip2@10.0.npz",
                   "clip10@1.0.npz"]  # fmt: skip


def test_every_named_configuration_is_well_formed() -> None:
    for name, spec in pb.CONFIGS.items():
        assert set(spec["tiers"]) <= set(pb.TIERS), name
        assert set(spec.get("groups", pb.GROUPS)) <= set(pb.GROUPS), name
        assert spec["backend"] in (None, "auto", "mvtools", "mvgpu", "rife-trt", "rife-ncnn")
        if spec.get("cascade"):
            assert spec["tiers"] == ("display",) and spec["backend"].startswith("rife")
    assert pb.CONFIGS["blend"]["backend"] is None
    two_d = pb.CONFIGS["profile-2d"]
    assert two_d["groups"] == ("2d",) and two_d["hold_fill"] and not two_d.get("cascade")


def test_no_row_runs_a_step_playback_does_not_have() -> None:
    # playback has no half-flow step since spike M0(x); a row for it would
    # publish numbers for something no one plays
    from buttereye.core.mpvctl import decide

    assert not hasattr(decide, "HALF_FLOW_GAIN")
    assert all(float(s.get("flow_scale", 1.0)) == 1.0 for s in pb.CONFIGS.values())


def test_profile_2d_takes_the_first_engine_of_the_apps_2d_order(tmp_path: Path) -> None:
    """RIFE on TensorRT, then RIFE-ncnn, then MVTools before mvgpu: whatever
    runs first here, as the session's ANIMATION_RANKING orders them."""
    from buttereye.core.mpvctl.session import ANIMATION_RANKING
    from buttereye.core.types import BackendId

    assert ANIMATION_RANKING.index(BackendId.MVTOOLS) < ANIMATION_RANKING.index(BackendId.MVGPU)
    eng = pb.Engines.__new__(pb.Engines)
    eng.consts = type("C", (), {"plugin_dir": tmp_path})()
    have: set[str] = set()

    def trt(spec: dict[str, Any]) -> dict[str, Any]:
        if "trt" not in have:
            raise pb.Unavailable("no TensorRT")
        return {}

    def ncnn(spec: dict[str, Any]) -> Path:
        if "ncnn" not in have:
            raise pb.Unavailable("no ncnn model")
        return tmp_path

    eng._trt, eng._ncnn = trt, ncnn  # type: ignore[method-assign]
    picked = []
    for add in ("", "mvgpu.so", "mvtools.so", "ncnn", "trt"):
        if add.endswith(".so"):
            (tmp_path / add).write_bytes(b"")
        elif add:
            have.add(add)
        eng.resolved = {}
        try:
            got = eng.resolve("profile-2d")
        except pb.Unavailable as exc:
            picked.append(f"none: {exc}"[:40])
            continue
        assert got["hold_fill"] is True and not got.get("cascade")
        picked.append(got["backend"] + ("/" + got["model"] if got.get("model") else ""))
    assert picked[0].startswith("none: no engine of the app")
    assert picked[1:] == [
        "mvgpu",
        "mvtools",  # MVTools before mvgpu for 2D
        "rife-ncnn/" + pb.AUTO_MODEL,
        "rife-trt/" + pb.AUTO_MODEL,
    ]
    assert eng.resolve("mvgpu") is pb.CONFIGS["mvgpu"]  # named engines are not resolved


# ---------------------------------------------------------------- run: the real template (CPU)


def test_the_playback_template_runs_mvtools_on_a_moment(xdg_env: Any) -> None:
    """The template itself, exec'd per moment with the app's user_data, on the
    CPU engine: no GPU. Skipped without VapourSynth or ButterEye's MVTools."""
    vs = pytest.importorskip("vapoursynth")
    eng = pb.Engines(np, build=False, hold_fill=True, cache_mb=256, threads=2)
    if not (eng.consts.plugin_dir / "mvtools.so").is_file():
        pytest.skip("ButterEye's MVTools plugin is not installed")
    drawn = eng.truth.draw(np, "pan", 144, 256, 9)
    mo = pb.Moment(
        id="pan", set="t", group="cg", table="headline", tier="2x", s=2, m=2, footage="t",
        sources=drawn[0::2], truths=[(3, drawn[3]), (5, drawn[5])], images_ok=False,
        chroma_loc="left",
    )  # fmt: skip
    ud = json.loads(eng.user_data("mvtools", 2, "left"))
    assert ud["backend"] == "mvtools" and ud["hold_fill"] is True and ud["still_skip"] is True
    assert ud["sc_threshold"] == 0.12 and ud["chromaloc"] == "left"
    assert ud["src_fps"] == [24, 1] and ud["target_fps"] == [48, 1]
    got = pb.score_moment(eng.truth, np, eng.run("mvtools", mo), mo)
    floor = pb.score_moment(eng.truth, np, eng.run("blend", mo), mo)
    assert got["worst"] > floor["worst"] and got["mse"] < floor["mse"]
    assert vs.get_outputs() == {} or len(vs.get_outputs()) == 0  # nothing left behind


# ---------------------------------------------------------------- selection


def test_candidate_starts_try_either_side_and_stay_inside() -> None:
    assert pb.candidate_starts(300, 120, 16, 600) == [300, 360, 240, 420, 180, 480, 120]
    # 60 fps: half a second is 30 frames; a start whose window leaves the footage is not tried
    assert pb.candidate_starts(20, 60, 8, 100) == [20, 50, 80]


def test_judge_rejects_still_cut_and_held_windows() -> None:
    assert pb.judge([3.0, 3.2, 2.9, 3.1], []) is None
    assert "too little motion" in str(pb.judge([0.4, 3.0, 3.0, 3.0], []))
    assert "a cut" in str(pb.judge([3.0, 40.0, 3.1, 2.9], []))
    assert "held frame at 12" in str(pb.judge([3.0, 3.0, 3.0], [12]))
    # a cut the source steps miss: fast motion right after it keeps two steps large
    natives = [0.4, 0.5, 0.4, 0.5, 59.3, 8.0, 8.4, 9.4, 11.7, 14.7, 18.1, 22.7]
    assert "native step 5 is 59.3" in str(pb.judge([2.0, 57.9, 56.8], [], 1.0, natives))
    assert pb.judge([20.0, 21.0, 22.0], [], 1.0, [8.0, 8.4, 9.4, 11.7, 14.7, 18.1]) is None


def test_pair_stats_measure_source_steps_and_find_held_frames() -> None:
    stats = pb.PairStats(np, {2})
    ramp = np.arange(64, dtype=np.uint8)[None, :].repeat(8, axis=0)
    for n in range(10, 19):
        shift = n if n != 15 else 14  # frame 15 repeats frame 14
        stats.feed(n, np.roll(ramp, shift, axis=1))
    got = stats.window(10, 2, "2x")
    assert got is not None
    steps, holds, natives = got
    assert len(steps) == 4 and min(steps) > 1.0 and len(natives) == 8
    assert holds == [15]
    assert stats.window(12, 2, "2x") is None  # frames 19 and 20 were never fed


def test_apply_selection_moves_drops_and_trims_pinned_frames() -> None:
    m = _tiny_manifest()
    m["footage"] = [
        {"id": "a", "set": "open", "fetch": "y4m-range", "urls": ["https://e/a"],
         "frames_total": 100, "frames": {str(n): {"sha256": "x"} for n in range(0, 9)}},
    ]  # fmt: skip
    keep = _moment(footage="a", f=0, decoded_sha256="h0")
    move = _moment(footage="a", f=0, s=4, m=4, tier="display", decoded_sha256="h1")
    lose = _moment(footage="a", f=0, s=2, m=2, tier="2x", decoded_sha256="h2")
    lose["f"] = 0
    m["moments"] = [keep, move]
    picks = [
        {"moment": keep, "f": 0, "reason": None},
        {"moment": move, "f": 30, "reason": None},
    ]
    dropped = pb.apply_selection(m, picks, 1.0)
    assert dropped == []
    assert keep["decoded_sha256"] == "h0"
    assert move["f"] == 30 and move["decoded_sha256"] is None
    # frames of the moved window are not pinned yet; those of the kept one stay
    assert set(m["footage"][0]["frames"]) == {str(n) for n in range(0, 9)}
    m["moments"].append(lose)
    dropped = pb.apply_selection(m, [{"moment": lose, "f": None, "reason": "still"}], 1.0)
    assert dropped == ["a 2x s=2 near 0: still"]
    assert lose not in m["moments"] and m["moments_selected"] is True
    assert m["selection"]["dropped"] == dropped


def _acescc(lin: np.ndarray) -> np.ndarray:
    return ((np.log2(lin) + 9.72) / 17.52).astype(np.float32)


def test_exr_exposure_protects_highlights_by_one_stop_at_most() -> None:
    dim = np.full((8, 8), 0.09, dtype=np.float32)  # median luma 0.09: the median rule gives 2.0
    plain = {c: _acescc(dim) for c in "RGB"}
    assert pb.exr_exposure(np, plain) == 2.0
    bright = dim.copy()
    bright[:2] = 5.0  # a quarter of the picture clips even at 1.6: pulled one stop, no more
    hot = {c: _acescc(bright) for c in "RGB"}
    assert pb.exr_exposure(np, hot) == 1.0
    some = dim.copy()
    some[:2] = 2.5  # 2.5 * 1.6 = 4.0: a smaller pull is enough
    assert pb.exr_exposure(np, {c: _acescc(some) for c in "RGB"}) == 1.6


# ---------------------------------------------------------------- report, merge, compare


def _doc(tier: str, mids: dict[str, dict[str, Any]], rows: dict[str, Any]) -> dict[str, Any]:
    cfg = {"spec": {}, "user_data": {"x2/left": {"backend": "mvgpu"}}}
    flat = dict(rows)
    return {
        "schema": 1,
        "set_version": 1,
        "manifest_sha256": "m",
        "moments_selected": True,
        "partial": False,
        "date": "2026-11-14",
        "machine": {"gpu": "g"},
        "software": {"git_rev": "abc"},
        "configs": {"blend": {"spec": {}, "user_data": {}}, "mvgpu": cfg},
        "skipped": {},
        "moments": mids,
        "results": {tier: rows},
        "summary": pb.summarize(flat, mids, ["blend", "mvgpu"]),
        "warnings": [],
        "seconds": 10.0,
    }


def test_runs_of_two_tiers_merge_into_one_publishable_result(tmp_path: Path) -> None:
    a = _doc("2x", {"bbb@1": _meta("cg", "headline")},
             {"bbb@1": {"blend": _cell(0.2, 50), "mvgpu": _cell(0.6, 10)}})  # fmt: skip
    b = _doc("display", {"bbb@1x6": _meta("cg", "headline", "display", 6, 6)},
             {"bbb@1x6": {"blend": _cell(0.1, 80), "mvgpu": _cell(0.5, 20)}})  # fmt: skip
    doc = pb.merge_results([a, b])
    assert set(doc["results"]) == {"2x", "display"}
    assert set(doc["summary"]) == {"2x-headline", "display-headline"}
    assert doc["seconds"] == 20.0
    assert pb.publish(doc, tmp_path).name == "results-2026-11-14.json"
    assert pb.published_as(doc, tmp_path) == "results-2026-11-14.json"
    later = copy.deepcopy(doc) | {"seconds": 30.0}
    assert pb.published_as(later, tmp_path) is None
    # a second result of the same day never overwrites the first
    assert pb.publish(later, tmp_path).name == "results-2026-11-14b.json"
    assert pb.publish(later, tmp_path).name == "results-2026-11-14c.json"
    assert json.loads((tmp_path / "results-2026-11-14.json").read_text()) == doc
    other = copy.deepcopy(b)
    other["software"]["git_rev"] = "def"
    with pytest.raises(pb.BenchError, match="software differs"):
        pb.merge_results([a, other])
    with pytest.raises(pb.BenchError, match="both runs scored tier 2x"):
        pb.merge_results([a, a])
    other = copy.deepcopy(b)
    other["configs"]["mvgpu"]["user_data"]["x2/left"] = {"backend": "mvtools"}
    with pytest.raises(pb.BenchError, match="other settings"):
        pb.merge_results([a, other])


def test_compare_names_what_moved() -> None:
    old = _doc("2x", {"bbb@1": _meta("cg", "headline"), "bbb@9": _meta("cg", "headline")},
               {"bbb@1": {"blend": _cell(0.2, 50), "mvgpu": _cell(0.6, 10)},
                "bbb@9": {"blend": _cell(0.2, 50), "mvgpu": _cell(0.6, 10)}})  # fmt: skip
    new = _doc("2x", {"bbb@1": _meta("cg", "headline")},
               {"bbb@1": {"blend": _cell(0.2, 50), "mvgpu": _cell(0.65, 8)}})  # fmt: skip
    diff = pb.compare(old, new)
    cell = diff["tables"]["2x-headline"]["cg"]["mvgpu"]
    assert cell["worst"] == pytest.approx(0.05) and cell["mse_pct"] == -20.0
    assert diff["tables"]["2x-headline"]["headline"]["blend"]["worst"] == 0.0
    assert diff["only_old"] == ["bbb@9"]
    text = pb.compare_text(diff)
    assert "| cg | +0.0000 / +0.0 | +0.0500 / -20.0 |" in text
    assert "moments only in the old file: bbb@9" in text


def test_selection_keeps_what_earlier_runs_dropped() -> None:
    m = _tiny_manifest()
    m["footage"] = [
        {"id": fid, "set": "open", "fetch": "whole", "urls": ["https://e/a"], "frames_total": 100}
        for fid in ("a", "b")
    ]
    a, b = _moment(footage="a"), _moment(footage="b")
    m["moments"] = [a, b]
    pb.apply_selection(m, [{"moment": a, "f": None, "reason": "still"}], 1.0)
    pb.apply_selection(m, [{"moment": b, "f": 10, "reason": None}], 1.0)
    assert m["selection"]["dropped"] == ["a 2x s=2 near 10: still"]
    # a run over the same footage again cannot see the dropped moment, and keeps it listed
    m["footage"][0]["frames_total"] = 100
    pb.apply_selection(m, [], 1.0)
    assert m["selection"]["dropped"] == ["a 2x s=2 near 10: still"]
    assert m["moments"] == [b]
    m["selection"]["notes"] = ["moved by hand first"]
    pb.apply_selection(m, [{"moment": b, "f": 12, "reason": None}], 1.0)
    assert m["selection"]["notes"] == ["moved by hand first"]  # other keys are kept


def test_a_cap_limits_the_moments_inside_a_span() -> None:
    m = _tiny_manifest()
    m["footage"] = [
        {"id": "a", "set": "open", "fetch": "whole", "urls": ["https://e/a"], "frames_total": 500}
    ]
    m["moments"] = [_moment(footage="a", f=f) for f in (10, 300, 350, 400)]
    m["moments"].append(_moment(footage="a", f=420, s=6, m=6, tier="display"))
    m["caps"] = [{"footage": "a", "tier": "2x", "from": 300, "max": 2, "what": "the credits"}]
    errs = pb.validate(m)
    assert errs == ["footage a 2x: 3 moments start in the credits ([300, 350, 400]), at most 2"]
    m["moments"][3]["f"] = 100  # the display moment at 420 does not count
    assert pb.validate(m) == []
    assert pb.load_manifest()["caps"][0]["footage"] == "bbb"


def test_the_docs_get_the_tables_and_every_credit_line() -> None:
    a = _doc("2x", {"bbb@1": _meta("cg", "headline")},
             {"bbb@1": {"blend": _cell(0.2, 50), "mvgpu": _cell(0.6, 10)}})  # fmt: skip
    m = pb.load_manifest()
    page = "x\n<!-- results:start -->\nold\n<!-- results:end -->\ny\n"
    page += "<!-- footage:start -->\n<!-- footage:end -->\n"
    out = pb.fill_docs(page, a, m)
    assert "old" not in out and "| cg | 1 | 0.200 / 1.00 | 0.600 / 1.00 |" in out
    assert "`docs/benchmark/results-2026-11-14.json`" in out
    assert "`docs/benchmark/results-2026-11-14b.json`" in pb.fill_docs(
        page, a, m, "results-2026-11-14b.json"
    )
    assert out.startswith("x\n") and "\ny\n" in out
    for st in m["sets"].values():
        assert st["attribution"] in out
    assert "| numbers only |" in out
    with pytest.raises(pb.BenchError, match="no results markers"):
        pb.fill_docs("nothing", a, m)
    page = (pb.REPO / "docs" / "benchmark.md").read_text()
    assert "/home/" not in page
