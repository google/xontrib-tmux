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

"""Hermetic unit tests for ``xontrib_tmux.commands``."""

import io
import os
from types import SimpleNamespace
from typing import Any

import libtmux  # type: ignore
import pytest

from xontrib_tmux import commands


def test_controlling_tty_rdev_parses_stat_with_parens_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Parses tty_nr after last ')' and falls back to stdio ttyname."""
    stat_line = (
        "12345 (web (worker) 1) S 1 123 123 34829 -1 0 0 0 0 0 0 0 0 20 0\n"
    )
    real_open = open

    def fake_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "/proc/self/stat":
            return io.StringIO(stat_line)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)
    assert commands._controlling_tty_rdev() == 34829

    # When /proc/self/stat is unavailable, falls back to stdio ttyname.
    def broken_open(path: Any, *args: Any, **kwargs: Any) -> Any:
        if path == "/proc/self/stat":
            raise OSError("no procfs")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", broken_open)

    def fake_ttyname(fd: int) -> str:
        if fd == 1:
            return "/dev/pts/7"
        raise OSError("no tty")

    monkeypatch.setattr(os, "ttyname", fake_ttyname)
    monkeypatch.setattr(
        os, "stat", lambda p: SimpleNamespace(st_rdev=9999)
    )
    assert commands._controlling_tty_rdev() == 9999

    # Returns 0 when neither /proc/self/stat nor stdio ttyname succeeds.
    monkeypatch.setattr(
        os, "ttyname", lambda fd: (_ for _ in ()).throw(OSError())
    )
    assert commands._controlling_tty_rdev() == 0


def test_is_inside_tmux_and_curtmux_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches controlling tty rdev against pane_tty and guards curtmux*."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert commands.is_inside_tmux() is False
    assert commands.curtmuxs() is None
    assert commands.curtmuxw() is None
    assert commands.curtmuxp() is None

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/test,123,0")
    monkeypatch.setenv("TMUX_PANE", "%9")

    # Case 1: process has no controlling tty (rdev == 0).
    monkeypatch.setattr(commands, "_controlling_tty_rdev", lambda: 0)
    assert commands.is_inside_tmux() is False

    # Case 2: tty differs from pane_tty (e.g. separate pty with inherited env).
    monkeypatch.setattr(commands, "_controlling_tty_rdev", lambda: 34810)

    class _FakeSrv:
        def cmd(self, *args: Any, **kwargs: Any) -> Any:
            assert args == ("display-message", "-p", "-t", "%9", "#{pane_tty}")
            return SimpleNamespace(stderr=[], stdout=["/dev/pts/4"])

    monkeypatch.setattr(libtmux.Server, "from_env", lambda: _FakeSrv())
    monkeypatch.setattr(os, "stat", lambda p: SimpleNamespace(st_rdev=34804))
    assert commands.is_inside_tmux() is False
    assert commands.curtmuxs() is None
    assert commands.curtmuxw() is None
    assert commands.curtmuxp() is None

    # Case 3: matching tty rdev -> inside tmux.
    monkeypatch.setattr(commands, "_controlling_tty_rdev", lambda: 34804)
    assert commands.is_inside_tmux() is True
    fake_sess = object()
    fake_win = object()
    fake_pane = object()
    monkeypatch.setattr(libtmux.Session, "from_env", lambda: fake_sess)
    monkeypatch.setattr(libtmux.Window, "from_env", lambda: fake_win)
    monkeypatch.setattr(libtmux.Pane, "from_env", lambda: fake_pane)
    assert commands.curtmuxs() is fake_sess
    assert commands.curtmuxw() is fake_win
    assert commands.curtmuxp() is fake_pane
