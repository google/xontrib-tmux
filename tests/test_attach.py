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

"""Hermetic unit tests for :mod:`xontrib_tmux.attach`."""

from __future__ import annotations

import builtins
import importlib
import os
import subprocess
from types import SimpleNamespace
from typing import Any

import libtmux  # type: ignore
import pytest

from xontrib_tmux.attach import (
    DifferentTmuxServerError,
    _server_socket_path,
    attach,
)

xt_attach = importlib.import_module("xontrib_tmux.attach")


def _fake_server(
    *,
    socket_name: str | None = None,
    socket_path: str | None = None,
    config_file: str | None = None,
    colors: int | None = None,
    tmux_bin: str | None = None,
    cmd_fn: Any = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        socket_name=socket_name,
        socket_path=socket_path,
        config_file=config_file,
        colors=colors,
        tmux_bin=tmux_bin,
        cmd=cmd_fn or (lambda *a, **kw: SimpleNamespace(stdout=[], stderr=[])),
    )


def test_server_socket_path_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resolves explicit socket_path, socket_name, and default socket."""
    monkeypatch.delenv("TMUX_TMPDIR", raising=False)
    uid = os.getuid()
    assert _server_socket_path(None) == f"/tmp/tmux-{uid}/default"
    assert (
        _server_socket_path(_fake_server(socket_name="priv"))
        == f"/tmp/tmux-{uid}/priv"
    )
    assert (
        _server_socket_path(_fake_server(socket_path="/var/tmp/my.sock"))
        == "/var/tmp/my.sock"
    )


def test_outside_tmux_attach_flags_and_subproc_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bare-terminal attach passes server flags and fails on non-zero rc."""
    monkeypatch.setattr(xt_attach, "is_inside_tmux", lambda: False)

    srv = _fake_server(
        socket_name="custom", config_file="/tmp/tmux.conf", colors=256
    )
    session = libtmux.Session(server=srv, session_id="$7")

    recorded: list[list[str]] = []
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd: (
            recorded.append(cmd),
            SimpleNamespace(returncode=0),
        )[1],
    )
    assert attach(session, exit_=True, flags_=["-d"]) is session
    assert recorded == [
        [
            "tmux",
            "-L",
            "custom",
            "-f",
            "/tmp/tmux.conf",
            "-2",
            "attach",
            "-x",
            "-d",
            "-t",
            "$7",
        ]
    ]

    # Bare shell with no terminal -> non-zero rc raises LibTmuxException.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd: SimpleNamespace(returncode=1),
    )
    with pytest.raises(libtmux.exc.LibTmuxException, match="exit status 1"):
        attach(session)

    # Inside xonsh when subproc_uncaptured raises CalledProcessError.
    def fail_subproc(cmd: list[str]) -> None:
        raise subprocess.CalledProcessError(1, cmd)

    mock_xsh = SimpleNamespace(env={}, subproc_uncaptured=fail_subproc)
    monkeypatch.setattr(builtins, "__xonsh__", mock_xsh, raising=False)
    with pytest.raises(libtmux.exc.LibTmuxException, match="exit status 1"):
        attach(session)


def test_inside_tmux_switch_client_picks_most_recent_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selects most recently active client on caller session, else server."""
    sock = "/tmp/tmux-1000/xt_test"
    monkeypatch.setenv("TMUX", f"{sock},123,0")
    monkeypatch.setattr(xt_attach, "is_inside_tmux", lambda: True)

    cmds: list[tuple[Any, ...]] = []
    client_rows = [
        "/dev/pts/1\tother\t999",
        "/dev/pts/2\tcaller_sess\t100",
        "/dev/pts/3\tcaller_sess\t250",
    ]

    def fake_cmd(*args: Any) -> SimpleNamespace:
        cmds.append(args)
        if args[0] == "list-clients":
            return SimpleNamespace(stdout=client_rows, stderr=[], returncode=0)
        return SimpleNamespace(stdout=[], stderr=[], returncode=0)

    srv = _fake_server(socket_path=sock, cmd_fn=fake_cmd)
    session = libtmux.Session(server=srv, session_id="$9")

    # Case 1: caller's session has attached clients -> pick /dev/pts/3 (250).
    monkeypatch.setattr(
        libtmux.Pane,
        "from_env",
        lambda: SimpleNamespace(session_name="caller_sess"),
    )
    assert attach(session) is session
    assert cmds[-1] == ("switch-client", "-c", "/dev/pts/3", "-t", "$9")

    # Case 2: caller's session has no client -> fall back to server's newest.
    cmds.clear()
    monkeypatch.setattr(
        libtmux.Pane,
        "from_env",
        lambda: SimpleNamespace(session_name="detached_sess"),
    )
    assert attach(session) is session
    assert cmds[-1] == ("switch-client", "-c", "/dev/pts/1", "-t", "$9")


def test_inside_tmux_errors_invalid_args_different_server_no_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside tmux validates flags, same-server socket, and live clients."""
    sock = "/tmp/tmux-1000/xt_caller"
    other_sock = "/tmp/tmux-1000/xt_other"
    monkeypatch.setenv("TMUX", f"{sock},123,0")
    monkeypatch.setattr(xt_attach, "is_inside_tmux", lambda: True)

    same_srv = _fake_server(socket_path=sock)
    same_sess = libtmux.Session(server=same_srv, session_id="$3")

    with pytest.raises(ValueError, match="exit_ and flags_"):
        attach(same_sess, exit_=True)
    with pytest.raises(ValueError, match="exit_ and flags_"):
        attach(same_sess, flags_=["-d"])
    with pytest.raises(ValueError, match="exit_ and flags_"):
        attach(same_sess, "-r")

    other_srv = _fake_server(socket_path=other_sock)
    other_sess = libtmux.Session(server=other_srv, session_id="$4")
    with pytest.raises(DifferentTmuxServerError) as exc_info:
        attach(other_sess)
    msg = str(exc_info.value)
    assert os.path.realpath(sock) in msg
    assert os.path.realpath(other_sock) in msg

    # Empty list-clients falls back to untargeted switch-client and raises.
    cmds: list[tuple[Any, ...]] = []

    def empty_clients_cmd(*args: Any) -> SimpleNamespace:
        cmds.append(args)
        if args[0] == "list-clients":
            return SimpleNamespace(stdout=[], stderr=[], returncode=0)
        return SimpleNamespace(
            stdout=[], stderr=["no current client"], returncode=1
        )

    no_client_srv = _fake_server(socket_path=sock, cmd_fn=empty_clients_cmd)
    no_client_sess = libtmux.Session(server=no_client_srv, session_id="$5")
    monkeypatch.setattr(
        libtmux.Pane,
        "from_env",
        lambda: SimpleNamespace(session_name="caller"),
    )
    with pytest.raises(libtmux.exc.LibTmuxException, match="no current client"):
        attach(no_client_sess)
    fmt = "#{client_name}\t#{client_session}\t#{client_activity}"
    assert cmds == [
        ("list-clients", "-F", fmt),
        ("switch-client", "-t", "$5"),
    ]
