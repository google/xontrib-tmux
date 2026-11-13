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

"""Hermetic unit tests for ``xontrib_tmux.peer`` and ``RemoteXonsh``."""

import ast
from collections.abc import Callable
import gc
import os
import pathlib
import socket
import sys
import threading
import time
from typing import Any
from unittest.mock import MagicMock

import pytest
from xonsh.completers.python import attr_complete

from xontrib_tmux import _load_xontrib_, _unload_xontrib_
from xontrib_tmux.peer import (
    PeerMacro,
    RemoteXonsh,
    _PeerController,
    _advance_active_ticket,
    _wait_ticket_turn,
)
from xontrib_tmux.protocol import (
    HIDDEN_PEER_ENV,
    PEER_SOCKET_ENV,
    recv_json,
    send_json,
)


class _MockPeerServer:
    """Minimal ``AF_UNIX`` mock peer server for hermetic client tests."""

    def __init__(
        self,
        socket_path: pathlib.Path,
        handler: Callable[[socket.socket, dict[str, Any]], None],
        bind_now: bool = True,
    ) -> None:
        self.socket_path = socket_path
        self._handler = handler
        self._stop = threading.Event()
        self._srv: socket.socket | None = None
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        if bind_now:
            self.bind()
        self._accept_thread = threading.Thread(
            target=self._serve, daemon=True
        )

    def bind(self) -> None:
        if self._srv is not None:
            return
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.socket_path))
        srv.listen(16)
        srv.settimeout(0.05)
        self._srv = srv

    def start(self) -> "_MockPeerServer":
        self._accept_thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            if self._srv is None:
                time.sleep(0.01)
                continue
            try:
                conn, _ = self._srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            t = threading.Thread(
                target=self._handle_conn, args=(conn,), daemon=True
            )
            with self.lock:
                self._threads.append(t)
            t.start()

    def _handle_conn(self, conn: socket.socket) -> None:
        try:
            with conn.makefile("r", encoding="utf-8") as f:
                req = recv_json(f)
                if req is None:
                    return
                with self.lock:
                    self.requests.append(req)
                self._handler(conn, req)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def close(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        self._accept_thread.join(timeout=1.0)
        with self.lock:
            threads = list(self._threads)
        for t in threads:
            t.join(timeout=1.0)
        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass


def _short_sock(tmp_path: pathlib.Path, name: str = "p.sock") -> pathlib.Path:
    """Returns a short socket path under 100 bytes."""
    candidate = tmp_path / name
    if len(str(candidate).encode("utf-8")) < 95:
        return candidate
    return pathlib.Path(f"/tmp/xt_test_{os.getpid()}_{name}")


def _peer_from_ctrl(ctrl: _PeerController) -> RemoteXonsh:
    """Builds a ``RemoteXonsh`` around an existing ``_PeerController``."""
    peer = RemoteXonsh.__new__(RemoteXonsh)
    peer._ctrl = ctrl
    peer._socket_path = ctrl.socket_path
    return peer


def _peer_from_socket(
    socket_path: pathlib.Path,
    start_time: float,
    *,
    is_hidden: bool = False,
    is_alive: Callable[[], bool] | None = None,
    on_kill: Callable[[], None] | None = None,
) -> RemoteXonsh:
    """Builds a test ``RemoteXonsh`` wired directly to ``socket_path``."""
    ctrl = _PeerController(
        socket_path=socket_path,
        start_time=start_time,
        is_hidden=is_hidden,
        is_alive=is_alive,
        on_kill=on_kill,
    )
    return _peer_from_ctrl(ctrl)


def test_send_string_and_record_false(tmp_path: pathlib.Path) -> None:
    sock = _short_sock(tmp_path, "send.sock")

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        rec = bool(req.get("record", True))
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {
                        "args": ["echo", "hi"],
                        "rtn": 0,
                        "out": "hi\n" if rec else None,
                        "err": "" if rec else None,
                    }
                ],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        r1 = peer.send("echo hi")
        assert r1.rtn == 0
        assert r1.out == "hi\n"
        assert r1.err == ""
        assert r1.commands[0].args == ["echo", "hi"]

        r2 = peer.send("echo hi", record=False)
        assert r2.rtn == 0
        assert r2.out is None
        assert r2.err is None
        assert r2.commands[0].out is None

        assert len(srv.requests) == 2
        assert srv.requests[0]["op"] == "run"
        assert srv.requests[0]["block"] == "echo hi\n"
        assert srv.requests[0]["record"] is True
        assert srv.requests[1]["record"] is False
    finally:
        srv.close()


def test_with_bang_macro_and_plain_with_error(
    tmp_path: pathlib.Path, xonsh_execer: Any
) -> None:
    sock = _short_sock(tmp_path, "macro.sock")

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {
                        "args": ["echo", "macro"],
                        "rtn": 0,
                        "out": "macro\n",
                        "err": "",
                    }
                ],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())

        macro = peer.send()
        assert isinstance(macro, PeerMacro)
        with pytest.raises(ValueError, match="with!"):
            with macro:
                pass

        ns: dict[str, Any] = {"peer": peer}
        xonsh_execer.exec(
            "with! peer.send() as run:\n    echo macro\n",
            glbs=ns,
            locs=ns,
        )
        run = ns["run"]
        assert run.rtn == 0
        assert run.out == "macro\n"
        assert "echo macro\n" in srv.requests[0]["block"]
    finally:
        srv.close()


def test_local_syntax_and_empty_validation(tmp_path: pathlib.Path) -> None:
    sock = _short_sock(tmp_path, "val.sock")
    peer = _peer_from_socket(sock, time.monotonic())

    with pytest.raises(SyntaxError):
        peer.send("if True:\n")
    with pytest.raises(ValueError):
        peer.send("# comment only\n")
    with pytest.raises(ValueError):
        peer.send("   \n")
    with pytest.raises(TypeError):
        peer.send(123)  # type: ignore[arg-type]


def test_fifo_ordering_across_concurrent_sends(tmp_path: pathlib.Path) -> None:
    sock = _short_sock(tmp_path, "fifo.sock")
    gate = threading.Event()
    seen: list[str] = []
    seen_lock = threading.Lock()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        blk = req["block"].strip()
        with seen_lock:
            seen.append(blk)
        if blk == "echo first":
            gate.wait(timeout=2.0)
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {
                        "args": blk.split(),
                        "rtn": 0,
                        "out": blk + "\n",
                        "err": "",
                    }
                ],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        r1 = peer.send("echo first")
        r2 = peer.send("echo second")
        assert r2.out == "echo second\n"
        gate.set()
        assert r1.out == "echo first\n"
        assert seen == ["echo first", "echo second"]
    finally:
        gate.set()
        srv.close()


def test_startup_holding_until_socket_bound(tmp_path: pathlib.Path) -> None:
    sock = _short_sock(tmp_path, "hold.sock")

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {
                        "args": ["echo", "ready"],
                        "rtn": 0,
                        "out": "ready\n",
                        "err": "",
                    }
                ],
            },
        )

    srv = _MockPeerServer(sock, handler, bind_now=False).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        assert "starting" in repr(peer)
        run = peer.send("echo ready")
        assert not run.future.done()
        time.sleep(0.05)
        srv.bind()
        assert run.rtn == 0
        assert run.out == "ready\n"
        assert "ready" in repr(peer)
    finally:
        srv.close()


def test_fail_fast_on_err_sentinel_and_dead_process(
    tmp_path: pathlib.Path,
) -> None:
    sock1 = _short_sock(tmp_path, "err1.sock")
    err1 = pathlib.Path(f"{sock1}.err")
    err1.write_text("Peer not using prompt_toolkit\n", encoding="utf-8")
    try:
        p1 = _peer_from_socket(sock1, time.monotonic())
        t0 = time.monotonic()
        r1 = p1.send("echo hi")
        with pytest.raises(RuntimeError, match="prompt_toolkit"):
            _ = r1.rtn
        assert time.monotonic() - t0 < 1.0
    finally:
        err1.unlink(missing_ok=True)

    sock2 = _short_sock(tmp_path, "dead.sock")
    alive = True
    p2 = _peer_from_socket(
        sock2, time.monotonic(), is_alive=lambda: alive
    )
    t0 = time.monotonic()
    r2 = p2.send("echo hi")
    alive = False
    with pytest.raises(RuntimeError, match="exited"):
        _ = r2.rtn
    assert time.monotonic() - t0 < 1.0


def test_start_timeout_expires(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sock = _short_sock(tmp_path, "timeout.sock")
    monkeypatch.setenv("XONTRIB_TMUX_READY_TIMEOUT", "0.05")
    peer = _peer_from_socket(sock, time.monotonic() - 1.0)
    run = peer.send("echo late")
    with pytest.raises(RuntimeError, match="Timed out"):
        _ = run.rtn


def test_disconnect_mid_block_and_peer_error_response(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "disc.sock")

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        blk = req["block"].strip()
        if blk == "echo drop":
            return
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": False,
                "error_type": "SyntaxError",
                "error": "peer rejected syntax",
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        r_drop = peer.send("echo drop")
        with pytest.raises(RuntimeError, match="disconnected"):
            _ = r_drop.rtn

        r_syn = peer.send("echo syn")
        with pytest.raises(SyntaxError, match="peer rejected syntax"):
            _ = r_syn.rtn
    finally:
        srv.close()


def test_kill_fails_inflight_and_rejects_new_sends(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "kill.sock")
    err_file = pathlib.Path(f"{sock}.err")
    err_file.write_text("", encoding="utf-8")
    err_file.unlink()

    in_handler = threading.Event()
    release = threading.Event()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        in_handler.set()
        release.wait(timeout=2.0)

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        pending = peer.send("sleep 10")
        assert in_handler.wait(timeout=1.0)

        peer.kill()
        assert "killed" in repr(peer)
        assert not sock.exists()
        with pytest.raises(RuntimeError, match="Peer exited"):
            _ = pending.rtn

        with pytest.raises(RuntimeError, match="killed"):
            peer.send("echo after")
        with pytest.raises(RuntimeError, match="killed"):
            peer.send()
    finally:
        release.set()
        srv.close()


def test_context_manager_kills_on_exit(tmp_path: pathlib.Path) -> None:
    killed_called = threading.Event()
    sock = _short_sock(tmp_path, "ctxmgr.sock")
    peer = _peer_from_socket(
        sock,
        time.monotonic(),
        is_hidden=True,
        is_alive=lambda: True,
        on_kill=killed_called.set,
    )
    with peer as ctx_peer:
        assert ctx_peer is peer
        assert not killed_called.is_set()
    assert killed_called.wait(timeout=1.0)


def test_hidden_peer_lifetime_kept_alive_by_pending_result(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "hidden_life.sock")
    kill_events: list[str] = []
    gate = threading.Event()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        gate.wait(timeout=2.0)
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {"args": ["echo", "ok"], "rtn": 0, "out": "ok\n", "err": ""}
                ],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        ctrl = _PeerController(
            socket_path=sock,
            start_time=time.monotonic(),
            is_hidden=True,
            is_alive=lambda: True,
            on_kill=lambda: kill_events.append("killed"),
        )
        peer = _peer_from_ctrl(ctrl)
        del ctrl
        res = peer.send("echo ok")
        del peer
        gc.collect()
        # Still pending -> hidden peer must remain alive.
        assert kill_events == []

        gate.set()
        assert res.out == "ok\n"
        # Wait briefly for _request_worker finally block to drop ctrl.
        deadline = time.monotonic() + 1.0
        while not kill_events and time.monotonic() < deadline:
            gc.collect()
            time.sleep(0.01)
        assert kill_events == ["killed"]
    finally:
        gate.set()
        srv.close()


def test_repr_and_tab_completion_never_block(
    tmp_path: pathlib.Path, xonsh_execer: Any
) -> None:
    del xonsh_execer
    sock = _short_sock(tmp_path, "comp.sock")
    peer = _peer_from_socket(sock, time.monotonic())
    t0 = time.monotonic()
    text = repr(peer)
    comps = attr_complete("peer.", {"peer": peer}, lambda o, a: o.startswith(a))
    dt = time.monotonic() - t0
    assert dt < 0.05
    assert "RemoteXonsh" in text
    assert {"peer.kill(", "peer.send("} <= set(comps)


def test_xontrib_exports_and_unloads_remote_xonsh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xontrib_tmux
    from xontrib_tmux import parser_patch

    curtmuxw_calls = 0

    def fake_curtmuxw() -> None:
        nonlocal curtmuxw_calls
        curtmuxw_calls += 1
        return None

    monkeypatch.setattr(xontrib_tmux, "curtmuxw", fake_curtmuxw)
    monkeypatch.setattr(parser_patch, "_is_hidden_peer", True, raising=False)
    mock_xsh = MagicMock()
    mock_xsh.ctx = {}
    _load_xontrib_(mock_xsh)
    assert mock_xsh.ctx["RemoteXonsh"] is RemoteXonsh
    assert curtmuxw_calls == 0
    _unload_xontrib_(mock_xsh)
    assert "RemoteXonsh" not in mock_xsh.ctx

    monkeypatch.setattr(parser_patch, "_is_hidden_peer", False, raising=False)
    _load_xontrib_(mock_xsh)
    assert curtmuxw_calls == 1
    _unload_xontrib_(mock_xsh)


def test_peer_layer_has_no_libtmux_or_tmux_env_references() -> None:
    peer_src = (
        pathlib.Path(__file__).resolve().parent.parent
        / "xontrib_tmux"
        / "peer.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(peer_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith("libtmux")
        elif isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("libtmux")
    assert '"TMUX"' not in peer_src
    assert '"TMUX_PANE"' not in peer_src


def test_hidden_init_pty_spawn_and_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import fcntl
    import pty
    import signal
    import subprocess
    import xontrib_tmux.peer as peer_mod

    sock = _short_sock(tmp_path, "spawn.sock")
    monkeypatch.setattr(peer_mod, "new_socket_path", lambda prefix: sock)
    monkeypatch.setattr(pty, "openpty", lambda: (101, 102))

    ioctl_calls: list[tuple[int, int]] = []

    def fake_ioctl(fd: int, req: int, arg: Any) -> bytes:
        ioctl_calls.append((fd, req))
        return b"\x00" * 8

    monkeypatch.setattr(fcntl, "ioctl", fake_ioctl)

    closed_fds: list[int] = []
    drainer_done = threading.Event()
    monkeypatch.setattr(os, "close", lambda fd: closed_fds.append(fd))

    def fake_read(fd: int, n: int) -> bytes:
        drainer_done.set()
        return b""

    monkeypatch.setattr(os, "read", fake_read)

    popen_calls: list[dict[str, Any]] = []
    fake_proc = MagicMock()
    fake_proc.pid = 424242
    fake_proc.poll.return_value = None

    def fake_popen(cmd: list[str], **kwargs: Any) -> Any:
        popen_calls.append({"cmd": cmd, **kwargs})
        return fake_proc

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    killpg_calls: list[tuple[int, int]] = []
    monkeypatch.setattr(
        os, "killpg", lambda pid, sig: killpg_calls.append((pid, sig))
    )

    peer = RemoteXonsh()
    assert len(popen_calls) == 1
    call = popen_calls[0]
    assert call["cmd"] == [sys.executable, "-m", "xonsh", "-i"]
    assert call["stdin"] == 102
    assert call["stdout"] == 102
    assert call["stderr"] == 102
    assert call["env"][PEER_SOCKET_ENV] == str(sock)
    assert call["env"][HIDDEN_PEER_ENV] == "1"
    assert 102 in closed_fds

    assert drainer_done.wait(timeout=1.0)
    assert "ready" in repr(peer) or "starting" in repr(peer)
    fake_proc.poll.return_value = 0
    assert "exited" in repr(peer)

    peer.kill()
    assert (424242, signal.SIGKILL) in killpg_calls
    assert 101 in closed_fds


def test_hidden_init_popen_failure_closes_fds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import fcntl
    import pty
    import subprocess
    import xontrib_tmux.peer as peer_mod

    sock = _short_sock(tmp_path, "fail_spawn.sock")
    monkeypatch.setattr(peer_mod, "new_socket_path", lambda prefix: sock)
    monkeypatch.setattr(pty, "openpty", lambda: (201, 202))
    monkeypatch.setattr(fcntl, "ioctl", lambda fd, req, arg: b"\x00" * 8)

    closed_fds: list[int] = []
    monkeypatch.setattr(os, "close", lambda fd: closed_fds.append(fd))

    def fail_popen(*args: Any, **kwargs: Any) -> Any:
        raise OSError("spawn failed")

    monkeypatch.setattr(subprocess, "Popen", fail_popen)
    with pytest.raises(OSError, match="spawn failed"):
        RemoteXonsh()
    assert set(closed_fds) == {201, 202}


def test_kill_and_second_send_do_not_block_while_starting(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "nonblock_kill.sock")
    peer = _peer_from_socket(sock, time.monotonic())

    t0 = time.monotonic()
    r1 = peer.send("echo 1")
    r2 = peer.send("echo 2")
    peer.kill()
    dt = time.monotonic() - t0

    assert dt < 0.1
    with pytest.raises(RuntimeError, match="Peer exited"):
        _ = r1.rtn
    with pytest.raises(RuntimeError, match="Peer exited"):
        _ = r2.rtn


def test_post_ready_exit_fails_immediately_without_startup_timeout(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "post_ready.sock")

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [
                    {"args": ["echo", "1"], "rtn": 0, "out": "1\n", "err": ""}
                ],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = _peer_from_socket(sock, time.monotonic())
        assert peer.send("echo 1").rtn == 0
    finally:
        srv.close()

    t0 = time.monotonic()
    r2 = peer.send("echo 2")
    with pytest.raises(RuntimeError, match="Peer exited"):
        _ = r2.rtn
    assert time.monotonic() - t0 < 0.2


def test_abandoned_ticket_does_not_stall_later_waiters(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "ticket_abandon.sock")
    ctrl = _PeerController(
        socket_path=sock,
        start_time=time.monotonic(),
        is_hidden=False,
        is_alive=lambda: True,
    )
    with ctrl.lock:
        t0 = ctrl.next_ticket
        ctrl.next_ticket += 1
        t1 = ctrl.next_ticket
        ctrl.next_ticket += 1
        t2 = ctrl.next_ticket
        ctrl.next_ticket += 1
    assert (t0, t1, t2) == (0, 1, 2)

    # Simulate t1 interrupting while waiting for t0.
    t1_done = threading.Event()
    original_wait = ctrl.send_cond.wait

    def _interrupt_t1() -> None:
        ctrl.send_cond.wait = lambda *a, **kw: (  # type: ignore[method-assign]
            (_ for _ in ()).throw(KeyboardInterrupt())
        )
        try:
            with pytest.raises(KeyboardInterrupt):
                _wait_ticket_turn(ctrl, t1)
        finally:
            ctrl.send_cond.wait = original_wait  # type: ignore[method-assign]
            t1_done.set()

    th1 = threading.Thread(target=_interrupt_t1)
    th1.start()
    assert t1_done.wait(timeout=1.0)
    th1.join(timeout=1.0)

    t2_done = threading.Event()

    def _wait_t2() -> None:
        _wait_ticket_turn(ctrl, t2)
        t2_done.set()

    th2 = threading.Thread(target=_wait_t2)
    th2.start()

    with ctrl.send_cond:
        _advance_active_ticket(ctrl)
    assert t2_done.wait(timeout=1.0)
    th2.join(timeout=1.0)
