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
from typing import Any

from .block import TmuxMacro
from .commands import curtmuxp, curtmuxs, curtmuxw, tmuxp, tmuxs, tmuxw
from .monkeypatch import *


def _load_xontrib_(xsh: Any, **kwargs: Any) -> None:
    """Loads the xontrib extension into the active xonsh session."""
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
                # Grab first word of command string (e.g., 'vim' from 'vim file.txt')
                first_word = cmd.strip().split(maxsplit=1)[0]
                w.rename_window(first_word)

            @events.on_postcommand  # type: ignore
            def rename_tmux_after(**kwargs: Any) -> None:
                """Runs after command finishes, resetting window name to 'xonsh'."""
                w.rename_window("xonsh")


def _unload_xontrib_(xsh: Any) -> None:
    """Unloads the xontrib extension from the active xonsh session."""
    xsh.ctx.pop('tmuxw', None)
    xsh.ctx.pop('tmuxs', None)
    xsh.ctx.pop('tmuxp', None)
