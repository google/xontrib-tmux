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
import time
from typing import Any

import pytest

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
