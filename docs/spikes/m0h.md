<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->
# Spike M0(h): PySide6.QtAsyncio driving the core loop

Date: 2026-10-07. Dev box: Fedora 44, Python 3.14.7, PySide6 6.11.2 (Fedora `python3-pyside6`), Qt 6.11.2, Hyprland (run headless with `QT_QPA_PLATFORM=offscreen`).
Reproducer: `spikes/m0h-qtasyncio/probe.py` (no network; temporary files only).

## Verdict

| Question | Result |
|---|---|
| Can `PySide6.QtAsyncio` run ButterEye's asyncio core in the GUI thread? | **No-go.** Subprocesses, Unix sockets, fd readers, pipes and signal handlers all raise `NotImplementedError`. Only timers and plain coroutines work. |
| Does a stock `asyncio` loop in a dedicated non-main thread do everything the core needs? | **Go.** Subprocess exec/shell, Unix-socket connections, `add_reader`, `sock_recv`, read pipes and timers all work. |

**Decision** (GUI.md §3, ruling R1): the core runs on a stock `asyncio.new_event_loop()` in a dedicated non-daemon `threading.Thread`; `CoreBridge` (Qt signals, queued connections) is the only bridge. `CoreBridge` is the single class that knows this, so a future `QtAsyncioBridge` with the same public surface can replace it in one file. Re-run the reproducer on every PySide6 bump; a go needs every "QtAsyncio" line below to read OK.

Why the core cannot avoid those APIs: mpv, vspipe, ffmpeg, `nvidia-smi` and `vulkaninfo` are subprocesses (`asyncio.create_subprocess_exec`, `start_new_session=True`, §2.4), and mpv control is JSON IPC over a Unix socket (SCOPE §4.3). Signal handling stays on the main thread (Qt's), which the thread design does not need.

## Statically unimplemented (`QAsyncioEventLoop` in `PySide6/QtAsyncio/events.py`, 6.11.2)

- `create_connection`
- `create_datagram_endpoint`
- `create_unix_connection`
- `create_server`
- `create_unix_server`
- `connect_accepted_socket`
- `sendfile`
- `start_tls`
- `add_reader`
- `remove_reader`
- `add_writer`
- `remove_writer`
- `sock_recv`
- `sock_recv_into`
- `sock_recvfrom`
- `sock_recvfrom_into`
- `sock_sendall`
- `sock_sendto`
- `sock_connect`
- `sock_accept`
- `sock_sendfile`
- `getaddrinfo`
- `getnameinfo`
- `connect_read_pipe`
- `connect_write_pipe`
- `add_signal_handler`
- `remove_signal_handler`
- `subprocess_exec`
- `subprocess_shell`

## Reproducer output (verbatim)

```
PySide6 6.11.2, Qt 6.11.2, Python 3.14.7 (main, Aug 10 2026, 00:00:00) [GCC 16.1.1 20260515 (Red Hat 16.1.1-2)]
QtAsyncio source: /usr/lib/python3.14/site-packages/PySide6/QtAsyncio/events.py
QAsyncioEventLoop methods raising NotImplementedError (29):
  create_connection, create_datagram_endpoint, create_unix_connection, create_server, create_unix_server, connect_accepted_socket, sendfile, start_tls, add_reader, remove_reader, add_writer, remove_writer, sock_recv, sock_recv_into, sock_recvfrom, sock_recvfrom_into, sock_sendall, sock_sendto, sock_connect, sock_accept, sock_sendfile, getaddrinfo, getnameinfo, connect_read_pipe, connect_write_pipe, add_signal_handler, remove_signal_handler, subprocess_exec, subprocess_shell
Runtime checks:
  QtAsyncio asyncio.create_subprocess_exec: NotImplementedError (QAsyncioEventLoop.subprocess_exec() is not implemented yet)
  QtAsyncio asyncio.create_subprocess_shell: NotImplementedError (QAsyncioEventLoop.subprocess_shell() is not implemented yet)
  QtAsyncio asyncio.open_unix_connection: NotImplementedError (QAsyncioEventLoop.create_unix_connection() is not implemented yet)
  QtAsyncio loop.add_reader: NotImplementedError (QAsyncioEventLoop.add_reader() is not implemented yet)
  QtAsyncio loop.sock_recv: NotImplementedError (QAsyncioEventLoop.sock_recv() is not implemented yet)
  QtAsyncio loop.connect_read_pipe: NotImplementedError (QAsyncioEventLoop.connect_read_pipe() is not implemented yet)
  QtAsyncio loop.add_signal_handler (main thread only): NotImplementedError (QAsyncioEventLoop.add_signal_handler() is not implemented yet)
  QtAsyncio asyncio.sleep (timers): OK (slept 0.048s)
  thread    asyncio.create_subprocess_exec: OK (rc=0 out='hi')
  thread    asyncio.create_subprocess_shell: OK (rc=0)
  thread    asyncio.open_unix_connection: OK (connected)
  thread    loop.add_reader: OK (read b'x')
  thread    loop.sock_recv: OK (read b'y')
  thread    loop.connect_read_pipe: OK (read b'z\n')
  thread    loop.add_signal_handler (main thread only): skipped (not applicable off the main thread)
  thread    asyncio.sleep (timers): OK (slept 0.050s)
Verdict QtAsyncio: NO-GO (7 required operations fail)
Verdict thread fallback: GO
```

## Side findings

- `QtAsyncio.run(coro, keep_running=False, quit_qapp=False)` never returned after the coroutine finished (`run_forever` keeps spinning; traced with `faulthandler`). With `quit_qapp=True` it returns. Not relevant to the thread design, but worth knowing for any future QtAsyncio attempt.
- `asyncio.create_subprocess_exec` from a non-main thread works on 3.14.7 (rc 0), matching GUI.md R1; no child-watcher setup is needed (pidfd).
- `add_signal_handler` is main-thread-only in stock asyncio too; the core does not install signal handlers (the GUI owns SIGINT/SIGTERM handling, the CLI runs its loop on the main thread).
