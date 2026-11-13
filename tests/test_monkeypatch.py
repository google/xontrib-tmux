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

"""Hermetic unit tests for ``monkeypatch.py`` and xontrib hooks."""

import builtins
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import libtmux  # type: ignore
import pytest

import xontrib_tmux
from xontrib_tmux import commands, monkeypatch as xt_monkeypatch
from xontrib_tmux.peer import PeerMacro
from xontrib_tmux.protocol import decode_captured


class _FakePeer:
    """Minimal stand-in recording calls to ``_send_from_caller``."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, dict[str, str] | None, bool]] = []

    def _send_from_caller(
        self,
        block: str | None = None,
        /,
        captured: dict[str, str] | None = None,
        record: bool = True,
    ) -> Any:
        self.calls.append((block, captured, record))
        if block is None:
            return PeerMacro(  # type: ignore[arg-type]
                self, record=record, captured=captured
            )
        return SimpleNamespace(
            block=block, captured=captured, record=record
        )


def _make_tmux_objects(
    monkeypatch: pytest.MonkeyPatch, peer: Any
) -> tuple[Any, libtmux.Session, libtmux.Window, libtmux.Pane]:
    """Builds fake-backed libtmux Server/Session/Window/Pane instances."""
    server = SimpleNamespace(socket_name=None, socket_path=None)
    pane = libtmux.Pane(server=server, pane_id="%1")
    window = libtmux.Window(server=server, window_id="@1")
    session = libtmux.Session(server=server, session_id="$1")

    monkeypatch.setattr(
        libtmux.Pane, "xonsh", property(lambda self: peer)
    )
    monkeypatch.setattr(
        libtmux.Window, "active_pane", property(lambda self: pane)
    )
    monkeypatch.setattr(
        libtmux.Session, "active_pane", property(lambda self: pane)
    )
    return server, session, window, pane


def test_pane_xonsh_property_delegates_to_get_pane_xonsh() -> None:
    """``Pane.xonsh`` is a property wrapping ``commands.get_pane_xonsh``."""
    assert isinstance(libtmux.Pane.xonsh, property)
    assert libtmux.Pane.xonsh.fget is commands.get_pane_xonsh


def test_send_routes_pane_window_session_and_forwards_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``pane.send``, ``window.send``, and ``session.send`` delegate cleanly."""
    fake_peer = _FakePeer()
    _srv, session, window, pane = _make_tmux_objects(monkeypatch, fake_peer)

    local_item = {"k": 42}
    res_p = pane.send("echo p", capture=["local_item"], record=False)
    res_w = window.send("echo w")
    res_s = session.send("echo s", record=False)
    macro = session.send(capture=["local_item"])

    assert res_p.block == "echo p"
    assert res_w.block == "echo w"
    assert res_s.block == "echo s"
    assert isinstance(macro, PeerMacro)

    assert len(fake_peer.calls) == 4
    assert fake_peer.calls[0][0] == "echo p"
    assert fake_peer.calls[0][2] is False
    assert decode_captured(fake_peer.calls[0][1] or {}) == {
        "local_item": {"k": 42}
    }
    assert fake_peer.calls[1] == ("echo w", None, True)
    assert fake_peer.calls[2] == ("echo s", None, False)
    assert fake_peer.calls[3][0] is None
    assert decode_captured(fake_peer.calls[3][1] or {}) == {
        "local_item": {"k": 42}
    }


def test_send_raises_when_pane_or_peer_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sending to a pane/window/session without a peer raises RuntimeError."""
    _srv, session, window, pane = _make_tmux_objects(monkeypatch, None)
    for target in (pane, window, session):
        with pytest.raises(RuntimeError, match="no peer xonsh"):
            target.send("echo nope")

    monkeypatch.setattr(
        libtmux.Session, "active_pane", property(lambda self: None)
    )
    with pytest.raises(RuntimeError, match="no peer xonsh"):
        session.send("echo no-active-pane")


def test_send_attach_monkeypatch_remains_intact() -> None:
    """``monkeypatch`` exposes ``_send`` and preserves ``Session.attach``."""
    assert libtmux.Pane.send is xt_monkeypatch._send
    assert libtmux.Window.send is xt_monkeypatch._send
    assert libtmux.Session.send is xt_monkeypatch._send
    assert libtmux.Session.attach is xt_monkeypatch._attach


def test_load_xontrib_exports_and_unloads_context_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_load_xontrib_`` exports ``RemoteXonsh``, ``xsess``, etc."""
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: None)
    mock_xsh = SimpleNamespace(ctx={})

    xontrib_tmux._load_xontrib_(mock_xsh)
    assert mock_xsh.ctx["RemoteXonsh"] is xontrib_tmux.RemoteXonsh
    assert mock_xsh.ctx["xsess"] is commands.xsess
    assert mock_xsh.ctx["xwin"] is commands.xwin
    assert mock_xsh.ctx["xpane"] is commands.xpane

    xontrib_tmux._unload_xontrib_(mock_xsh)
    for name in ("RemoteXonsh", "xsess", "xwin", "xpane"):
        assert name not in mock_xsh.ctx


def test_window_renaming_skips_hidden_peer_and_uses_first_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden peers skip renaming; normal shells rename once per block."""

    class _FakeEvent(set):
        def __call__(self, fn: Any) -> Any:
            self.add(fn)
            return fn

    fake_events = SimpleNamespace(
        on_precommand=_FakeEvent(),
        on_postcommand=_FakeEvent(),
    )
    monkeypatch.setattr(builtins, "events", fake_events, raising=False)
    mock_xsh = SimpleNamespace(ctx={})

    # 1. Hidden peer via curtmuxw() returning None (not controlling tty).
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: None)
    xontrib_tmux._load_xontrib_(mock_xsh)
    assert len(fake_events.on_precommand) == 0
    assert len(fake_events.on_postcommand) == 0
    xontrib_tmux._unload_xontrib_(mock_xsh)

    # 2. Hidden peer flag skips renaming even if curtmuxw() is mocked.
    fake_win = MagicMock()
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: fake_win)
    monkeypatch.setattr(
        xontrib_tmux.parser_patch, "_is_hidden_peer", True, raising=False
    )
    xontrib_tmux._load_xontrib_(mock_xsh)
    fake_win.rename_window.assert_not_called()
    assert len(fake_events.on_precommand) == 0
    xontrib_tmux._unload_xontrib_(mock_xsh)

    # 3. Normal inside-tmux shell renames once using first non-comment token.
    monkeypatch.setattr(
        xontrib_tmux.parser_patch, "_is_hidden_peer", False, raising=False
    )
    xontrib_tmux._load_xontrib_(mock_xsh)
    fake_win.rename_window.assert_called_once_with("xonsh")
    assert len(fake_events.on_precommand) == 1
    assert len(fake_events.on_postcommand) == 1

    fake_win.rename_window.reset_mock()
    pre = next(iter(fake_events.on_precommand))
    post = next(iter(fake_events.on_postcommand))

    pre(cmd="# comment\n\n  pytest tests/test_peer.py\necho second\n")
    fake_win.rename_window.assert_called_once_with("pytest")

    fake_win.rename_window.reset_mock()
    pre(cmd="# comment only\n   \n")
    fake_win.rename_window.assert_not_called()

    post()
    fake_win.rename_window.assert_called_once_with("xonsh")

    xontrib_tmux._unload_xontrib_(mock_xsh)
    assert len(fake_events.on_precommand) == 0
    assert len(fake_events.on_postcommand) == 0
