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
import pathlib
import shlex
import signal
import socket
import sys
import time
from types import SimpleNamespace
from typing import Any

import libtmux  # type: ignore
from libtmux.constants import PaneDirection, WindowDirection  # type: ignore
import pytest

from xontrib_tmux import commands
from xontrib_tmux.peer import RemoteXonsh


def _reset_pane_cache() -> None:
    with commands._pane_peers_lock:
        commands._pane_peers.clear()


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

    # Case 2: hidden peer has its own pty (rdev != pane_tty st_rdev).
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


def test_current_server_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """_current_server resolves unset, live, dead, and malformed $TMUX."""
    default_srv = object()
    env_srv = SimpleNamespace(is_alive=lambda: True)
    dead_srv = SimpleNamespace(is_alive=lambda: False)

    class _FakeServerCls:
        def __new__(cls) -> Any:
            return default_srv

        @staticmethod
        def from_env() -> Any:
            return env_srv

    monkeypatch.setattr(libtmux, "Server", _FakeServerCls)
    monkeypatch.delenv("TMUX", raising=False)
    assert commands._current_server() is default_srv

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/priv,1,0")
    assert commands._current_server() is env_srv

    # Dead server via is_alive() == False raises RuntimeError advising clear.
    monkeypatch.setattr(
        _FakeServerCls, "from_env", staticmethod(lambda: dead_srv)
    )
    with pytest.raises(RuntimeError, match="TMUX"):
        commands._current_server()

    # Dead server via LibTmuxException also raises RuntimeError advising clear.
    def _raise_libtmux() -> Any:
        raise libtmux.exc.LibTmuxException("no server")

    monkeypatch.setattr(
        _FakeServerCls, "from_env", staticmethod(_raise_libtmux)
    )
    with pytest.raises(RuntimeError, match="TMUX"):
        commands._current_server()

    # Malformed $TMUX values raise RuntimeError advising clearing TMUX.
    for bad in ("", "no-commas", "/tmp/sock,abc,0", ",1,0"):
        monkeypatch.setenv("TMUX", bad)
        with pytest.raises(RuntimeError, match="TMUX"):
            commands._current_server()


def test_xsess_xwin_xpane_argument_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checks forbidden/unknown kwargs, mutually exclusive args, outside."""
    for kw in (
        "shell",
        "window_shell",
        "window_command",
        "window_name",
        "session_name",
    ):
        with pytest.raises(TypeError, match=kw):
            commands.xsess("s", **{kw: "bash"})
        with pytest.raises(TypeError, match=kw):
            commands.xwin("w", session=SimpleNamespace(), **{kw: "bash"})
        with pytest.raises(TypeError, match=kw):
            commands.xpane(pane=SimpleNamespace(), **{kw: "bash"})

    with pytest.raises(TypeError, match="attach"):
        commands.xsess("s", attach=True)

    with pytest.raises(TypeError, match="unexpected keyword"):
        commands.xsess("s", bogus=1)
    with pytest.raises(TypeError, match="unexpected keyword"):
        commands.xwin("w", session=SimpleNamespace(), bogus=1)
    with pytest.raises(TypeError, match="unexpected keyword"):
        commands.xpane(pane=SimpleNamespace(), bogus=1)

    with pytest.raises(ValueError, match="size and percentage"):
        commands.xpane(pane=SimpleNamespace(), size=10, percentage=50)

    monkeypatch.setattr(commands, "is_inside_tmux", lambda: False)
    with pytest.raises(libtmux.exc.NotInsideTmux):
        commands.xwin()
    with pytest.raises(libtmux.exc.NotInsideTmux):
        commands.xpane()


def test_xsess_create_and_existing_session_behavior(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Tests atomic create-and-note in xsess and existing session checks."""
    sock_target = tmp_path / "p_1_0.sock"
    monkeypatch.setattr(commands, "new_socket_path", lambda p="p": sock_target)
    monkeypatch.setattr(commands.time, "time", lambda: 1700000000.5)

    recorded_cmds: list[tuple[Any, dict[str, Any]]] = []
    tmux_env_during_cmd: list[tuple[str | None, str | None]] = []
    created_session = SimpleNamespace(session_id="$5", session_name="work")

    class _FakeServer:
        socket_path = "/tmp/tmux-1000/priv"
        socket_name = None

        def __init__(self) -> None:
            self._sessions: dict[str, Any] = {}
            self.sessions = SimpleNamespace(
                get=lambda session_name, default=None: self._sessions.get(
                    session_name, default
                )
            )

        def has_session(self, name: str) -> bool:
            return name in self._sessions

        def cmd(self, cmd: str, *args: Any, **kwargs: Any) -> Any:
            recorded_cmds.append(((cmd, *args), kwargs))
            tmux_env_during_cmd.append(
                (os.environ.get("TMUX"), os.environ.get("TMUX_PANE"))
            )
            return SimpleNamespace(stderr=[], stdout=["$5"])

    fake_srv = _FakeServer()
    monkeypatch.setattr(commands, "_current_server", lambda: fake_srv)
    monkeypatch.setattr(
        libtmux.Session,
        "from_session_id",
        lambda server, session_id: created_session,
    )
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/priv,1,0")
    monkeypatch.setenv("TMUX_PANE", "%0")

    res = commands.xsess(
        "work",
        start_directory=str(tmp_path),
        x=120,
        y=30,
        environment={"FOO": "bar"},
    )
    assert res is created_session
    assert os.environ["TMUX"] == "/tmp/tmux-1000/priv,1,0"
    assert os.environ["TMUX_PANE"] == "%0"
    assert tmux_env_during_cmd == [(None, None)]

    args = recorded_cmds[0][0]
    assert args[:4] == ("new-session", "-d", "-P", "-F#{session_id}")
    assert "-swork" in args
    c_idx = args.index("-c")
    assert args[c_idx + 1] == str(tmp_path)
    assert "-eFOO=bar" in args
    expected_cmd = (
        f"env _XONTRIB_TMUX_PEER_SOCKET={shlex.quote(str(sock_target))} "
        f"{shlex.quote(sys.executable)} -m xonsh -i"
    )
    semi_idx = args.index(";")
    assert args[semi_idx - 1] == expected_cmd
    assert args[semi_idx:] == (
        ";",
        "set-option",
        "-p",
        "-t",
        "=work:",
        "-F",
        "@xontrib_tmux_peer",
        f"{sock_target}|1700000000.5|#{{pane_pid}}",
    )

    # Unnamed xsess() also pops TMUX/TMUX_PANE and runs as a single command.
    recorded_cmds.clear()
    tmux_env_during_cmd.clear()
    assert commands.xsess() is created_session
    assert len(recorded_cmds) == 1
    assert os.environ["TMUX"] == "/tmp/tmux-1000/priv,1,0"
    assert os.environ["TMUX_PANE"] == "%0"
    assert tmux_env_during_cmd == [(None, None)]
    unnamed_args = recorded_cmds[0][0]
    assert unnamed_args[:4] == ("new-session", "-d", "-P", "-F#{session_id}")
    unnamed_semi = unnamed_args.index(";")
    assert unnamed_args[unnamed_semi:] == (
        ";",
        "set-option",
        "-p",
        "-F",
        "@xontrib_tmux_peer",
        f"{sock_target}|1700000000.5|#{{pane_pid}}",
    )

    # Existing session with peer active pane returns existing Session.
    existing_pane = SimpleNamespace(pane_id="%10")
    existing_sess = SimpleNamespace(
        session_id="$6", session_name="live", active_pane=existing_pane
    )
    fake_srv._sessions["live"] = existing_sess
    monkeypatch.setattr(commands, "get_pane_xonsh", lambda p: object())
    assert commands.xsess("live", x=80) is existing_sess

    # Existing session without peer active pane raises RuntimeError.
    monkeypatch.setattr(commands, "get_pane_xonsh", lambda p: None)
    with pytest.raises(RuntimeError, match="active pane has no peer xonsh"):
        commands.xsess("live")


def test_xwin_and_xpane_command_construction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Checks chained set-option and last-window/last-pane focus retention."""
    sock_target = tmp_path / "p_2_0.sock"
    monkeypatch.setattr(commands, "new_socket_path", lambda p="p": sock_target)
    monkeypatch.setattr(commands.time, "time", lambda: 1700000100.0)

    recorded_cmds: list[tuple[Any, ...]] = []

    class _FakeServer:
        tmux_bin = None

        def cmd(self, cmd: str, *args: Any, **kwargs: Any) -> Any:
            recorded_cmds.append((cmd, *args))
            if cmd == "new-window":
                return SimpleNamespace(stderr=[], stdout=["@9"])
            return SimpleNamespace(stderr=[], stdout=["%19"])

    fake_srv = _FakeServer()
    sess = SimpleNamespace(server=fake_srv, session_id="$3")
    win_obj = SimpleNamespace(window_id="@9")
    monkeypatch.setattr(
        libtmux.Window,
        "from_window_id",
        lambda server, window_id: win_obj,
    )

    # xwin default attach=False omits -d and appends last-window.
    out_win = commands.xwin(
        "logs", session=sess, direction=WindowDirection.After
    )
    assert out_win is win_obj
    win_cmd = recorded_cmds[-1]
    assert "-d" not in win_cmd
    assert win_cmd[:4] == ("new-window", "-P", "-F#{window_id}", "-n")
    assert "logs" in win_cmd
    assert "-a" in win_cmd
    assert win_cmd[-10:] == (
        ";",
        "set-option",
        "-p",
        "-F",
        "@xontrib_tmux_peer",
        f"{sock_target}|1700000100.0|#{{pane_pid}}",
        ";",
        "last-window",
        "-t",
        "$3",
    )

    # xwin with attach=True omits last-window.
    commands.xwin("fg", session=sess, attach=True)
    win_fg_cmd = recorded_cmds[-1]
    assert "last-window" not in win_fg_cmd

    # xpane default attach=False omits -d and appends last-pane.
    target_pane = SimpleNamespace(
        server=fake_srv, pane_id="%4", window_id="@2"
    )
    new_pane = SimpleNamespace(pane_id="%19")
    monkeypatch.setattr(
        libtmux.Pane,
        "from_pane_id",
        lambda server, pane_id: new_pane,
    )
    out_pane = commands.xpane(
        target_pane, direction=PaneDirection.Right, percentage=30
    )
    assert out_pane is new_pane
    pane_cmd = recorded_cmds[-1]
    assert "-d" not in pane_cmd
    assert pane_cmd[:4] == ("split-window", "-h", "-p30", "-P")
    assert "-t" in pane_cmd
    assert pane_cmd[pane_cmd.index("-t") + 1] == "%4"
    assert pane_cmd[-10:] == (
        ";",
        "set-option",
        "-p",
        "-F",
        "@xontrib_tmux_peer",
        f"{sock_target}|1700000100.0|#{{pane_pid}}",
        ";",
        "last-pane",
        "-t",
        "@2",
    )

    # xpane when inside tmux uses Pane.from_env().
    monkeypatch.setattr(commands, "is_inside_tmux", lambda: True)
    monkeypatch.setattr(libtmux.Pane, "from_env", lambda: target_pane)
    commands.xpane(attach=True)
    pane_fg_cmd = recorded_cmds[-1]
    assert "last-pane" not in pane_fg_cmd


def test_get_pane_xonsh_lifecycle_and_caching(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Tests starting, listening, caching, kill-reconnect, and dead cleanup."""
    _reset_pane_cache()
    try:
        sock_path = tmp_path / "peer_a.sock"
        err_path = pathlib.Path(f"{sock_path}.err")
        option_val = f"{sock_path}|{time.time()}|424242"

        class _FakePane:
            def __init__(self) -> None:
                self.pane_id = "%11"
                self.server = SimpleNamespace(
                    socket_path="/tmp/tmux-1000/test", socket_name=None
                )
                self.option: str | None = option_val
                self.unset_calls = 0

            def cmd(self, cmd: str, *args: Any, **kwargs: Any) -> Any:
                if cmd == "display-message":
                    val = self.option or ""
                    return SimpleNamespace(
                        stderr=[], stdout=[f"{val}\t424242"]
                    )
                if cmd == "set-option" and "-u" in args:
                    self.unset_calls += 1
                    self.option = None
                    return SimpleNamespace(stderr=[], stdout=[])
                raise AssertionError(f"Unexpected cmd: {cmd} {args}")

        pane = _FakePane()

        # 1. Empty option -> None.
        pane.option = ""
        assert commands.get_pane_xonsh(pane) is None

        # 2. Starting (no socket file yet, within timeout, pid alive) -> cached.
        pane.option = option_val
        alive_flag = True

        def fake_kill(pid: int, sig: int) -> None:
            if sig == 0:
                if not alive_flag:
                    raise ProcessLookupError
                return
            killed_signals.append((pid, sig))

        killed_signals: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", fake_kill)
        monkeypatch.setattr(
            os, "killpg", lambda pgid, sig: killed_signals.append((pgid, sig))
        )

        rx1 = commands.get_pane_xonsh(pane)
        assert isinstance(rx1, RemoteXonsh)
        rx2 = commands.get_pane_xonsh(pane)
        assert rx2 is rx1

        # 3. Real listening socket: kill() when still alive causes reconnect.
        srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv_sock.bind(str(sock_path))
        srv_sock.listen(1)
        try:
            rx1.kill()
            assert (424242, signal.SIGKILL) in killed_signals
            # Re-bind socket to simulate process still alive after handle kill.
            srv_sock.close()
            srv_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv_sock.bind(str(sock_path))
            srv_sock.listen(1)

            rx3 = commands.get_pane_xonsh(pane)
            assert rx3 is not None
            assert rx3 is not rx1
        finally:
            srv_sock.close()

        # 4. ConnectionRefusedError on existing socket -> cleanup + None.
        assert sock_path.exists()
        assert commands.get_pane_xonsh(pane) is None
        assert not sock_path.exists()
        assert pane.unset_calls == 1

        # 5. .err sentinel present -> cleanup + None.
        pane.option = option_val
        sock_path.write_text("", encoding="utf-8")
        err_path.write_text("failed boot", encoding="utf-8")
        assert commands.get_pane_xonsh(pane) is None
        assert not sock_path.exists()
        assert not err_path.exists()
        assert pane.unset_calls == 2

        # 6. ProcessLookupError (dead PID) -> cleanup + None.
        pane.option = option_val
        alive_flag = False
        sock_path.write_text("", encoding="utf-8")
        assert commands.get_pane_xonsh(pane) is None
        assert not sock_path.exists()
        assert pane.unset_calls == 3

        # 7. Start timeout expired without socket -> cleanup + None.
        alive_flag = True
        pane.option = f"{sock_path}|1.0|424242"
        assert commands.get_pane_xonsh(pane) is None
        assert pane.unset_calls == 4
    finally:
        _reset_pane_cache()
