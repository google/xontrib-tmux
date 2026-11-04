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


def test_fstrings_parse_inside_with_bang_blocks(load_xontrib: Any) -> None:
    load_xontrib("tmux")
    from xonsh.execer import Execer  # type: ignore

    execer = Execer()
    code = (
        "@aliases.register\n"
        "def _triage(args):\n"
        "    issue = args[0]\n"
        "    with! tmuxs(f'gh-{issue}').send(capture=['issue']):\n"
        "        prompt = f'Diagnose GitHub issue {issue!r} {issue:s}'\n"
        "        gh issue view @(issue)\n"
        "        with! tmuxp().send(capture=['issue']):\n"
        "            f'leading fstring {issue=}'\n"
        "            claude -c @(f'Diagnose GitHub issue {issue}')\n"
    )
    tree = execer.parse(code, ctx={})
    assert tree is not None

    with pytest.raises(SyntaxError):
        execer.parse("with! p.send():\n    x = f'unterminated\n", ctx={})


def test_parser_patch_hook_removes_itself_and_preserves_loader(
    load_xontrib: Any,
) -> None:
    import inspect
    import sys
    import xonsh.parser_table as pt  # type: ignore
    from xontrib_tmux.parser_patch import _ParserTableImportHook  # type: ignore

    load_xontrib("tmux")
    assert not any(isinstance(f, _ParserTableImportHook) for f in sys.meta_path)
    assert pt.__spec__ is not None
    assert pt.__spec__.loader is not None
    assert hasattr(pt.__spec__.loader, "get_source")
    assert inspect.getsourcefile(pt) is not None


def test_captured_rc_src_suppresses_shell_history(
    load_xontrib: Any, monkeypatch: Any, tmp_path: Any
) -> None:
    import dill  # type: ignore
    from xonsh.history.dummy import DummyHistory  # type: ignore
    from xontrib_tmux.block import TmuxMacro, is_pending_capture_cmd  # type: ignore

    load_xontrib("tmux")

    class FakePtkHistory:
        def __init__(self) -> None:
            self._loaded_strings: List[str] = []

        def append_string(self, s: str) -> None:
            self._loaded_strings.insert(0, s)

    hist = DummyHistory()
    hist.inps = []

    def record_append(cmd: Any) -> None:
        if not hist.is_ignored(cmd):
            hist.inps.append(cmd["inp"].rstrip())

    monkeypatch.setattr(hist, "append", record_append)

    ptk_hist = FakePtkHistory()
    mock_xsh = MagicMock()
    mock_xsh.env = {"LAST_RETURN_CODE": 42}
    mock_xsh.history = hist
    mock_xsh.shell.shell.history = ptk_hist
    monkeypatch.setattr(builtins, "__xonsh__", mock_xsh, raising=False)

    post_fired: List[Any] = []
    mock_events = MagicMock()
    mock_events.on_postcommand.fire = lambda **kw: post_fired.append(kw)
    monkeypatch.setattr(builtins, "events", mock_events, raising=False)

    rc_file = tmp_path / "captured_abc123.xsh"
    macro = TmuxMacro(MagicMock(), captured={"issue": dill.dumps("42")})
    rc_bytes = macro.rc_src(rc_path=str(rc_file))
    assert rc_bytes is not None
    rc_file.write_bytes(rc_bytes)

    cmd_str = f"source {rc_file}"
    assert is_pending_capture_cmd(cmd_str)

    # 1. Plugin delivery execution: suppressed from history and postcommand,
    # preserves LAST_RETURN_CODE, and keeps the file on disk marked delivered.
    ptk_hist.append_string("echo before")
    ptk_hist.append_string(cmd_str)

    ns: dict[str, Any] = {}
    exec(rc_file.read_text(encoding="utf-8"), ns)
    assert ns["issue"] == "42"
    assert rc_file.exists()
    assert not is_pending_capture_cmd(cmd_str)

    mock_xsh.env["LAST_RETURN_CODE"] = 0
    hist.append({"inp": cmd_str + "\n", "rtn": 0})
    mock_events.on_postcommand.fire(cmd=cmd_str)

    assert mock_xsh.env["LAST_RETURN_CODE"] == 42
    assert post_fired == []
    assert hist.inps == []
    assert ptk_hist._loaded_strings == ["echo before"]

    # 2. If the user manually re-runs the same file later, it IS recorded.
    ptk_hist.append_string(cmd_str)
    exec(rc_file.read_text(encoding="utf-8"), {})
    hist.append({"inp": cmd_str + "\n", "rtn": 0})
    mock_events.on_postcommand.fire(cmd=cmd_str)

    assert hist.inps == [cmd_str]
    assert ptk_hist._loaded_strings == [cmd_str, "echo before"]
    assert len(post_fired) == 1


def test_autoload_disabled_skips_import_hook(monkeypatch: Any) -> None:
    import sys
    from xontrib_tmux import parser_patch  # type: ignore

    parser_patch._remove_hook()
    monkeypatch.setenv("XONTRIBS_AUTOLOAD_DISABLED", "tmux")
    monkeypatch.delitem(sys.modules, "xonsh.parser_table", raising=False)

    parser_patch.install_parser_patch_hook()
    assert not any(
        isinstance(f, parser_patch._ParserTableImportHook)
        for f in sys.meta_path
    )


def test_precommand_skips_pending_capture_and_unload_disconnects(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import dill  # type: ignore
    import xontrib_tmux  # type: ignore
    from xontrib_tmux.block import TmuxMacro  # type: ignore

    fake_window = MagicMock()
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: fake_window)

    pre_handlers: set = set()
    post_handlers: set = set()

    class FakeEvent(set):
        def __call__(self, fn: Any) -> Any:
            self.add(fn)
            return fn

    mock_events = MagicMock()
    mock_events.on_precommand = FakeEvent()
    mock_events.on_postcommand = FakeEvent()
    monkeypatch.setattr(builtins, "events", mock_events, raising=False)

    mock_xsh = MagicMock()
    mock_xsh.ctx = {}
    xontrib_tmux._load_xontrib_(mock_xsh)
    assert len(mock_events.on_precommand) == 1
    assert len(mock_events.on_postcommand) == 1

    rc_file = tmp_path / "captured_win.xsh"
    macro = TmuxMacro(MagicMock(), captured={"x": dill.dumps(1)})
    rc_bytes = macro.rc_src(rc_path=str(rc_file))
    assert rc_bytes is not None
    rc_file.write_bytes(rc_bytes)

    fake_window.rename_window.reset_mock()
    for handler in list(mock_events.on_precommand):
        handler(cmd=f"source {rc_file}")
    # Pending plugin capture script must not rename window to "source"
    fake_window.rename_window.assert_not_called()

    for handler in list(mock_events.on_precommand):
        handler(cmd="vim file.txt")
    fake_window.rename_window.assert_called_once_with("vim")

    xontrib_tmux._unload_xontrib_(mock_xsh)
    assert len(mock_events.on_precommand) == 0
    assert len(mock_events.on_postcommand) == 0
