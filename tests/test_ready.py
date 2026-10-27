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

import os
import pty
import termios
from typing import Any

import pytest


@pytest.fixture
def raw_and_cooked_tty() -> Any:
    """Yields a helper owning a real pty whose lflags can be toggled."""
    controller_fd, tty_fd = pty.openpty()
    name = os.ttyname(tty_fd)

    def set_cooked() -> None:
        attrs = termios.tcgetattr(tty_fd)
        attrs[3] |= termios.ICANON | termios.ECHO
        termios.tcsetattr(tty_fd, termios.TCSANOW, attrs)

    def set_raw() -> None:
        attrs = termios.tcgetattr(tty_fd)
        attrs[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(tty_fd, termios.TCSANOW, attrs)

    try:
        yield name, set_cooked, set_raw
    finally:
        os.close(tty_fd)
        os.close(controller_fd)


class FakePane:
    def __init__(self, pane_tty: Any) -> None:
        self.pane_tty = pane_tty


def test_tty_is_raw_tracks_lflags(raw_and_cooked_tty: Any) -> None:
    from xontrib_tmux.ready import tty_is_raw  # type: ignore

    name, set_cooked, set_raw = raw_and_cooked_tty

    set_cooked()
    assert not tty_is_raw(name)

    set_raw()
    assert tty_is_raw(name)


def test_tty_is_raw_on_missing_tty() -> None:
    from xontrib_tmux.ready import tty_is_raw  # type: ignore

    # Unreadable ttys shouldn't make callers block forever.
    assert tty_is_raw("/dev/pts/does-not-exist")


def test_wait_until_ready_returns_when_shell_takes_over(
    raw_and_cooked_tty: Any,
) -> None:
    from xontrib_tmux.ready import wait_until_ready  # type: ignore

    name, set_cooked, set_raw = raw_and_cooked_tty

    set_raw()
    assert wait_until_ready(FakePane(name), timeout=1.0, interval=0.01)


def test_wait_until_ready_times_out_while_cooked(
    raw_and_cooked_tty: Any,
) -> None:
    from xontrib_tmux.ready import wait_until_ready  # type: ignore

    name, set_cooked, set_raw = raw_and_cooked_tty

    set_cooked()
    assert not wait_until_ready(FakePane(name), timeout=0.1, interval=0.01)


def test_wait_until_ready_without_tty() -> None:
    from xontrib_tmux.ready import wait_until_ready  # type: ignore

    assert not wait_until_ready(FakePane(None), timeout=0.1, interval=0.01)
