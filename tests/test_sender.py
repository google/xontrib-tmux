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

import threading
from typing import Any, List

import pytest


class FakePane:
    """Records send_keys calls the way libtmux.Pane would receive them."""

    def __init__(self, pane_id: str) -> None:
        self.pane_id = pane_id
        self.pane_tty = "/dev/null"
        self.sent: List[str] = []

    def send_keys(self, keys: str, literal: bool = False,
                  enter: bool = True) -> None:
        assert literal and enter
        self.sent.append(keys)


@pytest.fixture
def gated_wait(monkeypatch: Any) -> Any:
    """Replaces wait_until_ready with a gate the test opens explicitly."""
    from xontrib_tmux import sender  # type: ignore

    gate = threading.Event()
    calls: List[Any] = []

    def fake_wait(pane: Any, timeout: Any = None, **kwargs: Any) -> bool:
        calls.append(pane)
        return gate.wait(timeout=5.0)

    monkeypatch.setattr(sender, "wait_until_ready", fake_wait)
    return gate, calls


def test_deliver_does_not_block_caller(gated_wait: Any) -> None:
    from xontrib_tmux.sender import deliver  # type: ignore

    gate, calls = gated_wait
    pane = FakePane("%deliver-async")

    future = deliver(pane, ["one", "two"])

    # The shell must get control back while the pane is still booting.
    assert not future.done()
    assert pane.sent == []

    gate.set()
    future.result(timeout=5.0)
    assert pane.sent == ["one", "two"]


def test_deliver_keeps_order_across_calls(gated_wait: Any) -> None:
    from xontrib_tmux.sender import deliver  # type: ignore

    gate, calls = gated_wait
    pane = FakePane("%deliver-order")

    first = deliver(pane, ["a"])
    second = deliver(pane, ["b", "c"])
    gate.set()

    first.result(timeout=5.0)
    second.result(timeout=5.0)
    assert pane.sent == ["a", "b", "c"]


def test_readiness_awaited_only_once_per_pane(gated_wait: Any) -> None:
    from xontrib_tmux.sender import deliver  # type: ignore

    gate, calls = gated_wait
    gate.set()
    pane = FakePane("%deliver-once")

    deliver(pane, ["a"]).result(timeout=5.0)
    # A pane mid-command is back in canonical mode; don't stall on it again.
    deliver(pane, ["b"]).result(timeout=5.0)

    assert len(calls) == 1
    assert pane.sent == ["a", "b"]


def test_failures_surface_on_the_future(gated_wait: Any) -> None:
    from xontrib_tmux.sender import deliver  # type: ignore

    gate, calls = gated_wait
    gate.set()

    class Broken(FakePane):
        def send_keys(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("pane is gone")

    future = deliver(Broken("%deliver-broken"), ["a"])
    with pytest.raises(RuntimeError, match="pane is gone"):
        future.result(timeout=5.0)


def test_macro_enter_returns_before_keys_are_sent(gated_wait: Any) -> None:
    from xontrib_tmux.block import PASTE_END, PASTE_START, TmuxMacro  # type: ignore

    gate, calls = gated_wait
    pane = FakePane("%macro-async")

    macro = TmuxMacro(pane)
    macro.macro_block = "ls\necho hi\n"
    with macro:
        assert pane.sent == []
    # macro_block is gone by now; the payload must have been snapshotted.
    gate.set()
    macro.future.result(timeout=5.0)
    assert pane.sent == [
        f"{PASTE_START}ls{PASTE_END}",
        f"{PASTE_START}echo hi{PASTE_END}",
    ]


def test_macro_nested_indented_block_sends_trailing_paste_end(
    gated_wait: Any,
) -> None:
    from xontrib_tmux.block import PASTE_END, PASTE_START, TmuxMacro  # type: ignore

    gate, _ = gated_wait
    pane = FakePane("%macro-nested")

    macro = TmuxMacro(pane)
    macro.macro_block = (
        "# Open the issue details\n"
        "gh issue view @(issue)\n"
        "# Open claude in a separate panel\n"
        "with! tmuxp().send(capture=['issue']):\n"
        '    claude -c @(f"Diagnose GitHub issue {issue}")\n'
    )
    with macro:
        pass
    gate.set()
    macro.future.result(timeout=5.0)
    assert pane.sent == [
        f"{PASTE_START}# Open the issue details{PASTE_END}",
        f"{PASTE_START}gh issue view @(issue){PASTE_END}",
        f"{PASTE_START}# Open claude in a separate panel{PASTE_END}",
        f"{PASTE_START}with! tmuxp().send(capture=['issue']):",
        '    claude -c @(f"Diagnose GitHub issue {issue}")',
        PASTE_END,
    ]


def test_macro_compound_block_with_else_and_following_stmt(
    gated_wait: Any,
) -> None:
    from xontrib_tmux.block import PASTE_END, PASTE_START, TmuxMacro  # type: ignore

    gate, _ = gated_wait
    pane = FakePane("%macro-compound")

    macro = TmuxMacro(pane)
    macro.macro_block = (
        "if cond:\n"
        "    a = 1\n"
        "\n"
        "    b = 2\n"
        "else:\n"
        "    a = 3\n"
        "\n"
        "echo done\n"
    )
    with macro:
        pass
    gate.set()
    macro.future.result(timeout=5.0)
    assert pane.sent == [
        f"{PASTE_START}if cond:",
        "    a = 1",
        "",
        "    b = 2",
        "else:",
        "    a = 3",
        PASTE_END,
        f"{PASTE_START}{PASTE_END}",
        f"{PASTE_START}echo done{PASTE_END}",
    ]


def test_macro_multiline_string_with_col0_lines(
    gated_wait: Any,
) -> None:
    from xontrib_tmux.block import PASTE_END, PASTE_START, TmuxMacro  # type: ignore

    gate, _ = gated_wait
    pane = FakePane("%macro-col0-str")

    macro = TmuxMacro(pane)
    macro.macro_block = (
        'msg = """first line\n'
        "\n"
        'second line at col 0"""\n'
        "echo @(msg)\n"
    )
    with macro:
        pass
    gate.set()
    macro.future.result(timeout=5.0)
    assert pane.sent == [
        f'{PASTE_START}msg = """first line',
        "",
        'second line at col 0"""',
        PASTE_END,
        f"{PASTE_START}echo @(msg){PASTE_END}",
    ]


def test_deliver_waits_for_raw_tty_between_commands(
    gated_wait: Any, monkeypatch: Any
) -> None:
    from xontrib_tmux import sender  # type: ignore
    from xontrib_tmux.block import PASTE_END, PASTE_START  # type: ignore

    gate, _ = gated_wait
    gate.set()

    pane = FakePane("%deliver-between")
    pane.pane_tty = "/dev/pts/fake"

    # Simulate tty going cooked after line 1 is sent, then returning to raw.
    states = iter([False, False, True])
    polled: List[str] = []

    def fake_tty_is_raw(tty: str) -> bool:
        polled.append(tty)
        return next(states, True)

    monkeypatch.setattr(sender, "tty_is_raw", fake_tty_is_raw)

    sender.deliver(
        pane,
        [
            f"{PASTE_START}cmd1{PASTE_END}",
            f"{PASTE_START}cmd2{PASTE_END}",
        ],
    ).result(timeout=5.0)

    assert pane.sent == [
        f"{PASTE_START}cmd1{PASTE_END}",
        f"{PASTE_START}cmd2{PASTE_END}",
    ]
    assert len(polled) >= 2
