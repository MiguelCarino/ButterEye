# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Shared fixtures and marker gating for the ButterEye test suite.

Markers (pyproject.toml): ``integration`` needs mpv + vspipe on PATH; ``gpu``
needs /dev/nvidia0 or a non-CPU Vulkan ICD with a render node; ``devbox`` needs
the installed buttereye-* RPMs; ``manual`` never runs unless ``--run-manual``.

Memory failsafes (``tools/memguard.py``): the run refuses to start when the
machine is short of memory, re-runs itself inside a systemd user scope capped
at ``TEST_MEM_MAX_MIB`` (the kernel kills only the tests when they exceed it),
and a watchdog stops the run and every mpv/vspipe it started when the system's
available memory falls below the floor. See that module for the overrides.
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tools import memguard

REPO_ROOT = Path(__file__).resolve().parent.parent

DEVBOX_RPMS = ("buttereye-vs-rife-ncnn", "buttereye-vs-mvtools", "buttereye-rife-ncnn-models")


TEST_MEM_MAX_MIB = 8192
_WATCHDOG = pytest.StashKey[memguard.Watchdog]()


def pytest_configure(config: pytest.Config) -> None:
    reason = memguard.preflight()
    if reason is not None:
        pytest.exit(f"memory failsafe: {reason}", returncode=3)
    cap = memguard.max_mib(TEST_MEM_MAX_MIB)
    argv = [sys.executable, "-m", "pytest", *config.invocation_params.args]
    why = memguard.reexec_capped(argv, cap, "tests")  # returns only when not re-run
    if not memguard.capped():
        print(f"memory failsafe: no hard cap ({why}); watchdog only", file=sys.stderr)
    config.stash[_WATCHDOG] = memguard.Watchdog(label="pytest").start()


def pytest_report_header(config: pytest.Config) -> str:
    cap = os.environ.get("BUTTEREYE_MEMCAP")
    head = f"memory failsafe: cap {cap} MiB" if cap else "memory failsafe: no hard cap"
    return f"{head}, watchdog floor {memguard.floor_mib()} MiB available"


def pytest_unconfigure(config: pytest.Config) -> None:
    dog = config.stash.get(_WATCHDOG, None)
    if dog is not None:
        dog.stop()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-manual", action="store_true", default=False, help="run tests marked 'manual'"
    )


@functools.cache
def have_integration() -> tuple[bool, str]:
    missing = [b for b in ("mpv", "vspipe") if shutil.which(b) is None]
    return (not missing, f"needs {' and '.join(missing)} on PATH" if missing else "")


@functools.cache
def have_gpu() -> tuple[bool, str]:
    if Path("/dev/nvidia0").exists():
        return True, ""
    render_nodes = list(Path("/dev/dri").glob("renderD*")) if Path("/dev/dri").is_dir() else []
    icds: list[Path] = []
    for d in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d"):
        p = Path(d)
        if p.is_dir():
            icds += [f for f in p.glob("*.json") if "lvp" not in f.name]  # lavapipe = CPU
    if render_nodes and icds:
        return True, ""
    return False, "needs /dev/nvidia0 or a non-CPU Vulkan device"


@functools.cache
def have_devbox() -> tuple[bool, str]:
    if shutil.which("rpm") is None:
        return False, "needs rpm and the buttereye-* RPMs"
    try:
        res = subprocess.run(
            ["rpm", "-q", *DEVBOX_RPMS], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"rpm -q failed: {exc}"
    if res.returncode != 0:
        missing = [ln for ln in res.stdout.splitlines() if "not installed" in ln]
        return False, "; ".join(missing) or "buttereye-* RPMs not installed"
    return True, ""


_GATES = {"integration": have_integration, "gpu": have_gpu, "devbox": have_devbox}


def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.get_closest_marker("manual") and not item.config.getoption("--run-manual"):
        pytest.skip("manual test (use --run-manual)")
    for name, gate in _GATES.items():
        if item.get_closest_marker(name):
            ok, why = gate()
            if not ok:
                pytest.skip(f"{name}: {why}")


@dataclass(frozen=True)
class XdgEnv:
    home: Path
    config: Path
    data: Path
    state: Path
    cache: Path
    runtime: Path


@pytest.fixture
def xdg_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[XdgEnv]:
    """Point HOME and every XDG dir at fresh temporary directories.

    The runtime dir is 0700. It is created under the real ``$XDG_RUNTIME_DIR`` as
    ``buttereye-test-*`` when available (Unix socket paths must stay short), else
    under ``tmp_path``; it is removed afterwards.
    """
    root = tmp_path / "xdg"
    dirs = {name: root / name for name in ("home", "config", "data", "state", "cache")}
    for d in dirs.values():
        d.mkdir(parents=True)
    real_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if real_runtime and Path(real_runtime).is_dir() and os.access(real_runtime, os.W_OK):
        runtime = Path(tempfile.mkdtemp(prefix="buttereye-test-", dir=real_runtime))
        cleanup = True
    else:
        runtime = root / "runtime"
        runtime.mkdir()
        cleanup = False
    runtime.chmod(0o700)
    monkeypatch.setenv("HOME", str(dirs["home"]))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(dirs["config"]))
    monkeypatch.setenv("XDG_DATA_HOME", str(dirs["data"]))
    monkeypatch.setenv("XDG_STATE_HOME", str(dirs["state"]))
    monkeypatch.setenv("XDG_CACHE_HOME", str(dirs["cache"]))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    try:
        yield XdgEnv(
            home=dirs["home"],
            config=dirs["config"],
            data=dirs["data"],
            state=dirs["state"],
            cache=dirs["cache"],
            runtime=runtime,
        )
    finally:
        if cleanup:
            shutil.rmtree(runtime, ignore_errors=True)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


class DesignDoc:
    """Access to the python blocks of docs/design/GUI.md (the frozen contract)."""

    path = REPO_ROOT / "docs" / "design" / "GUI.md"

    def __init__(self) -> None:
        self.text = self.path.read_text(encoding="utf-8")

    def section(self, heading: str) -> str:
        """Text from the heading line starting with ``heading`` to the next heading."""
        lines = self.text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln.startswith(heading))
        out: list[str] = []
        fenced = False
        for ln in lines[start + 1 :]:
            if ln.lstrip().startswith("```"):
                fenced = not fenced
            elif not fenced and ln.startswith("#"):
                break
            out.append(ln)
        return "\n".join(out)

    def python(self, heading: str) -> str:
        """The first ```python block under ``heading`` (non-Python prose lines dropped)."""
        body = self.section(heading)
        start = body.index("```python") + len("```python")
        end = body.index("```", start)
        code = body[start:end]
        keep = [ln for ln in code.splitlines() if not ln.startswith("__all__ re-exports")]
        return "\n".join(keep)


@pytest.fixture(scope="session")
def design_doc() -> DesignDoc:
    return DesignDoc()
