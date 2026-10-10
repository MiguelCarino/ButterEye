# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Per-file logs the user can copy: the in-memory book, tool-log tails, and the
core's session_log / job_log."""

from __future__ import annotations

import logging
from pathlib import Path

from buttereye.core import filelog
from buttereye.core.api import ButterEye
from buttereye.core.testing import scenarios as sc
from buttereye.core.types import JobId, SessionId


def _book() -> tuple[filelog.FileLogBook, logging.Logger]:
    book = filelog.FileLogBook()
    logger = logging.getLogger("buttereye.core.test_filelog")
    logger.setLevel(logging.INFO)
    logger.addHandler(book)
    return book, logger


def test_book_keeps_lines_per_item_only() -> None:
    book, logger = _book()
    try:
        a = filelog.for_item(logger, filelog.session_key("a"))
        b = filelog.for_item(logger, filelog.job_key("b"))
        a.info("%s: smoothing", "film.mkv")
        b.warning("copy failed")
        logger.info("not about any file")
        assert [ln.split(" ", 2)[2] for ln in book.lines("session:a")] == [
            "INFO film.mkv: smoothing"
        ]
        assert book.lines("job:b")[0].endswith("WARNING copy failed")
        assert book.lines("session:nobody") == []
    finally:
        logger.removeHandler(book)


def test_book_bounds_lines_and_items() -> None:
    book, logger = _book()
    try:
        one = filelog.for_item(logger, "k0")
        for i in range(filelog.MAX_LINES + 5):
            one.info("line %d", i)
        assert len(book.lines("k0")) == filelog.MAX_LINES
        assert book.lines("k0")[-1].endswith(f"line {filelog.MAX_LINES + 4}")
        for i in range(1, filelog.MAX_ITEMS + 1):
            filelog.for_item(logger, f"k{i}").info("x")
        assert book.lines("k0") == []  # the oldest item went first
        assert book.lines(f"k{filelog.MAX_ITEMS}")
    finally:
        logger.removeHandler(book)


def test_tail_reads_the_end_and_collapses_progress(tmp_path: Path) -> None:
    log = tmp_path / "render-x.log"
    log.write_bytes(
        b"first\n" + b"Frame: 1/9\rFrame: 9/9\n" + b"\n".join(b"n%d" % i for i in range(300))
    )
    out = filelog.tail(log, lines=5)
    assert out == ["n295", "n296", "n297", "n298", "n299"]
    assert "Frame: 9/9" in filelog.tail(log, lines=400)
    assert filelog.tail(log, lines=400, max_bytes=20)[0] != "first"  # a cut first line is dropped
    assert filelog.tail(tmp_path / "missing.log") == []


async def test_core_session_and_job_logs(tmp_path: Path) -> None:
    paths = sc.fake_paths(tmp_path)
    core = await ButterEye.open(paths)
    try:
        paths.logs_dir.mkdir(parents=True, exist_ok=True)
        (paths.logs_dir / "render-j1.log").write_text("ffmpeg: Error while encoding\n")
        job_logger = logging.getLogger("buttereye.core.render.jobs")
        filelog.for_item(job_logger, filelog.job_key("j1")).info("%s: converting", "film.mkv")
        text = await core.job_log(JobId("j1"))
        assert text.startswith("ButterEye ") and "Job: j1" in text
        assert "film.mkv: converting" in text
        assert "ffmpeg: Error while encoding" in text
        mpv = await core.session_log(SessionId("s9"))
        assert "Session: s9" in mpv and "(nothing logged for this file yet)" in mpv
        assert "(no log yet)" in mpv
    finally:
        await core.close(cancel_jobs=True)
    # after close the core no longer collects lines
    assert core._file_logs not in logging.getLogger("buttereye.core").handlers
