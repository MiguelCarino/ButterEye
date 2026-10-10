# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""The ``buttereye`` command (SCOPE §4.1) over FakeCore: commands, ``--json``,
exit codes, and no Qt in the CLI process."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from buttereye import cli
from buttereye.core.testing import FakeCore


def opener(scenario: str, after_open: Any = None) -> Any:
    fake = FakeCore.for_scenario(scenario)

    async def open_fake(paths: Any) -> Any:
        core = await fake.open(paths)
        if after_open is not None:
            asyncio.get_running_loop().create_task(after_open(core))
        return core

    return open_fake


def run(*argv: str, scenario: str = "all_ready", after_open: Any = None) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), opener=opener(scenario, after_open), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def doc(text: str) -> dict[str, Any]:
    d = json.loads(text)
    assert d["v"] == cli.JSON_VERSION
    return d  # type: ignore[no-any-return]


def test_version_and_usage() -> None:
    code, out, _ = run("--version")
    assert code == 0 and out.startswith("ButterEye ") and "Affero" in out
    code, out, _ = run()
    assert code == cli.EXIT_USAGE and "COMMAND" in out
    code, _, _ = run("bench", "--fps", "zero")
    assert code == cli.EXIT_USAGE


def test_doctor_human_and_json() -> None:
    code, out, _ = run("doctor")
    assert code == 0 and "blocking" in out
    code, out, _ = run("doctor", "--json")
    d = doc(out)
    assert code == 0 and d["command"] == "doctor" and d["ok"] is True
    assert isinstance(d["data"]["findings"], list)


def test_doctor_blocking_exits_3() -> None:
    code, out, _ = run("doctor", "--json", scenario="blocking_mpv")
    assert code == cli.EXIT_BLOCKING
    assert doc(out)["ok"] is False


def test_unimplemented_command_says_so() -> None:
    code, _, err = run("attach", "--socket", "/tmp/x")
    assert code == 1 and "BE-9001" in err
    code, out, _ = run("detach", "--orphans", "--json")
    d = doc(out)
    assert code == 1 and d["ok"] is False and d["error"]["code"] == "BE-9001"


def test_profiles_list_and_explain() -> None:
    code, out, _ = run("profiles", "list", "--json")
    d = doc(out)
    assert code == 0 and any(p["id"] == "quality" for p in d["data"]["profiles"])
    code, out, _ = run("profiles", "explain", "--fps", "24000/1001", "--height", "2160")
    assert code == 0 and "→ profile" in out


def test_plugins_models_clean_licence() -> None:
    for argv in (("plugins",), ("models",), ("clean", "--dry-run"), ("licence",)):
        code, out, _ = run(*argv, "--json")
        assert code == 0, argv
        assert doc(out)["command"] == argv[0]
    code, out, _ = run("licence")
    assert "Third-party components" in out and "Affero" in out


def test_bench_runs_and_lists() -> None:
    code, out, _ = run("bench", "--json", scenario="bench_results")
    d = doc(out)
    assert code == 0 and d["data"]["measurements"]
    code, out, _ = run("bench", "--history", scenario="bench_results")
    assert code == 0 and "recommended" in out


def test_render_follows_the_job_to_the_end(tmp_path: Path) -> None:
    src = tmp_path / "film.mkv"
    src.write_bytes(b"x")
    code, out, _ = run("render", str(src), "--json")
    d = doc(out)
    assert code == 0 and d["data"]["state"] == "succeeded"
    assert d["data"]["spec"]["output"] == str(tmp_path / "film.smooth.mkv")


def test_play_stays_until_the_session_ends(tmp_path: Path) -> None:
    video = tmp_path / "film.mkv"
    video.write_bytes(b"x")

    async def detach_soon(core: Any) -> None:
        for _ in range(100):
            await asyncio.sleep(0.05)
            sessions = await core.sessions()
            if sessions:
                await core.detach(sessions[0].sid)
                return

    code, out, _ = run("play", str(video), after_open=detach_soon)
    assert code == 0 and "Playing film.mkv" in out and "mpv closed" in out


def test_play_refused_without_live_play(tmp_path: Path) -> None:
    video = tmp_path / "film.mkv"
    video.write_bytes(b"x")
    code, _, err = run("play", str(video), scenario="fresh_box")
    assert code != 0 and err


def test_json_values_are_plain() -> None:
    from fractions import Fraction

    from buttereye.core.types import BackendId, Msg

    assert cli.plain({"r": Fraction(24000, 1001), "b": BackendId.MVTOOLS, "m": Msg("Hi")}) == {
        "r": "24000/1001",
        "b": "mvtools",
        "m": "Hi",
    }


def test_cli_imports_no_qt() -> None:
    code = (
        "import sys, buttereye.cli;"
        "assert not [m for m in sys.modules if m.startswith(('PySide6', 'shiboken6'))]"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize("argv", [("doctor", "-q"), ("plugins", "-q")])
def test_quiet_prints_nothing_on_success(argv: tuple[str, ...]) -> None:
    code, out, _ = run(*argv)
    assert code == 0 and out == ""
