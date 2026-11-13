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

"""End-to-end integration tests for peer cancellation."""

from collections.abc import Iterator
from concurrent.futures import CancelledError
import os
import pathlib
import signal
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
    assert (
        worker.send(
            "print('ran_q' in __xonsh__.ctx)"
        ).out
        == "False\n"
    )

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
    assert (
        worker.send(
            "print('ran_held' in __xonsh__.ctx)"
        ).out
        == "False\n"
    )
    assert worker.send("echo recovered").out == "recovered\n"


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
        "import pathlib, time\n"
        "from xontrib_tmux import RemoteXonsh\n"
        "from xontrib_tmux.peer import _PeerController\n"
        "ctrl = _PeerController(\n"
        "    socket_path=pathlib.Path(self_sock),\n"
        "    start_time=time.monotonic(),\n"
        "    is_hidden=False,\n"
        "    is_alive=lambda: True,\n"
        ")\n"
        "me = RemoteXonsh.__new__(RemoteXonsh)\n"
        "me._ctrl = ctrl\n"
        "me._socket_path = ctrl.socket_path\n"
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
