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
from typing import Any, Callable, Optional

from .block import TmuxMacro, is_pending_capture_cmd
from .commands import curtmuxp, curtmuxs, curtmuxw, tmuxp, tmuxs, tmuxw
from .monkeypatch import *
from .parser_patch import install_parser_patch_hook, patch_xonsh_parser


_precommand_handler: Optional[Callable[..., Any]] = None
_postcommand_handler: Optional[Callable[..., Any]] = None


def _load_xontrib_(xsh: Any, **kwargs: Any) -> None:
    """Loads the xontrib extension into the active xonsh session."""
    global _precommand_handler, _postcommand_handler
    install_parser_patch_hook()
    patch_xonsh_parser()

    # Make convenience functions available at the top level of the shell
    xsh.ctx['tmuxw'] = tmuxw
    xsh.ctx['tmuxs'] = tmuxs
    xsh.ctx['tmuxp'] = tmuxp

    if w := curtmuxw():
        w.rename_window("xonsh")

        events = getattr(
            builtins, "events", getattr(xsh, "builtins", None)
        )  # type: ignore
        if events is not None:
            @events.on_precommand  # type: ignore
            def rename_tmux_before(cmd: str, **kwargs: Any) -> None:
                """Runs before a command executes, naming window after command."""
                if is_pending_capture_cmd(cmd):
                    return
                parts = cmd.strip().split(maxsplit=1)
                if parts:
                    w.rename_window(parts[0])

            @events.on_postcommand  # type: ignore
            def rename_tmux_after(**kwargs: Any) -> None:
                """Runs after command finishes, resetting window name to 'xonsh'."""
                w.rename_window("xonsh")

            _precommand_handler = rename_tmux_before
            _postcommand_handler = rename_tmux_after


def _unload_xontrib_(xsh: Any) -> None:
    """Unloads the xontrib extension from the active xonsh session."""
    global _precommand_handler, _postcommand_handler
    xsh.ctx.pop('tmuxw', None)
    xsh.ctx.pop('tmuxs', None)
    xsh.ctx.pop('tmuxp', None)

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
    _precommand_handler = None
    _postcommand_handler = None
