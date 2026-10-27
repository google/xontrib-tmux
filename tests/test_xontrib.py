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

import builtins
import shutil
import subprocess
from typing import Any, List
from unittest.mock import MagicMock

import libtmux  # type: ignore
import pytest


def test_it_loads(load_xontrib: Any) -> None:
    load_xontrib("tmux")


def test_session_has_attach_monkeypatch(load_xontrib: Any) -> None:
    load_xontrib("tmux")
    import xontrib_tmux.monkeypatch as mp  # type: ignore
    assert libtmux.Session.attach == mp._attach


def test_attach_xonsh_subproc(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    session = libtmux.Session(server=libtmux.Server(), session_id="$42")

    recorded_cmds: List[Any] = []

    mock_xsh = MagicMock()
    mock_xsh.env = {"FOO": "bar"}
    mock_xsh.subproc_uncaptured = lambda cmd: recorded_cmds.append(cmd)

    monkeypatch.setattr(builtins, "__xonsh__", mock_xsh, raising=False)

    result = session.attach()
    assert result is session
    assert recorded_cmds == [["tmux", "attach", "-t", "$42"]]


def test_attach_outside_xonsh(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    session = libtmux.Session(server=libtmux.Server(), session_id="$99")

    recorded_cmds: List[Any] = []

    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda cmd: recorded_cmds.append(cmd))

    result = session.attach()
    assert result is session
    assert recorded_cmds == [["tmux", "attach", "-t", "$99"]]


def test_attach_server_options_and_flags(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    server = libtmux.Server(
        socket_name="custom_socket",
        config_file="/tmp/tmux.conf",
        colors=256,
    )
    session = libtmux.Session(server=server, session_id="$100")

    recorded_cmds: List[Any] = []
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda cmd: recorded_cmds.append(cmd))

    result = session.attach(exit_=True, flags_=["-d"])
    assert result is session
    assert recorded_cmds == [
        [
            "tmux",
            "-L", "custom_socket",
            "-f", "/tmp/tmux.conf",
            "-2",
            "attach",
            "-x",
            "-d",
            "-t", "$100",
        ]
    ]


def test_attach_server_socket_path(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    server = libtmux.Server(socket_path="/tmp/tmux_custom")
    session = libtmux.Session(server=server, session_id="$101")

    recorded_cmds: List[Any] = []
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda cmd: recorded_cmds.append(cmd))

    result = session.attach()
    assert result is session
    assert recorded_cmds == [
        [
            "tmux",
            "-S", "/tmp/tmux_custom",
            "attach",
            "-t", "$101",
        ]
    ]


def test_attach_missing_session_id(load_xontrib: Any) -> None:
    load_xontrib("tmux")
    session = libtmux.Session(server=libtmux.Server())
    session.session_id = None
    session.session_name = None

    with pytest.raises(ValueError, match="Session has no session_id"):
        session.attach()


class FakeQueryList(list):
    """Minimal stand-in for libtmux's QueryList."""

    _NO_DEFAULT = object()

    def get(self, default: Any = _NO_DEFAULT, **kwargs: Any) -> Any:
        matches = [
            obj
            for obj in self
            if all(getattr(obj, k) == v for k, v in kwargs.items())
        ]
        if not matches:
            if default is self._NO_DEFAULT:
                raise libtmux.exc.ObjectDoesNotExist
            return default
        if len(matches) > 1:
            raise libtmux.exc.MultipleObjectsReturned
        return matches[0]


class FakeSession:
    def __init__(self, session_name: str) -> None:
        self.session_name = session_name


class FakeServer:
    """Records new_session calls and serves up preexisting sessions."""

    def __init__(self, *session_names: str) -> None:
        self.sessions = FakeQueryList(
            FakeSession(name) for name in session_names
        )
        self.new_session_calls: List[Any] = []

    def new_session(
        self, session_name: Any = None, window_command: Any = None
    ) -> FakeSession:
        if session_name is not None and any(
            s.session_name == session_name for s in self.sessions
        ):
            raise libtmux.exc.TmuxSessionExists(
                f"Session named {session_name} exists"
            )
        self.new_session_calls.append((session_name, window_command))
        session = FakeSession(session_name)
        self.sessions.append(session)
        return session


@pytest.fixture
def fake_server(monkeypatch: Any) -> Any:
    """Patches libtmux.Server with a factory returning a FakeServer."""

    def install(*session_names: str) -> FakeServer:
        server = FakeServer(*session_names)
        monkeypatch.setattr(libtmux, "Server", lambda *a, **kw: server)
        monkeypatch.setattr(shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
        return server

    return install


def test_tmuxs_returns_existing_session(
    load_xontrib: Any, fake_server: Any
) -> None:
    load_xontrib("tmux")
    from xontrib_tmux.commands import tmuxs  # type: ignore

    server = fake_server("mysession")

    session = tmuxs("mysession")

    assert session is server.sessions[0]
    assert server.new_session_calls == []


def test_tmuxs_creates_session_when_missing(
    load_xontrib: Any, fake_server: Any
) -> None:
    load_xontrib("tmux")
    from xontrib_tmux.commands import tmuxs  # type: ignore

    server = fake_server("other")

    session = tmuxs("mysession")

    assert session.session_name == "mysession"
    assert server.new_session_calls == [("mysession", "/usr/bin/xonsh")]


def test_tmuxs_creates_session_when_missing_and_not_existing(
    load_xontrib: Any, fake_server: Any
) -> None:
    load_xontrib("tmux")
    from xontrib_tmux.commands import tmuxs  # type: ignore

    server = fake_server()

    session = tmuxs("mysession", existing=False)

    assert session.session_name == "mysession"
    assert server.new_session_calls == [("mysession", "/usr/bin/xonsh")]


def test_tmuxs_raises_when_existing_disallowed(
    load_xontrib: Any, fake_server: Any
) -> None:
    load_xontrib("tmux")
    from xontrib_tmux.commands import tmuxs  # type: ignore

    server = fake_server("mysession")

    with pytest.raises(libtmux.exc.TmuxSessionExists):
        tmuxs("mysession", existing=False)

    assert server.new_session_calls == []


def test_tmuxs_unnamed_session_always_created(
    load_xontrib: Any, fake_server: Any
) -> None:
    load_xontrib("tmux")
    from xontrib_tmux.commands import tmuxs  # type: ignore

    server = fake_server("mysession")

    tmuxs()

    assert server.new_session_calls == [(None, "/usr/bin/xonsh")]
