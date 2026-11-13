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
import importlib
from pathlib import Path
import subprocess
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import libtmux  # type: ignore
import pytest


def _stub_default_server() -> SimpleNamespace:
    return SimpleNamespace(
        socket_name=None,
        socket_path=None,
        config_file=None,
        colors=None,
    )


def test_it_loads(load_xontrib: Any) -> None:
    load_xontrib("tmux")


def test_session_has_attach_monkeypatch(load_xontrib: Any) -> None:
    load_xontrib("tmux")
    import xontrib_tmux.monkeypatch as mp  # type: ignore
    assert libtmux.Session.attach == mp._attach


def test_attach_xonsh_subproc(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    session = libtmux.Session(server=_stub_default_server(), session_id="$42")

    recorded_cmds: list[Any] = []

    mock_xsh = MagicMock()
    mock_xsh.env = {"FOO": "bar"}
    mock_xsh.subproc_uncaptured = lambda cmd: recorded_cmds.append(cmd)

    monkeypatch.setattr(builtins, "__xonsh__", mock_xsh, raising=False)

    result = session.attach()
    assert result is session
    assert recorded_cmds == [["tmux", "attach", "-t", "$42"]]


def test_attach_outside_xonsh(load_xontrib: Any, monkeypatch: Any) -> None:
    load_xontrib("tmux")
    session = libtmux.Session(server=_stub_default_server(), session_id="$99")

    recorded_cmds: list[Any] = []

    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd: recorded_cmds.append(cmd)
    )

    result = session.attach()
    assert result is session
    assert recorded_cmds == [["tmux", "attach", "-t", "$99"]]


def test_attach_server_options_and_flags(
    load_xontrib: Any, monkeypatch: Any
) -> None:
    load_xontrib("tmux")
    server = libtmux.Server(
        socket_name="custom_socket",
        config_file="/tmp/tmux.conf",
        colors=256,
    )
    session = libtmux.Session(server=server, session_id="$100")

    recorded_cmds: list[Any] = []
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd: recorded_cmds.append(cmd)
    )

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


def test_attach_server_socket_path(
    load_xontrib: Any, monkeypatch: Any
) -> None:
    load_xontrib("tmux")
    server = libtmux.Server(socket_path="/tmp/tmux_custom")
    session = libtmux.Session(server=server, session_id="$101")

    recorded_cmds: list[Any] = []
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd: recorded_cmds.append(cmd)
    )

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
    session = libtmux.Session(server=_stub_default_server())
    session.session_id = None
    session.session_name = None

    with pytest.raises(ValueError, match="Session has no session_id"):
        session.attach()


def test_fstrings_parse_inside_with_bang_blocks(load_xontrib: Any) -> None:
    load_xontrib("tmux")
    from xonsh.execer import Execer  # type: ignore

    execer = Execer()
    code = (
        "@aliases.register\n"
        "def _triage(args):\n"
        "    issue = args[0]\n"
        "    with! xsess(f'gh-{issue}').send(capture=['issue']):\n"
        "        prompt = f'Diagnose GitHub issue {issue!r} {issue:s}'\n"
        "        gh issue view @(issue)\n"
        "        with! xpane().send(capture=['issue']):\n"
        "            f'leading fstring {issue=}'\n"
        "            agy -p @(f'Diagnose GitHub issue {issue}')\n"
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
    assert not any(
        isinstance(f, _ParserTableImportHook) for f in sys.meta_path
    )
    assert pt.__spec__ is not None
    assert pt.__spec__.loader is not None
    assert hasattr(pt.__spec__.loader, "get_source")
    assert inspect.getsourcefile(pt) is not None


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


def test_load_exports_new_symbols_and_drops_legacy(
    monkeypatch: Any,
) -> None:
    import xontrib_tmux  # type: ignore

    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: None)
    mock_xsh = MagicMock()
    mock_xsh.ctx = {}
    xontrib_tmux._load_xontrib_(mock_xsh)

    for name in ("RemoteXonsh", "xsess", "xwin", "xpane"):
        assert name in mock_xsh.ctx
    for legacy in ("tmuxs", "tmuxw", "tmuxp"):
        assert legacy not in mock_xsh.ctx

    xontrib_tmux._unload_xontrib_(mock_xsh)
    for name in ("RemoteXonsh", "xsess", "xwin", "xpane"):
        assert name not in mock_xsh.ctx


def test_monkeypatch_wires_pane_text_and_send_surfaces() -> None:
    import xontrib_tmux  # type: ignore
    from xontrib_tmux.pane_text import pane_text  # type: ignore

    assert xontrib_tmux is not None
    assert libtmux.Pane.text is pane_text
    assert isinstance(libtmux.Pane.xonsh, property)
    assert callable(libtmux.Pane.send)
    assert callable(libtmux.Window.send)
    assert callable(libtmux.Session.send)


@pytest.mark.parametrize(
    "module_name",
    ["xontrib_tmux.ready", "xontrib_tmux.sender", "xontrib_tmux.block"],
)
def test_legacy_modules_removed(module_name: str) -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)


def test_pyproject_requires_xonsh_0_24_0_and_readme_docs() -> None:
    root = Path(__file__).resolve().parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")

    assert '"xonsh>=0.24.0"' in pyproject
    for needle in (
        "RemoteXonsh",
        "xsess",
        "xwin",
        "xpane",
        "by-value closure",
        "# captured:",
        "Do not capture secrets",
        "TMUX",
        "XONTRIB_TMUX_RENAME_WINDOW",
        "Window names",
    ):
        assert needle in readme


def test_precommand_skips_comments_and_unload_disconnects(
    monkeypatch: Any,
) -> None:
    import xontrib_tmux  # type: ignore

    fake_window = MagicMock()
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: fake_window)

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
    fake_window.rename_window.assert_called_once_with("xonsh")
    assert len(mock_events.on_precommand) == 1
    assert len(mock_events.on_postcommand) == 1

    fake_window.rename_window.reset_mock()
    for handler in list(mock_events.on_precommand):
        handler(cmd="# comment only\n   \n")
    fake_window.rename_window.assert_not_called()

    for handler in list(mock_events.on_precommand):
        handler(cmd="# captured: x=42, issue='gh-17'\n\nvim file.txt")
    fake_window.rename_window.assert_called_once_with("vim")

    fake_window.rename_window.reset_mock()
    for handler in list(mock_events.on_postcommand):
        handler()
    fake_window.rename_window.assert_called_once_with("xonsh")

    xontrib_tmux._unload_xontrib_(mock_xsh)
    assert len(mock_events.on_precommand) == 0
    assert len(mock_events.on_postcommand) == 0


def _make_rename_harness(
    monkeypatch: Any,
    env: Any,
    *,
    shell: Any = "interactive",
    rc_files: Any = (),
) -> tuple[Any, Any, Any]:
    import xontrib_tmux  # type: ignore

    fake_window = MagicMock()
    monkeypatch.setattr(xontrib_tmux, "curtmuxw", lambda: fake_window)

    class FakeEvent(set):
        def __call__(self, fn: Any) -> Any:
            self.add(fn)
            return fn

        def fire(self, **kwargs: Any) -> None:
            for fn in list(self):
                fn(**kwargs)

    mock_events = SimpleNamespace(
        on_precommand=FakeEvent(),
        on_postcommand=FakeEvent(),
        on_post_rc=FakeEvent(),
    )
    monkeypatch.setattr(builtins, "events", mock_events, raising=False)

    mock_xsh = SimpleNamespace(
        ctx={},
        env=env,
        shell=shell,
        rc_files=rc_files,
        builtins=mock_events,
    )
    return xontrib_tmux, fake_window, (mock_xsh, mock_events)


def test_rename_window_env_registration_and_unset_default(
    monkeypatch: Any,
) -> None:
    from xonsh.environ import Env  # type: ignore

    env = Env()
    xt, fake_window, (xsh, events) = _make_rename_harness(monkeypatch, env)

    xt._load_xontrib_(xsh)
    try:
        detail = env.get_docs("XONTRIB_TMUX_RENAME_WINDOW")
        assert detail.doc
        assert env["XONTRIB_TMUX_RENAME_WINDOW"] is True

        fake_window.rename_window.assert_called_once_with("xonsh")
        fake_window.rename_window.reset_mock()

        events.on_precommand.fire(cmd="vim notes.txt")
        fake_window.rename_window.assert_called_once_with("vim")

        fake_window.rename_window.reset_mock()
        events.on_postcommand.fire()
        fake_window.rename_window.assert_called_once_with("xonsh")
    finally:
        xt._unload_xontrib_(xsh)


@pytest.mark.parametrize("initial", [False, "0", "false", "OFF", "no"])
def test_rename_window_false_before_load_skips_all_renames(
    monkeypatch: Any, initial: Any
) -> None:
    from xonsh.environ import Env  # type: ignore

    env = Env()
    env["XONTRIB_TMUX_RENAME_WINDOW"] = initial
    xt, fake_window, (xsh, events) = _make_rename_harness(monkeypatch, env)

    xt._load_xontrib_(xsh)
    try:
        fake_window.rename_window.assert_not_called()
        events.on_precommand.fire(cmd="vim notes.txt")
        events.on_postcommand.fire()
        fake_window.rename_window.assert_not_called()
    finally:
        xt._unload_xontrib_(xsh)


def test_rename_window_rc_post_load_assignment_respected(
    monkeypatch: Any,
) -> None:
    from xonsh.environ import Env  # type: ignore

    # Case 1: `xontrib load tmux` precedes `$XONTRIB_TMUX_RENAME_WINDOW = False`
    env = Env()
    xt, fake_window, (xsh, events) = _make_rename_harness(
        monkeypatch, env, shell=None, rc_files=None
    )
    xt._load_xontrib_(xsh)
    try:
        fake_window.rename_window.assert_not_called()
        env["XONTRIB_TMUX_RENAME_WINDOW"] = False
        events.on_post_rc.fire()
        fake_window.rename_window.assert_not_called()
    finally:
        xt._unload_xontrib_(xsh)

    # Case 2: `xontrib load tmux` in rc with var unset renames on post_rc
    env2 = Env()
    xt2, fake_window2, (xsh2, events2) = _make_rename_harness(
        monkeypatch, env2, shell=None, rc_files=None
    )
    xt2._load_xontrib_(xsh2)
    try:
        fake_window2.rename_window.assert_not_called()
        events2.on_post_rc.fire()
        fake_window2.rename_window.assert_called_once_with("xonsh")
    finally:
        xt2._unload_xontrib_(xsh2)


def test_rename_window_runtime_toggle_and_plain_dict_env(
    monkeypatch: Any,
) -> None:
    from xonsh.environ import Env  # type: ignore

    env = Env()
    xt, fake_window, (xsh, events) = _make_rename_harness(monkeypatch, env)
    xt._load_xontrib_(xsh)
    try:
        fake_window.rename_window.assert_called_once_with("xonsh")

        # Toggle off using string and bool forms: existing window name preserved
        for off_val in ("0", "false", False):
            env["XONTRIB_TMUX_RENAME_WINDOW"] = off_val
            assert env["XONTRIB_TMUX_RENAME_WINDOW"] is False
            fake_window.rename_window.reset_mock()
            events.on_precommand.fire(cmd="htop")
            events.on_postcommand.fire()
            fake_window.rename_window.assert_not_called()

        # Toggle back on: takes effect on the next command
        for on_val in ("1", "true", True):
            env["XONTRIB_TMUX_RENAME_WINDOW"] = on_val
            assert env["XONTRIB_TMUX_RENAME_WINDOW"] is True
            fake_window.rename_window.reset_mock()
            events.on_precommand.fire(cmd="htop")
            fake_window.rename_window.assert_called_once_with("htop")
            fake_window.rename_window.reset_mock()
            events.on_postcommand.fire()
            fake_window.rename_window.assert_called_once_with("xonsh")
    finally:
        xt._unload_xontrib_(xsh)

    # Defensive fallback when xsh.env is a plain dict without .register
    plain_env: dict[str, Any] = {"XONTRIB_TMUX_RENAME_WINDOW": "0"}
    xt2, fw2, (xsh2, ev2) = _make_rename_harness(monkeypatch, plain_env)
    xt2._load_xontrib_(xsh2)
    try:
        fw2.rename_window.assert_not_called()
        plain_env["XONTRIB_TMUX_RENAME_WINDOW"] = "false"
        ev2.on_precommand.fire(cmd="ls")
        fw2.rename_window.assert_not_called()

        plain_env["XONTRIB_TMUX_RENAME_WINDOW"] = "true"
        ev2.on_precommand.fire(cmd="ls")
        fw2.rename_window.assert_called_once_with("ls")
    finally:
        xt2._unload_xontrib_(xsh2)
