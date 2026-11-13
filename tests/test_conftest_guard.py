# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests for the conftest default-tmux-server isolation guard."""

from __future__ import annotations

import os

import libtmux  # type: ignore
from libtmux.common import tmux_cmd  # type: ignore
import pytest

from xontrib_tmux import commands


def test_autouse_strips_tmux_env_vars() -> None:
    """Every test begins with TMUX and TMUX_PANE stripped from os.environ."""
    assert "TMUX" not in os.environ
    assert "TMUX_PANE" not in os.environ


def test_default_server_constructors_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proves attempts to target the default server fail the test."""
    with pytest.raises(pytest.fail.Exception):
        libtmux.Server()

    with pytest.raises(pytest.fail.Exception):
        libtmux.Server(socket_name="default")

    with pytest.raises(pytest.fail.Exception):
        libtmux.Server(socket_name="")

    with pytest.raises(pytest.fail.Exception):
        libtmux.Server(socket_path="/tmp/tmux-1000/default")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    with pytest.raises(pytest.fail.Exception):
        libtmux.Server.from_env()

    with pytest.raises(pytest.fail.Exception):
        tmux_cmd("list-sessions")

    with pytest.raises(pytest.fail.Exception):
        tmux_cmd("-Ldefault", "list-sessions")

    with pytest.raises(pytest.fail.Exception):
        tmux_cmd("-S", "/tmp/tmux-1000/default", "list-sessions")


def test_xsess_without_private_tmux_fails_instead_of_default_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling xsess() or _current_server() with no private socket fails."""
    with pytest.raises(pytest.fail.Exception):
        commands.xsess("should-fail-before-tmux")

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    monkeypatch.setenv("TMUX_PANE", "%0")
    with pytest.raises(pytest.fail.Exception):
        commands._current_server()


def test_explicit_private_socket_allowed() -> None:
    """Explicit non-default socket names and paths are permitted."""
    srv_name = libtmux.Server(socket_name="xt_guard_private_ok")
    srv_path = libtmux.Server(socket_path="/tmp/tmux-1000/xt_guard_private_ok")
    assert srv_name.socket_name == "xt_guard_private_ok"
    assert srv_path.socket_path == "/tmp/tmux-1000/xt_guard_private_ok"
