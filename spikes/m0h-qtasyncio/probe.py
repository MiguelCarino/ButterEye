# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 The ButterEye contributors
"""Spike M0(h): can PySide6.QtAsyncio drive ButterEye's core loop?

The core needs: subprocesses (mpv, vspipe, ffmpeg, nvidia-smi, vulkaninfo),
Unix-socket IPC to mpv, pipes, and timers. This reproducer
  1. lists every QAsyncioEventLoop method whose body raises NotImplementedError
     (static, from the installed source),
  2. calls the operations the core needs under QtAsyncio.run() and records each
     outcome,
  3. runs the same operations on a stock asyncio loop in a non-main thread (the
     thread-bridge fallback, GUI.md §3).

Run:  QT_QPA_PLATFORM=offscreen .venv/bin/python spikes/m0h-qtasyncio/probe.py
No network, no files outside a temporary directory.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import socket
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import PySide6  # type: ignore[import-untyped]  # noqa: E402
from PySide6 import QtAsyncio  # noqa: E402
from PySide6.QtCore import QCoreApplication  # type: ignore[import-untyped]  # noqa: E402

Check = Callable[[], Awaitable[str]]


def static_not_implemented() -> list[str]:
    src = inspect.getsource(QtAsyncio.events)
    tree = ast.parse(src)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "QAsyncioEventLoop":
            for fn in node.body:
                if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for sub in ast.walk(fn):
                    if (
                        isinstance(sub, ast.Raise)
                        and sub.exc is not None
                        and "NotImplementedError" in ast.unparse(sub.exc)
                    ):
                        out.append(fn.name)
                        break
    return out


def checks(tmp: str) -> list[tuple[str, Check]]:
    async def subprocess_exec() -> str:
        proc = await asyncio.create_subprocess_exec(
            "sh", "-c", "echo hi", stdout=asyncio.subprocess.PIPE, start_new_session=True
        )
        out, _ = await proc.communicate()
        return f"rc={proc.returncode} out={out.decode().strip()!r}"

    async def subprocess_shell() -> str:
        proc = await asyncio.create_subprocess_shell("true")
        return f"rc={await proc.wait()}"

    async def unix_connection() -> str:
        path = os.path.join(tmp, f"s-{threading.get_ident()}.sock")
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        srv.listen(1)
        try:
            reader, writer = await asyncio.open_unix_connection(path)
            writer.close()
            return "connected"
        finally:
            srv.close()
            os.unlink(path)

    async def add_reader() -> str:
        a, b = socket.socketpair()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bytes] = loop.create_future()
        try:
            loop.add_reader(a.fileno(), lambda: fut.set_result(a.recv(16)))
            b.send(b"x")
            data = await asyncio.wait_for(fut, 2)
            loop.remove_reader(a.fileno())
            return f"read {data!r}"
        finally:
            a.close()
            b.close()

    async def sock_recv() -> str:
        a, b = socket.socketpair()
        a.setblocking(False)
        try:
            b.send(b"y")
            data = await asyncio.get_running_loop().sock_recv(a, 16)
            return f"read {data!r}"
        finally:
            a.close()
            b.close()

    async def read_pipe() -> str:
        r, w = os.pipe()
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(r, "rb", 0)
        )
        os.write(w, b"z\n")
        os.close(w)
        line = await reader.readline()
        transport.close()
        return f"read {line!r}"

    async def signal_handler() -> str:
        import signal

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGUSR1, lambda: None)
        loop.remove_signal_handler(signal.SIGUSR1)
        return "ok"

    async def timers() -> str:
        t0 = asyncio.get_running_loop().time()
        await asyncio.sleep(0.05)
        return f"slept {asyncio.get_running_loop().time() - t0:.3f}s"

    return [
        ("asyncio.create_subprocess_exec", subprocess_exec),
        ("asyncio.create_subprocess_shell", subprocess_shell),
        ("asyncio.open_unix_connection", unix_connection),
        ("loop.add_reader", add_reader),
        ("loop.sock_recv", sock_recv),
        ("loop.connect_read_pipe", read_pipe),
        ("loop.add_signal_handler (main thread only)", signal_handler),
        ("asyncio.sleep (timers)", timers),
    ]


async def run_all(label: str, tmp: str, *, main_thread: bool) -> list[str]:
    lines = []
    for name, check in checks(tmp):
        if not main_thread and "main thread only" in name:
            lines.append(f"  {label:9} {name}: skipped (not applicable off the main thread)")
            continue
        try:
            result = await asyncio.wait_for(check(), 5)
            lines.append(f"  {label:9} {name}: OK ({result})")
        except NotImplementedError as exc:
            lines.append(f"  {label:9} {name}: NotImplementedError ({exc})")
        except Exception as exc:  # noqa: BLE001 - recording the outcome is the point
            lines.append(f"  {label:9} {name}: {type(exc).__name__} ({exc})")
    return lines


def main() -> int:
    print(f"PySide6 {PySide6.__version__}, Qt {PySide6.QtCore.qVersion()}, Python {sys.version}")
    print(f"QtAsyncio source: {QtAsyncio.events.__file__}")
    missing = static_not_implemented()
    print(f"QAsyncioEventLoop methods raising NotImplementedError ({len(missing)}):")
    print("  " + ", ".join(missing))

    results: list[str] = []
    with tempfile.TemporaryDirectory(prefix="m0h-") as tmp:
        app = QCoreApplication.instance() or QCoreApplication([])

        async def qt_main() -> None:
            results.extend(await run_all("QtAsyncio", tmp, main_thread=True))

        QtAsyncio.run(qt_main(), keep_running=False, quit_qapp=True)
        del app

        thread_lines: list[str] = []

        def worker() -> None:
            loop = asyncio.new_event_loop()
            try:
                thread_lines.extend(
                    loop.run_until_complete(run_all("thread", tmp, main_thread=False))
                )
            finally:
                loop.close()

        t = threading.Thread(target=worker, name="core-loop")
        t.start()
        t.join(30)
        results.extend(thread_lines)

    print("Runtime checks:")
    print("\n".join(results))
    qt_blocked = [r for r in results if r.lstrip().startswith("QtAsyncio") and ": OK" not in r]
    thread_ok = all(": OK" in r or "skipped" in r for r in thread_lines) and thread_lines
    verdict = "NO-GO" if qt_blocked else "GO"
    print(f"Verdict QtAsyncio: {verdict} ({len(qt_blocked)} required operations fail)")
    print(f"Verdict thread fallback: {'GO' if thread_ok else 'NO-GO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
