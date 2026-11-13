# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Target-side Unix socket peer server and prompt queue inside a peer xonsh."""

import atexit
import builtins
import collections
from dataclasses import dataclass, field
import io
import os
import selectors
import socket
import subprocess
import sys
import threading
from typing import Any, Callable

from . import parser_patch
from .parser_patch import mark_peer_server_started, write_startup_error
from .protocol import (
    check_block_syntax,
    recv_json,
    send_json,
    verify_peer_uid,
)


_NON_PTK_ERROR = "Peer xonsh is not running the prompt_toolkit shell"

_active_server: "_PeerServer | None" = None
_last_server: "_PeerServer | None" = None
_hooks_lock = threading.Lock()
_orig_apply_to_history: Callable[[Any], Any] | None = None
_orig_run_compiled_code: Callable[..., Any] | None = None
_orig_tee_stdout: Callable[..., Any] | None = None
_orig_stream_stderr: Callable[..., Any] | None = None
_orig_proc_proxy_wait: Callable[..., Any] | None = None
_stream_local = threading.local()


def _is_subproc_streaming() -> bool:
    """Returns True while a subprocess pipeline is echoing raw output."""
    return getattr(_stream_local, "depth", 0) > 0


class _PeerStdBuf(io.RawIOBase):
    """Binary buffer wrapper that tees writes when active."""

    def __init__(self, parent: "_PeerStdTee", orig_buf: Any) -> None:
        self._parent = parent
        self._orig_buf = orig_buf

    def writable(self) -> bool:
        return True

    def write(self, b: Any) -> int:
        raw = bytes(b) if isinstance(b, (memoryview, bytearray)) else bytes(b)
        if self._parent._active and not _is_subproc_streaming() and raw:
            text = raw.decode(
                self._parent.encoding, errors=self._parent.errors
            )
            if text:
                self._parent._chunks.append(text)
        try:
            written = self._orig_buf.write(b)
            return len(raw) if written is None else int(written)
        except OSError:
            return len(raw)

    def flush(self) -> None:
        if getattr(self._orig_buf, "closed", False):
            return
        try:
            self._orig_buf.flush()
        except (OSError, ValueError):
            pass

    def fileno(self) -> int:
        return self._orig_buf.fileno()


class _PeerStdTee(io.TextIOBase):
    """Text stream wrapper that records writes while passing through."""

    def __init__(self, name: str, chunks: list[str]) -> None:
        self._name = name
        std = getattr(sys, name)
        self.std = std
        self._chunks = chunks
        self._active = True
        self._encoding = getattr(std, "encoding", None) or "utf-8"
        self._errors = getattr(std, "errors", None) or "replace"
        if hasattr(std, "buffer"):
            self.buffer = _PeerStdBuf(self, std.buffer)
        setattr(sys, name, self)

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return getattr(self.std, "encoding", None) or self._encoding

    @property
    def errors(self) -> str:
        return getattr(self.std, "errors", None) or self._errors

    @property
    def newlines(self) -> Any:
        return getattr(self.std, "newlines", None)

    def write(self, s: str) -> int:
        if self._active and not _is_subproc_streaming() and s:
            self._chunks.append(s)
        try:
            self.std.write(s)
        except OSError:
            pass
        return len(s)

    def flush(self) -> None:
        if getattr(self.std, "closed", False):
            return
        flush_fn = getattr(self.std, "flush", None)
        if callable(flush_fn):
            try:
                flush_fn()
            except (OSError, ValueError):
                pass

    def fileno(self) -> int:
        return self.std.fileno()

    def isatty(self) -> bool:
        isatty_fn = getattr(self.std, "isatty", None)
        if callable(isatty_fn):
            return bool(isatty_fn())
        return False

    def close(self) -> None:
        self._active = False
        cur = getattr(sys, self._name, None)
        if cur is self:
            setattr(sys, self._name, self.std)
            return
        while cur is not None:
            inner = getattr(cur, "std", None)
            if inner is self:
                cur.std = self.std
                cur_buf = getattr(cur, "buffer", None)
                self_buf = getattr(self, "buffer", None)
                if (
                    cur_buf is not None
                    and self_buf is not None
                    and getattr(cur_buf, "stdbuf", None) is self_buf
                ):
                    cur_buf.stdbuf = getattr(self.std, "buffer", self.std)
                return
            cur = inner


def is_hidden_peer() -> bool:
    """Returns True if this shell is running as a hidden peer."""
    srv = _active_server
    return bool(
        (srv is not None and srv.is_hidden) or parser_patch._is_hidden_peer
    )


def _is_prompt_idle(sh: Any, app: Any) -> bool:
    """Returns True iff ``sh``'s prompt_toolkit prompt is idle and empty."""
    if sh is None or app is None:
        return False
    if not getattr(app, "is_running", False):
        return False
    if getattr(app, "is_done", False):
        return False
    if hasattr(app, "future"):
        future = app.future
        if future is None or (
            callable(getattr(future, "done", None)) and future.done()
        ):
            return False
    if hasattr(app, "loop"):
        loop = app.loop
        if loop is None:
            return False
        is_closed = getattr(loop, "is_closed", None)
        if callable(is_closed) and is_closed():
            return False
    prompter = getattr(sh, "prompter", None)
    default_buf = getattr(prompter, "default_buffer", None)
    if default_buf is None:
        return False
    if getattr(app, "current_buffer", None) is not default_buf:
        return False
    if getattr(default_buf, "text", None) != "":
        return False
    if getattr(default_buf, "complete_state", None) is not None:
        return False
    layout = getattr(app, "layout", None)
    if layout is not None and getattr(layout, "is_searching", False):
        return False
    win = getattr(default_buf, "window", None)
    if win is not None and layout is not None:
        if getattr(layout, "current_control", None) != getattr(
            win, "content", None
        ):
            return False
    if getattr(sh, "need_more_lines", False) is True:
        return False
    return True


def _normalize_pipeline(cp: Any, block_record: bool) -> dict[str, Any] | None:
    """Converts a ``CommandPipeline`` into a serializable command record."""
    if getattr(cp, "captured", None) != "hiddenobject":
        return None
    args = [str(a) for a in (getattr(cp, "args", None) or ())]
    raw_rtn = getattr(cp, "returncode", None)
    rtn = int(raw_rtn if raw_rtn is not None else 1)
    spec = getattr(cp, "spec", None)
    threadable = bool(getattr(spec, "threadable", False))
    has_cap = spec is not None and (
        getattr(spec, "captured_stdout", None) is not None
        or getattr(spec, "captured_stderr", None) is not None
    )
    if block_record and threadable and has_cap:
        out: str | None = "".join(getattr(cp, "lines", None) or ())
        err: str | None = getattr(cp, "errors", None) or ""
    else:
        out = None
        err = None
    return {"args": args, "rtn": rtn, "out": out, "err": err}


def _compute_final_rtn(
    post_rtn: int | None,
    records: list[dict[str, Any]],
    exc_type: type[BaseException] | None,
) -> int:
    """Computes the overall block exit status from shell hooks and records."""
    if exc_type is KeyboardInterrupt or (
        post_rtn is not None and post_rtn < 0
    ):
        return 130
    if exc_type is not None and not issubclass(
        exc_type,
        (KeyboardInterrupt, SystemExit, subprocess.CalledProcessError),
    ):
        return 1
    if post_rtn is not None and post_rtn != 0:
        return int(post_rtn)
    if records and records[-1]["rtn"] != 0:
        return int(records[-1]["rtn"])
    return 0


def _send_reply(
    conn: socket.socket, send_lock: threading.Lock, msg: dict[str, Any]
) -> None:
    """Sends a JSON reply on ``conn``, ignoring client disconnect errors."""
    with send_lock:
        try:
            send_json(conn, msg)
        except (BrokenPipeError, OSError):
            pass


def _install_global_hooks() -> None:
    """Installs global xonsh hooks once to observe active peer entries."""
    global _orig_apply_to_history, _orig_run_compiled_code
    global _orig_tee_stdout, _orig_stream_stderr, _orig_proc_proxy_wait
    from xonsh.procs.pipelines import CommandPipeline
    from xonsh.procs.proxies import ProcProxy
    import xonsh.shells.base_shell as base_shell

    with _hooks_lock:
        if _orig_apply_to_history is None:
            _orig_apply_to_history = CommandPipeline._apply_to_history

            def _apply_to_history_hook(cp_self: Any) -> Any:
                srv = _active_server
                if srv is not None:
                    srv._record_pipeline(cp_self)
                assert _orig_apply_to_history is not None
                return _orig_apply_to_history(cp_self)

            CommandPipeline._apply_to_history = (  # type: ignore[method-assign]
                _apply_to_history_hook
            )

        if _orig_run_compiled_code is None:
            _orig_run_compiled_code = base_shell.run_compiled_code

            def _run_compiled_code_hook(*args: Any, **kwargs: Any) -> Any:
                assert _orig_run_compiled_code is not None
                exc_info = _orig_run_compiled_code(*args, **kwargs)
                srv = _active_server
                if srv is not None:
                    srv._record_exc_info(exc_info)
                return exc_info

            base_shell.run_compiled_code = _run_compiled_code_hook

        if _orig_tee_stdout is None:
            _orig_tee_stdout = CommandPipeline.tee_stdout

            def _tee_stdout_hook(
                cp_self: Any, *args: Any, **kwargs: Any
            ) -> Any:
                assert _orig_tee_stdout is not None
                _stream_local.depth = getattr(_stream_local, "depth", 0) + 1
                try:
                    yield from _orig_tee_stdout(cp_self, *args, **kwargs)
                finally:
                    _stream_local.depth -= 1

            CommandPipeline.tee_stdout = (  # type: ignore[method-assign]
                _tee_stdout_hook
            )

        if _orig_stream_stderr is None:
            _orig_stream_stderr = CommandPipeline.stream_stderr

            def _stream_stderr_hook(
                cp_self: Any, *args: Any, **kwargs: Any
            ) -> Any:
                assert _orig_stream_stderr is not None
                _stream_local.depth = getattr(_stream_local, "depth", 0) + 1
                try:
                    return _orig_stream_stderr(cp_self, *args, **kwargs)
                finally:
                    _stream_local.depth -= 1

            CommandPipeline.stream_stderr = (  # type: ignore[method-assign]
                _stream_stderr_hook
            )

        if _orig_proc_proxy_wait is None:
            _orig_proc_proxy_wait = ProcProxy.wait

            def _proc_proxy_wait_hook(
                px_self: Any, *args: Any, **kwargs: Any
            ) -> Any:
                assert _orig_proc_proxy_wait is not None
                wait_and_get = getattr(px_self, "_wait_and_getattr", None)
                if not hasattr(px_self, "spec") and callable(wait_and_get):
                    spec = wait_and_get("spec")
                else:
                    spec = getattr(px_self, "spec", None)
                uncaptured = spec is None or (
                    getattr(spec, "captured_stdout", None) is None
                    and getattr(spec, "captured_stderr", None) is None
                )
                if uncaptured:
                    _stream_local.depth = (
                        getattr(_stream_local, "depth", 0) + 1
                    )
                    try:
                        return _orig_proc_proxy_wait(
                            px_self, *args, **kwargs
                        )
                    finally:
                        _stream_local.depth -= 1
                return _orig_proc_proxy_wait(px_self, *args, **kwargs)

            ProcProxy.wait = (  # type: ignore[method-assign]
                _proc_proxy_wait_hook
            )


@dataclass
class _PeerEntry:
    """State for a single submitted block currently running at the prompt."""

    req_id: str | None
    record: bool
    conn: socket.socket
    send_lock: threading.Lock
    records: list[dict[str, Any]] = field(default_factory=list)
    out_chunks: list[str] = field(default_factory=list)
    err_chunks: list[str] = field(default_factory=list)
    stdout_tee: _PeerStdTee | None = None
    stderr_tee: _PeerStdTee | None = None
    exc_type: type[BaseException] | None = None
    had_capture: bool = False
    prev_capture: Any = None
    precommand_ran: bool = False
    done_event: threading.Event = field(default_factory=threading.Event)


class _PeerServer:
    """Manages the peer Unix socket, prompt queue, and execution hooks."""

    def __init__(
        self,
        xsh: Any,
        socket_path: str,
        server_sock: socket.socket,
        is_hidden: bool = False,
        reject_reason: str | None = None,
    ) -> None:
        self.xsh = xsh
        self.socket_path = socket_path
        self.server_sock = server_sock
        self.is_hidden = is_hidden
        self.reject_reason = reject_reason

        self._stop_event = threading.Event()
        self._queue: collections.deque[
            tuple[
                dict[str, Any],
                socket.socket,
                threading.Lock,
                threading.Event,
            ]
        ] = collections.deque()
        self._queue_cond = threading.Condition()
        self._wakeup_event = threading.Event()
        self._entry_done = threading.Event()
        self._entry_lock = threading.Lock()
        self._active_entry: _PeerEntry | None = None
        self._conns_lock = threading.Lock()
        self._client_conns: set[socket.socket] = set()
        self._accept_wakeup_r: socket.socket | None = None
        self._accept_wakeup_w: socket.socket | None = None

        self._precommand_fn: Callable[..., Any] | None = None
        self._postcommand_fn: Callable[..., Any] | None = None
        self._pre_prompt_fn: Callable[..., Any] | None = None

    def start(self) -> None:
        """Installs hooks (when accepting blocks) and starts server threads."""
        global _active_server
        if self.reject_reason is None:
            _active_server = self
            _install_global_hooks()
            self._register_events()
            pump = threading.Thread(
                target=self._pump_loop,
                name="xontrib-tmux-peer-pump",
                daemon=True,
            )
            pump.start()
        self._accept_wakeup_r, self._accept_wakeup_w = socket.socketpair()
        self._accept_wakeup_r.setblocking(False)
        self._accept_wakeup_w.setblocking(False)
        accept_t = threading.Thread(
            target=self._accept_loop,
            name="xontrib-tmux-peer-accept",
            daemon=True,
        )
        accept_t.start()

    def stop(self) -> None:
        """Stops the server, disconnects event handlers, and unlinks socket."""
        global _active_server, _last_server
        self._stop_event.set()
        with self._queue_cond:
            while self._queue:
                _, _, _, ev = self._queue.popleft()
                ev.set()
            self._queue_cond.notify_all()
        with self._entry_lock:
            active = self._active_entry
            self._active_entry = None
        if active is not None:
            try:
                self._restore_capture(active)
            finally:
                active.done_event.set()
        self._wakeup_event.set()
        self._entry_done.set()
        if self._accept_wakeup_w is not None:
            try:
                self._accept_wakeup_w.send(b"\x01")
            except OSError:
                pass
        self._unregister_events()
        if _active_server is self:
            _active_server = None
        if _last_server is self:
            _last_server = None
        try:
            self.server_sock.close()
        except OSError:
            pass
        with self._conns_lock:
            conns = list(self._client_conns)
            self._client_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass

    def _get_events(self) -> Any:
        events = getattr(builtins, "events", None)
        if events is not None:
            return events
        try:
            from xonsh.events import events as xonsh_events

            return xonsh_events
        except ImportError:
            return None

    def _register_events(self) -> None:
        events = self._get_events()
        if events is None:
            return

        @events.on_precommand
        def _on_precommand(cmd: str = "", **kwargs: Any) -> None:
            with self._entry_lock:
                entry = self._active_entry
                if entry is None:
                    return
                entry.precommand_ran = True
                entry.records.clear()
                entry.out_chunks.clear()
                entry.err_chunks.clear()
                entry.exc_type = None
                if entry.record:
                    entry.stdout_tee = _PeerStdTee("stdout", entry.out_chunks)
                    entry.stderr_tee = _PeerStdTee("stderr", entry.err_chunks)
                    env = getattr(self.xsh, "env", None)
                    if env is not None:
                        entry.had_capture = "XONSH_CAPTURE_ALWAYS" in env
                        entry.prev_capture = env.get("XONSH_CAPTURE_ALWAYS")
                        env["XONSH_CAPTURE_ALWAYS"] = True

        @events.on_postcommand
        def _on_postcommand(
            cmd: str = "",
            rtn: int | None = 0,
            out: Any = None,
            ts: Any = None,
            **kwargs: Any,
        ) -> None:
            with self._entry_lock:
                entry = self._active_entry
                if entry is None:
                    return
                self._active_entry = None
            try:
                self._restore_capture(entry)
                exited = getattr(self.xsh, "exit", None) is not None or (
                    entry.exc_type is not None
                    and issubclass(entry.exc_type, SystemExit)
                )
                if exited:
                    _send_reply(
                        entry.conn,
                        entry.send_lock,
                        {
                            "id": entry.req_id,
                            "ok": False,
                            "error_type": "RuntimeError",
                            "error": "Peer exited",
                        },
                    )
                else:
                    final_rtn = _compute_final_rtn(
                        rtn, entry.records, entry.exc_type
                    )
                    _send_reply(
                        entry.conn,
                        entry.send_lock,
                        {
                            "id": entry.req_id,
                            "ok": True,
                            "rtn": final_rtn,
                            "commands": entry.records,
                            "out": (
                                "".join(entry.out_chunks)
                                if entry.record
                                else None
                            ),
                            "err": (
                                "".join(entry.err_chunks)
                                if entry.record
                                else None
                            ),
                        },
                    )
            finally:
                entry.done_event.set()
                self._entry_done.set()

        @events.on_pre_prompt
        def _on_pre_prompt(**kwargs: Any) -> None:
            with self._entry_lock:
                entry = self._active_entry
                if entry is not None:
                    self._active_entry = None
            if entry is not None:
                try:
                    self._restore_capture(entry)
                    _send_reply(
                        entry.conn,
                        entry.send_lock,
                        {
                            "id": entry.req_id,
                            "ok": False,
                            "error_type": "RuntimeError",
                            "error": "Peer failed to execute block",
                        },
                    )
                finally:
                    entry.done_event.set()
                    self._entry_done.set()
            self._wakeup_event.set()

        self._precommand_fn = _on_precommand
        self._postcommand_fn = _on_postcommand
        self._pre_prompt_fn = _on_pre_prompt

    def _unregister_events(self) -> None:
        events = self._get_events()
        if events is None:
            return
        for attr, fn in (
            ("on_precommand", self._precommand_fn),
            ("on_postcommand", self._postcommand_fn),
            ("on_pre_prompt", self._pre_prompt_fn),
        ):
            if fn is not None:
                ev = getattr(events, attr, None)
                if ev is not None and hasattr(ev, "discard"):
                    ev.discard(fn)
        self._precommand_fn = None
        self._postcommand_fn = None
        self._pre_prompt_fn = None

    def _restore_capture(self, entry: _PeerEntry) -> None:
        if entry.stdout_tee is not None:
            entry.stdout_tee.close()
            entry.stdout_tee = None
        if entry.stderr_tee is not None:
            entry.stderr_tee.close()
            entry.stderr_tee = None
        if entry.record and entry.precommand_ran:
            env = getattr(self.xsh, "env", None)
            if env is not None:
                if entry.had_capture:
                    env["XONSH_CAPTURE_ALWAYS"] = entry.prev_capture
                else:
                    env.pop("XONSH_CAPTURE_ALWAYS", None)

    def _record_pipeline(self, cp: Any) -> None:
        with self._entry_lock:
            entry = self._active_entry
            if entry is None:
                return
            rec = _normalize_pipeline(cp, entry.record)
            if rec is not None:
                entry.records.append(rec)
                if entry.record:
                    if rec["out"]:
                        entry.out_chunks.append(rec["out"])
                    if rec["err"]:
                        entry.err_chunks.append(rec["err"])

    def _record_exc_info(self, exc_info: Any) -> None:
        if not exc_info:
            return
        with self._entry_lock:
            entry = self._active_entry
            if entry is None:
                return
            entry.exc_type = exc_info[0]

    def _drop_conn(self, conn: socket.socket) -> None:
        with self._conns_lock:
            self._client_conns.discard(conn)
        try:
            conn.close()
        except OSError:
            pass

    def _unregister_and_drop(
        self,
        conn: socket.socket,
        sel: selectors.BaseSelector,
        pending_conns: dict[socket.socket, bytearray],
    ) -> None:
        try:
            sel.unregister(conn)
        except Exception:
            pass
        pending_conns.pop(conn, None)
        self._drop_conn(conn)

    def _accept_loop(self) -> None:
        """Accepts connections and queues initial requests in arrival order."""
        sel = selectors.DefaultSelector()
        pending_conns: dict[socket.socket, bytearray] = {}
        try:
            try:
                if self._stop_event.is_set():
                    return
                self.server_sock.setblocking(False)
                sel.register(
                    self.server_sock, selectors.EVENT_READ, data="accept"
                )
                if self._accept_wakeup_r is not None:
                    sel.register(
                        self._accept_wakeup_r,
                        selectors.EVENT_READ,
                        data="wakeup",
                    )
            except (OSError, ValueError):
                return
            while not self._stop_event.is_set():
                try:
                    events = sel.select()
                except (OSError, ValueError):
                    break
                if self._stop_event.is_set():
                    break
                accept_ready = False
                for key, _mask in events:
                    if key.data == "wakeup":
                        if self._accept_wakeup_r is not None:
                            try:
                                self._accept_wakeup_r.recv(1024)
                            except OSError:
                                pass
                    elif key.data == "accept":
                        accept_ready = True
                    elif key.data == "client":
                        conn = key.fileobj
                        if isinstance(conn, socket.socket):
                            self._drain_initial_conn(conn, sel, pending_conns)
                if accept_ready and not self._stop_event.is_set():
                    self._accept_ready_conns(sel, pending_conns)
        finally:
            for conn in list(pending_conns):
                try:
                    sel.unregister(conn)
                except Exception:
                    pass
                self._drop_conn(conn)
            pending_conns.clear()
            try:
                sel.close()
            except Exception:
                pass
            for sock in (self._accept_wakeup_r, self._accept_wakeup_w):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _accept_ready_conns(
        self,
        sel: selectors.BaseSelector,
        pending_conns: dict[socket.socket, bytearray],
    ) -> None:
        while not self._stop_event.is_set():
            try:
                conn, _ = self.server_sock.accept()
            except BlockingIOError:
                break
            except OSError:
                return
            try:
                verify_peer_uid(conn)
            except (PermissionError, OSError):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            with self._conns_lock:
                self._client_conns.add(conn)
            conn.setblocking(False)
            pending_conns[conn] = bytearray()
            sel.register(conn, selectors.EVENT_READ, data="client")
            for pconn in list(pending_conns):
                self._drain_initial_conn(pconn, sel, pending_conns)

    def _drain_initial_conn(
        self,
        conn: socket.socket,
        sel: selectors.BaseSelector,
        pending_conns: dict[socket.socket, bytearray],
    ) -> None:
        buf = pending_conns.get(conn)
        if buf is None:
            return
        while not self._stop_event.is_set():
            try:
                chunk = conn.recv(65536)
            except BlockingIOError:
                return
            except OSError:
                self._unregister_and_drop(conn, sel, pending_conns)
                return
            if not chunk:
                self._unregister_and_drop(conn, sel, pending_conns)
                return
            buf.extend(chunk)
            nl = buf.find(b"\n")
            if nl != -1:
                first_bytes = bytes(buf[: nl + 1])
                leftover = bytes(buf[nl + 1 :])
                try:
                    sel.unregister(conn)
                except Exception:
                    pass
                pending_conns.pop(conn, None)
                conn.setblocking(True)
                self._handoff_initial_conn(conn, first_bytes, leftover)
                return

    def _handoff_initial_conn(
        self,
        conn: socket.socket,
        first_bytes: bytes,
        leftover: bytes,
    ) -> None:
        send_lock = threading.Lock()
        pending_events: list[threading.Event] = []
        try:
            req = recv_json(io.StringIO(first_bytes.decode("utf-8")))
        except (UnicodeDecodeError, RuntimeError) as exc:
            _send_reply(
                conn,
                send_lock,
                {
                    "id": None,
                    "ok": False,
                    "error_type": "RuntimeError",
                    "error": str(exc),
                },
            )
            self._drop_conn(conn)
            return
        if req is None:
            self._drop_conn(conn)
            return

        if not self._dispatch_request(req, conn, send_lock, pending_events):
            self._drop_conn(conn)
            return

        t = threading.Thread(
            target=self._handle_client,
            args=(conn, send_lock, pending_events, leftover),
            name="xontrib-tmux-peer-client",
            daemon=True,
        )
        t.start()

    def _dispatch_request(
        self,
        req: dict[str, Any],
        conn: socket.socket,
        send_lock: threading.Lock,
        pending_events: list[threading.Event],
    ) -> bool:
        req_id = req.get("id")
        if self.reject_reason is not None:
            _send_reply(
                conn,
                send_lock,
                {
                    "id": req_id,
                    "ok": False,
                    "error_type": "RuntimeError",
                    "error": self.reject_reason,
                },
            )
            return True
        op = req.get("op")
        if op == "run":
            pending_events[:] = [
                ev for ev in pending_events if not ev.is_set()
            ]
            done_ev = threading.Event()
            pending_events.append(done_ev)
            with self._queue_cond:
                self._queue.append((req, conn, send_lock, done_ev))
                self._queue_cond.notify()
        else:
            _send_reply(
                conn,
                send_lock,
                {
                    "id": req_id,
                    "ok": False,
                    "error_type": "RuntimeError",
                    "error": f"Unknown op: {op!r}",
                },
            )
        return True

    def _handle_client(
        self,
        conn: socket.socket,
        send_lock: threading.Lock | None = None,
        pending_events: list[threading.Event] | None = None,
        leftover: bytes = b"",
    ) -> None:
        if send_lock is None:
            send_lock = threading.Lock()
        if pending_events is None:
            pending_events = []
        buf = bytearray(leftover)
        try:
            with conn.makefile("rb") as rb:
                while not self._stop_event.is_set():
                    nl = buf.find(b"\n")
                    if nl != -1:
                        raw_line = bytes(buf[: nl + 1])
                        del buf[: nl + 1]
                    else:
                        chunk = rb.readline()
                        if not chunk:
                            if buf:
                                raw_line = bytes(buf)
                                buf.clear()
                            else:
                                break
                        elif buf:
                            raw_line = bytes(buf) + chunk
                            buf.clear()
                        else:
                            raw_line = chunk
                    try:
                        req = recv_json(
                            io.StringIO(raw_line.decode("utf-8"))
                        )
                    except (UnicodeDecodeError, RuntimeError, OSError) as exc:
                        _send_reply(
                            conn,
                            send_lock,
                            {
                                "id": None,
                                "ok": False,
                                "error_type": "RuntimeError",
                                "error": str(exc),
                            },
                        )
                        break
                    if req is None:
                        break
                    if not self._dispatch_request(
                        req, conn, send_lock, pending_events
                    ):
                        return
        finally:
            for ev in pending_events:
                while not self._stop_event.is_set():
                    if ev.wait(timeout=0.05):
                        break
            self._drop_conn(conn)

    def _pump_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._queue_cond:
                while not self._queue and not self._stop_event.is_set():
                    self._queue_cond.wait()
                if self._stop_event.is_set():
                    return
                req, conn, send_lock, done_ev = self._queue.popleft()

            req_id = req.get("id")
            block = req.get("block")
            record = bool(req.get("record", True))
            try:
                normalized = check_block_syntax(block)
            except (SyntaxError, ValueError, TypeError) as exc:
                _send_reply(
                    conn,
                    send_lock,
                    {
                        "id": req_id,
                        "ok": False,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    },
                )
                done_ev.set()
                continue

            entry = _PeerEntry(
                req_id=req_id,
                record=record,
                conn=conn,
                send_lock=send_lock,
                done_event=done_ev,
            )
            self._submit_when_idle(entry, normalized)

    def _submit_when_idle(
        self, entry: _PeerEntry, normalized: str
    ) -> None:
        text_to_submit = normalized.rstrip("\n")
        try:
            while not self._stop_event.is_set():
                sh = getattr(getattr(self.xsh, "shell", None), "shell", None)
                app = getattr(getattr(sh, "prompter", None), "app", None)
                if _is_prompt_idle(sh, app):
                    loop = getattr(app, "loop", None)
                    if loop is None:
                        self._wakeup_event.wait(timeout=0.02)
                        self._wakeup_event.clear()
                        continue
                    submit_done = threading.Event()
                    submit_state: dict[str, Any] = {"submitted": False}

                    def _submit_on_loop(
                        _sh: Any = sh,
                        _app: Any = app,
                    ) -> None:
                        try:
                            if not _is_prompt_idle(_sh, _app):
                                submit_state["submitted"] = False
                                return
                            self._entry_done.clear()
                            with self._entry_lock:
                                self._active_entry = entry
                            _sh.prompter.default_buffer.text = text_to_submit
                            _sh.prompter.default_buffer.validate_and_handle()
                            submit_state["submitted"] = True
                        except Exception as exc:
                            with self._entry_lock:
                                if self._active_entry is entry:
                                    self._active_entry = None
                            self._restore_capture(entry)
                            submit_state["error"] = exc
                            submit_state["error_type"] = type(exc).__name__
                        finally:
                            submit_done.set()

                    try:
                        loop.call_soon_threadsafe(_submit_on_loop)
                    except RuntimeError:
                        submit_done.set()
                    while not self._stop_event.is_set():
                        if submit_done.wait(timeout=0.1):
                            break
                        is_closed = getattr(loop, "is_closed", None)
                        if callable(is_closed) and is_closed():
                            break
                    if self._stop_event.is_set():
                        return
                    if not submit_done.is_set():
                        self._wakeup_event.wait(timeout=0.02)
                        self._wakeup_event.clear()
                        continue

                    if "error" in submit_state:
                        exc = submit_state["error"]
                        _send_reply(
                            entry.conn,
                            entry.send_lock,
                            {
                                "id": entry.req_id,
                                "ok": False,
                                "error_type": submit_state.get(
                                    "error_type", "RuntimeError"
                                ),
                                "error": str(exc),
                            },
                        )
                        return

                    if submit_state.get("submitted"):
                        while not self._stop_event.is_set():
                            if self._entry_done.wait(timeout=0.1):
                                return
                        return

                self._wakeup_event.wait(timeout=0.02)
                self._wakeup_event.clear()
        finally:
            entry.done_event.set()


def _bind_peer_socket(socket_path: str) -> socket.socket:
    """Binds a ``0o600`` ``AF_UNIX`` stream listener at ``socket_path``."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(socket_path)
        os.chmod(socket_path, 0o600)
        srv.listen(16)
    except Exception:
        srv.close()
        raise
    return srv


def start_peer_server(
    xsh: Any, socket_path: str, is_hidden: bool = False
) -> None:
    """Starts the peer Unix socket server inside a running xonsh session.

    If the active shell is not ``PromptToolkitShell`` (no ``prompter.app``),
    writes ``<socket_path>.err`` and binds ``socket_path`` to reject incoming
    requests with a ``RuntimeError`` payload.

    Args:
        xsh: Active ``XonshSession`` (``builtins.__xonsh__``).
        socket_path: Filesystem path for the ``AF_UNIX`` listener socket.
        is_hidden: Whether the peer was started on a hidden terminal.
    """
    prompter = getattr(
        getattr(getattr(xsh, "shell", None), "shell", None), "prompter", None
    )
    has_ptk = getattr(prompter, "app", None) is not None
    reject_reason: str | None = None if has_ptk else _NON_PTK_ERROR
    if reject_reason is not None:
        write_startup_error(socket_path, reject_reason)

    global _last_server
    srv_sock = _bind_peer_socket(socket_path)
    mark_peer_server_started()
    server = _PeerServer(
        xsh=xsh,
        socket_path=socket_path,
        server_sock=srv_sock,
        is_hidden=is_hidden,
        reject_reason=reject_reason,
    )
    _last_server = server
    server.start()
    atexit.register(server.stop)
