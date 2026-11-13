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

"""Pytest fixtures isolating tests from the user's default tmux server."""

from __future__ import annotations

from collections.abc import Callable
import pathlib
from typing import Any

import libtmux  # type: ignore
from libtmux.common import tmux_cmd  # type: ignore
import pytest


def _validate_private_server_args(
    socket_name: str | None,
    socket_path: str | pathlib.Path | None,
) -> None:
    """Fails the test unless an explicit non-default tmux socket is given."""
    if socket_path is not None:
        path_str = str(socket_path).strip()
        if not path_str or pathlib.Path(path_str).name == "default":
            pytest.fail(
                "Refusing libtmux.Server with default or empty "
                f"socket_path={socket_path!r} inside tests"
            )
        return
    if socket_name is not None:
        name_str = str(socket_name).strip()
        if not name_str or name_str == "default":
            pytest.fail(
                "Refusing libtmux.Server with default or empty "
                f"socket_name={socket_name!r} inside tests"
            )
        return
    pytest.fail(
        "Refusing libtmux.Server() without an explicit private socket_name "
        "or socket_path inside tests"
    )


def _validate_private_tmux_cmd_args(args: tuple[Any, ...]) -> None:
    """Fails the test if tmux_cmd targets the default server."""
    if args == ("-V",):
        return
    i = 0
    while i < len(args):
        arg = str(args[i])
        if arg == "-L":
            if i + 1 >= len(args):
                break
            name = str(args[i + 1]).strip()
            if name and name != "default":
                return
            pytest.fail(
                f"Refusing tmux_cmd with default/empty -L {args[i + 1]!r}"
            )
        if arg.startswith("-L") and len(arg) > 2:
            name = arg[2:].strip()
            if name and name != "default":
                return
            pytest.fail(f"Refusing tmux_cmd with default/empty {arg!r}")
        if arg == "-S":
            if i + 1 >= len(args):
                break
            sock = str(args[i + 1]).strip()
            if sock and pathlib.Path(sock).name != "default":
                return
            pytest.fail(
                f"Refusing tmux_cmd with default/empty -S {args[i + 1]!r}"
            )
        if arg.startswith("-S") and len(arg) > 2:
            sock = arg[2:].strip()
            if sock and pathlib.Path(sock).name != "default":
                return
            pytest.fail(f"Refusing tmux_cmd with default/empty {arg!r}")
        i += 1
    pytest.fail(
        "Refusing tmux_cmd without an explicit private -L or -S socket "
        f"inside tests: args={args!r}"
    )


@pytest.fixture(autouse=True)
def _isolate_tmux_and_guard_default_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Strips inherited TMUX vars and forbids reaching the default server."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)

    orig_server_init = libtmux.Server.__init__
    orig_tmux_cmd_init = tmux_cmd.__init__

    def guarded_server_init(
        self: libtmux.Server,
        socket_name: str | None = None,
        socket_path: str | pathlib.Path | None = None,
        config_file: str | None = None,
        colors: int | None = None,
        on_init: Callable[[libtmux.Server], None] | None = None,
        socket_name_factory: Callable[[], str] | None = None,
        tmux_bin: str | pathlib.Path | None = None,
        **kwargs: Any,
    ) -> None:
        if (
            socket_path is None
            and socket_name is None
            and socket_name_factory is not None
        ):
            socket_name = socket_name_factory()
            socket_name_factory = None
        _validate_private_server_args(socket_name, socket_path)
        orig_server_init(
            self,
            socket_name=socket_name,
            socket_path=socket_path,
            config_file=config_file,
            colors=colors,
            on_init=on_init,
            socket_name_factory=socket_name_factory,
            tmux_bin=tmux_bin,
            **kwargs,
        )

    def guarded_tmux_cmd_init(
        self: tmux_cmd,
        *args: Any,
        tmux_bin: str | None = None,
    ) -> None:
        _validate_private_tmux_cmd_args(args)
        orig_tmux_cmd_init(self, *args, tmux_bin=tmux_bin)

    monkeypatch.setattr(libtmux.Server, "__init__", guarded_server_init)
    monkeypatch.setattr(tmux_cmd, "__init__", guarded_tmux_cmd_init)
