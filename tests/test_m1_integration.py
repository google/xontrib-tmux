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

"""Milestone 1 end-to-end integration tests with real xonsh peers."""

from collections.abc import Iterator
import os
import pathlib
import subprocess
import sys
import time
from typing import Any

import pytest
from xonsh.completers.python import attr_complete

from xontrib_tmux import RemoteXonsh


def _pid_alive(pid: int) -> bool:
    """Return True if a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_pid_dead(pid: int, timeout: float = 3.0) -> bool:
    """Poll briefly until ``pid`` no longer exists."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return not _pid_alive(pid)


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


def test_pth_autoload_disabled_strips_env_and_sets_dummy_history(
    monkeypatch: pytest.MonkeyPatch,
    spawn_peer: Any,
    tmp_path: pathlib.Path,
) -> None:
    monkeypatch.setenv("XONTRIBS_AUTOLOAD_DISABLED", "1")
    worker = spawn_peer()
    out_file = tmp_path / "peer_env.txt"
    res = worker.send(
        "import os, sys\n"
        f"with open({str(out_file)!r}, 'w', encoding='utf-8') as f:\n"
        "    f.write(repr((\n"
        "        'xontrib_tmux' in sys.modules,\n"
        "        os.environ.get('_XONTRIB_TMUX_PEER_SOCKET'),\n"
        "        os.environ.get('_XONTRIB_TMUX_HIDDEN'),\n"
        "        __xonsh__.env.get('XONSH_HISTORY_BACKEND'),\n"
        "    )))\n"
    )
    assert res.rtn == 0
    assert out_file.read_text(encoding="utf-8") == (
        "(True, None, None, 'dummy')"
    )


def test_multistatement_string_send_and_records(spawn_peer: Any) -> None:
    worker = spawn_peer()
    res = worker.send(
        "echo first-line\n"
        'python3 -c "import sys; sys.stdout.write(\'out2\\\\n\'); '
        'sys.stderr.write(\'err2\\\\n\')"\n'
    )
    assert bool(res) is True
    assert res.rtn == 0
    assert res.returncode == 0
    assert res.lines == ["first-line", "out2"]
    assert list(res) == ["first-line", "out2"]
    assert res.out == "first-line\nout2\n"
    assert res.output == "first-line\nout2\n"
    assert res.err == "err2\n"
    assert res.errors == "err2\n"
    assert len(res.commands) == 2
    assert res.commands[0].args == ["echo", "first-line"]
    assert res.commands[0].rtn == 0
    assert res.commands[0].out == "first-line\n"
    assert res.commands[0].err == ""
    assert res.commands[1].args[0] == "python3"
    assert res.commands[1].rtn == 0
    assert res.commands[1].out == "out2\n"
    assert res.commands[1].err == "err2\n"


def test_context_macro_with_real_xonsh_session(tmp_path: pathlib.Path) -> None:
    script = tmp_path / "macro_demo.xsh"
    script.write_text(
        "xontrib load tmux\n"
        "worker = RemoteXonsh()\n"
        "try:\n"
        "    with! worker.send() as run:\n"
        "        echo first-line\n"
        '        python3 -c "import sys; '
        "sys.stdout.write('out2\\\\n'); sys.stderr.write('err2\\\\n')\"\n"
        "    assert bool(run) is True, run\n"
        "    assert run.rtn == 0, run.rtn\n"
        "    assert run.lines == ['first-line', 'out2'], run.lines\n"
        "    assert run.err == 'err2\\n', repr(run.err)\n"
        "    assert len(run.commands) == 2, run.commands\n"
        "    assert run.commands[0].args == ['echo', 'first-line']\n"
        "    assert run.commands[1].out == 'out2\\n'\n"
        "    print('MACRO_OK')\n"
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
    assert "MACRO_OK" in proc.stdout


def test_record_false_and_large_unrecorded_drain(spawn_peer: Any) -> None:
    worker = spawn_peer()
    small = worker.send("echo unrecorded", record=False)
    assert small.rtn == 0
    assert small.out is None
    assert small.err is None
    assert small.lines == []
    assert len(small.commands) == 1
    assert small.commands[0].out is None
    assert small.commands[0].err is None

    t0 = time.monotonic()
    big = worker.send(
        'python3 -c "import sys; '
        "sys.stdout.write('x' * 260000 + '\\\\n'); "
        "sys.stderr.write('e' * 65536 + '\\\\n')\"",
        record=False,
    )
    assert big.rtn == 0
    assert time.monotonic() - t0 < 15.0
    assert big.out is None
    assert big.err is None
    assert big.commands[0].out is None
    assert big.commands[0].err is None


def test_thread_and_unthread_subproc_capture(
    spawn_peer: Any, tmp_path: pathlib.Path
) -> None:
    worker = spawn_peer()
    script = tmp_path / "script.py"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "sys.stdout.write('script-out\\n')\n"
        "sys.stderr.write('script-err\\n')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)

    # Mark script non-threadable in commands_cache so plain execution runs in
    # the foreground (out=None, err=None) unless @thread overrides it.
    prep = worker.send(
        "__xonsh__.commands_cache.threadable_predictors["
        f"__xonsh__.commands_cache.cached_name({str(script)!r})"
        "] = (lambda args, cache: False)\n"
    )
    assert prep.rtn == 0

    res_fg = worker.send(f"{script}\n")
    assert res_fg.rtn == 0
    assert res_fg.out == ""
    assert res_fg.err == ""
    assert res_fg.commands[0].out is None
    assert res_fg.commands[0].err is None

    res_thread = worker.send(
        "@thread echo threaded-out\n"
        f"@thread {script}\n"
    )
    assert res_thread.rtn == 0
    assert res_thread.out == "threaded-out\nscript-out\n"
    assert res_thread.err == "script-err\n"
    assert [c.out for c in res_thread.commands] == [
        "threaded-out\n",
        "script-out\n",
    ]
    assert [c.err for c in res_thread.commands] == ["", "script-err\n"]

    res_unthread = worker.send(
        "@unthread echo unthreaded-out\n"
        '@unthread python3 -c "import sys; sys.stdout.write(\'u-out\\\\n\'); '
        'sys.stderr.write(\'u-err\\\\n\')"\n'
    )
    assert res_unthread.rtn == 0
    assert res_unthread.out == ""
    assert res_unthread.err == ""
    assert [c.out for c in res_unthread.commands] == [None, None]
    assert [c.err for c in res_unthread.commands] == [None, None]


def test_failing_command_stops_block(spawn_peer: Any) -> None:
    worker = spawn_peer()
    res = worker.send(
        "echo before\nls /nonexistent_path_xyz_m1_test\necho after\n"
    )
    assert res.rtn != 0
    assert bool(res) is False
    assert len(res.commands) == 2
    assert res.commands[0].args == ["echo", "before"]
    assert res.commands[0].rtn == 0
    assert res.commands[0].out == "before\n"
    assert res.commands[1].args[:2] == ["ls", "/nonexistent_path_xyz_m1_test"]
    assert res.commands[1].rtn != 0
    assert "after" not in (res.out or "")


def test_uncaught_exception_after_ok_command_returns_one(
    spawn_peer: Any,
) -> None:
    worker = spawn_peer()
    res = worker.send("echo before\nraise ValueError('boom')\necho after\n")
    assert res.rtn == 1
    assert bool(res) is False
    assert [c.out for c in res.commands] == ["before\n"]


def test_nonblocking_repr_and_attr_complete_while_running(
    xonsh_execer: Any, spawn_peer: Any
) -> None:
    del xonsh_execer
    t0 = time.monotonic()
    worker = spawn_peer()
    worker_repr = repr(worker)
    dt_init = time.monotonic() - t0
    assert worker_repr.startswith("<RemoteXonsh ")
    assert dt_init < 0.5

    # Warm up so the sleep below measures active execution, not peer boot.
    assert worker.send("echo warm").rtn == 0

    t1 = time.monotonic()
    slow = worker.send("sleep 1.2\necho done-slow")
    slow_repr = repr(slow)
    comps = attr_complete("slow.", {"slow": slow}, lambda o, a: o.startswith(a))
    dt_pending = time.monotonic() - t1

    assert dt_pending < 0.2
    assert slow_repr == "<PeerResult pending>"
    stripped = {c.rstrip("(") for c in comps}
    assert {"slow.rtn", "slow.out", "slow.commands"} <= stripped

    assert slow.rtn == 0
    assert slow.out == "done-slow\n"


def test_fail_fast_readline_shell_type(
    monkeypatch: pytest.MonkeyPatch, spawn_peer: Any
) -> None:
    ready_timeout = 30.0
    monkeypatch.setenv("XONTRIB_TMUX_READY_TIMEOUT", str(ready_timeout))
    monkeypatch.setenv("SHELL_TYPE", "readline")
    worker = spawn_peer()
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="prompt_toolkit") as exc_info:
        _ = worker.send("echo should-fail").rtn
    elapsed = time.monotonic() - t0
    assert "Timed out" not in str(exc_info.value)
    assert elapsed < ready_timeout * 0.4


def test_fail_fast_rc_sys_exit(
    monkeypatch: pytest.MonkeyPatch,
    spawn_peer: Any,
    tmp_path: pathlib.Path,
) -> None:
    bad_rc = tmp_path / "bad_rc.xsh"
    bad_rc.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
    ready_timeout = 30.0
    monkeypatch.setenv("XONTRIB_TMUX_READY_TIMEOUT", str(ready_timeout))
    monkeypatch.setenv("XONSHRC", str(bad_rc))

    worker = spawn_peer()
    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="Peer exited") as exc_info:
        _ = worker.send("echo should-fail").rtn
    elapsed = time.monotonic() - t0
    assert "Timed out" not in str(exc_info.value)
    assert elapsed < ready_timeout * 0.4


def test_kill_and_context_manager_cleanup_child_and_socket(
    isolated_env: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    del isolated_env
    pids_file = tmp_path / "pids.txt"
    w1 = RemoteXonsh()
    sock1 = w1._socket_path
    err1 = pathlib.Path(f"{sock1}.err")
    try:
        res = w1.send(
            "import os, subprocess\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            f"with open({str(pids_file)!r}, 'w', encoding='utf-8') as f:\n"
            "    f.write(f'{os.getpid()} {child.pid}')\n"
        )
        assert res.rtn == 0
    finally:
        w1.kill()

    peer_pid1, child_pid1 = (
        int(x) for x in pids_file.read_text(encoding="utf-8").split()
    )
    assert _wait_pid_dead(peer_pid1)
    assert _wait_pid_dead(child_pid1)
    assert not sock1.exists()
    assert not err1.exists()

    pids_file2 = tmp_path / "pids2.txt"
    with RemoteXonsh() as w2:
        sock2 = w2._socket_path
        err2 = pathlib.Path(f"{sock2}.err")
        res2 = w2.send(
            "import os, subprocess\n"
            "child = subprocess.Popen(['sleep', '60'])\n"
            f"with open({str(pids_file2)!r}, 'w', encoding='utf-8') as f:\n"
            "    f.write(f'{os.getpid()} {child.pid}')\n"
        )
        assert res2.rtn == 0

    peer_pid2, child_pid2 = (
        int(x) for x in pids_file2.read_text(encoding="utf-8").split()
    )
    assert _wait_pid_dead(peer_pid2)
    assert _wait_pid_dead(child_pid2)
    assert not sock2.exists()
    assert not err2.exists()


def test_local_validation_and_send_after_kill(spawn_peer: Any) -> None:
    worker = spawn_peer()
    with pytest.raises(SyntaxError):
        worker.send("if True:\n")
    with pytest.raises(ValueError):
        worker.send("# comment only\n")

    worker.kill()
    with pytest.raises(RuntimeError):
        worker.send("echo after-kill")
