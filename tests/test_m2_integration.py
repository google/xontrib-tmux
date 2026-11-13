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

"""Milestone 2 end-to-end integration tests with real xonsh peers."""

from collections.abc import Iterator
from concurrent.futures import CancelledError
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest
from xonsh.completers.python import attr_complete

from xontrib_tmux import RemoteXonsh


def _isolated_subprocess_env(tmp_path: pathlib.Path) -> dict[str, str]:
    """Return an isolated copy of ``os.environ`` safe for real xonsh peers."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    home = tmp_path / "home"
    cfg = tmp_path / "xdg_config"
    data = tmp_path / "xdg_data"
    state = tmp_path / "xdg_state"
    syscfg = tmp_path / "sys_config"
    rcdir = tmp_path / "rc_dir"
    for d in (home, cfg, data, state, syscfg, rcdir):
        d.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(cfg)
    env["XDG_DATA_HOME"] = str(data)
    env["XDG_STATE_HOME"] = str(state)
    env["XONSH_CONFIG_DIR"] = str(cfg / "xonsh")
    env["XONSH_DATA_DIR"] = str(data / "xonsh")
    env["XONSH_SYS_CONFIG_DIR"] = str(syscfg)
    env["XONSHRC"] = str(tmp_path / "empty_rc.xsh")
    env["XONSHRC_DIR"] = str(rcdir)
    return env


@pytest.fixture
def isolated_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """Isolate ``os.environ`` from owner tmux, rc files, and history."""
    env = _isolated_subprocess_env(tmp_path)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XONSH_CONFIG_DIR",
        "XONSH_DATA_DIR",
        "XONSH_SYS_CONFIG_DIR",
        "XONSHRC",
        "XONSHRC_DIR",
    ):
        monkeypatch.setenv(key, env[key])
    return tmp_path


@pytest.fixture
def spawn_peer(isolated_env: pathlib.Path) -> Iterator[Any]:
    """Factory that spawns ``RemoteXonsh`` instances and always cleans up."""
    del isolated_env
    workers: list[RemoteXonsh] = []

    def _spawn() -> RemoteXonsh:
        worker = RemoteXonsh()
        workers.append(worker)
        return worker

    try:
        yield _spawn
    finally:
        for worker in reversed(workers):
            sock = worker._socket_path
            err_file = pathlib.Path(f"{sock}.err")
            try:
                worker.kill()
            finally:
                sock.unlink(missing_ok=True)
                err_file.unlink(missing_ok=True)


def test_env_ctx_lastcmd_copy_semantics_and_completion(
    xonsh_execer: Any, spawn_peer: Any
) -> None:
    del xonsh_execer
    worker = spawn_peer()

    # Before any command runs, all four lastcmd attributes are None.
    assert worker.lastcmd.args is None
    assert worker.lastcmd.rtn is None
    assert worker.lastcmd.out is None
    assert worker.lastcmd.err is None

    worker.env["CC"] = "clang"
    worker.ctx["target"] = "release"
    worker.ctx["items"] = [1, 2]
    assert "CC" in worker.env
    assert "target" in worker.ctx
    assert len(worker.env) >= 1
    assert "CC" in list(worker.env)
    assert worker.env.get("NO_SUCH_VAR_M2", "fallback") == "fallback"

    # Copy semantics: mutating fetched list does not mutate remote peer.
    path_copy = worker.env["PATH"]
    path_copy.append("/bogus_m2_path")
    assert "/bogus_m2_path" not in worker.env["PATH"]

    ctx_copy = worker.ctx["items"]
    ctx_copy.append(99)
    assert worker.ctx["items"] == [1, 2]

    # Unpicklable values raise locally without reaching peer.
    unpicklable = (i for i in range(2))
    with pytest.raises(TypeError):
        worker.env["BAD"] = unpicklable
    with pytest.raises(TypeError):
        worker.ctx["BAD"] = (i for i in range(2))

    r1 = worker.send("echo $CC @(target)")
    assert r1.rtn == 0
    assert r1.out == "clang release\n"
    assert worker.lastcmd.args == ["echo", "clang", "release"]
    assert worker.lastcmd.rtn == 0
    assert worker.lastcmd.out == "clang release\n"
    assert worker.lastcmd.err == ""

    # Delete + KeyError checks.
    del worker.env["CC"]
    assert "CC" not in worker.env
    with pytest.raises(KeyError):
        _ = worker.env["CC"]
    with pytest.raises(KeyError):
        del worker.env["CC"]

    del worker.ctx["items"]
    assert "items" not in worker.ctx
    with pytest.raises(KeyError):
        _ = worker.ctx["items"]

    # Tab completion while busy must not block, while env/lastcmd wait cleanly.
    busy = worker.send("sleep 0.8\n__xonsh__.env['AFTER_SLEEP'] = 'yes'")
    t0 = time.monotonic()
    c_worker = attr_complete(
        "worker.", {"worker": worker}, lambda o, a: o.startswith(a)
    )
    c_last = attr_complete(
        "worker.lastcmd.", {"worker": worker}, lambda o, a: o.startswith(a)
    )
    assert time.monotonic() - t0 < 0.2
    assert {"worker.env[", "worker.ctx[", "worker.lastcmd"} <= set(c_worker)
    assert {
        "worker.lastcmd.args",
        "worker.lastcmd.rtn",
        "worker.lastcmd.out",
        "worker.lastcmd.err",
    } <= set(c_last)
    assert worker.env["AFTER_SLEEP"] == "yes"
    assert busy.rtn == 0


def test_capture_in_string_and_with_bang_forms(
    spawn_peer: Any, tmp_path: pathlib.Path
) -> None:
    worker = spawn_peer()
    local_var = {"count": 3, "label": "items"}
    r_str = worker.send(
        "echo @(local_var['count'])-@(local_var['label'])",
        capture=["local_var"],
    )
    assert r_str.rtn == 0
    assert r_str.out == "3-items\n"
    assert "local_var" not in worker.ctx

    with pytest.raises(NameError, match="missing_m2_var"):
        worker.send("echo hi", capture=["missing_m2_var"])
    unpicklable = (i for i in range(3))
    with pytest.raises(TypeError):
        worker.send("echo hi", capture=["unpicklable"])

    script = tmp_path / "capture_macro.xsh"
    script.write_text(
        "xontrib load tmux\n"
        "worker = RemoteXonsh()\n"
        "try:\n"
        "    info = {'kind': 'macro', 'n': 7}\n"
        "    with! worker.send(capture=['info']) as run:\n"
        "        echo @(info['kind'])-@(info['n'])\n"
        "        saved_info = info\n"
        "    assert run.rtn == 0, run.rtn\n"
        "    assert run.out == 'macro-7\\n', repr(run.out)\n"
        "    assert 'info' not in worker.ctx\n"
        "    assert worker.ctx['saved_info'] == info\n"
        "    print('CAPTURE_MACRO_OK')\n"
        "finally:\n"
        "    worker.kill()\n",
        encoding="utf-8",
    )
    env = _isolated_subprocess_env(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-m", "xonsh", "--no-rc", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
        check=False,
    )
    assert proc.returncode == 0, (
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    assert "CAPTURE_MACRO_OK" in proc.stdout


def test_cancel_while_busy_and_behind_half_typed_prompt(
    spawn_peer: Any,
) -> None:
    worker = spawn_peer()
    assert worker.send("echo warm").rtn == 0

    blocker = worker.send("sleep 1.0")
    queued = worker.send("echo nope\n__xonsh__.ctx['ran_q'] = True")
    time.sleep(0.2)
    assert blocker.future.cancel() is False
    assert queued.future.cancel() is True
    assert blocker.rtn == 0
    with pytest.raises(CancelledError):
        _ = queued.rtn
    assert "ran_q" not in worker.ctx

    # Half-typed prompt line holds queued send until cancelled or cleared.
    master_fd = worker._ctrl.state.master_fd
    assert master_fd is not None
    os.write(master_fd, b"partial_var = ")
    time.sleep(0.2)

    held = worker.send("echo held\n__xonsh__.ctx['ran_held'] = True")
    time.sleep(0.15)
    assert held.future.cancel() is True
    with pytest.raises(CancelledError):
        _ = held.rtn

    # Clear the partial line with Ctrl-U so prompt becomes idle again.
    os.write(master_fd, b"\x15")
    assert "ran_held" not in worker.ctx
    assert worker.send("echo recovered").out == "recovered\n"


def test_callback_chaining_on_background_thread(spawn_peer: Any) -> None:
    worker = spawn_peer()
    done = threading.Event()
    chained_outputs: list[Any] = []
    chained_errors: list[BaseException] = []

    def _on_done(fut: Any) -> None:
        try:
            assert fut.result() == 0
            r2 = worker.send("echo chained-step\nprint('py-chained')")
            chained_outputs.append((r2.rtn, r2.out))
        except BaseException as exc:  # noqa: BLE001
            chained_errors.append(exc)
        finally:
            done.set()

    r1 = worker.send("echo first-step")
    r1.future.add_done_callback(_on_done)
    assert r1.out == "first-step\n"
    assert done.wait(timeout=10.0)
    assert chained_errors == []
    assert chained_outputs == [(0, "chained-step\npy-chained\n")]


def test_self_send_and_self_wait_inside_live_peer(spawn_peer: Any) -> None:
    worker = spawn_peer()
    sock_str = str(worker._socket_path)
    worker.ctx["self_sock"] = sock_str

    res = worker.send(
        "import time\n"
        "from xontrib_tmux import RemoteXonsh\n"
        "me = RemoteXonsh._from_socket(self_sock, time.monotonic())\n"
        "r_self = me.send(\"__xonsh__.ctx['self_ran'] = True\\n"
        "echo from-self\")\n"
        "errors = []\n"
        "for fn in (\n"
        "    lambda: r_self.rtn,\n"
        "    lambda: r_self.future.result(),\n"
        "    lambda: me.env['PATH'],\n"
        "    lambda: me.ctx['self_sock'],\n"
        "    lambda: me.lastcmd.rtn,\n"
        "):\n"
        "    try:\n"
        "        fn()\n"
        "    except RuntimeError as exc:\n"
        "        errors.append(str(exc))\n"
        "__xonsh__.ctx['self_errors'] = errors\n"
    )
    assert res.rtn == 0
    errors = worker.ctx["self_errors"]
    assert len(errors) == 5
    assert all("Cannot wait on this shell's own peer" in msg for msg in errors)

    # Queued self-send ran after the outer entry finished.
    assert worker.ctx["self_ran"] is True
    assert worker.lastcmd.args == ["echo", "from-self"]


def test_python_stdout_stderr_interleaving_exceptions_and_hand_entry(
    spawn_peer: Any,
) -> None:
    worker = spawn_peer()

    r_print = worker.send("print(1)")
    assert r_print.rtn == 0
    assert r_print.out == "1\n"
    assert r_print.err == ""
    assert r_print.commands == []

    # Interleaved Python stdout/stderr + command stdout/stderr + buffer.write.
    r_mix = worker.send(
        "import sys\n"
        "echo a\n"
        "print('b')\n"
        "sys.stdout.buffer.write(b'b2\\n'); sys.stdout.buffer.flush()\n"
        "echo c\n"
        "sys.stderr.write('py-err1\\n')\n"
        "python3 -c r\"import sys; sys.stderr.write('cmd-err\\n')\"\n"
        "sys.stderr.write('py-err2\\n')\n"
    )
    assert r_mix.rtn == 0
    assert r_mix.out == "a\nb\nb2\nc\n"
    assert r_mix.err == "py-err1\ncmd-err\npy-err2\n"
    assert [c.args for c in r_mix.commands] == [
        ["echo", "a"],
        ["echo", "c"],
        [
            "python3",
            "-c",
            "import sys; sys.stderr.write('cmd-err\\n')",
        ],
    ]

    # Uncaught Python exception traceback lands in err.
    r_exc = worker.send("print('before-boom')\n1 / 0")
    assert r_exc.rtn == 1
    assert r_exc.out == "before-boom\n"
    assert r_exc.err is not None
    assert "ZeroDivisionError" in r_exc.err

    # record=False captures neither Python output nor commands.
    r_unrec = worker.send("print('no-rec')\necho no-rec-cmd", record=False)
    assert r_unrec.rtn == 0
    assert r_unrec.out is None
    assert r_unrec.err is None

    # Foreground (@unthread) commands contribute nothing while Python prints do.
    r_fg = worker.send("@unthread echo hidden-fg\nprint('visible-py')")
    assert r_fg.rtn == 0
    assert r_fg.out == "visible-py\n"
    assert r_fg.commands[0].out is None

    # Hand-typed entries on pty must not leak into sent blocks or break prompt.
    master_fd = worker._ctrl.state.master_fd
    assert master_fd is not None
    os.write(master_fd, b"print('hand_typed_xyz')\r")
    time.sleep(0.25)
    os.write(master_fd, b"echo hand_cmd_xyz\r")
    time.sleep(0.25)

    r_after = worker.send("print('after-hand')")
    assert r_after.rtn == 0
    assert r_after.out == "after-hand\n"
    assert "hand_typed_xyz" not in r_after.out
    assert "hand_cmd_xyz" not in r_after.out


def test_env_wait_abandoned_on_keyboard_interrupt(spawn_peer: Any) -> None:
    worker = spawn_peer()
    assert worker.send("echo warm").rtn == 0

    busy = worker.send("sleep 1.0")
    time.sleep(0.15)
    timer = threading.Timer(
        0.15, os.kill, args=(os.getpid(), signal.SIGINT)
    )
    timer.start()
    t0 = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt):
            _ = worker.env["PATH"]
        assert time.monotonic() - t0 < 0.7
    finally:
        timer.cancel()

    assert busy.rtn == 0
    assert worker.send("echo survived").out == "survived\n"


def test_scoped_capture_closure_semantics_and_failure_restore(
    spawn_peer: Any,
) -> None:
    """Covers closure-like scoped capture across all 7 live-peer cases."""
    worker = spawn_peer()
    worker.ctx["x"] = "before"

    # 1-4: pre-existing restore, temp removal, rebinding discarded, dynamic fn.
    x = 42
    temp_var = "only-in-block"
    r_ok = worker.send(
        "def read_x():\n"
        "    return x\n"
        "in_val = (x, temp_var, read_x())\n"
        "x = 999\n"
        "after_rebind = read_x()\n"
        "saved = temp_var\n"
        "print(in_val, after_rebind)\n",
        capture=["x", "temp_var"],
    )
    assert r_ok.rtn == 0, r_ok.err
    assert r_ok.out == "(42, 'only-in-block', 42) 999\n"
    assert worker.ctx["x"] == "before"
    assert "temp_var" not in worker.ctx
    assert worker.ctx["saved"] == "only-in-block"
    assert worker.send("print(read_x())").out == "before\n"

    # 5a: restore after failing command (rtn != 0).
    x = "fail-cmd"
    r_fail = worker.send("ls /nonexistent_xt_capture_xyz", capture=["x"])
    assert r_fail.rtn != 0
    assert worker.ctx["x"] == "before"

    # 5b: restore after uncaught exception (1 / 0).
    x = "exc-cmd"
    r_exc = worker.send("1 / 0", capture=["x"])
    assert r_exc.rtn == 1
    assert worker.ctx["x"] == "before"

    # 5c: restore after interrupt (\x03 on pty master during sleep).
    x = "int-cmd"
    r_int = worker.send("sleep 5", capture=["x"])
    time.sleep(0.25)
    master_fd = worker._ctrl.state.master_fd
    assert master_fd is not None
    os.write(master_fd, b"\x03")
    assert r_int.rtn != 0
    assert worker.ctx["x"] == "before"

    # 6: 3-shell chain A -> B -> C with C outliving B.
    peer_a = worker
    peer_b = spawn_peer()
    peer_c = spawn_peer()
    peer_b.ctx["x"] = "b_before"
    peer_c.ctx["x"] = "c_before"
    peer_a.ctx["b_sock"] = str(peer_b._socket_path)
    peer_a.ctx["c_sock"] = str(peer_c._socket_path)

    x = "from_driver"
    r_chain = peer_a.send(
        "import time\n"
        "from xontrib_tmux import RemoteXonsh\n"
        "bh = RemoteXonsh._from_socket(b_sock, time.monotonic())\n"
        "rb = bh.send(\n"
        "    \"import time\\n\"\n"
        "    \"from xontrib_tmux import RemoteXonsh\\n\"\n"
        "    \"ch = RemoteXonsh._from_socket(c_sock, time.monotonic())\\n\"\n"
        "    \"ch.send(\\\"import time\\\\ntime.sleep(0.5)\\\\n\"\n"
        "    \"__xonsh__.ctx['c_saw'] = x\\\\nx = 'mut_c'\\\", \"\n"
        "    \"capture=['x'])\\n\"\n"
        "    \"__xonsh__.ctx['b_saw'] = x\\n\"\n"
        "    \"x = 'mut_b'\\n\",\n"
        "    capture=['x', 'c_sock'],\n"
        ")\n"
        "rb.end()\n",
        capture=["x"],
    )
    assert r_chain.rtn == 0, r_chain.err
    assert peer_a.ctx["x"] == "before"
    assert peer_b.ctx["b_saw"] == "from_driver"
    assert peer_b.ctx["x"] == "b_before"
    assert "c_sock" not in peer_b.ctx

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if "c_saw" in peer_c.ctx:
            break
        time.sleep(0.05)
    assert peer_c.ctx.get("c_saw") == "from_driver"
    assert peer_c.ctx["x"] == "c_before"

    # 7: shell sending to itself with capture=["x"].
    peer_a.ctx["self_sock"] = str(peer_a._socket_path)
    x = "outer_capture"
    r_self_wrap = peer_a.send(
        "import time\n"
        "from xontrib_tmux import RemoteXonsh\n"
        "me = RemoteXonsh._from_socket(self_sock, time.monotonic())\n"
        "x = 'captured_self'\n"
        "me.send(\"__xonsh__.ctx['self_saw'] = x\\nx = 'mut_self'\", "
        "capture=['x'])\n",
        capture=["x"],
    )
    assert r_self_wrap.rtn == 0, r_self_wrap.err
    assert peer_a.send("echo wait-self").rtn == 0
    assert peer_a.ctx["self_saw"] == "captured_self"
    assert peer_a.ctx["x"] == "before"
