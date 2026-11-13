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

"""Hermetic unit tests for ``xontrib_tmux.pane_text``."""

from types import SimpleNamespace
from typing import Any

import libtmux  # type: ignore
import pytest

from xontrib_tmux.pane_text import pane_text


class _FakePane:
    """Minimal ``libtmux.Pane`` test double capturing ``cmd()`` calls."""

    def __init__(
        self,
        pane_id: str = "%1",
        stdout: list[str] | None = None,
        stderr: list[str] | None = None,
        returncode: int = 0,
        live_pane_ids: set[str] | None = None,
    ) -> None:
        self.pane_id = pane_id
        self.calls: list[tuple[Any, ...]] = []
        self._proc = SimpleNamespace(
            stdout=stdout if stdout is not None else [],
            stderr=stderr if stderr is not None else [],
            returncode=returncode,
        )
        ids = live_pane_ids if live_pane_ids is not None else {pane_id}
        self.server = SimpleNamespace(
            panes=[SimpleNamespace(pane_id=pid) for pid in ids]
        )

    def cmd(self, *args: Any) -> Any:
        self.calls.append(args)
        return self._proc


def test_pane_text_scrollback_flag_forms() -> None:
    """``scrollback`` False/0/True/positive-int pass exact tmux flags."""
    pane_false: Any = _FakePane(stdout=["visible"])
    assert pane_text(pane_false, scrollback=False) == "visible"
    assert pane_false.calls == [("capture-pane", "-p", "-J")]

    pane_zero: Any = _FakePane(stdout=["visible"])
    assert pane_text(pane_zero, scrollback=0) == "visible"
    assert pane_zero.calls == [("capture-pane", "-p", "-J")]

    pane_true: Any = _FakePane(stdout=["hist", "visible"])
    assert pane_text(pane_true, scrollback=True) == "hist\nvisible"
    assert pane_true.calls == [("capture-pane", "-p", "-J", "-S", "-")]

    pane_one: Any = _FakePane(stdout=["one"])
    assert pane_text(pane_one, scrollback=1) == "one"
    assert pane_one.calls == [("capture-pane", "-p", "-J", "-S", "-1")]

    pane_num: Any = _FakePane(stdout=["recent"])
    assert pane_text(pane_num, scrollback=500) == "recent"
    assert pane_num.calls == [("capture-pane", "-p", "-J", "-S", "-500")]


def test_pane_text_validates_scrollback_type_and_sign() -> None:
    """Negative ``scrollback`` raises ValueError; bad types raise TypeError."""
    pane: Any = _FakePane(stdout=["ok"])
    with pytest.raises(ValueError, match="non-negative"):
        pane_text(pane, scrollback=-1)
    with pytest.raises(ValueError, match="non-negative"):
        pane_text(pane, scrollback=-50)
    for bad in ("10", 1.5, None):
        with pytest.raises(TypeError, match="bool or int"):
            pane_text(pane, scrollback=bad)  # type: ignore[arg-type]
    assert pane.calls == []


def test_pane_text_strips_trailing_whitespace_and_blank_padding() -> None:
    """Strips per-line trailing spaces, drops blank padding, no final \\n."""
    pane: Any = _FakePane(
        stdout=[
            "first line   ",
            "",
            "second line\t  ",
            "   ",
            "",
        ]
    )
    text = pane_text(pane)
    assert text == "first line\n\nsecond line"
    assert not text.endswith("\n")

    empty_pane: Any = _FakePane(stdout=["   ", ""])
    assert pane_text(empty_pane) == ""


def test_pane_text_raises_on_stderr_or_closed_pane() -> None:
    """Raises ``LibTmuxException`` on stderr or empty closed-pane capture."""
    err_pane: Any = _FakePane(
        pane_id="%9",
        stdout=[],
        stderr=["can't find pane: %9"],
    )
    with pytest.raises(libtmux.exc.LibTmuxException, match="can't find pane"):
        pane_text(err_pane)

    rc_pane: Any = _FakePane(pane_id="%9", stdout=[], stderr=[], returncode=1)
    with pytest.raises(libtmux.exc.LibTmuxException, match="exit status 1"):
        pane_text(rc_pane)

    closed_pane: Any = _FakePane(
        pane_id="%9",
        stdout=[],
        stderr=[],
        live_pane_ids={"%1"},
    )
    with pytest.raises(libtmux.exc.LibTmuxException, match="%9 is closed"):
        pane_text(closed_pane)

    # An open but genuinely empty pane returns "" instead of raising.
    open_empty: Any = _FakePane(
        pane_id="%9",
        stdout=[],
        stderr=[],
        live_pane_ids={"%9"},
    )
    assert pane_text(open_empty) == ""
