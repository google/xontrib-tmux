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

"""Sender-side ``RemoteXonsh`` peer controller and ``PeerMacro``."""

import atexit
from collections.abc import Callable, Iterator, MutableMapping, Sequence
from concurrent.futures import InvalidStateError
import fcntl
import inspect
import os
import pathlib
import pty
import queue
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from typing import Any
import uuid
import weakref

from .protocol import (
    HIDDEN_PEER_ENV,
    PEER_SOCKET_ENV,
    PROTOCOL_VERSION,
    capture_vars,
    check_block_syntax,
    decode_value,
    default_timeout,
    encode_value,
    new_socket_path,
    recv_json,
    send_json,
    verify_peer_uid,
)
from .result import CommandRecord, LastCmdProxy, PeerFuture, PeerResult


_DEFAULT_ROWS = 24
_DEFAULT_COLS = 80
_POLL_INTERVAL_S = 0.02
_EPOCH_THRESHOLD = 1_000_000_000.0
_SELF_WAIT_ERROR = "Cannot wait on this shell's own peer from the main thread"

_SyntaxWorkItem = tuple[str, threading.Event, list[Any]]
_syntax_worker_lock = threading.Lock()
_syntax_worker_pid: int | None = None
_syntax_worker_thread: threading.Thread | None = None
_syntax_worker_queue: queue.SimpleQueue[_SyntaxWorkItem] | None = None


def _syntax_worker_loop(
    work_q: queue.SimpleQueue[_SyntaxWorkItem],
) -> None:
    """Runs ``check_block_syntax`` off the caller stack."""
    while True:
        block, done, out = work_q.get()
        try:
            out.append(check_block_syntax(block))
            out.append(None)
        except BaseException as exc:  # noqa: BLE001 - forwarded to caller
            out.append(None)
            out.append(exc)
        finally:
            block = ""
            out = []
            done.set()
            done = None  # type: ignore[assignment]


def _ensure_syntax_worker() -> queue.SimpleQueue[_SyntaxWorkItem]:
    """Returns the queue for the current process's syntax-check thread."""
    global _syntax_worker_pid, _syntax_worker_thread, _syntax_worker_queue
    pid = os.getpid()
    with _syntax_worker_lock:
        if (
            _syntax_worker_pid != pid
            or _syntax_worker_queue is None
            or _syntax_worker_thread is None
            or not _syntax_worker_thread.is_alive()
        ):
            work_q: queue.SimpleQueue[_SyntaxWorkItem] = queue.SimpleQueue()
            thread = threading.Thread(
                target=_syntax_worker_loop,
                args=(work_q,),
                name="xontrib-tmux-syntax",
                daemon=True,
            )
            thread.start()
            _syntax_worker_pid = pid
            _syntax_worker_queue = work_q
            _syntax_worker_thread = thread
        return _syntax_worker_queue


def _check_syntax_detached(block: str) -> str:
    """Validates ``block`` syntax off the caller stack to avoid frame cycles.

    Running xonsh's parser on a dedicated helper thread ensures its internal
    retry ``SyntaxError`` traceback never pins ``RemoteXonsh`` or caller
    frames until a cyclic garbage collection pass runs.
    """
    work_q = _ensure_syntax_worker()
    done = threading.Event()
    box: list[Any] = []
    work_q.put((block, done, box))
    done.wait()
    normalized, exc = box[0], box[1]
    if exc is not None:
        raise exc.with_traceback(None)
    return str(normalized)


class _CleanupState:
    """Process, pty, and socket cleanup state kept separate from controller."""

    def __init__(
        self,
        socket_path: pathlib.Path,
        is_hidden: bool,
        on_kill: Callable[[], None] | None = None,
        master_fd: int | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.is_hidden = is_hidden
        self.on_kill = on_kill
        self.master_fd = master_fd
        self.killed = False
        self._cleaned = False
        self.lock = threading.Lock()

    def close_master(self) -> None:
        """Closes ``master_fd`` at most once."""
        with self.lock:
            fd = self.master_fd
            self.master_fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def unlink_sockets(self) -> None:
        """Removes the peer socket and startup-error sentinel if present."""
        for target in (
            self.socket_path,
            pathlib.Path(f"{self.socket_path}.err"),
        ):
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def cleanup(self, remove_sockets: bool = True) -> None:
        """Marks killed, invokes ``on_kill``, closes pty, unlinks files."""
        with self.lock:
            already_cleaned = self._cleaned
            self._cleaned = True
            self.killed = True
            on_kill = self.on_kill
        if not already_cleaned and on_kill is not None:
            try:
                on_kill()
            except OSError:
                pass
        self.close_master()
        if remove_sockets:
            self.unlink_sockets()


_hidden_states: weakref.WeakSet[_CleanupState] = weakref.WeakSet()


def _finalize_hidden_state(state: _CleanupState) -> None:
    """Finalizer callback for garbage-collected hidden peer controllers."""
    state.cleanup(remove_sockets=True)


def _cleanup_all_hidden_peers() -> None:
    """Terminates any remaining hidden peers when the starter process exits."""
    for state in list(_hidden_states):
        state.cleanup(remove_sockets=True)


atexit.register(_cleanup_all_hidden_peers)


class _PeerController:
    """Shared synchronization and lifetime state for a ``RemoteXonsh``."""

    def __init__(
        self,
        socket_path: pathlib.Path,
        start_time: float,
        is_hidden: bool,
        is_alive: Callable[[], bool] | None = None,
        on_kill: Callable[[], None] | None = None,
        master_fd: int | None = None,
    ) -> None:
        self.socket_path = socket_path
        self.start_time = start_time
        self.is_hidden = is_hidden
        self.is_alive = is_alive
        self.state = _CleanupState(
            socket_path=socket_path,
            is_hidden=is_hidden,
            on_kill=on_kill,
            master_fd=master_fd,
        )
        self.lock = threading.Lock()
        self.send_cond = threading.Condition(self.lock)
        self.stop_event = threading.Event()
        self.next_ticket = 0
        self.active_ticket = 0
        self.abandoned_tickets: set[int] = set()
        self.ready = False
        self.in_flight: weakref.WeakSet[PeerResult] = weakref.WeakSet()
        self.active_sockets: set[socket.socket] = set()
        if is_hidden:
            _hidden_states.add(self.state)
            self._finalizer = weakref.finalize(
                self, _finalize_hidden_state, self.state
            )


def _copy_starter_winsize() -> bytes:
    """Returns packed ``TIOCGWINSZ`` from starter's tty or 24x80 fallback."""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        if not os.isatty(fd):
            continue
        try:
            return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\x00" * 8)
        except OSError:
            continue
    return struct.pack("HHHH", _DEFAULT_ROWS, _DEFAULT_COLS, 0, 0)


def _drain_pty(state: _CleanupState) -> None:
    """Reads and discards hidden-pty output until EOF or close."""
    try:
        while True:
            with state.lock:
                fd = state.master_fd
            if fd is None:
                break
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
    finally:
        state.close_master()


def _safe_set_exception(result: PeerResult, exc: BaseException) -> None:
    """Sets ``exc`` on ``result`` without raising on concurrent completion."""
    if result.future.done():
        return
    try:
        result._set_exception(exc)
    except InvalidStateError:
        pass


def _read_err_sentinel(err_path: pathlib.Path) -> str | None:
    """Returns error message from ``<socket>.err`` if the file exists."""
    try:
        text = err_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return f"Peer startup failed ({exc})"
    return text or "Peer failed to start"


def _connect_if_ready(
    socket_path: pathlib.Path,
) -> tuple[socket.socket | None, bool]:
    """Attempts to connect to ``socket_path`` if bound and same-user.

    Returns:
        ``(sock, transient_busy)`` where ``sock`` is the connected socket or
        ``None``, and ``transient_busy`` is ``True`` on ``InterruptedError``
        or ``BlockingIOError`` (socket exists and is listening, retry briefly).
    """
    if not socket_path.exists():
        return (None, False)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(socket_path))
        verify_peer_uid(sock)
        return (sock, False)
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return (None, False)
    except (InterruptedError, BlockingIOError):
        sock.close()
        return (None, True)
    except BaseException:
        sock.close()
        raise


def _wait_after_ready(
    ctrl: _PeerController, err_path: pathlib.Path
) -> socket.socket:
    """Connects to an already-ready peer, failing immediately if it exited."""
    for _ in range(50):
        if ctrl.state.killed:
            raise RuntimeError("Peer exited")
        err_msg = _read_err_sentinel(err_path)
        if err_msg is not None:
            raise RuntimeError(err_msg)
        if ctrl.is_alive is not None and not ctrl.is_alive():
            raise RuntimeError("Peer exited")
        sock, transient_busy = _connect_if_ready(ctrl.socket_path)
        if sock is not None:
            return sock
        if not transient_busy:
            err_msg = _read_err_sentinel(err_path)
            raise RuntimeError(
                err_msg if err_msg is not None else "Peer exited"
            )
        if ctrl.stop_event.wait(0.01):
            raise RuntimeError("Peer exited")
    raise RuntimeError(f"Busy peer socket {ctrl.socket_path}")


def _wait_until_ready(ctrl: _PeerController) -> socket.socket:
    """Waits for the peer socket, failing fast on exit or ``.err``."""
    err_path = pathlib.Path(f"{ctrl.socket_path}.err")
    if ctrl.ready:
        return _wait_after_ready(ctrl, err_path)

    timeout = default_timeout()
    start = ctrl.start_time
    use_epoch = start > _EPOCH_THRESHOLD
    now_fn = time.time if use_epoch else time.monotonic
    if start <= 0:
        deadline = now_fn() - 1.0
    else:
        deadline = start + timeout

    while True:
        if ctrl.state.killed:
            raise RuntimeError("Peer exited")
        err_msg = _read_err_sentinel(err_path)
        if err_msg is not None:
            raise RuntimeError(err_msg)

        sock, _ = _connect_if_ready(ctrl.socket_path)
        if sock is not None:
            ctrl.ready = True
            return sock

        if ctrl.state.killed:
            raise RuntimeError("Peer exited")
        err_msg = _read_err_sentinel(err_path)
        if err_msg is not None:
            raise RuntimeError(err_msg)

        if ctrl.is_alive is not None and not ctrl.is_alive():
            err_msg = _read_err_sentinel(err_path)
            raise RuntimeError(
                err_msg if err_msg is not None else "Peer exited before ready"
            )

        remaining = deadline - now_fn()
        if remaining <= 0:
            raise RuntimeError(
                f"Timed out waiting for peer socket {ctrl.socket_path}"
            )
        ctrl.stop_event.wait(min(_POLL_INTERVAL_S, remaining))


def _decode_error_response(resp: dict[str, Any]) -> BaseException:
    """Converts a peer error response dict into a Python exception."""
    err_type = resp.get("error_type", "RuntimeError")
    err_msg = str(resp.get("error", "Peer request failed"))
    if err_type == "SyntaxError":
        return SyntaxError(err_msg)
    if err_type == "ValueError":
        return ValueError(err_msg)
    if err_type == "KeyError":
        return KeyError(err_msg)
    if err_type == "TypeError":
        return TypeError(err_msg)
    if err_type == "NameError":
        return NameError(err_msg)
    return RuntimeError(err_msg)


def _is_self_main_thread(socket_path: pathlib.Path) -> bool:
    """Returns True if called on the main thread of the target peer itself."""
    if threading.current_thread() is not threading.main_thread():
        return False
    from . import peer_server

    cur = peer_server.current_peer_socket_path()
    if not cur:
        return False
    return os.path.realpath(socket_path) == os.path.realpath(cur)


def _check_not_self_main_thread(socket_path: pathlib.Path) -> None:
    """Raises ``RuntimeError`` when waiting on oneself from the main thread."""
    if _is_self_main_thread(socket_path):
        raise RuntimeError(_SELF_WAIT_ERROR)


def _advance_active_ticket(ctrl: _PeerController) -> None:
    """Advances ``active_ticket`` past any abandoned tickets and notifies."""
    ctrl.active_ticket += 1
    while ctrl.active_ticket in ctrl.abandoned_tickets:
        ctrl.abandoned_tickets.discard(ctrl.active_ticket)
        ctrl.active_ticket += 1
    ctrl.send_cond.notify_all()


def _wait_ticket_turn(ctrl: _PeerController, ticket: int) -> None:
    """Waits until ``ctrl.active_ticket == ticket``, abandoning on interrupt."""
    with ctrl.send_cond:
        try:
            while ctrl.active_ticket != ticket:
                ctrl.send_cond.wait()
        except BaseException:
            if ctrl.active_ticket == ticket:
                _advance_active_ticket(ctrl)
            else:
                ctrl.abandoned_tickets.add(ticket)
            raise


class _BoundPeerResult(PeerResult):
    """``PeerResult`` enforcing the self-wait guard on the peer main thread."""

    def __init__(
        self,
        cancel_hook: Callable[[], bool] | None,
        self_wait_guard: Callable[[], None],
    ) -> None:
        super().__init__(cancel_hook=cancel_hook)
        self._self_wait_guard = self_wait_guard
        fut_ref = weakref.ref(self._future)

        def _guarded_result(timeout: float | None = None) -> Any:
            fut = fut_ref()
            assert fut is not None
            if not fut.done():
                self_wait_guard()
            return PeerFuture.result(fut, timeout=timeout)

        def _guarded_exception(
            timeout: float | None = None,
        ) -> BaseException | None:
            fut = fut_ref()
            assert fut is not None
            if not fut.done():
                self_wait_guard()
            return PeerFuture.exception(fut, timeout=timeout)

        self._future.result = _guarded_result  # type: ignore[method-assign]
        self._future.exception = (  # type: ignore[method-assign]
            _guarded_exception
        )

    def _wait(self) -> int:
        if not self._future.done():
            self._self_wait_guard()
        return super()._wait()


class _RequestState:
    """Tracks whether a queued block has been written to the peer socket."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.status = "queued_local"
        self.peer_cancelled = False


def _send_cancel_rpc(ctrl: _PeerController, target_id: str) -> bool:
    """Contacts the peer to cancel a previously transmitted block."""
    if ctrl.state.killed:
        return False
    if ctrl.is_alive is not None and not ctrl.is_alive():
        return False
    err_path = pathlib.Path(f"{ctrl.socket_path}.err")
    if _read_err_sentinel(err_path) is not None:
        return False
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(ctrl.socket_path))
        verify_peer_uid(sock)
        send_json(
            sock,
            {
                "id": uuid.uuid4().hex,
                "op": "cancel",
                "target_id": target_id,
            },
        )
        with sock.makefile("r", encoding="utf-8") as stream:
            resp = recv_json(stream)
    except (OSError, RuntimeError, PermissionError):
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    if resp is None or not resp.get("ok", False):
        return False
    return bool(resp.get("cancelled", False))


def _rpc(ctrl: _PeerController, payload: dict[str, Any]) -> dict[str, Any]:
    """Executes a synchronous FIFO-ordered JSON RPC against the peer."""
    _check_not_self_main_thread(ctrl.socket_path)
    with ctrl.lock:
        if ctrl.state.killed:
            raise RuntimeError("Peer has been killed")
        ticket = ctrl.next_ticket
        ctrl.next_ticket += 1

    sock: socket.socket | None = None
    try:
        _wait_ticket_turn(ctrl, ticket)
        try:
            if ctrl.state.killed:
                raise RuntimeError("Peer has been killed")
            sock = _wait_until_ready(ctrl)
            with ctrl.lock:
                if ctrl.state.killed:
                    sock.close()
                    sock = None
                    raise RuntimeError("Peer has been killed")
                ctrl.active_sockets.add(sock)
            send_json(sock, payload)
        finally:
            with ctrl.send_cond:
                _advance_active_ticket(ctrl)

        with sock.makefile("r", encoding="utf-8") as stream:
            resp = recv_json(stream)
        if resp is None:
            msg = (
                "Peer exited"
                if ctrl.state.killed
                else "Peer disconnected before completing request"
            )
            raise RuntimeError(msg)
        if not resp.get("ok", False):
            raise _decode_error_response(resp)
        return resp
    finally:
        if sock is not None:
            with ctrl.lock:
                ctrl.active_sockets.discard(sock)
            try:
                sock.close()
            except OSError:
                pass


def _request_worker(
    ctrl: _PeerController,
    ticket: int,
    req_id: str,
    normalized_block: str,
    record: bool,
    captured: dict[str, Any] | None,
    req_state: _RequestState,
    result: PeerResult,
) -> None:
    """Delivers one block in ticket order and awaits the peer's response."""
    sock: socket.socket | None = None
    try:
        _wait_ticket_turn(ctrl, ticket)
        should_send = True
        try:
            if result.future.cancelled():
                should_send = False
                return
            if ctrl.state.killed:
                raise RuntimeError("Peer exited")
            sock = _wait_until_ready(ctrl)
            with req_state.lock:
                if (
                    req_state.status == "cancelled_local"
                    or result.future.cancelled()
                ):
                    sock.close()
                    sock = None
                    should_send = False
                    return
                with ctrl.lock:
                    if ctrl.state.killed:
                        sock.close()
                        sock = None
                        raise RuntimeError("Peer exited")
                    ctrl.active_sockets.add(sock)
                req_state.status = "sent"
                send_json(
                    sock,
                    {
                        "id": req_id,
                        "op": "run",
                        "block": normalized_block,
                        "record": record,
                        "captured": captured if captured is not None else {},
                    },
                )
        finally:
            with ctrl.send_cond:
                _advance_active_ticket(ctrl)

        if not should_send or sock is None:
            return

        try:
            with sock.makefile("r", encoding="utf-8") as stream:
                resp = recv_json(stream)
        finally:
            with ctrl.lock:
                ctrl.active_sockets.discard(sock)
            try:
                sock.close()
            except OSError:
                pass
            sock = None
        if resp is None:
            msg = (
                "Peer exited"
                if ctrl.state.killed
                else "Peer disconnected before completing request"
            )
            raise RuntimeError(msg)
        if not resp.get("ok", False):
            if resp.get("cancelled", False):
                with req_state.lock:
                    req_state.peer_cancelled = True
                result._peer_ctrl = None  # type: ignore[attr-defined]
                result.future._cancel_hook = None
                result.future.cancel()
                return
            raise _decode_error_response(resp)

        records = [
            CommandRecord(
                args=list(item["args"]),
                rtn=int(item["rtn"]),
                out=item.get("out"),
                err=item.get("err"),
            )
            for item in resp.get("commands", [])
        ]
        rtn = int(resp.get("rtn", 0))
        result._peer_ctrl = None  # type: ignore[attr-defined]
        result.future._cancel_hook = None
        result._set_completed(
            rtn,
            records,
            recorded=record,
            out=resp.get("out"),
            err=resp.get("err"),
        )
    except BaseException as exc:  # noqa: BLE001 - surfaced on PeerResult
        result._peer_ctrl = None  # type: ignore[attr-defined]
        result.future._cancel_hook = None
        _safe_set_exception(result, exc)
    finally:
        result._peer_ctrl = None  # type: ignore[attr-defined]
        result.future._cancel_hook = None
        if sock is not None:
            with ctrl.lock:
                ctrl.active_sockets.discard(sock)
            try:
                sock.close()
            except OSError:
                pass


class _MappingProxy(MutableMapping[str, Any]):
    """Live ``MutableMapping`` stand-in for a peer's ``env`` or ``ctx``."""

    def __init__(self, peer: "RemoteXonsh", prefix: str) -> None:
        self._peer = peer
        self._prefix = prefix

    def __getitem__(self, key: str) -> Any:
        resp = _rpc(
            self._peer._ctrl,
            {"id": uuid.uuid4().hex, "op": f"{self._prefix}_get", "key": key},
        )
        return decode_value(resp["value"])

    def __setitem__(self, key: str, value: Any) -> None:
        # Pickle on the caller thread so unpicklable values raise immediately.
        encoded = encode_value(value)
        _rpc(
            self._peer._ctrl,
            {
                "id": uuid.uuid4().hex,
                "op": f"{self._prefix}_set",
                "key": key,
                "value": encoded,
            },
        )

    def __delitem__(self, key: str) -> None:
        _rpc(
            self._peer._ctrl,
            {"id": uuid.uuid4().hex, "op": f"{self._prefix}_del", "key": key},
        )

    def __contains__(self, key: object) -> bool:
        _check_not_self_main_thread(self._peer._socket_path)
        if not isinstance(key, str):
            return False
        resp = _rpc(
            self._peer._ctrl,
            {
                "id": uuid.uuid4().hex,
                "op": f"{self._prefix}_contains",
                "key": key,
            },
        )
        return bool(resp.get("contains", False))

    def __iter__(self) -> Iterator[str]:
        resp = _rpc(
            self._peer._ctrl,
            {"id": uuid.uuid4().hex, "op": f"{self._prefix}_keys"},
        )
        return iter(list(resp.get("keys", [])))

    def __len__(self) -> int:
        resp = _rpc(
            self._peer._ctrl,
            {"id": uuid.uuid4().hex, "op": f"{self._prefix}_keys"},
        )
        return len(resp.get("keys", []))

    def __repr__(self) -> str:
        return (
            f"<{type(self).__name__} socket={self._peer._socket_path.name}>"
        )


class EnvProxy(_MappingProxy):
    """Live ``MutableMapping`` stand-in for a peer's ``__xonsh__.env``."""

    def __init__(self, peer: "RemoteXonsh") -> None:
        super().__init__(peer, prefix="env")


class CtxProxy(_MappingProxy):
    """Live ``MutableMapping`` stand-in for a peer's ``__xonsh__.ctx``."""

    def __init__(self, peer: "RemoteXonsh") -> None:
        super().__init__(peer, prefix="ctx")


class PeerMacro:
    """Context manager macro for sending ``with!`` blocks to ``RemoteXonsh``."""

    __xonsh_block__ = str
    macro_block: str
    macro_globals: dict[Any, Any]
    macro_locals: dict[Any, Any]

    def __init__(
        self,
        peer: "RemoteXonsh",
        record: bool = True,
        captured: dict[str, Any] | None = None,
    ) -> None:
        """Initializes a ``with!`` macro wrapper for ``peer.send()``."""
        self._peer = peer
        self._record = record
        self._captured = captured

    def __enter__(self) -> PeerResult:
        """Validates ``macro_block`` and dispatches it to the peer."""
        if not hasattr(self, "macro_block"):
            raise ValueError(
                'PeerMacro is missing attribute "macro_block". '
                'Ensure you are using "with!" rather than "with".'
            )
        return self._peer._submit_block(
            self.macro_block,
            record=self._record,
            captured=self._captured,
        )

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Clears xonsh-injected macro attributes to avoid frame retention."""
        for attr in ("macro_block", "macro_globals", "macro_locals"):
            if hasattr(self, attr):
                delattr(self, attr)


class RemoteXonsh:
    """Handle to a cooperating interactive xonsh peer process."""

    def __init__(self) -> None:
        """Spawns a new hidden-terminal peer xonsh in the background."""
        socket_path = new_socket_path("hidden")
        master_fd, slave_fd = pty.openpty()
        try:
            winsize = _copy_starter_winsize()
            fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

            child_env = os.environ.copy()
            child_env[PEER_SOCKET_ENV] = str(socket_path)
            child_env[HIDDEN_PEER_ENV] = "1"

            def _setup_child_tty() -> None:
                os.setsid()
                fcntl.ioctl(0, termios.TIOCSCTTY, 0)

            start_time = time.monotonic()
            cmd = [sys.executable, "-m", "xonsh", "-i"]
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=child_env,
                preexec_fn=_setup_child_tty,
                close_fds=True,
            )
        except BaseException:
            try:
                os.close(slave_fd)
            except OSError:
                pass
            try:
                os.close(master_fd)
            except OSError:
                pass
            raise

        os.close(slave_fd)

        def _is_alive() -> bool:
            return proc.poll() is None

        def _on_kill() -> None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

        ctrl = _PeerController(
            socket_path=socket_path,
            start_time=start_time,
            is_hidden=True,
            is_alive=_is_alive,
            on_kill=_on_kill,
            master_fd=master_fd,
        )
        drainer = threading.Thread(
            target=_drain_pty,
            args=(ctrl.state,),
            daemon=True,
        )
        drainer.start()

        self._ctrl = ctrl
        self._socket_path = socket_path

    @classmethod
    def _from_controller(cls, ctrl: _PeerController) -> "RemoteXonsh":
        """Constructs a ``RemoteXonsh`` around an existing controller."""
        obj = cls.__new__(cls)
        obj._ctrl = ctrl
        obj._socket_path = ctrl.socket_path
        return obj

    @classmethod
    def _from_socket(
        cls,
        socket_path: pathlib.Path | str,
        start_time: float,
        is_alive: Callable[[], bool] | None = None,
        on_kill: Callable[[], None] | None = None,
    ) -> "RemoteXonsh":
        """Attaches a ``RemoteXonsh`` handle to an existing peer socket."""
        sock_path = pathlib.Path(socket_path)
        ctrl = _PeerController(
            socket_path=sock_path,
            start_time=start_time,
            is_hidden=False,
            is_alive=is_alive,
            on_kill=on_kill,
        )
        return cls._from_controller(ctrl)

    @property
    def env(self) -> EnvProxy:
        """Returns a live ``MutableMapping`` proxy for the peer's ``env``."""
        return EnvProxy(self)

    @property
    def ctx(self) -> CtxProxy:
        """Returns a live ``MutableMapping`` proxy for the peer's ``ctx``."""
        return CtxProxy(self)

    @property
    def lastcmd(self) -> LastCmdProxy:
        """Returns a non-blocking proxy for the peer's ``lastcmd``."""
        return LastCmdProxy(self._get_lastcmd_attr)

    def _get_lastcmd_attr(self, attr: str) -> Any:
        """Fetches one attribute from the peer's ``__xonsh__.lastcmd``."""
        resp = _rpc(
            self._ctrl,
            {"id": uuid.uuid4().hex, "op": "lastcmd_get"},
        )
        return resp.get(attr)

    def send(
        self,
        block: str | None = None,
        /,
        capture: Sequence[str] | None = None,
        record: bool = True,
    ) -> PeerResult | PeerMacro:
        """Runs a xonsh block on the peer or returns a ``with!`` macro.

        Args:
            block: Command block string, or ``None`` for ``with!`` macro mode.
            capture: Optional sequence of caller variable names to copy into
                the peer's ``ctx`` before executing the block.
            record: Whether to record stdout/stderr of recorded commands.

        Returns:
            A ``PeerResult`` when ``block`` is given, else a ``PeerMacro``.
        """
        if self._ctrl.state.killed:
            raise RuntimeError("Peer has been killed")
        captured: dict[str, str] | None = None
        if capture is not None:
            frame = inspect.currentframe()
            caller_frame = frame.f_back if frame is not None else None
            del frame
            try:
                captured = capture_vars(caller_frame, capture)
            finally:
                caller_frame = None
        return self._send_from_caller(block, captured=captured, record=record)

    def _send_from_caller(
        self,
        block: str | None = None,
        /,
        captured: dict[str, Any] | None = None,
        record: bool = True,
    ) -> PeerResult | PeerMacro:
        """Dispatches ``send()`` with pre-captured variables."""
        if self._ctrl.state.killed:
            raise RuntimeError("Peer has been killed")
        if block is None:
            return PeerMacro(self, record=record, captured=captured)
        return self._submit_block(block, record=record, captured=captured)

    def _submit_block(
        self,
        block: str,
        record: bool = True,
        captured: dict[str, Any] | None = None,
    ) -> PeerResult:
        """Validates syntax synchronously and dispatches the block."""
        if self._ctrl.state.killed:
            raise RuntimeError("Peer has been killed")
        normalized = _check_syntax_detached(block)
        ctrl = self._ctrl
        sock_path = self._socket_path
        req_id = uuid.uuid4().hex
        req_state = _RequestState()
        ctrl_ref = weakref.ref(ctrl)

        def _cancel_hook() -> bool:
            c = ctrl_ref()
            if c is None or c.state.killed:
                return False
            if c.is_alive is not None and not c.is_alive():
                return False
            err_path = pathlib.Path(f"{c.socket_path}.err")
            if _read_err_sentinel(err_path) is not None:
                return False
            with req_state.lock:
                if req_state.peer_cancelled:
                    return True
                if req_state.status in ("queued_local", "cancelled_local"):
                    req_state.status = "cancelled_local"
                    return True
            return _send_cancel_rpc(c, req_id)

        result = _BoundPeerResult(
            cancel_hook=_cancel_hook,
            self_wait_guard=lambda: _check_not_self_main_thread(sock_path),
        )
        # Keep hidden peer alive while this result is pending; cleared by
        # _request_worker's finally block (avoiding a done-callback cycle).
        result._peer_ctrl = ctrl  # type: ignore[attr-defined]

        with ctrl.lock:
            if ctrl.state.killed:
                result._peer_ctrl = None  # type: ignore[attr-defined]
                raise RuntimeError("Peer has been killed")
            ticket = ctrl.next_ticket
            ctrl.next_ticket += 1
            ctrl.in_flight.add(result)

        worker = threading.Thread(
            target=_request_worker,
            args=(
                ctrl,
                ticket,
                req_id,
                normalized,
                record,
                captured,
                req_state,
                result,
            ),
            daemon=True,
        )
        try:
            worker.start()
        except BaseException:
            result._peer_ctrl = None  # type: ignore[attr-defined]
            result.future._cancel_hook = None
            raise
        return result

    def kill(self) -> None:
        """Terminates the peer and fails all in-flight results."""
        ctrl = self._ctrl
        with ctrl.lock:
            ctrl.state.killed = True
            ctrl.stop_event.set()
            pending = list(ctrl.in_flight)
            sockets = list(ctrl.active_sockets)
            ctrl.active_sockets.clear()
            ctrl.send_cond.notify_all()

        for res in pending:
            _safe_set_exception(res, RuntimeError("Peer exited"))

        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

        if ctrl.state.on_kill is None and ctrl.socket_path.exists():
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(0.5)
                sock.connect(str(ctrl.socket_path))
                try:
                    send_json(
                        sock,
                        {
                            "version": PROTOCOL_VERSION,
                            "id": uuid.uuid4().hex,
                            "op": "kill",
                        },
                    )
                finally:
                    sock.close()
            except OSError:
                pass

        ctrl.state.cleanup(remove_sockets=True)

    def __enter__(self) -> "RemoteXonsh":
        """Enters the context manager, returning ``self``."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Kills the peer when exiting the context manager."""
        self.kill()

    def __repr__(self) -> str:
        """Returns a non-blocking summary without contacting the peer."""
        kind = "hidden" if self._ctrl.is_hidden else "attached"
        if self._ctrl.state.killed:
            status = "killed"
        elif self._ctrl.is_alive is not None and not self._ctrl.is_alive():
            status = "exited"
        elif self._socket_path.exists():
            status = "ready"
        else:
            status = "starting"
        return (
            f"<RemoteXonsh {kind} {status} socket={self._socket_path.name}>"
        )
