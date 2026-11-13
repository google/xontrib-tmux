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

"""Hermetic unit tests for :mod:`xontrib_tmux.protocol`."""

import ast
import builtins
from concurrent.futures import ThreadPoolExecutor
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest

from xontrib_tmux import protocol


def test_constants_and_no_tmux_or_xonsh_top_level_imports() -> None:
    assert protocol.PROTOCOL_VERSION == 1
    assert protocol.PEER_SOCKET_ENV == "_XONTRIB_TMUX_PEER_SOCKET"
    assert protocol.HIDDEN_PEER_ENV == "_XONTRIB_TMUX_HIDDEN"
    assert protocol.TIMEOUT_ENV_VAR == "XONTRIB_TMUX_READY_TIMEOUT"
    assert protocol.DEFAULT_TIMEOUT == 120.0

    src = pathlib.Path(protocol.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    top_imported: set[str] = set()
    all_imported: set[str] = set()
    str_literals: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_imported.add(node.module.split(".")[0])
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            all_imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            all_imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            str_literals.add(node.value)
    assert "libtmux" not in all_imported
    assert "xonsh" not in top_imported
    assert "TMUX" not in str_literals
    assert "TMUX_PANE" not in str_literals

    # Also verify importing protocol.py directly in a clean interpreter does
    # not load xonsh or libtmux.
    code = (
        "import importlib.util, sys; "
        "spec = importlib.util.spec_from_file_location("
        f"'p', {protocol.__file__!r}); "
        "mod = importlib.util.module_from_spec(spec); "
        "spec.loader.exec_module(mod); "
        "assert 'xonsh' not in sys.modules; "
        "assert 'libtmux' not in sys.modules"
    )
    proc = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_default_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(protocol.TIMEOUT_ENV_VAR, raising=False)
    monkeypatch.delattr(builtins, "__xonsh__", raising=False)
    assert protocol.default_timeout() == 120.0

    monkeypatch.setenv(protocol.TIMEOUT_ENV_VAR, "3.5")
    assert protocol.default_timeout() == 3.5

    monkeypatch.setenv(protocol.TIMEOUT_ENV_VAR, "not-a-number")
    assert protocol.default_timeout() == 120.0

    # builtins.__xonsh__.env takes precedence over os.environ.
    monkeypatch.setenv(protocol.TIMEOUT_ENV_VAR, "9.0")
    monkeypatch.setattr(
        builtins,
        "__xonsh__",
        SimpleNamespace(env={protocol.TIMEOUT_ENV_VAR: 5}),
        raising=False,
    )
    assert protocol.default_timeout() == 5.0

    monkeypatch.setattr(
        builtins,
        "__xonsh__",
        SimpleNamespace(env={protocol.TIMEOUT_ENV_VAR: object()}),
        raising=False,
    )
    assert protocol.default_timeout() == 120.0


def test_socket_dir_xdg_and_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_rt_") as td:
        rt = pathlib.Path(td)
        os.chmod(rt, 0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))

        sdir = protocol.socket_dir()
        assert sdir == rt / "xontrib-tmux"
        assert sdir.is_dir()
        assert stat.S_IMODE(os.stat(sdir).st_mode) == 0o700

        # Reject unsafe permissions on existing directory.
        os.chmod(sdir, 0o755)
        try:
            with pytest.raises(PermissionError):
                protocol.socket_dir()
        finally:
            os.chmod(sdir, 0o700)

        # Reject symlinks.
        link_parent = rt / "lp"
        link_parent.mkdir(mode=0o700)
        target = rt / "real"
        target.mkdir(mode=0o700)
        (link_parent / "xontrib-tmux").symlink_to(target)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(link_parent))
        with pytest.raises(PermissionError):
            protocol.socket_dir()

        # Reject regular file or dangling symlink (FileExistsError).
        file_parent = rt / "fp"
        file_parent.mkdir(mode=0o700)
        (file_parent / "xontrib-tmux").write_text(
            "not a dir", encoding="utf-8"
        )
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(file_parent))
        with pytest.raises(PermissionError):
            protocol.socket_dir()

        dangling_parent = rt / "dp"
        dangling_parent.mkdir(mode=0o700)
        (dangling_parent / "xontrib-tmux").symlink_to(rt / "missing_target")
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(dangling_parent))
        with pytest.raises(PermissionError):
            protocol.socket_dir()


def test_socket_dir_rejects_wrong_uid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="xt_uid_") as td:
        rt = pathlib.Path(td)
        os.chmod(rt, 0o700)
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(rt))
        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)

        with pytest.raises(PermissionError):
            protocol.socket_dir()


def test_socket_dir_long_xdg_falls_back_to_tmp(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    long_xdg = tmp_path / ("x" * 90)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long_xdg))

    sdir = protocol.socket_dir()
    assert sdir == pathlib.Path(f"/tmp/xontrib-tmux-{os.getuid()}")
    assert sdir.is_dir()
    assert stat.S_IMODE(os.stat(sdir).st_mode) == 0o700


def test_new_socket_path_uniqueness_and_length(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = str(tmp_path)
    pad = max(1, 61 - len(base))
    near_limit = tmp_path / ("d" * pad)
    near_limit.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(near_limit))

    p1 = protocol.new_socket_path("hidden")
    p2 = protocol.new_socket_path("hidden")
    assert p1 != p2
    assert len(str(p1).encode("utf-8")) < 100
    assert len(str(p2).encode("utf-8")) < 100

    with pytest.raises(ValueError):
        protocol.new_socket_path("x" * 100)


def test_verify_peer_uid(monkeypatch: pytest.MonkeyPatch) -> None:
    s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = protocol.verify_peer_uid(s1)
        assert pid == os.getpid()

        real_uid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
        with pytest.raises(PermissionError):
            protocol.verify_peer_uid(s1)
    finally:
        s1.close()
        s2.close()


def test_strip_macro_comments() -> None:
    raw = "echo hi #0: comment\n#1: pure comment\nx = 1 #12: end\n"
    assert protocol.strip_macro_comments(raw) == "echo hi\n\nx = 1\n"


def test_check_block_syntax_valid_and_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XONTRIBS_AUTOLOAD_DISABLED", "1")
    assert protocol.check_block_syntax("echo hi") == "echo hi\n"
    assert (
        protocol.check_block_syntax("echo hi #0: inline\n") == "echo hi\n"
    )
    multiline = "if True:\n    echo @(f'val={1 + 2}')\n"
    assert protocol.check_block_syntax(multiline) == multiline
    with_fstr = "with! p.send():\n    x = f'ok {1}'\n"
    assert protocol.check_block_syntax(with_fstr) == with_fstr

    for bad in (
        'x = "unclosed',
        "if True:\n",
        ";\n",
        "  echo bad_indent\n",
        "with! p.send():\n    x = f'unterminated\n",
    ):
        with pytest.raises(SyntaxError):
            protocol.check_block_syntax(bad)

    for empty in ("", "   \n\t\n", "# just a comment\n", "#0: macro\n"):
        with pytest.raises(ValueError, match="no statements"):
            protocol.check_block_syntax(empty)

    with pytest.raises(TypeError):
        protocol.check_block_syntax(123)  # type: ignore[arg-type]


def test_check_block_syntax_thread_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import xonsh.built_ins
    import xonsh.execer

    main_execer = xonsh.execer.Execer(filename="<main>")
    monkeypatch.setattr(xonsh.built_ins.XSH, "execer", main_execer)

    blocks = [
        "echo alpha\necho beta\n",
        "echo hello",
        "if True:\n    x = 1",
        "x = f'item {2 + 3}'",
        "ls -la",
    ]
    main_blocks = [
        "echo one\necho (two)\nif True:\n    echo three\n",
        "echo alpha\necho beta\n",
    ]

    def main_loop() -> None:
        for i in range(200):
            tree = main_execer.parse(main_blocks[i % len(main_blocks)], ctx={})
            assert tree is not None

    def worker(idx: int) -> None:
        for i in range(50):
            b = blocks[(idx + i) % len(blocks)]
            res = protocol.check_block_syntax(b)
            assert res.endswith("\n")
            with pytest.raises(SyntaxError):
                protocol.check_block_syntax("if True:\n")

    with ThreadPoolExecutor(max_workers=5) as pool:
        main_fut = pool.submit(main_loop)
        worker_futs = [pool.submit(worker, i) for i in range(4)]
        main_fut.result()
        for wf in worker_futs:
            wf.result()
    assert xonsh.built_ins.XSH.execer is main_execer


def test_send_and_recv_json() -> None:
    s1, s2 = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        orig: dict[str, Any] = {"op": "run", "id": "abc"}
        protocol.send_json(s1, orig)
        assert "version" not in orig

        with s2.makefile("r", encoding="utf-8") as f2:
            msg = protocol.recv_json(f2)
            assert msg == {"version": 1, "op": "run", "id": "abc"}

            # Version mismatch.
            protocol.send_json(s1, {"version": 99, "op": "run"})
            with pytest.raises(RuntimeError, match="Protocol version mismatch"):
                protocol.recv_json(f2)

            # Missing version.
            s1.sendall(b'{"op": "run"}\n')
            with pytest.raises(RuntimeError, match="Protocol version mismatch"):
                protocol.recv_json(f2)

            # Truncated / malformed JSON.
            s1.sendall(b'{"op": "run"\n')
            with pytest.raises(RuntimeError, match="Malformed JSON"):
                protocol.recv_json(f2)

            # Non-dict JSON.
            s1.sendall(b"[1, 2]\n")
            with pytest.raises(RuntimeError, match="JSON object"):
                protocol.recv_json(f2)

            # EOF returns None.
            s1.close()
            assert protocol.recv_json(f2) is None
    finally:
        s1.close()
        s2.close()
