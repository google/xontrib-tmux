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
from collections.abc import Callable, MutableMapping
from concurrent.futures import CancelledError
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
    CtxProxy,
    EnvProxy,
    PeerMacro,
    RemoteXonsh,
    _PeerController,
    _wait_ticket_turn,
)
from xontrib_tmux.protocol import (
    HIDDEN_PEER_ENV,
    PEER_SOCKET_ENV,
    decode_captured,
    decode_value,
    encode_value,
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
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
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
        peer = RemoteXonsh._from_socket(sock, time.monotonic())

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
    peer = RemoteXonsh._from_socket(sock, time.monotonic())

    with pytest.raises(SyntaxError):
        peer.send("if True:\n")
    with pytest.raises(ValueError):
        peer.send("# comment only\n")
    with pytest.raises(ValueError):
        peer.send("   \n")
    with pytest.raises(TypeError):
        peer.send(123)  # type: ignore[arg-type]
    with pytest.raises(NameError, match="x"):
        peer.send("echo hi", capture=["x"])


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
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
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
                    {"args": ["echo", "ready"], "rtn": 0, "out": "ready\n",
                     "err": ""}
                ],
            },
        )

    srv = _MockPeerServer(sock, handler, bind_now=False).start()
    try:
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
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
        p1 = RemoteXonsh._from_socket(sock1, time.monotonic())
        t0 = time.monotonic()
        r1 = p1.send("echo hi")
        with pytest.raises(RuntimeError, match="prompt_toolkit"):
            _ = r1.rtn
        assert time.monotonic() - t0 < 1.0
    finally:
        err1.unlink(missing_ok=True)

    sock2 = _short_sock(tmp_path, "dead.sock")
    alive = True
    p2 = RemoteXonsh._from_socket(
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
    peer = RemoteXonsh._from_socket(sock, time.monotonic() - 1.0)
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
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        r_drop = peer.send("echo drop")
        with pytest.raises(RuntimeError, match="disconnected"):
            _ = r_drop.rtn

        r_syn = peer.send("echo syn")
        with pytest.raises(SyntaxError, match="peer rejected syntax"):
            _ = r_syn.rtn
    finally:
        srv.close()


def test_kill_fails_inflight_sends_kill_op_and_rejects_new_sends(
    tmp_path: pathlib.Path,
) -> None:
    sock = _short_sock(tmp_path, "kill.sock")
    err_file = pathlib.Path(f"{sock}.err")
    err_file.write_text("", encoding="utf-8")
    err_file.unlink()

    in_handler = threading.Event()
    release = threading.Event()
    kill_seen = threading.Event()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req.get("op") == "kill":
            kill_seen.set()
            return
        in_handler.set()
        release.wait(timeout=2.0)

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        pending = peer.send("sleep 10")
        assert in_handler.wait(timeout=1.0)

        peer.kill()
        assert kill_seen.wait(timeout=1.0)
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
    peer = RemoteXonsh._from_socket(
        sock,
        time.monotonic(),
        is_alive=lambda: True,
        on_kill=killed_called.set,
    )
    with peer as ctx_peer:
        assert ctx_peer is peer
        assert not killed_called.is_set()
    assert killed_called.wait(timeout=1.0)

    # Dropping a non-hidden _from_socket peer must NOT kill it.
    non_hidden_killed = False

    def _k() -> None:
        nonlocal non_hidden_killed
        non_hidden_killed = True

    p2 = RemoteXonsh._from_socket(
        _short_sock(tmp_path, "nonhidden.sock"),
        time.monotonic(),
        is_alive=lambda: True,
        on_kill=_k,
    )
    del p2
    gc.collect()
    assert non_hidden_killed is False


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
        peer = RemoteXonsh._from_controller(ctrl)
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
    peer = RemoteXonsh._from_socket(sock, time.monotonic())
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
    peer = RemoteXonsh._from_socket(sock, time.monotonic())

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
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        assert peer.send("echo 1").rtn == 0
    finally:
        srv.close()

    t0 = time.monotonic()
    r2 = peer.send("echo 2")
    with pytest.raises(RuntimeError, match="Peer exited"):
        _ = r2.rtn
    assert time.monotonic() - t0 < 0.2


def test_env_and_ctx_proxies_and_lastcmd_round_trip(
    tmp_path: pathlib.Path, xonsh_execer: Any
) -> None:
    del xonsh_execer
    sock = _short_sock(tmp_path, "proxies.sock")
    env_store: dict[str, Any] = {"PATH": ["/usr/bin"]}
    ctx_store: dict[str, Any] = {"existing": 42}
    lastcmd_data: dict[str, Any] = {
        "args": None,
        "rtn": None,
        "out": None,
        "err": None,
    }
    ops_seen: list[str] = []

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        op = req["op"]
        ops_seen.append(op)
        rid = req["id"]
        if op.startswith("env_") or op.startswith("ctx_"):
            prefix, action = op.split("_", 1)
            store = env_store if prefix == "env" else ctx_store
            if action == "get":
                key = req["key"]
                if key not in store:
                    send_json(
                        conn,
                        {
                            "id": rid,
                            "ok": False,
                            "error_type": "KeyError",
                            "error": repr(key),
                        },
                    )
                else:
                    send_json(
                        conn,
                        {
                            "id": rid,
                            "ok": True,
                            "value": encode_value(store[key]),
                        },
                    )
            elif action == "set":
                store[req["key"]] = decode_value(req["value"])
                send_json(conn, {"id": rid, "ok": True})
            elif action == "del":
                key = req["key"]
                if key not in store:
                    send_json(
                        conn,
                        {
                            "id": rid,
                            "ok": False,
                            "error_type": "KeyError",
                            "error": repr(key),
                        },
                    )
                else:
                    del store[key]
                    send_json(conn, {"id": rid, "ok": True})
            elif action == "contains":
                send_json(
                    conn,
                    {"id": rid, "ok": True, "contains": req["key"] in store},
                )
            elif action == "keys":
                send_json(
                    conn,
                    {"id": rid, "ok": True, "keys": list(store.keys())},
                )
        elif op == "lastcmd_get":
            send_json(conn, {"id": rid, "ok": True, **lastcmd_data})

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        t0 = time.monotonic()
        assert isinstance(peer.env, EnvProxy)
        assert isinstance(peer.env, MutableMapping)
        assert isinstance(peer.ctx, CtxProxy)
        assert isinstance(peer.ctx, MutableMapping)
        assert "EnvProxy" in repr(peer.env)
        assert "CtxProxy" in repr(peer.ctx)
        assert repr(peer.lastcmd) == "<LastCmdProxy>"
        c_worker = attr_complete(
            "worker.", {"worker": peer}, lambda o, a: o.startswith(a)
        )
        c_last = attr_complete(
            "worker.lastcmd.", {"worker": peer}, lambda o, a: o.startswith(a)
        )
        assert time.monotonic() - t0 < 0.05
        assert ops_seen == []
        assert {"worker.env[", "worker.ctx[", "worker.lastcmd"} <= set(
            c_worker
        )
        assert {
            "worker.lastcmd.args",
            "worker.lastcmd.rtn",
            "worker.lastcmd.out",
            "worker.lastcmd.err",
        } <= set(c_last)

        # Unpicklable values must fail locally without hitting the socket.
        unpicklable = (i for i in range(2))
        with pytest.raises(TypeError):
            peer.env["BAD"] = unpicklable
        with pytest.raises(TypeError):
            peer.ctx["BAD"] = (i for i in range(2))
        assert ops_seen == []

        # env CRUD + copy semantics.
        assert "PATH" in peer.env
        assert 123 not in peer.env  # type: ignore[operator]
        path_copy = peer.env["PATH"]
        path_copy.append("/extra")
        assert peer.env["PATH"] == ["/usr/bin"]
        peer.env["CC"] = "clang"
        assert env_store["CC"] == "clang"
        assert peer.env.get("MISSING", "fallback") == "fallback"
        assert sorted(peer.env) == ["CC", "PATH"]
        assert len(peer.env) == 2
        del peer.env["CC"]
        assert "CC" not in peer.env
        with pytest.raises(KeyError):
            _ = peer.env["CC"]
        with pytest.raises(KeyError):
            del peer.env["CC"]

        # ctx CRUD.
        assert peer.ctx["existing"] == 42
        peer.ctx["target"] = {"mode": "release"}
        assert list(peer.ctx) == ["existing", "target"]
        del peer.ctx["existing"]
        assert len(peer.ctx) == 1

        # lastcmd before and after a command.
        assert peer.lastcmd.args is None
        assert peer.lastcmd.rtn is None
        assert peer.lastcmd.out is None
        assert peer.lastcmd.err is None
        lastcmd_data.update(
            {"args": ["echo", "clang"], "rtn": 0, "out": "clang\n", "err": ""}
        )
        assert peer.lastcmd.args == ["echo", "clang"]
        assert peer.lastcmd.rtn == 0
        assert peer.lastcmd.out == "clang\n"
        assert peer.lastcmd.err == ""
    finally:
        srv.close()


def test_capture_in_string_and_macro_forms(tmp_path: pathlib.Path) -> None:
    sock = _short_sock(tmp_path, "capture.sock")
    seen_reqs: list[dict[str, Any]] = []

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        seen_reqs.append(req)
        send_json(
            conn,
            {
                "id": req["id"],
                "ok": True,
                "rtn": 0,
                "commands": [],
            },
        )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        local_var = {"count": 3, "label": "items"}

        res1 = peer.send("echo hi", capture=["local_var"])
        assert res1.rtn == 0

        macro = peer.send(capture=["local_var"])
        assert isinstance(macro, PeerMacro)
        macro.macro_block = "echo from-macro\n"
        macro.macro_globals = {}
        macro.macro_locals = {}
        with macro as res2:
            assert res2.rtn == 0

        assert len(seen_reqs) == 2
        assert decode_captured(seen_reqs[0]["captured"]) == {
            "local_var": {"count": 3, "label": "items"}
        }
        assert decode_captured(seen_reqs[1]["captured"]) == {
            "local_var": {"count": 3, "label": "items"}
        }

        with pytest.raises(NameError, match="no_such_var"):
            peer.send("echo hi", capture=["no_such_var"])
        unpicklable = (i for i in range(3))
        with pytest.raises(TypeError):
            peer.send("echo hi", capture=["unpicklable"])
        with pytest.raises(TypeError):
            peer.send("echo hi", capture="local_var")  # type: ignore[arg-type]
        assert len(seen_reqs) == 2
    finally:
        srv.close()


def test_future_cancel_local_and_peer_arbitrated(
    tmp_path: pathlib.Path,
) -> None:
    # 1. Local cancel while still holding before socket exists.
    unready_sock = _short_sock(tmp_path, "unready_cancel.sock")
    ctrl = _PeerController(
        socket_path=unready_sock,
        start_time=time.monotonic(),
        is_hidden=False,
        is_alive=lambda: True,
    )
    unready_peer = RemoteXonsh._from_controller(ctrl)
    try:
        r_local = unready_peer.send("echo never-sent")
        assert r_local.future.cancel() is True
        with pytest.raises(CancelledError):
            _ = r_local.rtn
    finally:
        unready_peer.kill()

    # 2. Dead peer returns False on cancel.
    dead_sock = _short_sock(tmp_path, "dead_cancel.sock")
    dead_ctrl = _PeerController(
        socket_path=dead_sock,
        start_time=time.monotonic(),
        is_hidden=False,
        is_alive=lambda: False,
    )
    dead_peer = RemoteXonsh._from_controller(dead_ctrl)
    r_dead = dead_peer.send("echo dead")
    assert r_dead.future.cancel() is False
    try:
        _ = r_dead.rtn
    except RuntimeError:
        pass

    # 2b. Startup .err sentinel returns False on cancel even while queued.
    err_sock = _short_sock(tmp_path, "err_cancel.sock")
    pathlib.Path(f"{err_sock}.err").write_text("bad rc", encoding="utf-8")
    err_ctrl = _PeerController(
        socket_path=err_sock,
        start_time=time.monotonic(),
        is_hidden=False,
        is_alive=lambda: True,
    )
    err_peer = RemoteXonsh._from_controller(err_ctrl)
    r_err = err_peer.send("echo err")
    assert r_err.future.cancel() is False
    with pytest.raises(RuntimeError, match="bad rc"):
        _ = r_err.rtn
    pathlib.Path(f"{err_sock}.err").unlink(missing_ok=True)

    # 3. Peer-arbitrated cancel round-trip (accepted vs rejected).
    sock = _short_sock(tmp_path, "cancel_rt.sock")
    run_conns: dict[str, socket.socket] = {}
    run_reqs: dict[str, dict[str, Any]] = {}
    run_seen = threading.Event()
    allow_finish = threading.Event()

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        op = req["op"]
        if op == "run":
            run_reqs[req["id"]] = req
            run_conns[req["id"]] = conn
            run_seen.set()
            if req["block"].strip() == "echo running":
                allow_finish.wait(timeout=2.0)
                send_json(
                    conn,
                    {
                        "id": req["id"],
                        "ok": True,
                        "rtn": 0,
                        "commands": [
                            {
                                "args": ["echo", "running"],
                                "rtn": 0,
                                "out": "running\n",
                                "err": "",
                            }
                        ],
                    },
                )
            else:
                allow_finish.wait(timeout=2.0)
        elif op == "cancel":
            target_id = req["target_id"]
            target_req = run_reqs.get(target_id)
            if target_req and target_req["block"].strip() == "echo queued":
                target_conn = run_conns[target_id]
                send_json(
                    target_conn,
                    {"id": target_id, "ok": False, "cancelled": True},
                )
                send_json(
                    conn,
                    {"id": req["id"], "ok": True, "cancelled": True},
                )
            else:
                send_json(
                    conn,
                    {"id": req["id"], "ok": True, "cancelled": False},
                )

    srv = _MockPeerServer(sock, handler).start()
    try:
        peer = RemoteXonsh._from_socket(sock, time.monotonic())
        running = peer.send("echo running")
        assert run_seen.wait(timeout=1.0)
        run_seen.clear()
        queued = peer.send("echo queued")
        assert run_seen.wait(timeout=1.0)

        assert running.future.cancel() is False
        assert queued.future.cancel() is True

        allow_finish.set()
        assert running.rtn == 0
        assert running.out == "running\n"
        with pytest.raises(CancelledError):
            _ = queued.rtn
    finally:
        allow_finish.set()
        srv.close()


def test_self_send_and_self_wait_guard(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import xontrib_tmux.peer_server as peer_server_mod

    sock = _short_sock(tmp_path, "self.sock")
    link_sock = _short_sock(tmp_path, "self_link.sock")
    os.symlink(sock, link_sock)

    def handler(conn: socket.socket, req: dict[str, Any]) -> None:
        if req["op"] == "run":
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
                            "out": "hi\n",
                            "err": "",
                        }
                    ],
                },
            )
        elif req["op"] == "env_get":
            send_json(
                conn,
                {"id": req["id"], "ok": True, "value": encode_value("val")},
            )
        elif req["op"] == "lastcmd_get":
            send_json(
                conn,
                {
                    "id": req["id"],
                    "ok": True,
                    "args": ["echo"],
                    "rtn": 0,
                    "out": "hi\n",
                    "err": "",
                },
            )

    srv = _MockPeerServer(sock, handler).start()
    try:
        monkeypatch.setattr(
            peer_server_mod, "current_peer_socket_path", lambda: str(sock)
        )
        # Connect via symlink to verify realpath comparison.
        self_peer = RemoteXonsh._from_socket(link_sock, time.monotonic())

        # Sending from main thread inside peer is allowed and queued.
        hold_res = self_peer.send("echo hi")
        # Keep a pending result using a starting/unready self socket to test
        # _wait() self-wait guard across waiting members.
        unbound_sock = _short_sock(tmp_path, "self_unbound.sock")
        monkeypatch.setattr(
            peer_server_mod,
            "current_peer_socket_path",
            lambda: str(unbound_sock),
        )
        unready_ctrl = _PeerController(
            socket_path=unbound_sock,
            start_time=time.monotonic(),
            is_hidden=False,
            is_alive=lambda: True,
        )
        blocked_peer = RemoteXonsh._from_controller(unready_ctrl)
        pending = blocked_peer._submit_block("echo later")
        try:
            with pytest.raises(RuntimeError, match="own peer"):
                _ = pending.rtn
            with pytest.raises(RuntimeError, match="own peer"):
                _ = pending.out
            with pytest.raises(RuntimeError, match="own peer"):
                pending.end()
            with pytest.raises(RuntimeError, match="own peer"):
                _ = bool(pending)
            with pytest.raises(RuntimeError, match="own peer"):
                _ = pending.future.result()
            with pytest.raises(RuntimeError, match="own peer"):
                _ = pending.future.exception()
        finally:
            blocked_peer.kill()
            try:
                _ = pending.future.result(timeout=1.0)
            except RuntimeError:
                pass

        monkeypatch.setattr(
            peer_server_mod, "current_peer_socket_path", lambda: str(sock)
        )

        # env/ctx/lastcmd RPCs on self main thread always raise immediately.
        with pytest.raises(RuntimeError, match="own peer"):
            _ = self_peer.env["A"]
        with pytest.raises(RuntimeError, match="own peer"):
            self_peer.env["A"] = "1"
        with pytest.raises(RuntimeError, match="own peer"):
            _ = "A" in self_peer.ctx
        with pytest.raises(RuntimeError, match="own peer"):
            _ = self_peer.lastcmd.rtn

        # Background thread is allowed to wait on self_peer.
        bg_results: list[Any] = []

        def _bg() -> None:
            bg_results.append(self_peer.send("echo hi").out)
            bg_results.append(self_peer.env["A"])
            bg_results.append(self_peer.lastcmd.rtn)
            _ = hold_res.future.result(timeout=2.0)

        t = threading.Thread(target=_bg)
        t.start()
        t.join(timeout=3.0)
        assert not t.is_alive()
        assert bg_results == ["hi\n", "val", 0]

        # Once hold_res is already finished, reading on main thread works.
        assert hold_res.out == "hi\n"
    finally:
        srv.close()


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

    from xontrib_tmux.peer import _advance_active_ticket

    with ctrl.send_cond:
        _advance_active_ticket(ctrl)
    assert t2_done.wait(timeout=1.0)
    th2.join(timeout=1.0)
