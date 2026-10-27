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

"""Provides macro block execution and variable capturing for tmux."""

import inspect
import re
import tempfile
import types
from concurrent.futures import Future
from typing import Any, Dict, List, Optional

import dill
import libtmux  # type: ignore

from .sender import deliver


PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"


class TmuxMacro:
    """Context manager macro for sending code blocks to tmux panes."""

    # make sure the macro_block comes back as a string
    __xonsh_block__ = str
    macro_block: str
    macro_globals: dict[Any, Any]
    macro_locals: dict[Any, Any]

    def __init__(
        self,
        pane: libtmux.Pane,
        captured: Optional[Dict[str, bytes]] = None,
    ):
        self.pane = pane
        self.captured = captured
        # Resolves once the block has been typed into the pane; sending is
        # done in the background so the calling shell never blocks.
        self.future: Optional["Future[Any]"] = None

    def rc_src(self) -> Optional[bytes]:
        """Generates script bytes to unpickle captured variables in tmux."""
        if self.captured is None:
            return None

        src = "import dill\n"
        for var, pickled_val in self.captured.items():
            src += f"{var} = dill.loads({pickled_val})\n"
        src += "del dill\n"
        return src.encode('utf-8')

    def lines(self) -> List[str]:
        """Builds the lines to type into the pane, in order."""
        lines = []
        # Pickle the captured dict and unpickle it on the other side
        rc_src = self.rc_src()
        if rc_src is not None:
            with tempfile.NamedTemporaryFile(
                delete=False, prefix='captured_', suffix='.xsh'
            ) as rc_file:
                rc_file.write(rc_src)
            lines.append(f"source {rc_file.name}")

        # strip comments off the macro block
        stripped = re.sub(
            r'\s*#\d+:.*$', '', self.macro_block, flags=re.MULTILINE
        )
        # Wrap text in paste codes to disable auto-indentation
        lines.extend(
            f"{PASTE_START}{line}{PASTE_END}"
            for line in stripped.splitlines()
        )
        return lines

    def __enter__(self):
        # Detect common misuse
        if not hasattr(self, 'macro_block'):
            raise ValueError('TmuxMacro is missing attribute "macro_block". '
                             'If using it as a context manager, ensure you '
                             'are using "with!" rather than "with".')
        # Snapshot the payload now -- __exit__ drops macro_block, and the
        # delivery thread may not have run yet.
        self.future = deliver(self.pane, self.lines())
        return self

    def __exit__(self, exc_type, exc, tb):
        if hasattr(self, 'macro_block'):
            del self.macro_block
        if hasattr(self, 'macro_globals'):
            del self.macro_globals
        if hasattr(self, 'macro_locals'):
            del self.macro_locals

def capture_vars(frame: types.FrameType, names: List[str]) -> Dict[str, bytes]:
    """Captures the values of variables (by name) from the caller's scope.

    Args:
        frame: the caller's frame.
        names: list of variable name strings to look up.

    Returns:
        dict mapping each name to its value in the caller's frame.

    Raises:
        NameError: if a name isn't found in the caller's locals or globals.
    """
    try:
        caller_locals = frame.f_locals
        caller_globals = frame.f_globals
        result = {}
        for name in names:
            if name in caller_locals:
                result[name] = dill.dumps(caller_locals[name])
            elif name in caller_globals:
                result[name] = dill.dumps(caller_globals[name])
            else:
                raise NameError(
                    f"name '{name}' is not defined in caller's scope"
                )
        return result
    finally:
        # avoid reference cycles from holding onto the frame
        del frame

def send(
    pane: libtmux.Pane,
    inline_cmds: Optional[str] = None,
    /,
    capture: Optional[List[str]] = None,
) -> Any:
    """Sends commands to a tmux pane or returns a TmuxMacro context manager.

    Never blocks: keys are typed by a background worker once the pane's shell
    has taken over its tty.

    Returns:
        A :class:`TmuxMacro` when used as a context manager, otherwise a
        future that resolves when ``inline_cmds`` has been typed.
    """
    curr = inspect.currentframe()
    caller_frame = curr.f_back.f_back if curr and curr.f_back else None
    captured = (
        capture_vars(caller_frame, capture)
        if (capture is not None and caller_frame is not None)
        else None
    )
    macro = TmuxMacro(pane, captured)
    if inline_cmds is None:
        # to be used as context manager
        return macro
    else:
        # TODO: handle captured
        return deliver(pane, [inline_cmds])
