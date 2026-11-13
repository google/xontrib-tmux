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

"""Hermetic unit tests for ``parser_patch`` bootstrap and ``peer_server``."""

import ast
import asyncio
import atexit
import builtins
import io
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from typing import Any

import prompt_toolkit.shortcuts
import pytest

from xontrib_tmux import parser_patch, peer_server, protocol


class _FakeEvent(set):
    """Minimal xonsh-like event set supporting decorator and ``.fire()``."""

    def __call__(self, fn: Any) -> Any:
        self.add(fn)
        return fn

    def fire(self, **kwargs: Any) -> None:
        for fn in list(self):
            fn(**kwargs)


def _make_fake_events() -> SimpleNamespace:
    return SimpleNamespace(
        on_precommand=_FakeEvent(),
        on_postcommand=_FakeEvent(),
        on_pre_prompt=_FakeEvent(),
        on_pre_rc=_FakeEvent(),
        on_post_rc=_FakeEvent(),
        on_post_init=_FakeEvent(),
    )


def test_peer_server_has_no_tmux_coupling() -> None:
    src = pathlib.Path(peer_server.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported: set[str] = set()
    str_literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            str_literals.add(node.value)
    assert "libtmux" not in imported
    assert "TMUX" not in str_literals
    assert "TMUX_PANE" not in str_literals


def test_is_prompt_idle_states() -> None:
    assert not peer_server._is_prompt_idle(None, None)

    default_buf = SimpleNamespace(text="", complete_state=None)
    layout = SimpleNamespace(is_searching=False)
    app = SimpleNamespace(
        is_running=True,
        current_buffer=default_buf,
        layout=layout,
    )
    sh = SimpleNamespace(
        prompter=SimpleNamespace(default_buffer=default_buf),
        need_more_lines=False,
    )
    assert peer_server._is_prompt_idle(sh, app)

    app.is_running = False
    assert not peer_server._is_prompt_idle(sh, app)
    app.is_running = True

    default_buf.text = "echo half"
    assert not peer_server._is_prompt_idle(sh, app)
    default_buf.text = ""

    default_buf.complete_state = object()
    assert not peer_server._is_prompt_idle(sh, app)
    default_buf.complete_state = None

    app.current_buffer = SimpleNamespace(text="")
    assert not peer_server._is_prompt_idle(sh, app)
    app.current_buffer = default_buf

    layout.is_searching = True
    assert not peer_server._is_prompt_idle(sh, app)
    layout.is_searching = False

    sh.need_more_lines = True
    assert not peer_server._is_prompt_idle(sh, app)
    sh.need_more_lines = False

    # Synthetic mock with .window.content mismatch.
    default_buf.window = SimpleNamespace(content="default_ctrl")
    layout.current_control = "search_ctrl"
    assert not peer_server._is_prompt_idle(sh, app)
    layout.current_control = "default_ctrl"
    assert peer_server._is_prompt_idle(sh, app)

    app.is_done = True
    assert not peer_server._is_prompt_idle(sh, app)
    app.is_done = False

    app.future = None
    assert not peer_server._is_prompt_idle(sh, app)
    app.future = SimpleNamespace(done=lambda: True)
    assert not peer_server._is_prompt_idle(sh, app)
    app.future = SimpleNamespace(done=lambda: False)
    assert peer_server._is_prompt_idle(sh, app)

    app.loop = None
    assert not peer_server._is_prompt_idle(sh, app)
    app.loop = SimpleNamespace(is_closed=lambda: True)
    assert not peer_server._is_prompt_idle(sh, app)
    app.loop = SimpleNamespace(is_closed=lambda: False)
    assert peer_server._is_prompt_idle(sh, app)


def test_is_prompt_idle_with_real_prompt_session() -> None:
    ps = prompt_toolkit.shortcuts.PromptSession()
    sh = SimpleNamespace(prompter=ps, need_more_lines=False)
    assert not peer_server._is_prompt_idle(sh, ps.app)
    loop = asyncio.new_event_loop()
    try:
        # is_running alone without future/loop must NOT be treated as idle.
        ps.app._is_running = True
        assert not peer_server._is_prompt_idle(sh, ps.app)

        fut = loop.create_future()
        ps.app.loop = loop
        ps.app.future = fut
        assert peer_server._is_prompt_idle(sh, ps.app)

        ps.default_buffer._set_text("partial")
        assert not peer_server._is_prompt_idle(sh, ps.app)
        ps.default_buffer._set_text("")
        ps.app.layout.focus(ps.search_buffer)
        assert not peer_server._is_prompt_idle(sh, ps.app)
        ps.app.layout.focus(ps.default_buffer)
        assert peer_server._is_prompt_idle(sh, ps.app)

        # Ctrl-C / exit sets future (is_done=True) while _is_running stays True.
        fut.set_exception(KeyboardInterrupt())
        assert ps.app.is_running is True
        assert ps.app.is_done is True
        assert not peer_server._is_prompt_idle(sh, ps.app)

        ps.app.future = None
        assert not peer_server._is_prompt_idle(sh, ps.app)

        ps.app.future = loop.create_future()
        loop.close()
        assert not peer_server._is_prompt_idle(sh, ps.app)
    finally:
        ps.app._is_running = False
        ps.app.future = None
        ps.app.loop = None
        if not loop.is_closed():
            loop.close()


def test_submit_when_idle_retries_done_and_closed_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Done/exiting prompts and closed loops must retry rather than drop."""
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_retry_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)
        layout = SimpleNamespace(is_searching=False)
        attempts = {"calls": 0}
        idle_checks = {"count": 0}

        class _FlakyLoop:
            def __init__(self, closed: bool = False) -> None:
                self._closed = closed

            def is_closed(self) -> bool:
                was_closed = self._closed
                if was_closed:
                    self._closed = False
                return was_closed

            def call_soon_threadsafe(self, fn: Any) -> None:
                attempts["calls"] += 1
                if attempts["calls"] == 1:
                    raise RuntimeError("Event loop is closed")
                if attempts["calls"] == 2:
                    # Simulate loop closing before callback runs.
                    self._closed = True
                    return
                if attempts["calls"] == 3:
                    # Simulate app exiting before _submit_on_loop executes.
                    app.hold_done = True
                    app.is_done = True
                    app.future = SimpleNamespace(done=lambda: True)
                threading.Thread(target=fn, daemon=True).start()

        class _ProbeApp:
            def __init__(self) -> None:
                self.is_running = True
                self._is_done = True
                self.hold_done = False
                self.future: Any = SimpleNamespace(done=lambda: True)
                self.current_buffer = default_buf
                self.layout = layout
                self.loop = _FlakyLoop()

            @property
            def is_done(self) -> bool:
                idle_checks["count"] += 1
                if self.hold_done:
                    self.hold_done = False
                    return True
                # Stay done briefly on startup, then become ready.
                if idle_checks["count"] >= 3 and self._is_done:
                    self._is_done = False
                    self.loop._closed = False
                    self.future = SimpleNamespace(done=lambda: False)
                return self._is_done

            @is_done.setter
            def is_done(self, val: bool) -> None:
                self._is_done = val

        app = _ProbeApp()

        def validate_and_handle() -> None:
            if app.is_done:
                raise RuntimeError(
                    "Return value already set. Application.exit() failed."
                )
            submitted = default_buf.text
            default_buf.text = ""
            fake_events.on_precommand.fire(cmd=submitted)
            fake_events.on_postcommand.fire(cmd=submitted, rtn=0)

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, shell=SimpleNamespace(shell=sh)
        )

        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(3.0)
                cli.connect(sock_path)
                with cli.makefile("r", encoding="utf-8") as f:
                    protocol.send_json(
                        cli, {"id": "r1", "op": "run", "block": "echo ok"}
                    )
                    resp = protocol.recv_json(f)
            assert resp is not None
            assert resp["ok"] is True
            assert resp["rtn"] == 0
            assert attempts["calls"] >= 4
        finally:
            srv.stop()


def test_parser_patch_pops_env_and_overrides_autoload_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    sock_path = str(tmp_path / "peer.sock")
    monkeypatch.setattr(parser_patch, "_peer_socket_path", None)
    monkeypatch.setattr(parser_patch, "_is_hidden_peer", False)
    monkeypatch.setattr(parser_patch, "_peer_server_started", False)
    monkeypatch.setattr(parser_patch, "_peer_events_attached", False)
    parser_patch._remove_hook()

    monkeypatch.setenv("XONTRIBS_AUTOLOAD_DISABLED", "tmux")
    monkeypatch.setenv(protocol.PEER_SOCKET_ENV, sock_path)
    monkeypatch.setenv(protocol.HIDDEN_PEER_ENV, "1")
    monkeypatch.delitem(sys.modules, "xonsh.parser_table", raising=False)
    monkeypatch.setattr(parser_patch, "_ensure_startup_atexit", lambda: None)

    try:
        parser_patch.install_parser_patch_hook()
        assert protocol.PEER_SOCKET_ENV not in os.environ
        assert protocol.HIDDEN_PEER_ENV not in os.environ
        assert parser_patch._peer_socket_path == sock_path
        assert parser_patch._is_hidden_peer is True
        assert any(
            isinstance(f, parser_patch._ParserTableImportHook)
            for f in sys.meta_path
        )
    finally:
        parser_patch._remove_hook()
        monkeypatch.setattr(parser_patch, "_peer_socket_path", None)
        monkeypatch.setattr(parser_patch, "_is_hidden_peer", False)
        monkeypatch.setattr(parser_patch, "_peer_events_attached", False)


def test_parser_patch_startup_events_and_atexit_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import xonsh.events
    import xontrib_tmux

    fake_events = _make_fake_events()
    monkeypatch.setattr(xonsh.events, "events", fake_events)

    sock_path = str(tmp_path / "boot.sock")
    monkeypatch.setattr(parser_patch, "_peer_socket_path", sock_path)
    monkeypatch.setattr(parser_patch, "_is_hidden_peer", True)
    monkeypatch.setattr(parser_patch, "_peer_server_started", False)
    monkeypatch.setattr(parser_patch, "_peer_events_attached", False)

    # Startup failure atexit writes <sock>.err when not yet started.
    parser_patch._on_startup_exit()
    err_file = pathlib.Path(f"{sock_path}.err")
    assert err_file.exists()
    assert "exited" in err_file.read_text(encoding="utf-8")
    err_file.unlink()

    # Once marked started, atexit sentinel is disarmed.
    parser_patch.mark_peer_server_started()
    parser_patch._on_startup_exit()
    assert not err_file.exists()

    # Test on_post_rc and on_post_init handlers.
    parser_patch._attach_peer_events()
    fake_xsh = SimpleNamespace(env={}, ctx={})
    monkeypatch.setattr(builtins, "__xonsh__", fake_xsh, raising=False)

    fake_events.on_post_rc.fire()
    assert fake_xsh.env["XONSH_HISTORY_BACKEND"] == "dummy"

    loaded_calls: list[Any] = []
    started_calls: list[tuple[Any, str, bool]] = []
    monkeypatch.setattr(
        xontrib_tmux, "_load_xontrib_", lambda x: loaded_calls.append(x)
    )
    monkeypatch.setattr(
        peer_server,
        "start_peer_server",
        lambda x, p, is_hidden=False: started_calls.append((x, p, is_hidden)),
    )

    fake_events.on_post_init.fire()
    assert loaded_calls == [fake_xsh]
    assert started_calls == [(fake_xsh, sock_path, True)]


def test_normalize_pipeline_and_final_rtn() -> None:
    # Non-hiddenobject pipelines (e.g. !() or $()) are ignored.
    obj_cp = SimpleNamespace(
        captured="object", args=["echo", "x"], returncode=0
    )
    assert peer_server._normalize_pipeline(obj_cp, True) is None
    stdout_cp = SimpleNamespace(
        captured="stdout", args=["echo", "x"], returncode=0
    )
    assert peer_server._normalize_pipeline(stdout_cp, True) is None

    # Threadable command preserves lines via "".join(cp.lines).
    rec_cp = SimpleNamespace(
        captured="hiddenobject",
        args=["echo", "hi"],
        returncode=0,
        spec=SimpleNamespace(
            threadable=True, captured_stdout=object(), captured_stderr=object()
        ),
        lines=["hi\n"],
        errors=None,
    )
    assert peer_server._normalize_pipeline(rec_cp, True) == {
        "args": ["echo", "hi"],
        "rtn": 0,
        "out": "hi\n",
        "err": "",
    }
    no_nl_cp = SimpleNamespace(
        captured="hiddenobject",
        args=["echo", "-n", "hi"],
        returncode=0,
        spec=SimpleNamespace(
            threadable=True, captured_stdout=object(), captured_stderr=None
        ),
        lines=["hi"],
        errors="warn\n",
    )
    assert peer_server._normalize_pipeline(no_nl_cp, True) == {
        "args": ["echo", "-n", "hi"],
        "rtn": 0,
        "out": "hi",
        "err": "warn\n",
    }

    # record=False or threadable=False -> out=None, err=None.
    assert peer_server._normalize_pipeline(rec_cp, False) == {
        "args": ["echo", "hi"],
        "rtn": 0,
        "out": None,
        "err": None,
    }
    unthread_cp = SimpleNamespace(
        captured="hiddenobject",
        args=["vim"],
        returncode=0,
        spec=SimpleNamespace(
            threadable=False, captured_stdout=None, captured_stderr=None
        ),
        lines=[],
        errors=None,
    )
    assert peer_server._normalize_pipeline(unthread_cp, True) == {
        "args": ["vim"],
        "rtn": 0,
        "out": None,
        "err": None,
    }

    # Final rtn calculation.
    assert peer_server._compute_final_rtn(0, [{"rtn": 0}], None) == 0
    assert (
        peer_server._compute_final_rtn(
            2, [{"rtn": 0}, {"rtn": 2}], subprocess.CalledProcessError
        )
        == 2
    )
    assert peer_server._compute_final_rtn(0, [{"rtn": 0}], ValueError) == 1
    assert (
        peer_server._compute_final_rtn(None, [], KeyboardInterrupt) == 130
    )
    assert peer_server._compute_final_rtn(-2, [], None) == 130
    assert peer_server._compute_final_rtn(0, [{"rtn": 3}], None) == 3


def test_start_peer_server_non_ptk_writes_err_and_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_noptk_") as td:
        sock_path = os.path.join(td, "p.sock")
        fake_xsh = SimpleNamespace(
            shell=SimpleNamespace(shell=SimpleNamespace())
        )
        monkeypatch.setattr(parser_patch, "_peer_server_started", False)

        peer_server.start_peer_server(fake_xsh, sock_path)
        try:
            assert parser_patch._peer_server_started is True
            err_text = pathlib.Path(f"{sock_path}.err").read_text(
                encoding="utf-8"
            )
            assert "prompt_toolkit" in err_text
            assert stat.S_IMODE(os.stat(sock_path).st_mode) == 0o600

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(2.0)
                cli.connect(sock_path)
                protocol.send_json(
                    cli, {"id": "1", "op": "run", "block": "echo 1"}
                )
                with cli.makefile("r", encoding="utf-8") as f:
                    resp = protocol.recv_json(f)
            assert resp == {
                "version": 1,
                "id": "1",
                "ok": False,
                "error_type": "RuntimeError",
                "error": "Peer xonsh is not running the prompt_toolkit shell",
            }
        finally:
            if peer_server._last_server is not None:
                peer_server._last_server.stop()


def test_start_peer_server_run_errors_and_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_ptk_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)
        layout = SimpleNamespace(is_searching=False)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=layout,
            loop=_FakeLoop(),
        )

        env: dict[str, Any] = {"XONSH_CAPTURE_ALWAYS": False}

        def validate_and_handle() -> None:
            submitted = default_buf.text
            default_buf.text = ""
            fake_events.on_precommand.fire(cmd=submitted)
            srv = peer_server._active_server
            assert srv is not None
            if "raise" in submitted:
                srv._record_exc_info((ValueError, ValueError("boom"), None))
                rtn = 0
            else:
                cp = SimpleNamespace(
                    captured="hiddenobject",
                    args=["echo", "ok"],
                    returncode=0,
                    spec=SimpleNamespace(
                        threadable=True,
                        captured_stdout=object(),
                        captured_stderr=object(),
                    ),
                    lines=["ok\n"],
                    errors=None,
                )
                srv._record_pipeline(cp)
                rtn = 0
            fake_events.on_postcommand.fire(cmd=submitted, rtn=rtn)

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env=env, shell=SimpleNamespace(shell=sh)
        )

        peer_server.start_peer_server(fake_xsh, sock_path, is_hidden=True)
        srv = peer_server._active_server
        assert srv is not None
        try:
            assert peer_server.is_hidden_peer() is True

            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(3.0)
                cli.connect(sock_path)
                with cli.makefile("r", encoding="utf-8") as f:
                    # 1. Recorded run toggles and restores XONSH_CAPTURE_ALWAYS.
                    protocol.send_json(
                        cli,
                        {
                            "id": "r1",
                            "op": "run",
                            "block": "echo ok",
                            "record": True,
                        },
                    )
                    r1 = protocol.recv_json(f)
                    assert r1 == {
                        "version": 1,
                        "id": "r1",
                        "ok": True,
                        "rtn": 0,
                        "out": "ok\n",
                        "err": "",
                        "commands": [
                            {
                                "args": ["echo", "ok"],
                                "rtn": 0,
                                "out": "ok\n",
                                "err": "",
                            }
                        ],
                    }
                    assert env["XONSH_CAPTURE_ALWAYS"] is False

                    # 3. Unrecorded run leaves out/err as None.
                    protocol.send_json(
                        cli,
                        {
                            "id": "r2",
                            "op": "run",
                            "block": "echo ok",
                            "record": False,
                        },
                    )
                    r2 = protocol.recv_json(f)
                    assert r2 is not None
                    assert r2["out"] is None
                    assert r2["err"] is None
                    assert r2["commands"][0]["out"] is None
                    assert r2["commands"][0]["err"] is None

                    # 4. Uncaught exception forces rtn=1.
                    protocol.send_json(
                        cli,
                        {
                            "id": "r3",
                            "op": "run",
                            "block": "raise ValueError('boom')",
                            "record": True,
                        },
                    )
                    r3 = protocol.recv_json(f)
                    assert r3 is not None
                    assert r3["ok"] is True
                    assert r3["rtn"] == 1

                    # 5. SyntaxError rejected cleanly.
                    protocol.send_json(
                        cli,
                        {"id": "r4", "op": "run", "block": "if True:\n"},
                    )
                    r4 = protocol.recv_json(f)
                    assert r4 is not None
                    assert r4["ok"] is False
                    assert r4["error_type"] == "SyntaxError"

                    # 5. Unknown op rejected (including removed ping op).
                    protocol.send_json(cli, {"id": "u1", "op": "ping"})
                    u1 = protocol.recv_json(f)
                    assert u1 is not None
                    assert u1["ok"] is False
                    assert u1["error_type"] == "RuntimeError"
        finally:
            srv.stop()


def test_global_hooks_and_pre_prompt_safety_net(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from xonsh.procs.pipelines import CommandPipeline
    import xonsh.shells.base_shell as base_shell

    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_hooks_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)
        layout = SimpleNamespace(is_searching=False)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=layout,
            loop=_FakeLoop(),
        )

        def validate_and_handle() -> None:
            # Simulate a failure to compile where on_pre_prompt fires without
            # on_postcommand ever running.
            default_buf.text = ""
            fake_events.on_pre_prompt.fire()

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, shell=SimpleNamespace(shell=sh)
        )

        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            # Verify global hooks are installed and forward to _active_server.
            cp = SimpleNamespace(
                captured="hiddenobject",
                args=["echo", "z"],
                returncode=0,
                spec=SimpleNamespace(
                    threadable=True,
                    captured_stdout=object(),
                    captured_stderr=object(),
                ),
                lines=["z\n"],
                errors=None,
                proc=None,
            )
            fake_entry = peer_server._PeerEntry(
                req_id="x",
                record=True,
                conn=SimpleNamespace(),  # type: ignore[arg-type]
                send_lock=threading.Lock(),
            )
            with srv._entry_lock:
                srv._active_entry = fake_entry
            CommandPipeline._apply_to_history(cp)
            assert fake_entry.records == [
                {"args": ["echo", "z"], "rtn": 0, "out": "z\n", "err": ""}
            ]
            exc_info = base_shell.run_compiled_code(
                compile("1 / 0", "<t>", "eval"), {}, {}, "eval"
            )
            assert exc_info[0] is ZeroDivisionError
            assert fake_entry.exc_type is ZeroDivisionError
            with srv._entry_lock:
                srv._active_entry = None

            # Verify on_pre_prompt recovers if on_postcommand never ran.
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(3.0)
                cli.connect(sock_path)
                with cli.makefile("r", encoding="utf-8") as f:
                    protocol.send_json(
                        cli, {"id": "f1", "op": "run", "block": "echo hi"}
                    )
                    resp = protocol.recv_json(f)
                    assert resp is not None
                    assert resp["ok"] is False
                    assert resp["error_type"] == "RuntimeError"
        finally:
            srv.stop()


def test_accept_loop_rejects_bad_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(
        peer_server,
        "verify_peer_uid",
        lambda conn: (_ for _ in ()).throw(PermissionError("bad uid")),
    )

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_uid_") as td:
        sock_path = os.path.join(td, "p.sock")
        fake_xsh = SimpleNamespace(
            shell=SimpleNamespace(shell=SimpleNamespace())
        )
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._last_server
        assert srv is not None
        try:
            assert peer_server._active_server is None
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(2.0)
                cli.connect(sock_path)
                with cli.makefile("r", encoding="utf-8") as f:
                    assert protocol.recv_json(f) is None
        finally:
            srv.stop()


def test_normalize_non_string_args_and_postcommand_send_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    non_str_cp = SimpleNamespace(
        captured="hiddenobject",
        args=["echo", int, 42],
        returncode=0,
        spec=SimpleNamespace(
            threadable=True, captured_stdout=object(), captured_stderr=None
        ),
        lines=["ok\n"],
        errors=None,
    )
    rec = peer_server._normalize_pipeline(non_str_cp, True)
    assert rec == {
        "args": ["echo", str(int), "42"],
        "rtn": 0,
        "out": "ok\n",
        "err": "",
    }
    a_sock, b_sock = socket.socketpair()
    try:
        protocol.send_json(a_sock, {"commands": [rec]})
        with b_sock.makefile("r", encoding="utf-8") as f:
            assert protocol.recv_json(f)["commands"] == [rec]
    finally:
        a_sock.close()
        b_sock.close()

    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_senderr_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)
        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=SimpleNamespace(call_soon_threadsafe=lambda fn: None),
        )
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, shell=SimpleNamespace(shell=sh)
        )
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            class _BrokenConn:
                def sendall(self, data: bytes) -> None:
                    raise OSError("broken pipe")

            done_ev = threading.Event()
            entry = peer_server._PeerEntry(
                req_id="broken",
                record=True,
                conn=_BrokenConn(),  # type: ignore[arg-type]
                send_lock=threading.Lock(),
                done_event=done_ev,
            )
            srv._entry_done.clear()
            with srv._entry_lock:
                srv._active_entry = entry
            fake_events.on_postcommand.fire(cmd="echo", rtn=0)
            assert done_ev.is_set()
            assert srv._entry_done.is_set()
        finally:
            srv.stop()


def test_rc_system_exit_writes_err_and_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import xonsh.environ
    import xonsh.events

    fake_events = _make_fake_events()
    monkeypatch.setattr(xonsh.events, "events", fake_events)
    monkeypatch.setattr(parser_patch, "_peer_events_attached", False)
    monkeypatch.setattr(parser_patch, "_orig_rc_runner", None)
    sock_path = str(tmp_path / "rc_exit.sock")
    monkeypatch.setattr(parser_patch, "_peer_socket_path", sock_path)
    monkeypatch.setattr(parser_patch, "_is_hidden_peer", True)

    def _fake_runner(*args: Any, **kwargs: Any) -> Any:
        return (SystemExit, SystemExit(1), None)

    monkeypatch.setattr(xonsh.environ, "run_script_with_cache", _fake_runner)
    parser_patch._attach_peer_events()
    fake_xsh = SimpleNamespace(env={}, ctx={})
    monkeypatch.setattr(builtins, "__xonsh__", fake_xsh, raising=False)

    fake_events.on_pre_rc.fire()
    with pytest.raises(SystemExit) as exc_info:
        xonsh.environ.run_script_with_cache("rc.xsh", fake_xsh)
    assert exc_info.value.code == 1
    err_text = pathlib.Path(f"{sock_path}.err").read_text(encoding="utf-8")
    assert "Peer exited during rc" in err_text

    fake_events.on_post_rc.fire()
    assert xonsh.environ.run_script_with_cache is _fake_runner


def test_hidden_peer_env_term_normalization_and_early_atexit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    import xonsh.events

    fake_events = _make_fake_events()
    monkeypatch.setattr(xonsh.events, "events", fake_events)
    sock_path = str(tmp_path / "env.sock")
    monkeypatch.setenv(parser_patch.PEER_SOCKET_ENV, sock_path)
    monkeypatch.setenv(parser_patch.HIDDEN_PEER_ENV, "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setattr(parser_patch, "_peer_socket_path", None)
    monkeypatch.setattr(parser_patch, "_is_hidden_peer", False)
    monkeypatch.setattr(parser_patch, "_peer_events_attached", False)
    monkeypatch.setattr(parser_patch, "_orig_rc_runner", None)
    monkeypatch.setattr(parser_patch, "_peer_server_started", False)
    monkeypatch.setattr(
        parser_patch, "_startup_atexit_registered", False
    )
    registered: list[Any] = []
    monkeypatch.setattr(atexit, "register", lambda fn: registered.append(fn))

    parser_patch._capture_peer_env()
    assert registered == [parser_patch._on_startup_exit]
    assert os.environ["TERM"] == "xterm-256color"
    assert peer_server.is_hidden_peer() is True

    parser_patch._attach_peer_events()
    fake_xsh = SimpleNamespace(
        env={"TERM": "dumb", "SHELL_TYPE": "readline"}, ctx={}
    )
    monkeypatch.setattr(builtins, "__xonsh__", fake_xsh, raising=False)
    fake_events.on_post_rc.fire()
    assert fake_xsh.env["TERM"] == "xterm-256color"
    assert fake_xsh.env["SHELL_TYPE"] == "readline"
    assert fake_xsh.env["XONSH_HISTORY_BACKEND"] == "dummy"


def test_client_eof_after_run_still_receives_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_eof_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=_FakeLoop(),
        )

        def validate_and_handle() -> None:
            def _finish() -> None:
                time.sleep(0.05)
                default_buf.text = ""
                fake_events.on_postcommand.fire(cmd="echo eof", rtn=0)

            threading.Thread(target=_finish, daemon=True).start()

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, shell=SimpleNamespace(shell=sh)
        )
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(3.0)
                cli.connect(sock_path)
                protocol.send_json(
                    cli, {"id": "eof1", "op": "run", "block": "echo eof"}
                )
                cli.shutdown(socket.SHUT_WR)
                with cli.makefile("r", encoding="utf-8") as f:
                    resp = protocol.recv_json(f)
            assert resp == {
                "version": 1,
                "id": "eof1",
                "ok": True,
                "rtn": 0,
                "out": "",
                "err": "",
                "commands": [],
            }
        finally:
            srv.stop()


def test_postcommand_reports_peer_exited_on_xsh_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_exit_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=_FakeLoop(),
        )
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, exit=None, shell=SimpleNamespace(shell=sh)
        )

        def validate_and_handle() -> None:
            default_buf.text = ""
            fake_xsh.exit = 0
            fake_events.on_postcommand.fire(cmd="exit", rtn=0)

        default_buf.validate_and_handle = validate_and_handle
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.settimeout(3.0)
                cli.connect(sock_path)
                protocol.send_json(
                    cli, {"id": "ex1", "op": "run", "block": "exit"}
                )
                with cli.makefile("r", encoding="utf-8") as f:
                    resp = protocol.recv_json(f)
            assert resp == {
                "version": 1,
                "id": "ex1",
                "ok": False,
                "error_type": "RuntimeError",
                "error": "Peer exited",
            }
        finally:
            srv.stop()


def test_peer_std_tee_capture_passthrough_and_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_PeerStdTee captures text/bytes, passes through, and unhooks cleanly."""

    class _FakeOrig(io.TextIOWrapper):
        pass

    orig_out_raw = io.BytesIO()
    orig_out = _FakeOrig(
        orig_out_raw,
        encoding="utf-8",
        errors="replace",
        write_through=True,
    )
    orig_err_raw = io.BytesIO()
    orig_err = _FakeOrig(
        orig_err_raw,
        encoding="utf-8",
        errors="strict",
        write_through=True,
    )
    monkeypatch.setattr(orig_out, "isatty", lambda: True)
    monkeypatch.setattr(orig_out, "fileno", lambda: 11)
    monkeypatch.setattr(sys, "stdout", orig_out)
    monkeypatch.setattr(sys, "stderr", orig_err)

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    tee_out = peer_server._PeerStdTee("stdout", out_chunks)
    tee_err = peer_server._PeerStdTee("stderr", err_chunks)
    sys.stdout = tee_out
    sys.stderr = tee_err

    assert tee_out.encoding == "utf-8"
    assert tee_out.errors == "replace"
    assert tee_out.isatty() is True
    assert tee_out.fileno() == 11

    assert tee_out.write("hello ") == 6
    assert tee_out.buffer.write(b"world\n") == 6
    assert tee_out.buffer.write(bytearray(b"arr")) == 3
    assert tee_out.buffer.write(memoryview(b"-mv\n")) == 4
    assert tee_err.write("oops\n") == 5
    tee_out.flush()
    tee_err.flush()

    assert "".join(out_chunks) == "hello world\narr-mv\n"
    assert orig_out_raw.getvalue() == b"hello world\narr-mv\n"
    assert "".join(err_chunks) == "oops\n"
    assert orig_err_raw.getvalue() == b"oops\n"

    # Suppressed writes pass through to original stream without capturing.
    peer_server._stream_local.depth = 1
    try:
        assert tee_out.write("subproc\n") == 8
        assert tee_out.buffer.write(b"subbuf\n") == 7
        assert tee_err.write("suberr\n") == 7
    finally:
        peer_server._stream_local.depth = 0

    assert "".join(out_chunks) == "hello world\narr-mv\n"
    assert orig_out_raw.getvalue().endswith(b"subproc\nsubbuf\n")
    assert "".join(err_chunks) == "oops\n"
    assert orig_err_raw.getvalue().endswith(b"suberr\n")

    # Direct close() restores original streams.
    tee_out.close()
    tee_err.close()
    assert sys.stdout is orig_out
    assert sys.stderr is orig_err


def test_peer_std_tee_close_unhooks_nested_xonsh_tee(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing _PeerStdTee inside a nested xonsh Tee unhooks both levels."""
    from xonsh.shells.base_shell import Tee

    orig_out = io.TextIOWrapper(
        io.BytesIO(),
        encoding="utf-8",
        write_through=True,
    )
    orig_err = io.TextIOWrapper(
        io.BytesIO(),
        encoding="utf-8",
        write_through=True,
    )
    monkeypatch.setattr(sys, "stdout", orig_out)
    monkeypatch.setattr(sys, "stderr", orig_err)

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    tee_out = peer_server._PeerStdTee("stdout", out_chunks)
    tee_err = peer_server._PeerStdTee("stderr", err_chunks)
    sys.stdout = tee_out
    sys.stderr = tee_err

    outer = Tee()
    assert sys.stdout is not orig_out
    assert sys.stdout.std is tee_out
    assert sys.stdout.buffer.stdbuf is tee_out.buffer

    tee_out.close()
    tee_err.close()
    assert sys.stdout.std is orig_out
    assert sys.stderr.std is orig_err

    outer.close()
    assert sys.stdout is orig_out
    assert sys.stderr is orig_err


def test_global_hooks_suppress_tee_during_subproc_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global hooks suppress Python tee during subproc streaming only."""
    from xonsh.procs.pipelines import CommandPipeline
    from xonsh.procs.proxies import ProcProxy

    class _FakeOrig(io.TextIOWrapper):
        pass

    orig_out_raw = io.BytesIO()
    orig_err_raw = io.BytesIO()
    orig_out = _FakeOrig(orig_out_raw, encoding="utf-8", write_through=True)
    orig_err = _FakeOrig(orig_err_raw, encoding="utf-8", write_through=True)
    monkeypatch.setattr(sys, "stdout", orig_out)
    monkeypatch.setattr(sys, "stderr", orig_err)

    peer_server._install_global_hooks()

    out_chunks: list[str] = []
    err_chunks: list[str] = []
    tee_out = peer_server._PeerStdTee("stdout", out_chunks)
    tee_err = peer_server._PeerStdTee("stderr", err_chunks)
    sys.stdout = tee_out
    sys.stderr = tee_err
    try:
        captured_streams = SimpleNamespace(
            spec=SimpleNamespace(
                captured_stdout=object(), captured_stderr=object()
            )
        )
        uncaptured_proxy = SimpleNamespace(
            spec=SimpleNamespace(captured_stdout=None, captured_stderr=None)
        )

        # Uncaptured ProcProxy.wait suppresses tee writes during execution.
        monkeypatch.setattr(
            peer_server,
            "_orig_proc_proxy_wait",
            lambda px, *a, **k: sys.stdout.write("fg\n"),
        )
        ProcProxy.wait(uncaptured_proxy)
        assert "".join(out_chunks) == ""
        assert orig_out_raw.getvalue() == b"fg\n"

        # Captured ProcProxy.wait does NOT suppress writes.
        ProcProxy.wait(captured_streams)
        assert "".join(out_chunks) == "fg\n"

        # tee_stdout and stream_stderr suppress Python tee during streaming.
        def fake_tee_stdout(cp: Any, *a: Any, **k: Any) -> Any:
            sys.stdout.write("echoed\n")
            yield "chunk"

        monkeypatch.setattr(peer_server, "_orig_tee_stdout", fake_tee_stdout)
        list(CommandPipeline.tee_stdout(captured_streams))
        assert "".join(out_chunks) == "fg\n"
        assert orig_out_raw.getvalue().endswith(b"echoed\n")

        monkeypatch.setattr(
            peer_server,
            "_orig_stream_stderr",
            lambda cp, *a, **k: sys.stderr.write("err-echo\n"),
        )
        CommandPipeline.stream_stderr(captured_streams)
        assert "".join(err_chunks) == ""
        assert orig_err_raw.getvalue() == b"err-echo\n"
    finally:
        tee_out.close()
        tee_err.close()


def test_peer_server_run_interleaves_subproc_and_python_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run records interleaved subproc + Python stdout/stderr."""
    orig_out_raw = io.BytesIO()
    orig_err_raw = io.BytesIO()
    orig_out = io.TextIOWrapper(
        orig_out_raw, encoding="utf-8", write_through=True
    )
    orig_err = io.TextIOWrapper(
        orig_err_raw, encoding="utf-8", write_through=True
    )
    monkeypatch.setattr(sys, "stdout", orig_out)
    monkeypatch.setattr(sys, "stderr", orig_err)

    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_iv_") as tmpdir:
        sock_path = os.path.join(tmpdir, "peer.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=_FakeLoop(),
        )

        def _make_cp(args: list[str], out: str, err: str) -> SimpleNamespace:
            return SimpleNamespace(
                captured="hiddenobject",
                args=args,
                returncode=0,
                spec=SimpleNamespace(
                    threadable=True,
                    captured_stdout=object(),
                    captured_stderr=object(),
                ),
                lines=[out] if out else [],
                errors=err or None,
            )

        def validate_and_handle() -> None:
            text = default_buf.text
            default_buf.text = ""
            fake_events.on_precommand.fire(cmd=text)
            srv = peer_server._active_server
            assert srv is not None
            if "INTERLEAVE" in text:
                srv._record_pipeline(_make_cp(["echo", "a"], "a\n", ""))
                sys.stdout.write("b\n")
                sys.stderr.write("e1\n")
                srv._record_pipeline(_make_cp(["echo", "c"], "c\n", "e2\n"))
            elif "NOREC" in text:
                sys.stdout.write("hidden-out\n")
                sys.stderr.write("hidden-err\n")
            else:
                sys.stdout.write("hand-out\n")
            fake_events.on_postcommand.fire(cmd=text, rtn=0)

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={"XONSH_CAPTURE_ALWAYS": False},
            ctx={},
            shell=SimpleNamespace(shell=sh),
        )

        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                cli.connect(sock_path)
                cli.settimeout(5.0)
                with cli.makefile("r", encoding="utf-8") as f:
                    # Hand-typed command (no active entry): no tee installed.
                    default_buf.text = "hand_typed()"
                    default_buf.validate_and_handle()
                    assert sys.stdout is orig_out
                    assert sys.stderr is orig_err

                    # record=True interleaves subproc + Python stdout/stderr.
                    protocol.send_json(
                        cli,
                        {
                            "id": "r1",
                            "op": "run",
                            "block": "INTERLEAVE",
                            "record": True,
                        },
                    )
                    rep1 = protocol.recv_json(f)
                    assert rep1 is not None
                    assert rep1["ok"] is True
                    assert rep1["out"] == "a\nb\nc\n"
                    assert rep1["err"] == "e1\ne2\n"
                    assert [c["args"] for c in rep1["commands"]] == [
                        ["echo", "a"],
                        ["echo", "c"],
                    ]
                    assert rep1["commands"][0]["out"] == "a\n"
                    assert rep1["commands"][1]["err"] == "e2\n"
                    assert sys.stdout is orig_out
                    assert sys.stderr is orig_err

                    # record=False omits Python tees and returns None.
                    protocol.send_json(
                        cli,
                        {
                            "id": "r2",
                            "op": "run",
                            "block": "NOREC",
                            "record": False,
                        },
                    )
                    rep2 = protocol.recv_json(f)
                    assert rep2 is not None
                    assert rep2["ok"] is True
                    assert rep2["out"] is None
                    assert rep2["err"] is None
                    assert rep2["commands"] == []
                    assert sys.stdout is orig_out
                    assert sys.stderr is orig_err
        finally:
            srv.stop()


def test_sequential_connections_preserve_arrival_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separate-socket run ops preserve arrival order without overtake."""
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_ord_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=_FakeLoop(),
        )
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, ctx={}, shell=SimpleNamespace(shell=sh)
        )
        seen: list[str] = []

        def validate_and_handle() -> None:
            text = default_buf.text
            default_buf.text = ""
            fake_events.on_precommand.fire(cmd=text)
            seen.append(text)
            fake_events.on_postcommand.fire(cmd=text, rtn=0)

        default_buf.validate_and_handle = validate_and_handle
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            expected: list[str] = []
            for i in range(50):
                b1 = f"echo a{i}"
                b2 = f"echo b{i}"
                expected.extend([b1, b2])
                with (
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c1,
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c2,
                ):
                    c1.settimeout(3.0)
                    c2.settimeout(3.0)
                    c1.connect(sock_path)
                    protocol.send_json(
                        c1, {"id": f"a-{i}", "op": "run", "block": b1}
                    )
                    c2.connect(sock_path)
                    protocol.send_json(
                        c2, {"id": f"b-{i}", "op": "run", "block": b2}
                    )
                    with (
                        c1.makefile("r", encoding="utf-8") as f1,
                        c2.makefile("r", encoding="utf-8") as f2,
                    ):
                        r1 = protocol.recv_json(f1)
                        r2 = protocol.recv_json(f2)
                        assert r1 is not None and r1["ok"] is True
                        assert r2 is not None and r2["ok"] is True
            assert seen == expected
        finally:
            srv.stop()


def test_partial_slow_connection_does_not_block_fast_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An incomplete slow first line must not stall other connecting clients."""
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_slow_") as td:
        sock_path = os.path.join(td, "p.sock")
        fake_buf = SimpleNamespace(text="", complete_state=None)
        fake_app = SimpleNamespace(
            is_running=True,
            current_buffer=fake_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=SimpleNamespace(),
        )
        fake_sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=fake_buf, app=fake_app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={},
            ctx={},
            shell=SimpleNamespace(shell=fake_sh),
        )
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c_slow,
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c_fast,
            ):
                c_slow.settimeout(2.0)
                c_fast.settimeout(2.0)
                c_slow.connect(sock_path)
                c_slow.sendall(b'{"version":1,"id":"slow"')

                c_fast.connect(sock_path)
                protocol.send_json(
                    c_fast, {"id": "fast", "op": "run", "block": "if True:\n"}
                )
                with c_fast.makefile("r", encoding="utf-8") as f_fast:
                    rf = protocol.recv_json(f_fast)
                    assert rf is not None
                    assert rf["id"] == "fast"
                    assert rf["ok"] is False
                    assert rf["error_type"] == "SyntaxError"

                c_slow.sendall(b',"op":"run","block":"if True:\\n"}\n')
                with c_slow.makefile("r", encoding="utf-8") as f_slow:
                    rs = protocol.recv_json(f_slow)
                    assert rs is not None
                    assert rs["id"] == "slow"
                    assert rs["ok"] is False
                    assert rs["error_type"] == "SyntaxError"
        finally:
            srv.stop()


def test_cancel_queued_pending_prearrival_and_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_events = _make_fake_events()
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    monkeypatch.setattr(peer_server, "_install_global_hooks", lambda: None)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_cancel_") as td:
        sock_path = os.path.join(td, "p.sock")
        default_buf = SimpleNamespace(text="", complete_state=None)
        started_slow = threading.Event()
        release_slow = threading.Event()
        executed: list[str] = []

        class _FakeLoop:
            def call_soon_threadsafe(self, fn: Any) -> None:
                threading.Thread(target=fn, daemon=True).start()

        app = SimpleNamespace(
            is_running=True,
            current_buffer=default_buf,
            layout=SimpleNamespace(is_searching=False),
            loop=_FakeLoop(),
        )

        def validate_and_handle() -> None:
            text = default_buf.text
            executed.append(text)
            default_buf.text = ""
            if text == "slow":

                def _finish_slow() -> None:
                    started_slow.set()
                    release_slow.wait(timeout=3.0)
                    fake_events.on_precommand.fire(cmd=text)
                    fake_events.on_postcommand.fire(cmd=text, rtn=0)

                threading.Thread(target=_finish_slow, daemon=True).start()
            else:
                fake_events.on_precommand.fire(cmd=text)
                fake_events.on_postcommand.fire(cmd=text, rtn=0)

        default_buf.validate_and_handle = validate_and_handle
        sh = SimpleNamespace(
            prompter=SimpleNamespace(default_buffer=default_buf, app=app),
            need_more_lines=False,
        )
        fake_xsh = SimpleNamespace(
            env={}, ctx={}, shell=SimpleNamespace(shell=sh)
        )
        peer_server.start_peer_server(fake_xsh, sock_path)
        srv = peer_server._active_server
        assert srv is not None
        try:
            with (
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c_run,
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c_ctl,
            ):
                c_run.settimeout(3.0)
                c_ctl.settimeout(3.0)
                c_run.connect(sock_path)
                c_ctl.connect(sock_path)
                with (
                    c_run.makefile("r", encoding="utf-8") as f_run,
                    c_ctl.makefile("r", encoding="utf-8") as f_ctl,
                ):
                    # 1. Pre-arrival cancel registers _cancelled_ids.
                    protocol.send_json(
                        c_ctl,
                        {"id": "c0", "op": "cancel", "target_id": "pre1"},
                    )
                    assert protocol.recv_json(f_ctl) == {
                        "version": 1,
                        "id": "c0",
                        "ok": True,
                        "cancelled": True,
                    }
                    protocol.send_json(
                        c_run, {"id": "pre1", "op": "run", "block": "x = 1"}
                    )
                    assert protocol.recv_json(f_run) == {
                        "version": 1,
                        "id": "pre1",
                        "ok": False,
                        "cancelled": True,
                    }

                    # 2. Running block cannot be cancelled; queued block can.
                    protocol.send_json(
                        c_run, {"id": "b1", "op": "run", "block": "slow"}
                    )
                    assert started_slow.wait(timeout=2.0)
                    protocol.send_json(
                        c_run, {"id": "b2", "op": "run", "block": "skip_me"}
                    )
                    # Wait briefly so b2 sits in srv._queue behind b1.
                    deadline = time.monotonic() + 1.0
                    while len(srv._queue) < 1 and time.monotonic() < deadline:
                        time.sleep(0.01)

                    protocol.send_json(
                        c_ctl, {"id": "c1", "op": "cancel", "target_id": "b1"}
                    )
                    assert protocol.recv_json(f_ctl) == {
                        "version": 1,
                        "id": "c1",
                        "ok": True,
                        "cancelled": False,
                    }
                    protocol.send_json(
                        c_ctl, {"id": "c2", "op": "cancel", "target_id": "b2"}
                    )
                    assert protocol.recv_json(f_ctl) == {
                        "version": 1,
                        "id": "c2",
                        "ok": True,
                        "cancelled": True,
                    }

                    release_slow.set()
                    r_b1 = protocol.recv_json(f_run)
                    r_b2 = protocol.recv_json(f_run)
                    assert {r_b1["id"], r_b2["id"]} == {"b1", "b2"}
                    by_id = {r_b1["id"]: r_b1, r_b2["id"]: r_b2}
                    assert by_id["b1"]["ok"] is True
                    assert by_id["b2"] == {
                        "version": 1,
                        "id": "b2",
                        "ok": False,
                        "cancelled": True,
                    }

                    # 3. Cancel while popped & waiting on half-typed prompt.
                    default_buf.text = "half = "
                    protocol.send_json(
                        c_run,
                        {"id": "b3", "op": "run", "block": "never_runs"},
                    )
                    deadline = time.monotonic() + 1.0
                    while srv._pending_run is None:
                        if time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
                    protocol.send_json(
                        c_ctl, {"id": "c3", "op": "cancel", "target_id": "b3"}
                    )
                    assert protocol.recv_json(f_ctl) == {
                        "version": 1,
                        "id": "c3",
                        "ok": True,
                        "cancelled": True,
                    }
                    assert protocol.recv_json(f_run) == {
                        "version": 1,
                        "id": "b3",
                        "ok": False,
                        "cancelled": True,
                    }
                    default_buf.text = ""
                    srv._wakeup_event.set()
                    time.sleep(0.05)

                    # Invalid target_id type returns cancelled=False.
                    protocol.send_json(
                        c_ctl, {"id": "c4", "op": "cancel", "target_id": 123}
                    )
                    assert protocol.recv_json(f_ctl) == {
                        "version": 1,
                        "id": "c4",
                        "ok": True,
                        "cancelled": False,
                    }
            assert executed == ["slow"]
        finally:
            release_slow.set()
            srv.stop()
