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

"""xonsh extension for tmux integration and dynamic window renaming."""

import builtins
from collections.abc import Mapping
from typing import Any, Callable, Optional

from . import parser_patch
from .commands import (
    curtmuxp,
    curtmuxs,
    curtmuxw,
    xpane,
    xsess,
    xwin,
)
from .monkeypatch import *
from .parser_patch import install_parser_patch_hook, patch_xonsh_parser
from .peer import RemoteXonsh


_RENAME_ENV_VAR = "XONTRIB_TMUX_RENAME_WINDOW"
_RENAME_ENV_DOC = (
    "Whether xontrib-tmux renames the active tmux window to 'xonsh' "
    "and to running commands."
)
_FALSES = frozenset({"", "0", "n", "f", "no", "none", "false", "off"})
_UNSET = object()

_precommand_handler: Optional[Callable[..., Any]] = None
_postcommand_handler: Optional[Callable[..., Any]] = None
_post_rc_handler: Optional[Callable[..., Any]] = None


def _parse_bool(val: Any) -> bool:
    """Converts a value to bool using xonsh's boolean environment rules."""
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() not in _FALSES
    return bool(val)


def _get_env(xsh: Any) -> Any:
    """Returns xsh.env if available, else __xonsh__.env if present."""
    env = getattr(xsh, "env", None)
    if env is not None:
        return env
    bxsh = getattr(builtins, "__xonsh__", None)
    return getattr(bxsh, "env", None)


def _register_rename_env(xsh: Any) -> None:
    """Registers $XONTRIB_TMUX_RENAME_WINDOW with xsh.env if supported."""
    env = _get_env(xsh)
    register = getattr(env, "register", None)
    if callable(register):
        register(
            _RENAME_ENV_VAR,
            type="bool",
            default=True,
            doc=_RENAME_ENV_DOC,
        )


def _should_rename_window(xsh: Any) -> bool:
    """Returns True unless $XONTRIB_TMUX_RENAME_WINDOW is set to false."""
    env = _get_env(xsh)
    if env is None or not hasattr(env, "get"):
        return True
    if isinstance(env, Mapping):
        if _RENAME_ENV_VAR not in env:
            return True
        return _parse_bool(env[_RENAME_ENV_VAR])
    val = env.get(_RENAME_ENV_VAR, True)
    return _parse_bool(val)


def _load_xontrib_(xsh: Any, **kwargs: Any) -> None:
    """Loads the xontrib extension into the active xonsh session."""
    global _precommand_handler, _postcommand_handler, _post_rc_handler
    install_parser_patch_hook()
    patch_xonsh_parser()

    # Make peer and tmux helpers available at the top level of the shell.
    xsh.ctx["RemoteXonsh"] = RemoteXonsh
    xsh.ctx["xsess"] = xsess
    xsh.ctx["xwin"] = xwin
    xsh.ctx["xpane"] = xpane

    _register_rename_env(xsh)

    if not getattr(parser_patch, "_is_hidden_peer", False) and (
        w := curtmuxw()
    ):
        events = getattr(
            builtins, "events", getattr(xsh, "builtins", None)
        )  # type: ignore
        on_post_rc = getattr(events, "on_post_rc", None)
        if (
            getattr(xsh, "shell", _UNSET) is None
            and getattr(xsh, "rc_files", _UNSET) is None
            and callable(on_post_rc)
            and hasattr(on_post_rc, "discard")
        ):
            @on_post_rc  # type: ignore
            def rename_tmux_after_rc(**kwargs: Any) -> None:
                """Names tmux window 'xonsh' after rc files finish."""
                on_post_rc.discard(rename_tmux_after_rc)
                if _should_rename_window(xsh):
                    w.rename_window("xonsh")

            _post_rc_handler = rename_tmux_after_rc
        elif _should_rename_window(xsh):
            w.rename_window("xonsh")

        if events is not None:
            @events.on_precommand  # type: ignore
            def rename_tmux_before(cmd: str, **kwargs: Any) -> None:
                """Names tmux window after first command before execution."""
                if not _should_rename_window(xsh):
                    return
                for line in cmd.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    parts = stripped.split(maxsplit=1)
                    if parts:
                        w.rename_window(parts[0])
                    return

            @events.on_postcommand  # type: ignore
            def rename_tmux_after(**kwargs: Any) -> None:
                """Resets tmux window name to 'xonsh' after command."""
                if not _should_rename_window(xsh):
                    return
                w.rename_window("xonsh")

            _precommand_handler = rename_tmux_before
            _postcommand_handler = rename_tmux_after


def _unload_xontrib_(xsh: Any) -> None:
    """Unloads the xontrib extension from the active xonsh session."""
    global _precommand_handler, _postcommand_handler, _post_rc_handler
    xsh.ctx.pop("RemoteXonsh", None)
    xsh.ctx.pop("xsess", None)
    xsh.ctx.pop("xwin", None)
    xsh.ctx.pop("xpane", None)

    events = getattr(
        builtins, "events", getattr(xsh, "builtins", None)
    )  # type: ignore
    if events is not None:
        on_pre = getattr(events, "on_precommand", None)
        if on_pre is not None and _precommand_handler is not None:
            on_pre.discard(_precommand_handler)
        on_post = getattr(events, "on_postcommand", None)
        if on_post is not None and _postcommand_handler is not None:
            on_post.discard(_postcommand_handler)
        on_post_rc = getattr(events, "on_post_rc", None)
        if on_post_rc is not None and _post_rc_handler is not None:
            on_post_rc.discard(_post_rc_handler)
    _precommand_handler = None
    _postcommand_handler = None
    _post_rc_handler = None
